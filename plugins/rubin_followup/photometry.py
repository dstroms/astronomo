"""Stack a short sequence of one field and measure a faint, *known-position* source in it.

Pipeline
--------
``analyze`` (the plugin's reduction step; it sees only FITS paths, so it is target-agnostic):

1. Load frames in time order; subtract the sky (``Background2D``), repair isolated hot pixels, detect stars.
2. Register every frame to the first with a translation estimated from the brightest stars.  Frames
   whose star fluxes fall below ``min_transparency`` of the reference (cloud) are dropped.
3. Mean-stack the frames with *integer-pixel* shifts.  No interpolation means no correlated noise, so
   aperture errors stay honest; the cost is a sub-pixel smear that a 5-px aperture does not notice.
4. Detect sources in the stack, run aperture photometry on them, and write ``stack.fits``.

``calibrate`` + ``measure_target`` (called by the plugin's cross-reference step, which knows the
target and has the catalogues):

5. Fit a translation of the Gaia DR3 stars onto the detected stars (pointing error), then a photometric
   zero point from the matched, unsaturated stars.
6. **Forced aperture photometry at the alert position** on the stack: flux, error, SNR, and a 5-sigma
   limiting magnitude.  The error floor is the *empirical* scatter of the same aperture placed on
   empty sky near the target, so a source on a bright galaxy is not given the error of a dark field.

Data-reduction rule
-------------------
Everything here is plain Python on pixel data.  ``analyze`` returns heavy findings (the source list, the
WCS) for cross-referencing and the dashboard, plus ``findings["agent_summary"]``: the rounded, concise
JSON from :func:`summarize`.  Only that summary may be shown to a language model.

Known limitations (reported, not hidden)
----------------------------------------
* Translation-only registration (no field rotation), as in the other plugins.  Keep sequences short.
* The camera is broadband (the IR-cut filter passes roughly 400-700 nm) and the zero point comes from
  Gaia G without a colour term, so magnitudes carry a colour-dependent systematic of a few tenths of
  a magnitude, larger for very red or very blue sources.
* ~1 e-/ADU is assumed for the Poisson term of the error.
* A mean stack is not outlier-clipped: a satellite trail or cosmic ray survives, diluted by 1/N.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from astropy.time import Time
from photutils.aperture import ApertureStats, CircularAnnulus, CircularAperture
from scipy.ndimage import median_filter, uniform_filter
from scipy.spatial import cKDTree

from core.digest import round_floats, short
from tools.astrometry import (
    angular_separation_arcsec,
    estimate_translation,
    pixel_scale_arcsec,
    shift_wcs,
    wcs_from_dict,
    wcs_from_header,
    wcs_to_dict,
)
from tools.imaging import detect_stars, frame_times, load_image, subtract_background

log = logging.getLogger(__name__)


@dataclass
class PhotometryParams:
    fwhm_px: float = 3.0
    detect_sigma: float = 5.0
    aperture_radius_px: float = 5.0
    annulus_px: tuple[float, float] = (9.0, 14.0)
    saturation_adu: float = 55000.0
    hot_pixel_sigma: float = 8.0
    # Registration and stacking.
    registration_stars: int = 80
    transparency_stars: int = 40
    max_registration_shift_px: float = 250.0
    min_frames: int = 5
    min_transparency: float = 0.5
    max_sources: int = 300
    # Measurement at the target.
    detect_snr: float = 5.0
    marginal_snr: float = 3.0
    noise_apertures: int = 80
    noise_radius_px: tuple[float, float] = (20.0, 90.0)
    source_exclusion_px: float = 10.0
    # Astrometric and photometric calibration against Gaia.
    match_tol_px: float = 2.5
    zp_min_stars: int = 8
    zp_snr_min: float = 10.0
    zp_mag_range: tuple[float, float] = (10.5, 16.5)


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def aperture_photometry(
    data: np.ndarray, positions: np.ndarray, rms: float, p: PhotometryParams, n_frames: int = 1
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(flux, error, background-only error)`` in ADU for each position, local sky from an annulus.

    ``n_frames`` is the number of frames averaged into ``data`` (it scales the Poisson term of a mean).
    """
    pos = np.asarray(positions, dtype=float).reshape(-1, 2)
    if len(pos) == 0:
        return np.empty(0), np.empty(0), np.empty(0)
    try:
        ap = CircularAperture(pos, r=p.aperture_radius_px)
        an = CircularAnnulus(pos, r_in=p.annulus_px[0], r_out=p.annulus_px[1])
        sky = np.atleast_1d(np.asarray(ApertureStats(data, an, sigma_clip=SigmaClip(3.0)).median, dtype=float))
        total = np.atleast_1d(np.asarray(ApertureStats(data, ap).sum, dtype=float))
    except Exception:  # noqa: BLE001 - e.g. an aperture entirely off the image
        nan = np.full(len(pos), np.nan)
        return nan, nan.copy(), nan.copy()
    area, n_ann = float(ap.area), float(an.area)
    flux = total - sky * area
    bkg_err = math.sqrt(area * rms**2 * (1.0 + area / n_ann))
    err = np.sqrt(np.clip(flux, 0.0, None) / max(n_frames, 1) + bkg_err**2)
    return flux, err, np.full(len(pos), bkg_err)


