"""Zwicky Transient Facility archive adapter (NASA/IPAC IRSA) for the asteroid finder.

What ZTF really looks like (probed, not assumed)
------------------------------------------------
ZTF surveys the northern sky with a 47 deg^2 camera on the Palomar 48-inch.  The public archive
holds every exposure, but *one position* is observed irregularly: mostly single 30 s exposures a
few nights apart, with the occasional multi-hour burst of repeats (e.g. 10 exposures ~24 min
apart, or a 57-exposure deep-drilling night).  A mover can only be detected in a **visit** of at
least three exposures of the same field/CCD/quadrant/filter, so the adapter

1. asks IRSA's MOST ("Moving Object Search Tool") which ZTF exposures a *known* asteroid was
   predicted to fall in (MOST returns a text table; the anonymous service needs no key),
2. groups them into visits (same field/CCD/quadrant/filter, gaps under ``max_gap_h``),
3. turns every visit with enough exposures into one mission target whose ``not_before`` is the
   time of its first exposure -- so the harness's wait_for_visit node reproduces the survey's
   real, irregular cadence (days between visits, minutes between frames),
4. for the visit being observed, downloads a small science-image cutout of every exposure from
   IRSA's IBE service, centred on the asteroid's predicted mid-visit position, and writes plain
   FITS with ``DATE-OBS``/``EXPTIME`` and an *exact* TAN WCS refitted from ZTF's TPV solution.

Known objects are used only to know *where and when to look*: the detection pipeline never sees
the designation, and the cross-reference stage rediscovers it from positions and rates.  A frame
with a bad quality flag (``INFOBITS >= 33554432``, IPAC's recommended cut) is dropped.

Credit (required by the data policy) is returned in :attr:`ZTFArchive.credit` and printed in the
report.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS
from astropy.wcs.utils import fit_wcs_from_points

from core.archive import ArchiveError, FrameBatch, FrameCallback, http_get

log = logging.getLogger(__name__)

MOST_URL = "https://irsa.ipac.caltech.edu/cgi-bin/MOST/nph-most"
IBE_SCI_URL = "https://irsa.ipac.caltech.edu/ibe/data/ztf/products/sci/"
#: MPC observatory code of the Palomar 48-inch (for topocentric Horizons ephemerides).
OBSERVATORY_CODE = "I41"
CREDIT = (
    "Based on observations obtained with the Samuel Oschin 48-inch Telescope at Palomar Observatory as part of the "
    "Zwicky Transient Facility project (Bellm et al. 2019, PASP 131, 018002; Masci et al. 2019, PASP 131, 018003). "
    "Data retrieved from the NASA/IPAC Infrared Science Archive (IRSA), operated by the Jet Propulsion Laboratory, "
    "California Institute of Technology, under contract with NASA."
)
BAD_INFOBITS = 33554432  # IPAC: infobits < 2**25 is the recommended "good science image" cut
_OBJECT_NAME = re.compile(r"^[A-Za-z0-9 ()/.\-_]{1,40}$")
FILTER_NAMES = {"zg": "ZTF g", "zr": "ZTF r", "zi": "ZTF i"}


@dataclass
class Exposure:
    t_iso: str  # exposure start, UTC
    filefracday: str
    field: int
    ccd: int
    q: int
    fc: str
    ra: float  # predicted position of the queried object at this time
    dec: float
    vmag: float | None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _num(row: Any, key: str) -> float | None:
    try:
        v = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def parse_most_table(text: str) -> list[Exposure]:
    """IRSA MOST output (IPAC ASCII table after a ``\\key = value`` header) -> exposures, sorted by time."""
    from astropy.table import Table

    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("|")), None)
    if start is None:
        reason = next((ln for ln in lines if "error" in ln.lower() or "no " in ln.lower()), "no result table")
        raise ArchiveError(f"MOST returned no table ({reason.strip()[:160]})")
    table = Table.read("\n".join(lines[start:]), format="ascii.fixed_width", delimiter="|", header_start=0, data_start=2)
    need = {"ra_obj", "dec_obj", "filefracday", "field", "ccdid", "qid", "filtercode", "obsjd"}
    missing = need - set(table.colnames)
    if missing:
        raise ArchiveError(f"MOST table lacks columns {sorted(missing)}")
    out: list[Exposure] = []
    for row in table:
        if "imgtypecode" in table.colnames and str(row["imgtypecode"]).strip() not in ("o", ""):
            continue  # not an object (science) exposure
        try:
            t = Time(float(row["obsjd"]), format="jd", scale="utc").to_datetime(timezone=timezone.utc)
            out.append(
                Exposure(
                    t_iso=t.isoformat(timespec="milliseconds"),
                    filefracday=str(int(row["filefracday"])),
                    field=int(row["field"]), ccd=int(row["ccdid"]), q=int(row["qid"]),
                    fc=str(row["filtercode"]).strip(),
                    ra=float(row["ra_obj"]), dec=float(row["dec_obj"]), vmag=_num(row, "vmag"),
                )
            )
        except (TypeError, ValueError) as exc:
            log.warning("MOST: skipping unreadable row: %s", exc)
    out.sort(key=lambda e: e.t_iso)
    return out


def group_visits(exposures: list[Exposure], max_gap_h: float = 6.0) -> list[list[Exposure]]:
    """Chains of exposures of one field/CCD/quadrant/filter with gaps under ``max_gap_h``."""
    chains: dict[tuple, list[list[Exposure]]] = {}
    for e in exposures:  # already time-sorted
        key = (e.field, e.ccd, e.q, e.fc)
        runs = chains.setdefault(key, [])
        if runs and datetime.fromisoformat(e.t_iso) - datetime.fromisoformat(runs[-1][-1].t_iso) <= timedelta(hours=max_gap_h):
            runs[-1].append(e)
        else:
            runs.append([e])
    visits = [run for runs in chains.values() for run in runs]
    visits.sort(key=lambda run: run[0].t_iso)
    return visits


def ibe_path(e: Exposure) -> str:
    ffd = e.filefracday
    return f"{ffd[:4]}/{ffd[4:8]}/{ffd[8:]}/ztf_{ffd}_{e.field:06d}_{e.fc}_c{e.ccd:02d}_o_q{e.q}_sciimg.fits"


def fit_tan_wcs(header: fits.Header, shape: tuple[int, int]) -> tuple[WCS, float]:
    """A pure-TAN WCS fitted to ``header``'s (TPV) solution on a grid; returns it and the max residual (arcsec).

    ZTF's solution carries small PV distortion terms.  Downstream tools (and the dashboard's WCS
    round-trip) are TAN-only, so the cutout is given an equivalent TAN solution and the worst-case
    approximation error over the cutout is *measured* and reported, not assumed.
    """
    ny, nx = shape
    true = WCS(header)
    gx, gy = np.meshgrid(np.linspace(0, nx - 1, 9), np.linspace(0, ny - 1, 9))
    xy = (gx.ravel(), gy.ravel())
    ra, dec = true.all_pix2world(*xy, 0)
    from astropy.coordinates import SkyCoord

    world = SkyCoord(ra, dec, unit="deg")
    tan = fit_wcs_from_points(xy, world, proj_point="center", projection="TAN", sip_degree=None)
    ra2, dec2 = tan.all_pix2world(*xy, 0)
    resid = SkyCoord(ra2, dec2, unit="deg").separation(world).arcsec
    tan.pixel_shape = (nx, ny)
    return tan, float(np.max(resid))


class ZTFArchive:
    """:class:`core.archive.ArchiveSource` for ZTF science-image cutouts."""

    name = "ztf"
    credit = CREDIT

    def __init__(
        self,
        cache_dir: Path,
        *,
        most_url: str = MOST_URL,
        ibe_url: str = IBE_SCI_URL,
        cutout_arcmin: float = 6.0,
        max_frames: int = 12,
        min_frames: int = 3,
        max_gap_h: float = 6.0,
        http_get_fn=http_get,
    ):
        self.cache_dir = Path(cache_dir) / "ztf"
        self.most_url, self.ibe_url = most_url, ibe_url.rstrip("/") + "/"
        self.cutout_arcmin, self.max_frames, self.min_frames, self.max_gap_h = cutout_arcmin, max_frames, min_frames, max_gap_h
        self._get = http_get_fn

    # ------------------------------------------------------------------ #
    # Survey side: where and when did ZTF look?
    # ------------------------------------------------------------------ #
    def survey(self, obj: str, begin: str, end: str) -> dict[str, Any]:
        """Visits of ``obj`` between two ISO dates, plus the raw cadence statistics (cached on disk)."""
        if not _OBJECT_NAME.match(obj):
            raise ArchiveError(f"refusing to query MOST with an odd object name: {obj!r}")
        cache = self.cache_dir / f"most_{re.sub(r'[^A-Za-z0-9]+', '_', obj)}_{begin}_{end}.json"
        if cache.is_file():
            exposures = [Exposure(**d) for d in json.loads(cache.read_text())]
            log.info("MOST (cached): %d exposure(s) for %s", len(exposures), obj)
        else:
            params = {
                "catalog": "ztf", "input_type": "name_input", "obj_name": obj, "obj_type": "Asteroid",
                "obs_begin": begin, "obs_end": end, "output_mode": "Brief", "mcenter": "eph",
                "ephem_step": "1", "ephem_step_units": "days",
            }
            body = self._get(self.most_url, params=params, timeout=180.0)
            exposures = parse_most_table(body.decode("utf-8", "replace"))
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps([e.as_dict() for e in exposures]))
            log.info("MOST: %d ZTF exposure(s) predicted to contain %s between %s and %s", len(exposures), obj, begin, end)

        runs = group_visits(exposures, self.max_gap_h)
        usable = [r for r in runs if len(r) >= self.min_frames]
        return {
            "object": obj,
            "window": [begin, end],
            "n_exposures": len(exposures),
            "n_visits_total": len(runs),
            "n_single_or_pair": len(runs) - len(usable),
            "visits": usable,
        }

    def targets_for(self, obj: str, begin: str, end: str, priority0: float = 10.0) -> tuple[list[dict], dict[str, Any]]:
        """Mission targets (one per usable visit, time-ordered) and a cadence summary."""
        survey = self.survey(obj, begin, end)
        targets: list[dict] = []
        for i, run in enumerate(survey["visits"]):
            used = run[: self.max_frames]
            mid = used[len(used) // 2]
            first, last = used[0], used[-1]
            span_h = (datetime.fromisoformat(last.t_iso) - datetime.fromisoformat(first.t_iso)).total_seconds() / 3600.0
            night = first.t_iso[:10]
            vmags = [e.vmag for e in used if e.vmag is not None]
            targets.append(
                {
                    "name": f"({obj}) {night} F{first.field} c{first.ccd:02d}q{first.q} {first.fc[-1]}",
                    "designation": obj,
                    "ra_deg": mid.ra, "dec_deg": mid.dec,
                    "priority": priority0 - i * 1e-3,  # ties are broken by not_before anyway
                    "not_before": first.t_iso,
                    "v_mag": float(np.mean(vmags)) if vmags else None,
                    "visit": {
                        "n_available": len(run), "n_used": len(used), "span_h": round(span_h, 2),
                        "cutout_arcmin": self.cutout_arcmin, "exposures": [e.as_dict() for e in used],
                    },
                    "survey": {
                        "window": survey["window"], "n_exposures": survey["n_exposures"],
                        "n_visits_total": survey["n_visits_total"], "n_usable_visits": len(survey["visits"]),
                    },
                }
            )
        return targets, {k: v for k, v in survey.items() if k != "visits"}

    # ------------------------------------------------------------------ #
    # Frame side: cutouts for one visit
    # ------------------------------------------------------------------ #
    def acquire(self, target: dict, out_dir: Path, on_frame: FrameCallback | None = None) -> FrameBatch:
        visit = target.get("visit") or {}
        exposures = [Exposure(**d) for d in visit.get("exposures", [])]
        if not exposures:
            raise ArchiveError(f"target {target.get('name')} carries no ZTF exposures")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        size = float(visit.get("cutout_arcmin", self.cutout_arcmin))
        ra0, dec0 = float(target["ra_deg"]), float(target["dec_deg"])

        paths: list[str] = []
        skipped: list[dict[str, str]] = []
        urls: list[str] = []
        wcs_err: list[float] = []
        total = len(exposures)
        for i, e in enumerate(exposures):
            t_obs = datetime.fromisoformat(e.t_iso)
            if on_frame:
                on_frame(i, total, t_obs)
            try:
                raw = self._cutout(e, ra0, dec0, size)
                path, info = self._write_frame(raw, e, target, out_dir, len(paths))
            except ArchiveError as exc:
                log.warning("ZTF frame %s skipped: %s", e.filefracday, exc)
                skipped.append({"frame": e.filefracday, "reason": str(exc)[:200]})
                continue
            paths.append(path)
            wcs_err.append(info["wcs_fit_err_arcsec"])
            urls.append(self.ibe_url + ibe_path(e))
        if on_frame:
            on_frame(total, total, None)
        if len(paths) < self.min_frames:
            raise ArchiveError(
                f"only {len(paths)} usable ZTF cutout(s) of {total} for {target.get('name')} (need {self.min_frames}); "
                f"skipped: {skipped[:3]}"
            )
        first = exposures[0]
        plan = {
            "gain": 0, "exposure_s": 30.0, "filter": FILTER_NAMES.get(first.fc, first.fc),
            "count": len(paths), "instrument": "ZTF (Palomar 48-inch)", "pixel_scale_arcsec": 1.01,
        }
        meta = {
            "source": self.name, "credit": self.credit, "observatory_code": OBSERVATORY_CODE,
            "field": first.field, "ccd": first.ccd, "quadrant": first.q, "filter": first.fc,
            "n_requested": total, "n_used": len(paths), "skipped": skipped, "urls": urls[:3] + (["..."] if len(urls) > 3 else []),
            "cutout_arcmin": size, "tan_fit_max_err_arcsec": round(max(wcs_err), 4) if wcs_err else None,
            "first_obs": exposures[0].t_iso, "last_obs": exposures[-1].t_iso,
            "cadence_s": _median_gap_s(exposures),
        }
        return FrameBatch(paths, plan, meta)

    def _cutout(self, e: Exposure, ra: float, dec: float, size_arcmin: float) -> bytes:
        cache = self.cache_dir / "cutouts" / f"{e.filefracday}_{e.field}_{e.ccd}_{e.q}_{e.fc}_{ra:.4f}_{dec:+.4f}_{size_arcmin:g}.fits"
        if cache.is_file() and cache.stat().st_size > 2880:
            return cache.read_bytes()
        params = {"center": f"{ra:.6f},{dec:.6f}", "size": f"{size_arcmin:g}arcmin", "gzip": "false"}
        data = self._get(self.ibe_url + ibe_path(e), params=params, timeout=120.0)
        if not data or not data.startswith(b"SIMPLE"):
            raise ArchiveError(f"IBE returned no FITS cutout for {e.filefracday} ({(data or b'')[:80]!r})")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        return data

    def _write_frame(self, raw: bytes, e: Exposure, target: dict, out_dir: Path, index: int) -> tuple[str, dict]:
        import io

        with fits.open(io.BytesIO(raw)) as hdul:
            data = np.asarray(hdul[0].data, dtype=np.float32)
            src = hdul[0].header.copy()
        if data.ndim != 2 or min(data.shape) < 32:
            raise ArchiveError(f"cutout is too small ({data.shape})")
        if int(src.get("INFOBITS", 0) or 0) >= BAD_INFOBITS:
            raise ArchiveError(f"flagged bad by the ZTF pipeline (INFOBITS={src.get('INFOBITS')})")
        obsjd = src.get("OBSJD")
        t_start = (
            Time(float(obsjd), format="jd", scale="utc").to_datetime(timezone=timezone.utc)
            if obsjd else datetime.fromisoformat(e.t_iso)
        )
        try:
            tan, err = fit_tan_wcs(src, data.shape)
        except Exception as exc:  # noqa: BLE001 - astropy raises many types for odd WCS
            raise ArchiveError(f"unusable WCS: {exc}") from exc

        hdr = fits.Header()
        hdr["DATE-OBS"] = (t_start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], "exposure start, UTC (ZTF OBSJD)")
        hdr["EXPTIME"] = (float(src.get("EXPTIME", 30.0)), "exposure time, s")
        hdr["OBJECT"] = str(target.get("name", ""))[:68]
        hdr["TELESCOP"], hdr["INSTRUME"], hdr["ORIGIN"] = "Palomar 48-inch (Oschin)", "ZTF", "IRSA/ZTF science image cutout"
        hdr["FILTER"] = FILTER_NAMES.get(e.fc, e.fc)
        for k in ("FIELDID", "CCDID", "QID", "MAGZP", "SEEING", "GAIN", "SATURATE", "AIRMASS", "INFOBITS", "OBSJD"):
            if k in src:
                hdr[k] = src[k]
        hdr["PIXSCALE"] = float(src.get("PIXSCALE", 1.012))
        hdr["ZTFFRAME"] = e.filefracday
        hdr["TANFITER"] = (round(err, 4), "max |TAN fit - ZTF TPV| over the cutout, arcsec")
        hdr.update(tan.to_header(relax=True))
        # to_header() copies DATE-OBS/MJD-OBS from the source WCS if present; ours must win.
        hdr["DATE-OBS"] = t_start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        hdr.remove("MJD-OBS", ignore_missing=True)
        path = out_dir / f"ztf_{index + 1:03d}_{e.filefracday}.fits"
        fits.PrimaryHDU(data, hdr).writeto(path, overwrite=True)
        return str(path), {"wcs_fit_err_arcsec": err}


def _median_gap_s(exposures: list[Exposure]) -> float | None:
    if len(exposures) < 2:
        return None
    ts = [datetime.fromisoformat(e.t_iso).timestamp() for e in exposures]
    return float(np.median(np.diff(ts)))
