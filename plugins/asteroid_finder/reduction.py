"""Moving-object detection for short image sequences (astropy + photutils).

Pipeline
--------
1. Load each FITS frame, model and subtract the sky (``Background2D``).
2. Detect point sources with ``DAOStarFinder`` (hot pixels fail the sharpness cut).
3. Register frames to the first frame with a translation-only model estimated from the
   brightest stars (pair-difference voting; see ``tools.astrometry``).
4. Reject stationary sources: anything that re-appears at the same registered position in
   at least half the frames is a star/hot pixel, the remainder are *candidates*.
5. Link candidates across frames into straight-line, constant-velocity tracks and keep
   tracks with enough detections, small residuals and real net motion.
6. Aperture photometry at the fitted position in every frame, sky coordinates and rate
   from the (pointing-based) WCS.

Data-reduction rule
-------------------
Everything above is plain Python on pixel data.  ``analyze()`` returns *heavy* findings (per-frame
points, reference-star lists, the WCS) for the dashboard, cross-referencing and the mission log, plus
``findings["agent_summary"]``: the concise, rounded JSON produced by :func:`summarize`.  That
summary -- never the heavy findings, never an array -- is what may be shown to a language model
(see ``core/digest.py``, which enforces it).

Known limitations (reported, not hidden)
----------------------------------------
* Translation-only registration: ignores field rotation (fine for the short sequences of
  a stacked, tracking Seestar; not for hours-long alt-az sequences).
* Absolute astrometry is only as good as the header pointing until the cross-reference
  stage calibrates it against Gaia.
* Straight-line motion assumption; fast or curved movers (e.g. very close NEOs) will be
  missed or fragmented.
* Photometry is instrumental (uncalibrated) and ``snr_approx`` assumes gain ~1 e-/ADU.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from astropy.time import Time
from astropy.visualization import ZScaleInterval
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from photutils.aperture import ApertureStats, CircularAnnulus, CircularAperture
from photutils.background import Background2D, MedianBackground
from photutils.detection import DAOStarFinder
from scipy.spatial import cKDTree

from core.digest import round_floats, short
from tools.astrometry import (
    estimate_translation,
    sky_motion,
    wcs_from_header,
    wcs_to_dict,
)

log = logging.getLogger(__name__)


@dataclass
class ReductionParams:
    fwhm_px: float = 3.0
    detect_sigma: float = 5.0
    # A source is "stationary" if it recurs within this radius after registration.  Keep it
    # near the centroiding/registration precision: anything that moves less than this
    # between frames is indistinguishable from a star, so a loose value hides slow movers.
    stationary_tol_px: float = 1.2
    link_tol_px: float = 2.0
    min_detections: int = 3
    min_motion_px: float = 3.0  # net displacement over the whole sequence
    max_speed_px_s: float = 3.0
    registration_stars: int = 80
    max_registration_shift_px: float = 60.0
    # "stars": translation from matched bright stars (frames with only a pointing guess, e.g. the
    # Seestar).  "wcs": from each frame's own *solved* WCS -- for survey cutouts that are already
    # on a common sky grid and show too few stars (a 6' ZTF cutout at high latitude has ~10) to
    # match reliably.  Falls back to "stars" if any frame lacks a real celestial WCS.
    registration: str = "stars"
    max_reference_stars: int = 150
    aperture_radius_px: float = 4.5
    annulus_px: tuple[float, float] = (7.0, 11.0)
    cutout_half_size_px: int = 20
    max_figure_tracks: int = 3


@dataclass
class _Frame:
    path: str
    header: fits.Header
    data: np.ndarray  # background-subtracted, float32
    t_mid: datetime
    t_start: datetime
    exptime: float
    rms: float
    xy: np.ndarray  # (N, 2) detected centroids in this frame's pixels
    flux: np.ndarray
    shift: np.ndarray = field(default_factory=lambda: np.zeros(2))  # frame -> reference
    registered: bool = True


# --------------------------------------------------------------------------- #
# Loading & detection
# --------------------------------------------------------------------------- #
def _load_image(path: str) -> tuple[np.ndarray, fits.Header]:
    with fits.open(path) as hdul:
        for hdu in hdul:
            if hdu.data is not None and hdu.data.ndim >= 2:
                data = np.asarray(hdu.data, dtype=np.float32)
                header = hdu.header.copy()
                if not header.get("DATE-OBS") and hdul[0].header.get("DATE-OBS"):
                    header["DATE-OBS"] = hdul[0].header["DATE-OBS"]
                break
        else:
            raise ValueError(f"{path}: no image data found")
    if data.ndim == 3:  # e.g. RGB stack: collapse to luminance-like mean
        data = data.mean(axis=0)
    return data, header


def _frame_times(header: fits.Header, fallback_index: int) -> tuple[datetime, float]:
    exptime = float(header.get("EXPTIME", header.get("EXPOSURE", 10.0)))
    raw = header.get("DATE-OBS")
    if raw:
        start = Time(raw, scale="utc").to_datetime(timezone=timezone.utc)
    else:
        log.warning("Frame has no DATE-OBS; assuming %.0f s spacing", exptime)
        start = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=fallback_index * exptime)
    return start, exptime


def _subtract_background(data: np.ndarray) -> tuple[np.ndarray, float]:
    try:
        bkg = Background2D(
            data,
            box_size=(64, 64),
            filter_size=(3, 3),
            sigma_clip=SigmaClip(sigma=3.0, maxiters=5),
            bkg_estimator=MedianBackground(),
        )
        return (data - bkg.background).astype(np.float32), float(bkg.background_rms_median)
    except Exception:  # noqa: BLE001 - e.g. tiny test images
        log.warning("Background2D failed; falling back to global sigma-clipped median", exc_info=True)
        clipped = SigmaClip(sigma=3.0, maxiters=5)(data, masked=True)
        return (data - float(np.ma.median(clipped))).astype(np.float32), float(np.ma.std(clipped))


def _detect(data: np.ndarray, rms: float, params: ReductionParams) -> tuple[np.ndarray, np.ndarray]:
    finder = DAOStarFinder(threshold=params.detect_sigma * rms, fwhm=params.fwhm_px, exclude_border=True)
    table = finder(data)
    if table is None or len(table) == 0:
        return np.empty((0, 2)), np.empty(0)
    xy = np.column_stack([np.asarray(table["xcentroid"], float), np.asarray(table["ycentroid"], float)])
    return xy, np.asarray(table["flux"], float)


def load_frames(paths: list[str], params: ReductionParams) -> list[_Frame]:
    frames: list[_Frame] = []
    for i, path in enumerate(paths):
        data, header = _load_image(path)
        start, exptime = _frame_times(header, i)
        sub, rms = _subtract_background(data)
        xy, flux = _detect(sub, rms, params)
        log.info("Reduction: %s -> %d sources (bkg rms %.1f ADU)", Path(path).name, len(xy), rms)
        frames.append(
            _Frame(
                path=str(path),
                header=header,
                data=sub,
                t_start=start,
                t_mid=start + timedelta(seconds=exptime / 2.0),
                exptime=exptime,
                rms=rms,
                xy=xy,
                flux=flux,
            )
        )
    frames.sort(key=lambda f: f.t_start)
    return frames


# --------------------------------------------------------------------------- #
# Registration, candidate selection, linking
# --------------------------------------------------------------------------- #
def _brightest(frame: _Frame, n: int) -> np.ndarray:
    order = np.argsort(frame.flux)[::-1][:n]
    return frame.xy[order]


def _register_by_wcs(frames: list[_Frame]) -> list[dict] | None:
    """Shift of every frame into the first frame's grid from the frames' own WCS (``None``: not possible)."""
    from astropy.wcs import WCS

    wcss = []
    for fr in frames:
        if not str(fr.header.get("CTYPE1", "")).startswith("RA---") or fr.header.get("CRVAL1") is None:
            return None  # only a pointing-derived guess, not a solved WCS
        try:
            wcss.append(WCS(fr.header).celestial)
        except Exception:  # noqa: BLE001
            return None
    ref = wcss[0]
    report = []
    for k, (fr, w) in enumerate(zip(frames, wcss)):
        ny, nx = fr.data.shape
        cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0
        ra, dec = w.all_pix2world([cx], [cy], 0)
        rx, ry = ref.all_world2pix(ra, dec, 0)
        fr.shift = np.array([float(rx[0]) - cx, float(ry[0]) - cy]) if k else np.zeros(2)
        fr.registered = True
        report.append({"frame": k, "dx": float(fr.shift[0]), "dy": float(fr.shift[1]), "n_matches": int(len(fr.xy)), "ok": True, "method": "wcs"})
    return report


def register_frames(frames: list[_Frame], params: ReductionParams) -> list[dict]:
    """Estimate each frame's shift into the first frame's pixel grid."""
    if params.registration == "wcs":
        report = _register_by_wcs(frames)
        if report is not None:
            return report
        log.warning("Reduction: registration='wcs' requested but a frame has no solved WCS; matching stars instead")
    ref = frames[0]
    ref_stars = _brightest(ref, params.registration_stars)
    report = [{"frame": 0, "dx": 0.0, "dy": 0.0, "n_matches": int(len(ref_stars)), "ok": True}]
    for k, fr in enumerate(frames[1:], start=1):
        est = estimate_translation(
            _brightest(fr, params.registration_stars),
            ref_stars,
            max_shift=params.max_registration_shift_px,
            tol=2.0,
            min_matches=8,
        )
        if est is None:
            fr.registered = False
            log.warning("Reduction: registration failed for %s; frame excluded", Path(fr.path).name)
            report.append({"frame": k, "dx": None, "dy": None, "n_matches": 0, "ok": False})
        else:
            fr.shift = np.array([est["dx"], est["dy"]])
            report.append({"frame": k, "dx": est["dx"], "dy": est["dy"], "n_matches": est["n_matches"], "ok": True})
    return report


def find_candidates(frames: list[_Frame], params: ReductionParams) -> list[dict]:
    """Sources (in reference-frame pixels) that are *not* stationary across frames."""
    usable = [(i, f) for i, f in enumerate(frames) if f.registered]
    aligned = {i: f.xy + f.shift for i, f in usable}
    trees = {i: cKDTree(a) for i, a in aligned.items() if len(a)}
    need = max(2, math.ceil(0.5 * len(usable)))
    cands: list[dict] = []
    for i, f in usable:
        for j, pos in enumerate(aligned[i]):
            seen = sum(
                1
                for k, tree in trees.items()
                if tree.query(pos, distance_upper_bound=params.stationary_tol_px)[0] != np.inf
            )
            if seen < need:
                cands.append(
                    {
                        "frame": i,
                        "x": float(pos[0]),
                        "y": float(pos[1]),
                        "t": f.t_mid.timestamp(),
                        "flux": float(f.flux[j]),
                    }
                )
    return cands


def _fit_track(pts: list[dict]) -> dict:
    t = np.array([p["t"] for p in pts])
    t0 = float(t.mean())
    A = np.column_stack([np.ones_like(t), t - t0])
    cx, *_ = np.linalg.lstsq(A, np.array([p["x"] for p in pts]), rcond=None)
    cy, *_ = np.linalg.lstsq(A, np.array([p["y"] for p in pts]), rcond=None)
    resid = np.hypot(A @ cx - [p["x"] for p in pts], A @ cy - [p["y"] for p in pts])
    rms = float(np.sqrt(np.mean(resid**2))) if len(pts) > 2 else 0.0
    return {"t0": t0, "x0": float(cx[0]), "vx": float(cx[1]), "y0": float(cy[0]), "vy": float(cy[1]), "rms": rms}


def link_tracks(cands: list[dict], frame_times: dict[int, float], params: ReductionParams) -> list[dict]:
    """Greedy linear-motion linking.  Returns fits with their member candidates."""
    if len(frame_times) < params.min_detections:
        return []
    by_frame: dict[int, list[dict]] = {}
    for c in cands:
        by_frame.setdefault(c["frame"], []).append(c)
    frames = sorted(frame_times)
    span = frame_times[frames[-1]] - frame_times[frames[0]]
    min_speed = params.min_motion_px / span if span > 0 else float("inf")
    trees = {f: cKDTree([[c["x"], c["y"]] for c in cs]) for f, cs in by_frame.items()}

    raw: list[list[dict]] = []
    for ai, fa in enumerate(frames):
        for fb in frames[ai + 1:]:
            if fa not in by_frame or fb not in by_frame:
                continue
            dt = frame_times[fb] - frame_times[fa]
            for ca in by_frame[fa]:
                for cb in by_frame[fb]:
                    vx, vy = (cb["x"] - ca["x"]) / dt, (cb["y"] - ca["y"]) / dt
                    speed = math.hypot(vx, vy)
                    if speed < min_speed or speed > params.max_speed_px_s:
                        continue
                    members = {fa: ca, fb: cb}
                    for fk in frames:
                        if fk in members or fk not in trees:
                            continue
                        dtk = frame_times[fk] - frame_times[fa]
                        pred = (ca["x"] + vx * dtk, ca["y"] + vy * dtk)
                        dist, idx = trees[fk].query(pred, distance_upper_bound=params.link_tol_px)
                        if np.isfinite(dist):
                            members[fk] = by_frame[fk][int(idx)]
                    if len(members) >= params.min_detections:
                        raw.append([members[f] for f in sorted(members)])

    fitted = []
    for pts in raw:
        fit = _fit_track(pts)
        motion = math.hypot(fit["vx"], fit["vy"]) * (pts[-1]["t"] - pts[0]["t"])
        if fit["rms"] <= params.link_tol_px and motion >= params.min_motion_px:
            fitted.append({"fit": fit, "members": pts, "motion_px": motion})
    fitted.sort(key=lambda tr: (-len(tr["members"]), tr["fit"]["rms"]))

    used: set[tuple[int, float, float]] = set()
    accepted = []
    for tr in fitted:
        keys = {(m["frame"], m["x"], m["y"]) for m in tr["members"]}
        if keys & used:
            continue
        used |= keys
        accepted.append(tr)
    return accepted


# --------------------------------------------------------------------------- #
# Photometry & products
# --------------------------------------------------------------------------- #
def _photometry(frame: _Frame, x: float, y: float, params: ReductionParams) -> dict[str, Any]:
    try:
        ap = CircularAperture((x, y), r=params.aperture_radius_px)
        an = CircularAnnulus((x, y), r_in=params.annulus_px[0], r_out=params.annulus_px[1])
        local_bkg = float(ApertureStats(frame.data, an, sigma_clip=SigmaClip(3.0)).median)
        total = float(ApertureStats(frame.data, ap).sum)
        flux = total - local_bkg * float(ap.area)
        noise = math.sqrt(max(flux, 0.0) + float(ap.area) * frame.rms**2)
        snr = flux / noise if noise > 0 else float("nan")
        mag = -2.5 * math.log10(flux / frame.exptime) if flux > 0 else None
        return {"flux": flux, "snr_approx": snr, "mag_inst": mag}
    except Exception:  # noqa: BLE001 - e.g. aperture entirely off-image
        return {"flux": float("nan"), "snr_approx": float("nan"), "mag_inst": None}


def _build_detection(idx: int, track: dict, frames: list[_Frame], wcs, params: ReductionParams) -> dict[str, Any]:
    fit, members = track["fit"], track["members"]
    hit_frames = {m["frame"] for m in members}
    points = []
    for fi, fr in enumerate(frames):
        if not fr.registered:
            continue
        t = fr.t_mid.timestamp()
        ax, ay = fit["x0"] + fit["vx"] * (t - fit["t0"]), fit["y0"] + fit["vy"] * (t - fit["t0"])
        fx, fy = ax - fr.shift[0], ay - fr.shift[1]  # back into this frame's own pixels
        phot = _photometry(fr, fx, fy, params)
        point = {
            "frame": fi,
            "file": Path(fr.path).name,
            "t_iso": fr.t_mid.isoformat(),
            "x": float(ax),
            "y": float(ay),
            "frame_x": float(fx),
            "frame_y": float(fy),
            "detected": fi in hit_frames,
            **phot,
        }
        if wcs is not None:
            ra, dec = wcs.all_pix2world([ax], [ay], 0)
            point["ra_deg"], point["dec_deg"] = float(ra[0]) % 360.0, float(dec[0])
        points.append(point)

    used_pts = [p for p in points]
    t_first, t_last = used_pts[0]["t_iso"], used_pts[-1]["t_iso"]
    dt_min = (datetime.fromisoformat(t_last) - datetime.fromisoformat(t_first)).total_seconds() / 60.0
    mid_t = fit["t0"]
    mid_x, mid_y = fit["x0"], fit["y0"]
    det: dict[str, Any] = {
        "id": f"T{idx + 1:02d}",
        "n_points": len(members),
        "n_frames": len(used_pts),
        "x_mid": mid_x,
        "y_mid": mid_y,
        "vx_px_s": fit["vx"],
        "vy_px_s": fit["vy"],
        "speed_px_s": math.hypot(fit["vx"], fit["vy"]),
        "motion_px": track["motion_px"],
        "rms_px": fit["rms"],
        "epoch_mid_iso": datetime.fromtimestamp(mid_t, tz=timezone.utc).isoformat(),
        "snr_median": float(np.nanmedian([p["snr_approx"] for p in used_pts])),
        "mag_inst": _median_or_none([p["mag_inst"] for p in used_pts]),
        "points": points,
        "wcs_calibrated": False,
        "quality": "good" if len(members) >= max(4, len(used_pts)) and fit["rms"] < 1.0 else "marginal",
    }
    if wcs is not None and all("ra_deg" in p for p in used_pts):
        ra, dec = wcs.all_pix2world([mid_x], [mid_y], 0)
        det["ra_deg"], det["dec_deg"] = float(ra[0]) % 360.0, float(dec[0])
        rate, pa = sky_motion(
            used_pts[0]["ra_deg"], used_pts[0]["dec_deg"], used_pts[-1]["ra_deg"], used_pts[-1]["dec_deg"], dt_min
        )
        det["rate_arcsec_per_min"], det["pa_deg"] = rate, pa
    return det


def _median_or_none(values: list) -> float | None:
    vals = [v for v in values if v is not None and np.isfinite(v)]
    return float(np.median(vals)) if vals else None


def _make_figure(frames: list[_Frame], detections: list[dict], out_path: Path, params: ReductionParams) -> str:
    ref = next(f for f in frames if f.registered)
    n_rows = 1 + min(len(detections), params.max_figure_tracks)
    n_cols = max(len([f for f in frames if f.registered]), 1)
    fig = Figure(figsize=(max(9.0, 2.0 * n_cols), 5.5 + 1.8 * (n_rows - 1)), dpi=110)
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(n_rows, n_cols, height_ratios=[4.0] + [1.0] * (n_rows - 1), hspace=0.35, wspace=0.05)

    lo, hi = ZScaleInterval().get_limits(ref.data)
    ax = fig.add_subplot(gs[0, :])
    ax.imshow(ref.data, origin="lower", cmap="gray", vmin=lo, vmax=hi * 1.5)
    obj = ref.header.get("OBJECT") or Path(ref.path).parent.name
    ax.set_title(f"{obj}: first frame, {len(detections)} moving candidate(s)", fontsize=9)
    for d in detections:
        xs, ys = [p["x"] for p in d["points"]], [p["y"] for p in d["points"]]
        ax.plot(xs, ys, "-", color="cyan", lw=0.8)
        ax.scatter(xs[:1], ys[:1], s=350, facecolors="none", edgecolors="cyan", linewidths=1.0)
        ax.annotate(d["id"], (xs[0], ys[0]), color="yellow", fontsize=8, xytext=(14, 14), textcoords="offset points")
    ax.set_xticks([])
    ax.set_yticks([])

    h = params.cutout_half_size_px
    for r, d in enumerate(detections[: params.max_figure_tracks], start=1):
        col = 0
        for p in d["points"]:
            fr = frames[p["frame"]]
            x, y = int(round(p["frame_x"])), int(round(p["frame_y"]))
            cut = np.zeros((2 * h + 1, 2 * h + 1), dtype=np.float32)
            y0, y1, x0, x1 = max(y - h, 0), min(y + h + 1, fr.data.shape[0]), max(x - h, 0), min(x + h + 1, fr.data.shape[1])
            if y1 > y0 and x1 > x0:
                cut[(y0 - (y - h)):(y0 - (y - h)) + (y1 - y0), (x0 - (x - h)):(x0 - (x - h)) + (x1 - x0)] = fr.data[y0:y1, x0:x1]
            cax = fig.add_subplot(gs[r, col])
            cax.imshow(cut, origin="lower", cmap="gray", vmin=lo, vmax=hi * 4)
            cax.set_xticks([])
            cax.set_yticks([])
            cax.set_title(f"{d['id']} f{p['frame'] + 1}" + ("" if p["detected"] else " (forced)"), fontsize=7)
            col += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    return str(out_path)


# --------------------------------------------------------------------------- #
# Concise, model-safe summary
# --------------------------------------------------------------------------- #
def summarize(findings: dict[str, Any], max_tracks: int = 5) -> dict[str, Any]:
    """Reduce full findings to a few dozen numbers.

    Pure function of ``findings``: no pixels, no per-frame tables, no paths, no WCS.  Tracks are
    ranked by SNR and capped at ``max_tracks``; floats are rounded to cut token count.
    """
    dets = sorted(findings.get("detections") or [], key=lambda d: -(d.get("snr_median") or 0.0))
    tracks = []
    for d in dets[:max_tracks]:
        track = {
            "id": d.get("id"),
            "ra_deg": _round_or_none(d.get("ra_deg"), 4),
            "dec_deg": _round_or_none(d.get("dec_deg"), 4),
            "rate_arcsec_per_min": d.get("rate_arcsec_per_min"),
            "pa_deg": d.get("pa_deg"),
            "snr": d.get("snr_median"),
            "mag_inst": d.get("mag_inst"),
            "n_detections": d.get("n_points"),
            "n_frames": d.get("n_frames"),
            "motion_px": d.get("motion_px"),
            "rms_px": d.get("rms_px"),
            "quality": d.get("quality"),
        }
        tracks.append(track)
    out = {
        "n_frames": findings.get("n_frames"),
        "n_frames_used": findings.get("n_frames_used"),
        "exposure_s": findings.get("exposure_s"),
        "epoch_mid_iso": findings.get("epoch_mid_iso"),
        "background_rms_adu": findings.get("background_rms_adu"),
        "n_candidate_sources": findings.get("n_candidate_sources"),
        "n_tracks": len(dets),
        "tracks": round_floats(tracks, 4),
        "warnings": [short(w, 160) for w in (findings.get("warnings") or [])[:3]],
    }
    if len(dets) > max_tracks:
        out["n_tracks_omitted"] = len(dets) - max_tracks
    out = round_floats(out, 4)  # also turns NaN/inf into None
    # round_floats works in significant figures; RA/Dec need fixed decimals, so restore them.
    for tr, d in zip(out["tracks"], dets):
        for key in ("ra_deg", "dec_deg"):
            tr[key] = _round_or_none(d.get(key), 4)
        for key in [k for k, v in tr.items() if v is None]:  # absent values cost tokens for nothing
            del tr[key]
    return {k: v for k, v in out.items() if v is not None}


def _round_or_none(value: Any, ndigits: int) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), ndigits)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def analyze(
    fits_paths: list[str],
    params: ReductionParams | None = None,
    figure_dir: str | Path | None = None,
) -> dict[str, Any]:
    params = params or ReductionParams()
    if not fits_paths:
        raise ValueError("analyze() needs at least one FITS path")

    frames = load_frames(list(fits_paths), params)
    reg_report = register_frames(frames, params) if len(frames) > 1 else []
    usable = [f for f in frames if f.registered]
    findings: dict[str, Any] = {
        "n_frames": len(frames),
        "n_frames_used": len(usable),
        "frame_paths": [f.path for f in frames],
        "exposure_s": frames[0].exptime,
        "epoch_first_iso": frames[0].t_start.isoformat(),
        "epoch_last_iso": frames[-1].t_start.isoformat(),
        "epoch_mid_iso": (frames[0].t_mid + (frames[-1].t_mid - frames[0].t_mid) / 2).isoformat(),
        "background_rms_adu": float(np.median([f.rms for f in frames])),
        "n_sources_per_frame": [int(len(f.xy)) for f in frames],
        "registration": reg_report,
        "params": {k: v for k, v in params.__dict__.items()},
        "warnings": [],
        "detections": [],
        "figures": [],
    }

    wcs = wcs_from_header(frames[0].header)
    if wcs is None:
        findings["warnings"].append("No pointing/WCS information in headers; sky coordinates unavailable.")
    else:
        ref_xy = _brightest(frames[0], params.max_reference_stars)
        findings["astrometry"] = {
            "wcs": wcs_to_dict(wcs),
            "reference_stars": ref_xy.tolist(),
            "frame_shape": list(frames[0].data.shape),
            "calibrated": False,
        }

    if len(usable) < params.min_detections:
        findings["warnings"].append(
            f"Only {len(usable)} usable frame(s); at least {params.min_detections} are needed to link tracks."
        )
        findings["summary"] = f"{len(usable)} usable frames; insufficient for moving-object linking."
        findings["agent_summary"] = summarize(findings)
        return findings

    cands = find_candidates(frames, params)
    times = {i: f.t_mid.timestamp() for i, f in enumerate(frames) if f.registered}
    tracks = link_tracks(cands, times, params)
    detections = [_build_detection(i, tr, frames, wcs, params) for i, tr in enumerate(tracks)]
    findings["n_candidate_sources"] = len(cands)
    findings["detections"] = detections

    if figure_dir is not None:
        try:
            fig_path = Path(figure_dir) / "reduction_overview.png"
            findings["figures"].append(_make_figure(frames, detections, fig_path, params))
        except Exception:  # noqa: BLE001 - a plotting bug must not discard the science
            log.exception("Reduction: figure generation failed")
            findings["warnings"].append("Figure generation failed; see logs.")

    if detections:
        best = max(detections, key=lambda d: d["snr_median"])
        findings["summary"] = (
            f"{len(detections)} moving-object candidate(s) in {len(usable)} frames "
            f"(best: {best['id']}, {best['n_points']}/{best['n_frames']} detections, "
            f"{best['motion_px']:.1f} px net motion, SNR~{best['snr_median']:.0f})."
        )
    else:
        findings["summary"] = f"No moving-object candidates in {len(usable)} frames ({len(cands)} unmatched sources rejected)."
    findings["agent_summary"] = summarize(findings)
    log.info("Reduction: %s", findings["summary"])
    return findings