def _peak_adu(data: np.ndarray, x: float, y: float) -> float:
    ny, nx = data.shape
    xi, yi = int(round(x)), int(round(y))
    cut = data[max(yi - 2, 0):min(yi + 3, ny), max(xi - 2, 0):min(xi + 3, nx)]
    return float(cut.max()) if cut.size else float("nan")


def repair_hot_pixels(sub: np.ndarray, rms: float, sigma: float) -> int:
    """Replace isolated one-pixel spikes by the local median (in place); returns how many.

    A spike is a pixel far above its 3x3 median whose eight neighbours are *not* raised.  A real
    star's core always has raised neighbours (its PSF spans a few pixels), so it is left alone.
    """
    med = median_filter(sub, size=3, mode="nearest")
    ring = (uniform_filter(sub, size=3, mode="nearest") * 9.0 - sub) / 8.0
    hot = ((sub - med) > sigma * rms) & (ring < 0.25 * sub)
    sub[hot] = med[hot]
    return int(hot.sum())


def _paste(acc: np.ndarray, weight: np.ndarray, data: np.ndarray, dx: float, dy: float) -> None:
    """``acc[y + dy, x + dx] += data[y, x]`` for the integer-rounded shift (reference = frame + shift)."""
    ny, nx = data.shape
    sx, sy = int(round(dx)), int(round(dy))
    x0, x1 = max(0, -sx), min(nx, nx - sx)
    y0, y1 = max(0, -sy), min(ny, ny - sy)
    if x1 <= x0 or y1 <= y0:
        return
    acc[y0 + sy:y1 + sy, x0 + sx:x1 + sx] += data[y0:y1, x0:x1]
    weight[y0 + sy:y1 + sy, x0 + sx:x1 + sx] += 1.0


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
@dataclass
class _Frame:
    header: fits.Header
    data: np.ndarray  # background-subtracted, hot pixels repaired, float32
    t_start: Any
    t_mid: Any
    exptime: float
    rms: float
    sky: float
    xy: np.ndarray
    flux: np.ndarray
    n_hot: int


def _sorted_paths(paths: list[str]) -> list[str]:
    keyed = []
    for i, path in enumerate(paths):
        try:
            raw = fits.getheader(path).get("DATE-OBS")
            key = Time(raw, scale="utc").unix if raw else float(i)
        except Exception:  # noqa: BLE001 - the real error surfaces when the frame is loaded
            key = float(i)
        keyed.append((key, i, path))
    return [p for _, _, p in sorted(keyed)]


def _load_frame(path: str, index: int, p: PhotometryParams) -> _Frame:
    data, header = load_image(path)
    start, exptime = frame_times(header, index)
    sub, rms, sky = subtract_background(data)
    n_hot = repair_hot_pixels(sub, rms, p.hot_pixel_sigma)
    xy, flux = detect_stars(sub, rms, p.fwhm_px, p.detect_sigma)
    return _Frame(header, sub, start, start + timedelta(seconds=exptime / 2.0), exptime, rms, sky, xy, flux, n_hot)


def _brightest(frame: _Frame, n: int) -> np.ndarray:
    return frame.xy[np.argsort(frame.flux)[::-1][:n]]


# --------------------------------------------------------------------------- #
# Concise, model-safe summary
# --------------------------------------------------------------------------- #
def summarize(findings: dict[str, Any]) -> dict[str, Any]:
    """A few rounded numbers: no source list, no pixels, no WCS, no paths."""
    stack = findings.get("stack") or {}
    out: dict[str, Any] = {
        "n_frames": findings.get("n_frames"),
        "n_frames_used": findings.get("n_frames_used"),
        "total_exposure_s": findings.get("total_exposure_s"),
        "n_sources": findings.get("n_sources"),
        "stack_rms_adu": stack.get("rms_adu"),
        "min_transparency": findings.get("min_transparency"),
        "warnings": [short(w, 160) for w in (findings.get("warnings") or [])[:3]],
    }
    return {k: v for k, v in round_floats(out, 4).items() if v is not None}


# --------------------------------------------------------------------------- #
# Reduction: frames -> stack + source list
# --------------------------------------------------------------------------- #
def analyze(
    fits_paths: list[str],
    params: PhotometryParams | None = None,
    figure_dir: str | Path | None = None,
) -> dict[str, Any]:
    p = params or PhotometryParams()
    if not fits_paths:
        raise ValueError("analyze() needs at least one FITS path")
    paths = _sorted_paths(list(fits_paths))
    out_dir = Path(figure_dir) if figure_dir is not None else Path(paths[0]).parent
    warnings: list[str] = []

    ref = _load_frame(paths[0], 0, p)
    findings: dict[str, Any] = {
        "n_frames": len(paths), "n_frames_used": 0, "n_sources": 0,
        "exposure_s": ref.exptime, "total_exposure_s": 0.0,
        "epoch_first_iso": ref.t_start.isoformat(),
        "object": str(ref.header.get("OBJECT") or ""),
        "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.__dict__.items()},
        "warnings": warnings, "detections": [], "figures": [],
    }

    def finish(summary: str) -> dict[str, Any]:
        findings["summary"] = summary
        findings["agent_summary"] = summarize(findings)
        log.info("Stack: %s", summary)
        return findings

    if len(paths) < p.min_frames:
        warnings.append(f"Only {len(paths)} frame(s); at least {p.min_frames} are needed for a useful stack.")
        return finish(f"{len(paths)} frames: too few to stack.")
    if len(ref.xy) < 8:
        warnings.append(f"Only {len(ref.xy)} star(s) detected in the first frame; frames cannot be registered.")
        return finish("Too few stars in the first frame to register the sequence.")

    ref_stars = _brightest(ref, p.registration_stars)
    ref_flux, _, _ = aperture_photometry(ref.data, ref.xy, ref.rms, p)
    order = np.argsort(ref.flux)[::-1]
    peaks = np.array([_peak_adu(ref.data, *ref.xy[i]) + ref.sky for i in order])
    ok = (ref_flux[order] > 0) & np.isfinite(ref_flux[order]) & (peaks < p.saturation_adu)
    tr_idx = order[ok][: p.transparency_stars]  # stars used to measure each frame's transparency

    ny, nx = ref.data.shape
    acc = np.zeros((ny, nx), dtype=np.float64)
    weight = np.zeros((ny, nx), dtype=np.float64)
    shifts: list[tuple[float, float]] = []
    transparency: list[float] = []
    times, rms_list, n_hot = [], [], 0
    failed = cloudy = 0
    for i, path in enumerate(paths):
        fr = ref if i == 0 else _load_frame(path, i, p)
        est = (
            estimate_translation(_brightest(fr, p.registration_stars), ref_stars, max_shift=p.max_registration_shift_px,
                                 tol=2.0, min_matches=8)
            if i else {"dx": 0.0, "dy": 0.0, "n_matches": len(ref_stars)}
        )
        if est is None:
            failed += 1
            log.warning("Stack: registration failed for %s; frame excluded", Path(path).name)
            continue
        dx, dy = est["dx"], est["dy"]
        if len(tr_idx):
            f, _, _ = aperture_photometry(fr.data, ref.xy[tr_idx] - np.array([dx, dy]), fr.rms, p)
            good = np.isfinite(f)
            trans = float(np.median(f[good] / ref_flux[tr_idx][good])) if good.any() else float("nan")
        else:
            trans = 1.0
        if not math.isfinite(trans) or trans < p.min_transparency:
            cloudy += 1
            log.warning("Stack: %s has transparency %.2f (< %.2f); frame excluded", Path(path).name, trans, p.min_transparency)
            continue
        _paste(acc, weight, fr.data, dx, dy)
        shifts.append((dx, dy))
        transparency.append(trans)
        times.append(fr.t_mid)
        rms_list.append(fr.rms)
        n_hot += fr.n_hot
        if i:
            del fr  # keep memory flat over long sequences

    if failed:
        warnings.append(f"Registration failed for {failed} frame(s); they were excluded.")
    if cloudy:
        warnings.append(f"{cloudy} frame(s) with transparency below {p.min_transparency:.0%} of the first frame were excluded.")
    n_used = len(shifts)
    findings["n_frames_used"] = n_used
    if n_used < p.min_frames:
        return finish(f"Only {n_used} frame(s) could be registered and used; too few to stack.")

    stack = np.where(weight > 0, acc / np.maximum(weight, 1.0), 0.0).astype(np.float32)
    sx = np.array([s[0] for s in shifts])
    sy = np.array([s[1] for s in shifts])
    # Pixels covered by every used frame (the stack is shallower, noisier, towards the edges).
    box = [int(math.ceil(max(sx.max(), 0.0))), int(nx + math.floor(min(sx.min(), 0.0))),
           int(math.ceil(max(sy.max(), 0.0))), int(ny + math.floor(min(sy.min(), 0.0)))]
    stack, stack_rms, _ = subtract_background(stack)

    xy, dao_flux = detect_stars(stack, stack_rms, p.fwhm_px, p.detect_sigma)
    if len(xy):
        keep = np.argsort(dao_flux)[::-1][: p.max_sources]
        xy = xy[keep]
    flux, err, _ = aperture_photometry(stack, xy, stack_rms, p, n_used)
    peak = np.array([_peak_adu(stack, x, y) for x, y in xy]) if len(xy) else np.empty(0)
    good = np.isfinite(flux) & np.isfinite(err)
    sources = [[round(float(x), 2), round(float(y), 2), round(float(f), 1), round(float(e), 1), round(float(pk), 1)]
               for (x, y), f, e, pk in zip(xy[good], flux[good], err[good], peak[good])]

    total_exposure = float(ref.exptime * n_used)
    stack_path = out_dir / "stack.fits"
    out_dir.mkdir(parents=True, exist_ok=True)
    hdr = ref.header.copy()
    for key in ("BZERO", "BSCALE", "BITPIX"):
        hdr.remove(key, ignore_missing=True)
    hdr["EXPTIME"] = (total_exposure, "[s] total exposure of the stack")
    hdr["NFRAMES"] = (n_used, "frames averaged")
    hdr["STKRMS"] = (float(stack_rms), "[ADU/px] background rms of the stack")
    hdr["COMMENT"] = "Astronomo mean stack, integer-pixel registration, reference = first frame"
    fits.PrimaryHDU(data=stack.astype(np.float32), header=hdr).writeto(stack_path, overwrite=True)

    wcs = wcs_from_header(ref.header, shape=(ny, nx))
    if wcs is None:
        warnings.append("No pointing/WCS information in the headers; astrometric calibration is unavailable.")
    else:
        findings["astrometry"] = {
            "wcs": wcs_to_dict(wcs),
            "reference_stars": [[x, y] for x, y, *_ in sources[:150]],
            "frame_shape": [int(ny), int(nx)],
        }
    t0 = times[0]
    findings.update(
        total_exposure_s=total_exposure, n_sources=len(sources), sources=sources,
        epoch_first_iso=min(times).isoformat(), epoch_last_iso=max(times).isoformat(),
        epoch_mid_iso=(min(times) + (max(times) - min(times)) / 2).isoformat(),
        span_s=float((max(times) - t0).total_seconds()),
        min_transparency=round(float(min(transparency)), 3),
        transparency=[round(float(v), 3) for v in transparency],
        drift_px=[float(sx[-1] - sx[0]), float(sy[-1] - sy[0])],
        hot_pixels_repaired=int(n_hot),
        stack={"path": str(stack_path), "n_frames": n_used, "rms_adu": float(stack_rms), "shape": [int(ny), int(nx)],
               "full_coverage_box": box},
    )
    if len(sources) < p.zp_min_stars:
        warnings.append(f"Only {len(sources)} source(s) detected in the stack; calibration against Gaia is unlikely to work.")
    return finish(
        f"Stacked {n_used}/{len(paths)} frames ({total_exposure:.0f} s total); {len(sources)} sources detected, "
        f"stack background rms {stack_rms:.2f} ADU/px."
    )


# --------------------------------------------------------------------------- #
# Calibration against Gaia
# --------------------------------------------------------------------------- #
def calibrate(astrometry: dict, sources: list[list[float]], gaia_rows: list[dict], p: PhotometryParams | None = None) -> dict[str, Any]:
    """Pointing correction and photometric zero point from Gaia DR3 stars matched to the stack's sources.

    ``zero_point`` satisfies ``G = zero_point - 2.5 log10(flux_adu)`` for the stack's aperture fluxes.
    The returned ``wcs`` already includes the pointing correction.
    """
    p = p or PhotometryParams()
    wcs = wcs_from_dict(astrometry["wcs"])
    ny, nx = astrometry["frame_shape"]
    scale = pixel_scale_arcsec(wcs)
    if len(gaia_rows) < p.zp_min_stars:
        return {"status": "failed", "reason": f"only {len(gaia_rows)} Gaia source(s) in the field"}
    gaia_px = np.array(wcs.all_world2pix([r["ra_deg"] for r in gaia_rows], [r["dec_deg"] for r in gaia_rows], 0)).T
    inside = (gaia_px[:, 0] > -50) & (gaia_px[:, 0] < nx + 50) & (gaia_px[:, 1] > -50) & (gaia_px[:, 1] < ny + 50)
    est = estimate_translation(
        gaia_px[inside][:150], np.asarray(astrometry["reference_stars"], dtype=float), max_shift=150.0, tol=2.0, min_matches=8
    )
    if est is None:
        return {"status": "failed", "reason": "no consistent star pattern match (frames may be rotated relative to north)",
                "n_gaia": int(inside.sum())}
    new_wcs = shift_wcs(wcs, est["dx"], est["dy"])
    out: dict[str, Any] = {
        "status": "astrometry_only", "wcs": wcs_to_dict(new_wcs), "dx_px": est["dx"], "dy_px": est["dy"],
        "offset_arcsec": float(math.hypot(est["dx"], est["dy"]) * scale), "n_matches": est["n_matches"],
        "rms_arcsec": est["rms_px"] * scale, "zero_point": None,
    }

    src = np.asarray(sources, dtype=float).reshape(-1, 5)
    dist, idx = cKDTree(gaia_px + np.array([est["dx"], est["dy"]])).query(src[:, :2], distance_upper_bound=p.match_tol_px)
    matched = np.isfinite(dist)
    g = np.full(len(src), np.nan)
    g[matched] = np.array([gaia_rows[i]["g_mag"] for i in idx[matched]])
    flux, err, peak = src[:, 2], src[:, 3], src[:, 4]
    with np.errstate(divide="ignore", invalid="ignore"):
        use = (
            matched & (flux > 0) & (flux / err >= p.zp_snr_min) & (peak < p.saturation_adu)
            & (g >= p.zp_mag_range[0]) & (g <= p.zp_mag_range[1])
        )
        zp = g + 2.5 * np.log10(flux)
    zp = zp[use]
    for _ in range(4):  # clip blends, variables and mismatches
        if len(zp) < p.zp_min_stars:
            break
        med = float(np.median(zp))
        sig = 1.4826 * float(np.median(np.abs(zp - med)))
        keep = np.abs(zp - med) <= max(3.0 * sig, 0.05)
        if keep.all():
            break
        zp = zp[keep]
    if len(zp) < p.zp_min_stars:
        out["reason"] = f"only {len(zp)} usable Gaia star(s) for a zero point (need {p.zp_min_stars})"
        return out
    scatter = 1.4826 * float(np.median(np.abs(zp - np.median(zp))))
    out.update(
        status="calibrated", zero_point=float(np.median(zp)), zp_scatter=scatter, zp_n=int(len(zp)),
        zp_err=max(scatter / math.sqrt(len(zp)), 0.02),
    )
    return out


# --------------------------------------------------------------------------- #
# Forced photometry at a known position
# --------------------------------------------------------------------------- #
def empirical_noise(
    stack: np.ndarray, xy: tuple[float, float], source_xy: np.ndarray, rms: float, p: PhotometryParams, n_frames: int, seed: int = 0
) -> tuple[float | None, int]:
    """1-sigma scatter (ADU) of the *same* aperture measurement on empty sky around ``xy``."""
    rng = np.random.default_rng(seed)
    ny, nx = stack.shape
    pad = p.annulus_px[1] + 2.0
    pts: list[tuple[float, float]] = []
    for _ in range(4000):
        if len(pts) >= p.noise_apertures:
            break
        r, th = rng.uniform(*p.noise_radius_px), rng.uniform(0.0, 2.0 * math.pi)
        x, y = xy[0] + r * math.cos(th), xy[1] + r * math.sin(th)
        if not (pad < x < nx - pad and pad < y < ny - pad):
            continue
        if len(source_xy) and float(np.min(np.hypot(source_xy[:, 0] - x, source_xy[:, 1] - y))) < p.source_exclusion_px:
            continue
        pts.append((x, y))
    if len(pts) < 20:
        return None, len(pts)
    flux, _, _ = aperture_photometry(stack, np.array(pts), rms, p, n_frames)
    flux = flux[np.isfinite(flux)]
    if len(flux) < 20:
        return None, len(flux)
    return 1.4826 * float(np.median(np.abs(flux - np.median(flux)))), int(len(flux))


def measure_target(
    stack: np.ndarray, rms: float, n_frames: int, xy: tuple[float, float], sources: list[list[float]],
    zero_point: float | None, zp_err: float = 0.0, p: PhotometryParams | None = None, seed: int = 0,
) -> dict[str, Any]:
    """Aperture photometry of the stack at pixel position ``xy`` (0-based), with an honest error."""
    p = p or PhotometryParams()
    ny, nx = stack.shape
    x, y = float(xy[0]), float(xy[1])
    pad = p.annulus_px[1] + 1.0
    out: dict[str, Any] = {"x": x, "y": y}
    if not (math.isfinite(x) and math.isfinite(y)) or not (0 <= x < nx and 0 <= y < ny):
        return {**out, "status": "outside_frame"}
    if not (pad <= x <= nx - 1 - pad and pad <= y <= ny - 1 - pad):
        return {**out, "status": "edge"}
    flux, err, bkg_err = aperture_photometry(stack, np.array([[x, y]]), rms, p, n_frames)
    if not np.isfinite(flux[0]):
        return {**out, "status": "unmeasurable"}
    src = np.asarray(sources, dtype=float).reshape(-1, 5)
    emp, n_emp = empirical_noise(stack, (x, y), src[:, :2], rms, p, n_frames, seed)
    sigma_bkg = max(float(bkg_err[0]), emp or 0.0)
    f = float(flux[0])
    sigma = math.sqrt(max(f, 0.0) / max(n_frames, 1) + sigma_bkg**2)
    snr = f / sigma
    out.update(
        status="ok", flux_adu=f, flux_err_adu=sigma, snr=snr, analytic_noise_adu=float(bkg_err[0]),
        empirical_noise_adu=emp, n_noise_apertures=n_emp,
        detected=bool(snr >= p.detect_snr), marginal=bool(p.marginal_snr <= snr < p.detect_snr),
    )
    if len(src):
        d = np.hypot(src[:, 0] - x, src[:, 1] - y)
        i = int(np.argmin(d))
        out["nearest_source_px"] = float(d[i])
        near = (d < 8.0) & (src[:, 2] > max(f, 0.0))
        out["brighter_neighbour_px"] = float(d[near].min()) if near.any() else None
    if zero_point is not None:
        out["limit_mag_5sigma"] = float(zero_point - 2.5 * math.log10(p.detect_snr * sigma_bkg))
        if f > 0 and snr >= p.marginal_snr:
            m_stat = 2.5 / math.log(10.0) / snr
            out["mag"] = float(zero_point - 2.5 * math.log10(f))
            out["mag_err"] = float(math.hypot(m_stat, zp_err))
    return out


def gaia_counterpart(rows: list[dict], ra: float, dec: float, radius_arcsec: float = 3.0) -> dict | None:
    """The nearest Gaia star within ``radius_arcsec`` of a position, or ``None``."""
    best, best_sep = None, radius_arcsec
    for r in rows:
        sep = angular_separation_arcsec(ra, dec, r["ra_deg"], r["dec_deg"])
        if sep <= best_sep:
            best, best_sep = r, sep
    return None if best is None else {"g_mag": best["g_mag"], "separation_arcsec": best_sep, "source_id": best.get("source_id")}
