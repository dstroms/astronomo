"""Differential photometry and transit fitting for a long single-target frame sequence.

Pipeline
--------
1. Load frames in time order; model and subtract the sky (``Background2D``) and detect stars
   (``DAOStarFinder``) in each.  The finder starts at 3 px.  A soft field that it misses is
   re-detected with a wider kernel, and the aperture moves out with that width.
2. In the first frame pick the **target star** (brightest star near the image centre, since the
   telescope is pointed at the host) and a set of **comparison stars** (isolated, unsaturated,
   not far brighter or fainter than the target).
3. Register every frame to the first with a translation estimated from the brightest stars, so
   each star can be re-located despite pointing drift.
4. Circular-aperture photometry of the target and the comparison stars (local sky annulus).
5. **Differential photometry**: divide the target flux by the summed flux of the comparison
   ensemble, after rejecting comparison stars that vary more than their photon noise allows.
   Anything common to all stars (atmospheric transparency, thin cloud) cancels.
6. Fit a trapezoid transit times a low-order polynomial baseline (``plugins/transit_finder/transit_model``):
   a coarse grid over mid-time and duration, then a least-squares polish.  The decision to call
   something a transit uses the depth signal-to-noise (error bars inflated for red noise), the
   Bayesian-information-criterion improvement over a transit-free baseline, and coverage.

The search is **blind**: ``analyze`` does not know where the transit is expected.  Comparing the
result with the catalogue ephemeris is the cross-reference step's job.

Data-reduction rule
-------------------
Everything above is plain Python on pixel data.  ``analyze()`` returns *heavy* findings (the
light curve, the WCS, reference stars) for the dashboard, cross-referencing and the mission log, plus
``findings["agent_summary"]``: the concise, rounded JSON from :func:`summarize`.  Only that summary
may be shown to a language model (see ``core/digest.py``, which enforces it).

Known limitations (reported, not hidden)
----------------------------------------
* Field rotation of an alt-az mount is measured per frame (``tools.astrometry.estimate_rotation``) and,
  by default (``correct_rotation``), corrected: each frame is registered to the first with a rotation
  about the frame centre plus a shift, so the apertures follow the stars.  Past ``warn_rotation_edge_px``
  of displacement at the frame corner the run says so.  The correction is unproven on a real sequence.
  With ``correct_rotation=False`` registration is translation-only, and past ``max_rotation_edge_px``
  (or when registration fails and a rotation explains why) the run refuses to produce a light curve,
  because stars near the edge would be mis-registered and the photometry quietly wrong.  Per-star
  centroid refinement absorbs only small residuals.
* The model is a trapezoid, not a limb-darkened Mandel-Agol curve; see ``plugins/transit_finder/transit_model``.
* Photometry is instrumental differential photometry in the camera band: no colour terms, no
  airmass fit.  Remaining slow trends are absorbed by the polynomial baseline, which can also
  absorb part of a transit that is not fully covered by out-of-transit data.
* Times are the UTC mid-exposure from ``DATE-OBS``; no barycentric correction is applied here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from astropy.time import Time
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Circle
from photutils.aperture import ApertureStats, CircularAnnulus, CircularAperture
from scipy.optimize import least_squares
from scipy.spatial import cKDTree

from core.digest import round_floats, short
from tools.astrometry import estimate_rotation, estimate_translation, find_rotation, wcs_from_header, wcs_to_dict
from tools.imaging import NARROW_STAR_FWHM_PX, average_2x2, detect_stars, frame_times, load_image, subtract_background
from plugins.transit_finder.transit_model import transit_flux, transit_shape

log = logging.getLogger(__name__)

# DAOStarFinder rejects stars that are much broader than its kernel.  These are the retries, in pixels.
_WIDER_KERNELS_PX = (4.0, 6.0, 8.0, 10.0, 14.0)
_MIN_STARS = 8  # below this, the target and its comparison stars cannot be chosen


@dataclass
class PhotometryParams:
    fwhm_px: float = 3.0
    detect_sigma: float = 5.0
    aperture_radius_px: float = 5.0
    annulus_px: tuple[float, float] = (9.0, 14.0)
    # Target / comparison selection (all in first-frame pixels).
    # Where the target star is expected in the first frame.  ``None`` = the image centre (a telescope
    # pointed at the host).  Archive frames carry the target's sky position in the TARGRA/TARGDEC
    # header keywords plus a WCS, from which ``analyze`` fills this in.
    target_xy: tuple[float, float] | None = None
    target_search_radius_px: float = 45.0  # brightest star within this of ``target_xy`` / the image centre
    comp_exclusion_radius_px: float = 30.0  # comparison stars must be at least this far from the target
    comp_flux_range: tuple[float, float] = (0.15, 4.0)  # allowed flux relative to the target
    isolation_radius_px: float = 16.0  # reject stars with a non-negligible neighbour this close
    saturation_adu: float = 55000.0
    edge_margin_px: float = 40.0
    max_comps: int = 10
    min_comps: int = 2
    # Registration / centroiding.
    registration_stars: int = 80
    # The largest shift between a frame and the first.  A mount that drifts (the first real night: about
    # 12 px per frame, 250 px after 20 frames) needs room; matching still needs 8 stars to agree.
    max_registration_shift_px: float = 600.0
    # Field rotation, as displacement at the frame corner (px).  None disables the refusal.
    warn_rotation_edge_px: float = 1.5
    max_rotation_edge_px: float | None = 4.0
    # Register with rotation (about the frame centre) as well as a shift.  The refusal above then
    # applies only when this is off.
    correct_rotation: bool = True
    # Below this corner displacement a rotation fit adds noise, not accuracy: shift only.
    rotation_min_edge_px: float = 0.3
    # A colour mosaic with sharp stars: "auto" averages each frame 2x2 before measuring when the stars are
    # narrower than ``NARROW_STAR_FWHM_PX`` and the frame is from a Seestar without a BAYERPAT keyword
    # (its headers carry none, so the green channel cannot be picked out); "always" and "off" force it.
    mosaic_averaging: str = "auto"
    refine_centroids: bool = True
    centroid_half_box_px: int = 5
    max_centroid_shift_px: float = 3.0
    max_missing_fraction: float = 0.05  # a comparison star missing in more frames than this is dropped
    # Fit and detection.
    baseline_order: int = 1  # polynomial degree of the out-of-transit baseline
    ingress_fraction: float = 0.1  # ingress duration / total duration, held fixed in the fit
    min_frames: int = 12
    min_snr: float = 5.0
    min_delta_bic: float = 10.0
    min_in_transit_points: int = 4
    min_baseline_points: int = 3  # per side, for "full" coverage
    bin_minutes: float = 10.0  # for the figure only


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
@dataclass
class _Frame:
    path: str
    header: fits.Header
    data: np.ndarray  # background-subtracted, float32
    t_mid: datetime
    t_start: datetime
    exptime: float
    rms: float
    sky: float
    xy: np.ndarray
    flux: np.ndarray


def _sorted_paths(paths: list[str]) -> list[str]:
    """Chronological order from DATE-OBS (cheap: headers only); unreadable ones keep their place."""
    keyed = []
    for i, path in enumerate(paths):
        try:
            raw = fits.getheader(path).get("DATE-OBS")
            key = Time(raw, scale="utc").unix if raw else float(i)
        except Exception:  # noqa: BLE001 - the real error surfaces when the frame is loaded
            key = float(i)
        keyed.append((key, i, path))
    return [p for _, _, p in sorted(keyed)]


def _scale_to_width(p: PhotometryParams, fwhm_px: float) -> PhotometryParams:
    """Apertures and the centroid box, grown so they still cover a star of width ``fwhm_px``."""
    scale = float(fwhm_px) / p.fwhm_px
    if scale <= 1.05:
        return p
    inner, outer = p.annulus_px
    return replace(
        p,
        fwhm_px=float(fwhm_px),
        aperture_radius_px=p.aperture_radius_px * scale,
        annulus_px=(inner * scale, outer * scale),
        centroid_half_box_px=max(1, int(round(p.centroid_half_box_px * scale))),
    )


def _nearest_detection_px(xy: np.ndarray, target_xy: tuple[float, float] | None) -> float | None:
    """Distance from ``target_xy`` to the nearest detection, or None when there is no target or no detection."""
    if target_xy is None or len(xy) == 0:
        return None
    centre = np.asarray(target_xy, dtype=float)
    return float(np.min(np.hypot(*(np.asarray(xy, dtype=float) - centre).T)))


def _target_unseen(xy: np.ndarray, p: PhotometryParams) -> bool:
    """True when a known target position has no detection inside the search radius."""
    nearest = _nearest_detection_px(xy, p.target_xy)
    return nearest is not None and nearest > p.target_search_radius_px


def _widen_if_soft(ref: "_Frame", p: PhotometryParams, loader) -> tuple[PhotometryParams, "_Frame", str | None]:
    """Re-detect when the configured kernel cannot do the measurement, and keep the wider kernel that works.

    A focused frame with the target among its detections is unchanged.  The search runs once, on the
    first frame; later frames use the kernel it chose.  Two cases widen it.  Too few stars at all: the
    first real night had stars of about 8.5 px and the 3 px finder saw 4.  Plenty of stars, but none
    within the search radius of the target: on 2026-11-18 Hat-P-23 was 4.9 px wide, the 3 px finder saw
    159 other stars and the nearest of them was 146 px from the target, and a 6 px finder put a star
    5 px from the predicted position.
    """
    unseen = _target_unseen(ref.xy, p)
    if len(ref.xy) >= _MIN_STARS and not unseen:
        return p, ref, None
    # The kernel that sees the most stars, when the field is simply soft.  The narrowest successful
    # kernel can split a soft star into several peaks, and those then fail the blend cut.  A tie goes
    # to the narrower kernel so the aperture stays on the star.  When the target itself is missing,
    # the narrowest kernel that places a detection on it wins instead.
    chosen, best = None, None
    for kernel in _WIDER_KERNELS_PX:
        if kernel <= p.fwhm_px + 0.2:
            continue
        xy, _flux = detect_stars(ref.data, ref.rms, kernel, p.detect_sigma)
        if len(xy) < _MIN_STARS:
            continue
        if unseen and _target_unseen(xy, p):
            continue
        if unseen:
            chosen = kernel
            break
        rank = (len(xy), -kernel)
        if best is None or rank > best:
            best, chosen = rank, kernel
    if chosen is None:
        return p, ref, None
    widened = _scale_to_width(p, chosen)
    reloaded = _load_frame(ref.path, 0, widened, loader)
    if unseen:
        note = (
            f"The {p.fwhm_px:.0f} px finder saw {len(ref.xy)} stars but none within {p.target_search_radius_px:.0f} px "
            f"of the target, so the first frame was re-measured with a {chosen:.0f} px kernel ({len(reloaded.xy)} stars)."
        )
    else:
        note = (
            f"Stars are wider than the {p.fwhm_px:.0f} px finder; measured with a {chosen:.0f} px kernel "
            f"({len(reloaded.xy)} stars in the first frame).  Refocusing would give a cleaner measurement."
        )
    log.info("Photometry: %s", note)
    return widened, reloaded, note


def _averaged(loader):
    """``loader`` with every frame it returns averaged 2x2 (see :func:`tools.imaging.average_2x2`)."""

    def load(path: str):
        data, header = loader(path)
        return average_2x2(data), header

    return load


def _average_if_narrow(path: str, ref: "_Frame", p: PhotometryParams, loader) -> tuple[Any, "_Frame", str | None]:
    """Re-read the sequence 2x2-averaged when sharp stars on a colour mosaic would be seen as rings.

    Returns ``(loader, reference frame, note)``; the note is None when nothing changed.  Found on the first
    in-focus night (2026-11-16): at 2.2 px a star was four bright green pixels around a dim centre and the
    finder returned nothing there, where 8.5 px stars had been fine.
    """
    mode = p.mosaic_averaging
    width = None
    if mode == "off":
        return loader, ref, None
    if mode == "auto":
        if ref.header.get("BAYERPAT") or "seestar" not in str(ref.header.get("INSTRUME") or "").lower():
            return loader, ref, None
        from core.setup_checks import measure_star_width  # the same width the first-frame check reports

        found = measure_star_width(loader(path)[0])
        if found is None or found["fwhm_px"] >= NARROW_STAR_FWHM_PX:
            return loader, ref, None
        width = float(found["fwhm_px"])
    averaged = _averaged(loader)
    note = (
        (f"Stars are {width:.1f} px wide" if width is not None else "Averaging was forced")
        + ": narrower than a colour mosaic samples cleanly, so every frame was averaged over 2x2 pixels before "
        "measuring.  The frames carry no BAYERPAT, so the green channel was not picked out (unconfirmed).  "
        "Photon-noise errors from the averaged pixels are too small; the light-curve scatter is the honest figure."
    )
    log.info("Photometry: %s", note)
    return averaged, _load_frame(path, 0, p, averaged), note


def _load_frame(path: str, index: int, p: PhotometryParams, loader=load_image) -> _Frame:
    data, header = loader(path)
    start, exptime = frame_times(header, index)
    sub, rms, sky = subtract_background(data)
    xy, flux = detect_stars(sub, rms, p.fwhm_px, p.detect_sigma)
    return _Frame(
        path=str(path), header=header, data=sub, t_start=start, t_mid=start + timedelta(seconds=exptime / 2.0),
        exptime=exptime, rms=rms, sky=sky, xy=xy, flux=flux,
    )


# --------------------------------------------------------------------------- #
# Photometry
# --------------------------------------------------------------------------- #
def _aperture_photometry(data: np.ndarray, positions: np.ndarray, rms: float, p: PhotometryParams) -> tuple[np.ndarray, np.ndarray]:
    """Background-subtracted aperture flux and its 1-sigma error for each position (ADU, ~1 e-/ADU)."""
    pos = np.asarray(positions, dtype=float).reshape(-1, 2)
    if len(pos) == 0:
        return np.empty(0), np.empty(0)
    try:
        ap = CircularAperture(pos, r=p.aperture_radius_px)
        an = CircularAnnulus(pos, r_in=p.annulus_px[0], r_out=p.annulus_px[1])
        sky = np.atleast_1d(np.asarray(ApertureStats(data, an, sigma_clip=SigmaClip(3.0)).median, dtype=float))
        total = np.atleast_1d(np.asarray(ApertureStats(data, ap).sum, dtype=float))
    except Exception:  # noqa: BLE001 - e.g. an aperture entirely off the image
        return np.full(len(pos), np.nan), np.full(len(pos), np.nan)
    area, n_ann = float(ap.area), float(an.area)
    flux = total - sky * area
    var = np.clip(flux, 0.0, None) + area * rms**2 * (1.0 + area / n_ann)
    return flux, np.sqrt(var)


def _refine_centroids(data: np.ndarray, pos: np.ndarray, p: PhotometryParams) -> np.ndarray:
    """Re-centre each position on the local flux-weighted centroid (accepted only if it moves little)."""
    out = np.array(pos, dtype=float)
    ny, nx = data.shape
    h = p.centroid_half_box_px
    for i, (x, y) in enumerate(pos):
        xi, yi = int(round(x)), int(round(y))
        if xi - h < 0 or yi - h < 0 or xi + h + 1 > nx or yi + h + 1 > ny:
            continue
        cut = np.asarray(data[yi - h:yi + h + 1, xi - h:xi + h + 1], dtype=float)
        w = np.clip(cut - 0.15 * cut.max(), 0.0, None)
        total = w.sum()
        if total <= 0:
            continue
        xs, ys = np.arange(xi - h, xi + h + 1), np.arange(yi - h, yi + h + 1)
        cx, cy = float((w.sum(axis=0) * xs).sum() / total), float((w.sum(axis=1) * ys).sum() / total)
        if math.hypot(cx - x, cy - y) <= p.max_centroid_shift_px:
            out[i] = (cx, cy)
    return out


def _peak_adu(data: np.ndarray, x: float, y: float, sky: float) -> float:
    ny, nx = data.shape
    xi, yi = int(round(x)), int(round(y))
    cut = data[max(yi - 3, 0):min(yi + 4, ny), max(xi - 3, 0):min(xi + 4, nx)]
    return float(cut.max()) + sky if cut.size else float("nan")


def _brightest(frame: _Frame, n: int) -> np.ndarray:
    return frame.xy[np.argsort(frame.flux)[::-1][:n]]


def _select_stars(ref: _Frame, p: PhotometryParams) -> tuple[dict | None, list[str]]:
    """Choose the target and comparison stars in the reference frame."""
    warnings: list[str] = []
    ny, nx = ref.data.shape
    if len(ref.xy) < 8:
        return None, [f"Only {len(ref.xy)} star(s) detected in the first frame; cannot do differential photometry."]
    flux, _ = _aperture_photometry(ref.data, ref.xy, ref.rms, p)
    centre = np.array(p.target_xy, dtype=float) if p.target_xy is not None else np.array([(nx - 1) / 2.0, (ny - 1) / 2.0])
    dist = np.hypot(*(ref.xy - centre).T)
    margin = p.edge_margin_px
    inside = (ref.xy[:, 0] > margin) & (ref.xy[:, 0] < nx - margin) & (ref.xy[:, 1] > margin) & (ref.xy[:, 1] < ny - margin)
    ok = np.isfinite(flux) & (flux > 0)
    peaks = np.array([_peak_adu(ref.data, x, y, ref.sky) for x, y in ref.xy])
    unsat = peaks < p.saturation_adu

    where = "the predicted target position" if p.target_xy is not None else "the image centre"
    near = np.flatnonzero(ok & (dist <= p.target_search_radius_px))
    if len(near) == 0:
        return None, [f"No star within {p.target_search_radius_px:.0f} px of {where}; cannot identify the target."]
    t_idx = int(near[np.argmax(flux[near])])
    t_flux = float(flux[t_idx])
    rivals = [int(i) for i in near if i != t_idx and flux[i] > 0.5 * t_flux]
    if rivals:
        warnings.append(
            f"{len(rivals)} other star(s) within {p.target_search_radius_px:.0f} px of {where} are at least half "
            "as bright as the chosen target; its identification is ambiguous."
        )
    if not unsat[t_idx]:
        warnings.append("The target star is at or near saturation; photometry is non-linear.")
    tree = cKDTree(ref.xy)

    def blended(i: int) -> bool:
        for j in tree.query_ball_point(ref.xy[i], p.isolation_radius_px):
            if j != i and flux[j] > 0.05 * flux[i]:
                return True
        return False

    if blended(t_idx):
        warnings.append("The target star has a close neighbour inside the isolation radius (possible blend).")

    lo, hi = p.comp_flux_range
    cands = []
    for i in range(len(ref.xy)):
        if i == t_idx or not (ok[i] and inside[i] and unsat[i]):
            continue
        if math.hypot(*(ref.xy[i] - ref.xy[t_idx])) < p.comp_exclusion_radius_px:
            continue
        if not (lo <= flux[i] / t_flux <= hi) or blended(i):
            continue
        cands.append(i)
    cands.sort(key=lambda i: abs(math.log10(flux[i] / t_flux)))
    cands = cands[: p.max_comps]
    if len(cands) < p.min_comps:
        return None, warnings + [f"Only {len(cands)} usable comparison star(s) (need {p.min_comps}); cannot do differential photometry."]

    return {
        "target_xy": ref.xy[t_idx].copy(),
        "target_flux": t_flux,
        "comps_xy": ref.xy[cands].copy(),
        "comps_flux": flux[cands].copy(),
    }, warnings


# --------------------------------------------------------------------------- #
# Differential light curve
# --------------------------------------------------------------------------- #
def differential_light_curve(
    ft: np.ndarray, et: np.ndarray, fc: np.ndarray, ec: np.ndarray, p: PhotometryParams
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """``(relative flux, error, comp-used mask, notes)`` for frames that are already complete.

    ``fc``/``ec`` are ``(n_frames, n_comps)``.  Comparison stars whose ratio to the rest of the
    ensemble scatters well beyond their photon noise are rejected (variables, blends, clipped
    stars), up to three passes.
    """
    notes: list[str] = []
    n, k = fc.shape
    keep = np.ones(k, dtype=bool)
    for _ in range(3):
        idx = np.flatnonzero(keep)
        if len(idx) <= p.min_comps:
            break
        ref = fc[:, idx].sum(axis=1)
        z = []
        for i in idx:
            rest = ref - fc[:, i]
            ratio = fc[:, i] / rest
            ratio = ratio / np.median(ratio)
            expected = np.sqrt((ec[:, i] / fc[:, i]) ** 2 + (np.sqrt((ec[:, idx] ** 2).sum(axis=1) - ec[:, i] ** 2) / rest) ** 2)
            z.append(np.std(ratio, ddof=1) / max(float(np.median(expected)), 1e-9))
        z = np.array(z)
        bad = (z > 2.0 * np.median(z)) & (z > 1.5)
        if not bad.any() or len(idx) - int(bad.sum()) < p.min_comps:
            break
        for i in idx[bad]:
            notes.append(f"Comparison star #{int(i) + 1} rejected (scatter {z[list(idx).index(i)]:.1f}x its photon noise).")
            keep[i] = False
    ref = fc[:, keep].sum(axis=1)
    ref_err = np.sqrt((ec[:, keep] ** 2).sum(axis=1))
    rel = ft / ref
    err = rel * np.sqrt((et / ft) ** 2 + (ref_err / ref) ** 2)
    scale = np.median(rel)
    return rel / scale, err / scale, keep, notes


# --------------------------------------------------------------------------- #
# Transit fit
# --------------------------------------------------------------------------- #
def _tau(t: np.ndarray, centre: float, half_span: float) -> np.ndarray:
    return (np.asarray(t, dtype=float) - centre) / half_span


def _poly(tau: np.ndarray, order: int) -> np.ndarray:
    return np.vander(np.atleast_1d(tau), order + 1, increasing=True)


def red_noise_beta(resid: np.ndarray, sizes: tuple[int, ...] = (3, 4, 5, 6, 8)) -> float:
    """Time-averaging red-noise factor (>= 1): binned scatter relative to white-noise expectation."""
    n = len(resid)
    if n < 12:
        return 1.0
    s1 = float(np.std(resid, ddof=1))
    vals = []
    for m in sizes:
        bins = n // m
        if bins < 4:
            continue
        binned = resid[: bins * m].reshape(bins, m).mean(axis=1)
        expected = s1 / math.sqrt(m) * math.sqrt(bins / (bins - 1))
        if expected > 0:
            vals.append(float(np.std(binned, ddof=1)) / expected)
    return max(1.0, float(np.median(vals))) if vals else 1.0


def fit_fixed_transit(
    t: np.ndarray, f: np.ndarray, e: np.ndarray, t_mid: float, duration: float, ingress: float, order: int = 1
) -> dict[str, float]:
    """Depth of a transit whose mid-time and duration are *known* (linear fit; for upper limits).

    Returns ``depth``, ``depth_err`` (error bars inflated if the fit is poor), and ``n_in``.
    """
    centre, half = 0.5 * (t[0] + t[-1]), max(0.5 * (t[-1] - t[0]), 1.0)
    shape = transit_shape(t, t_mid, duration, ingress)
    A = np.column_stack([_poly(_tau(t, centre, half), order), -shape]) / e[:, None]
    sol, *_ = np.linalg.lstsq(A, f / e, rcond=None)
    resid = f / e - A @ sol
    dof = max(len(t) - A.shape[1], 1)
    scale = math.sqrt(max(1.0, float(resid @ resid) / dof))
    cov = np.linalg.pinv(A.T @ A) * scale**2
    base = float(_poly(_tau(np.array([t_mid]), centre, half), order)[0] @ sol[:-1])
    base = base if base > 0 else 1.0
    return {"depth": float(sol[-1] / base), "depth_err": float(math.sqrt(max(cov[-1, -1], 0.0)) / base), "n_in": int((shape > 0.5).sum())}


def fit_transit(t: np.ndarray, f: np.ndarray, e: np.ndarray, p: PhotometryParams) -> dict[str, Any]:
    """Blind trapezoid-transit fit with a polynomial baseline.  All times in seconds."""
    n = len(t)
    order = p.baseline_order
    centre, half = 0.5 * (t[0] + t[-1]), max(0.5 * (t[-1] - t[0]), 1.0)
    span = t[-1] - t[0]
    cadence = float(np.median(np.diff(t))) if n > 1 else 1.0
    tau = _tau(t, centre, half)
    P = _poly(tau, order)
    w = 1.0 / e
    rho = p.ingress_fraction

    # Null model: baseline only.
    sol0, *_ = np.linalg.lstsq(P * w[:, None], f * w, rcond=None)
    chi2_flat = float((((f - P @ sol0) * w) ** 2).sum())

    # Coarse grid: for a given (mid-time, duration) the dip amplitude and baseline are linear.
    t_min = max(1800.0, 2.0 * cadence)
    if 0.98 * span <= t_min:
        # The shortest dip the fit allows does not fit inside the series, so its bounds would cross.
        # A short visit (a few tens of minutes) can still give a night's magnitude, just not a fitted dip.
        return {"ok": False, "reason": f"the series spans {span / 60.0:.0f} min, too short to fit a dip of at least "
                                       f"{t_min / 60.0:.0f} min", "chi2_flat": chi2_flat, "n": n}
    durations = np.geomspace(t_min, max(min(0.8 * span, 6.0 * 3600.0), t_min * 1.5), 9)
    step = max(cadence / 2.0, float(durations[0]) / 10.0, span / 400.0)
    mids = np.arange(t[0], t[-1] + step, step)
    best: tuple[float, float, float, np.ndarray, float] | None = None  # chi2, tm, T, coeffs, amplitude
    for T in durations:
        for tm in mids:
            shape = transit_shape(t, tm, T, rho * T)
            if (shape > 0.5).sum() < 2:
                continue
            A = np.column_stack([P, -shape]) * w[:, None]
            sol, *_ = np.linalg.lstsq(A, f * w, rcond=None)
            if sol[-1] <= 0:
                continue
            chi2 = float((((f - np.column_stack([P, -shape]) @ sol) * w) ** 2).sum())
            if best is None or chi2 < best[0]:
                best = (chi2, float(tm), float(T), sol[:-1].copy(), float(sol[-1]))
    if best is None:
        return {"ok": False, "reason": "no dip-shaped feature in the light curve", "chi2_flat": chi2_flat, "n": n}

    _, tm0, T0, c0, a0 = best
    base_mid = float(_poly(_tau(np.array([tm0]), centre, half), order)[0] @ c0)
    d0 = float(np.clip(a0 / base_mid if base_mid > 0 else 0.01, 1e-4, 0.5))
    x0 = np.concatenate([[tm0, T0, d0], c0])
    lower = np.concatenate([[t[0], t_min, 0.0], np.full(order + 1, -np.inf)])
    upper = np.concatenate([[t[-1], 0.98 * span, 0.6], np.full(order + 1, np.inf)])
    x0 = np.clip(x0, lower + 1e-9, upper - 1e-9)

    def residuals(theta: np.ndarray) -> np.ndarray:
        tm, T, d = theta[:3]
        return (f - (P @ theta[3:]) * transit_flux(t, tm, d, T, rho * T)) * w

    res = least_squares(residuals, x0, bounds=(lower, upper), x_scale=np.maximum(np.abs(x0), 1e-3), method="trf")
    tm, T, d = (float(v) for v in res.x[:3])
    coeffs = res.x[3:]
    chi2 = float((res.fun**2).sum())
    k_t = 3 + order + 1
    dof = max(n - k_t, 1)
    chi2_red = chi2 / dof
    scale = math.sqrt(max(1.0, chi2_red))
    try:
        cov = np.linalg.pinv(res.jac.T @ res.jac)
        sig = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    except np.linalg.LinAlgError:
        sig = np.full(len(res.x), np.nan)

    baseline = P @ coeffs
    model = baseline * transit_flux(t, tm, d, T, rho * T)
    resid = (f - model) / baseline
    beta = red_noise_beta(resid)
    infl = max(scale, beta)  # one inflation factor for every error bar and for the BIC

    delta_bic = (chi2_flat - chi2) / infl**2 - 3.0 * math.log(n)
    depth_err = float(sig[2] * infl)
    in_mask = np.abs(t - tm) < T / 2.0
    n_pre = int((t < tm - T / 2.0).sum())
    n_post = int((t > tm + T / 2.0).sum())
    return {
        "ok": True,
        "t_mid_s": tm, "t_mid_err_s": float(sig[0] * infl),
        "duration_s": T, "duration_err_s": float(sig[1] * infl),
        "ingress_s": min(rho * T, T / 2.0),
        "depth": d, "depth_err": depth_err,
        "snr": d / depth_err if depth_err > 0 else float("nan"),
        "chi2": chi2, "chi2_flat": chi2_flat, "chi2_red": chi2_red, "dof": dof,
        "delta_bic": float(delta_bic), "beta": beta,
        "n": n, "n_in": int(in_mask.sum()), "n_pre": n_pre, "n_post": n_post,
        "rms": float(np.std(resid, ddof=1)),
        "baseline": baseline, "model": model,
        "coeffs": [float(c) for c in coeffs], "centre": float(centre), "half_span": float(half),
    }


# --------------------------------------------------------------------------- #
# Figure
# --------------------------------------------------------------------------- #
def _binned(t: np.ndarray, y: np.ndarray, width: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    edges = np.arange(t[0], t[-1] + width, width)
    which = np.digitize(t, edges)
    tb, yb, eb = [], [], []
    for b in np.unique(which):
        sel = which == b
        if sel.sum() >= 2:
            tb.append(float(t[sel].mean()))
            yb.append(float(y[sel].mean()))
            eb.append(float(y[sel].std(ddof=1) / math.sqrt(sel.sum())))
    return np.array(tb), np.array(yb), np.array(eb)


def _make_figure(
    t: np.ndarray, f: np.ndarray, fit: dict[str, Any] | None, name: str, t0_iso: str, out_path: Path, p: PhotometryParams,
    detected: bool, dip_label: str = "transit fit",
) -> str:
    hours = t / 3600.0
    fig = Figure(figsize=(9.0, 6.0), dpi=110)
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(2, 1, height_ratios=[3.0, 1.2], hspace=0.08)
    ax = fig.add_subplot(gs[0])
    ax_r = fig.add_subplot(gs[1], sharex=ax)

    if fit and fit.get("ok"):
        flat = f / fit["baseline"]
        model = fit["model"] / fit["baseline"]
        resid = flat - model
    else:
        flat, model, resid = f, None, f - np.median(f)

    ax.plot(hours, flat, ".", color="0.65", ms=4, label="frames")
    tb, yb, eb = _binned(t, flat, p.bin_minutes * 60.0)
    if len(tb):
        ax.errorbar(tb / 3600.0, yb, eb, fmt="o", color="k", ms=4, lw=1, label=f"{p.bin_minutes:g}-min bins")
    if model is not None:
        label = dip_label if detected else "best dip (not significant)"
        ax.plot(hours, model, "-", color="tab:red" if detected else "tab:orange", lw=1.6, label=label)
    ax.set_ylabel("Relative flux (baseline-corrected)" if model is not None else "Relative flux")
    ax.set_title(f"{name}: differential light curve", fontsize=10)
    ax.legend(loc="lower left", fontsize=8)
    ax.tick_params(labelbottom=False)

    ax_r.plot(hours, resid * 1e3, ".", color="0.5", ms=3)
    ax_r.axhline(0.0, color="tab:red", lw=0.8)
    ax_r.set_ylabel("Resid. (ppt)")
    ax_r.set_xlabel(f"Hours since {t0_iso[:19].replace('T', ' ')} UTC")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    return str(out_path)


def render_finder(
    frame_path: str,
    target_xy: Sequence[float],
    comparison_xy: Sequence[Sequence[float]],
    out_path: str | Path,
    *,
    name: str = "Target",
    aperture_radius_px: float = 8.0,
    loader=None,
) -> str:
    """PNG of the first frame with the measured target (T) and comparison stars (C) marked.

    The marks are the pixel positions the light curve used. This is the field image the AAVSO
    Exoplanet form asks for. It is not a new plate solution: a rotated Seestar frame can disagree
    with a north-up header, and the Gaia identity check is what says whether T is the host.
    """
    read = loader or load_image
    data, _header = read(frame_path)
    data = np.asarray(data, dtype=np.float32)
    if data.ndim != 2 or data.size == 0:
        raise ValueError(f"{frame_path}: finder chart needs a 2-D image")
    step = max(1, int(math.ceil(max(data.shape) / 900)))
    shown = data[::step, ::step]
    finite = shown[np.isfinite(shown)]
    if finite.size == 0:
        raise ValueError(f"{frame_path}: finder chart has no finite pixels")
    lo, hi = np.percentile(finite, [1.0, 99.5])
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        lo, hi = float(np.min(finite)), float(np.max(finite)) + 1.0

    aspect = shown.shape[0] / max(shown.shape[1], 1)
    fig = Figure(figsize=(7.2, max(3.6, 7.2 * aspect)), dpi=110)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1)
    ax.imshow(shown, cmap="gray", vmin=lo, vmax=hi, origin="lower", interpolation="nearest")
    radius = max(6.0, 2.5 * float(aperture_radius_px) / step)

    def mark(xy: Sequence[float], colour: str, label: str) -> None:
        x, y = float(xy[0]) / step, float(xy[1]) / step
        ax.add_patch(Circle((x, y), radius, fill=False, edgecolor=colour, linewidth=1.2))
        ax.annotate(label, (x, y), textcoords="offset points", xytext=(5, 5), color=colour, fontsize=8)

    mark(target_xy, "tab:red", "T")
    for i, xy in enumerate(comparison_xy, start=1):
        mark(xy, "tab:cyan", f"C{i}")
    ax.set_title(f"{name}: target (T) and comparison stars (C)", fontsize=10)
    ax.set_xlabel("First frame. Marks are the stars this light curve used, in pixel coordinates.")
    ax.set_xticks([])
    ax.set_yticks([])
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dest, bbox_inches="tight", facecolor="white")
    return str(dest)


# --------------------------------------------------------------------------- #
# Concise, model-safe summary
# --------------------------------------------------------------------------- #
def summarize(findings: dict[str, Any]) -> dict[str, Any]:
    """A few dozen rounded numbers: no light curve, no pixels, no WCS, no paths."""
    out: dict[str, Any] = {
        "n_frames": findings.get("n_frames"),
        "n_frames_used": findings.get("n_frames_used"),
        "cadence_s": findings.get("cadence_s"),
        "span_h": findings.get("span_h"),
        "n_comparison_stars": findings.get("n_comps"),
        "rms_ppt": findings.get("rms_ppt"),
        "n_transits_detected": len(findings.get("detections") or []),
        "warnings": [short(w, 160) for w in (findings.get("warnings") or [])[:3]],
    }
    fit = findings.get("fit") or {}
    if fit.get("ok"):
        out["best_fit"] = {
            k: fit.get(k)
            for k in ("depth_pct", "depth_err_pct", "duration_h", "t_mid_iso", "snr", "delta_bic", "red_noise_beta",
                      "n_in_transit", "n_pre_transit", "n_post_transit", "significant")
        }
    for det in (findings.get("detections") or [])[:1]:
        out["transit"] = {k: det.get(k) for k in ("id", "quality", "coverage")}
    out = round_floats(out, 4)
    return {k: v for k, v in out.items() if v is not None}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def target_hint(header: fits.Header, wcs) -> tuple[float, float] | None:
    """Pixel position of the target named by the ``TARGRA``/``TARGDEC`` keywords (degrees), if present and on the frame."""
    ra, dec = header.get("TARGRA"), header.get("TARGDEC")
    if ra is None or dec is None:
        return None
    try:
        x, y = wcs.all_world2pix([float(ra)], [float(dec)], 0)
        x, y = float(x[0]), float(y[0])
    except Exception:  # noqa: BLE001 - a malformed hint must not stop the reduction
        return None
    ny, nx = header.get("NAXIS2"), header.get("NAXIS1")
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    if nx is not None and ny is not None and not (0 <= x < nx and 0 <= y < ny):
        log.warning("TARGRA/TARGDEC project outside the frame (%.0f, %.0f); ignoring the target hint", x, y)
        return None
    return x, y


def _register_frame(
    frame_stars: np.ndarray, ref_stars: np.ndarray, pivot: tuple[float, float], corner_px: float, p: PhotometryParams,
) -> tuple[dict[str, Any] | None, float | None]:
    """Register a frame to the first: ``(transform, explaining_angle_deg)``.

    The transform is ``{"angle_deg", "dx", "dy", "n_matches"}`` with ``ref ~ R(angle) (frame - pivot) + pivot + (dx, dy)``,
    or ``None`` when the frame cannot be registered.  ``explaining_angle_deg`` is the rotation that rescued
    (or, with ``correct_rotation`` off, would have rescued) a frame the shift-only match lost.
    """
    est = estimate_translation(frame_stars, ref_stars, max_shift=p.max_registration_shift_px, tol=2.0, min_matches=8)
    hit = None
    if est is None and p.correct_rotation:
        # A small rotation moves outer stars more than the 2 px tolerance, but a wider match still finds
        # the shift.  The rotation fit below then pulls the outer stars in.  (find_rotation's grid is 0.25
        # degrees wide, too coarse for the rotation a few minutes of alt-az tracking gives.)
        loose = estimate_translation(frame_stars, ref_stars, max_shift=p.max_registration_shift_px, tol=8.0, min_matches=8)
        if loose is not None:
            rot = estimate_rotation(frame_stars, ref_stars, (loose["dx"], loose["dy"]), pivot)
            if rot is not None:
                return {"angle_deg": rot["angle_deg"], "dx": rot["dx"], "dy": rot["dy"], "n_matches": rot["n_matches"]}, rot["angle_deg"]
    if est is None:
        hit = find_rotation(frame_stars, ref_stars, pivot, max_shift=p.max_registration_shift_px)
        if hit is None:
            return None, None
        if not p.correct_rotation:
            return None, hit["angle_deg"]
        est = {"dx": hit["dx"], "dy": hit["dy"], "n_matches": hit["n_matches"]}
    rot = estimate_rotation(
        frame_stars, ref_stars, (est["dx"], est["dy"]), pivot, angle0_deg=hit["angle_deg"] if hit else 0.0,
    )
    angle = rot["angle_deg"] if rot is not None else (hit["angle_deg"] if hit else 0.0)
    explained = hit["angle_deg"] if hit else None
    if p.correct_rotation and rot is not None and math.radians(abs(angle)) * corner_px >= p.rotation_min_edge_px:
        return {"angle_deg": angle, "dx": rot["dx"], "dy": rot["dy"], "n_matches": rot["n_matches"]}, explained
    if p.correct_rotation and hit is not None:
        # The refined fit failed but the grid search did find the angle: use that angle and its shift.
        return {"angle_deg": hit["angle_deg"], "dx": hit["dx"], "dy": hit["dy"], "n_matches": hit["n_matches"]}, explained
    return {"angle_deg": angle if rot is not None else 0.0, "dx": est["dx"], "dy": est["dy"],
            "n_matches": est["n_matches"], "shift_only": True}, explained


def _into_frame(ref_xy: np.ndarray, pivot: tuple[float, float], reg: dict[str, Any]) -> np.ndarray:
    """Where first-frame positions fall in a registered frame (inverse of the transform above)."""
    if reg.get("shift_only") or abs(reg["angle_deg"]) == 0.0:
        return ref_xy - np.array([reg["dx"], reg["dy"]])
    c = np.asarray(pivot, dtype=float)
    th = math.radians(reg["angle_deg"])
    rot = np.array([[math.cos(th), math.sin(th)], [-math.sin(th), math.cos(th)]])
    return (ref_xy - c - np.array([reg["dx"], reg["dy"]])) @ rot.T + c


def analyze(
    fits_paths: list[str],
    params: PhotometryParams | None = None,
    figure_dir: str | Path | None = None,
    *,
    loader=None,
    dip_label: str = "transit fit",
) -> dict[str, Any]:
    """Differential light curve of one pointing.

    ``loader`` reads a FITS path the way :func:`tools.imaging.load_image` does (default).  ``dip_label`` is the
    legend of a significant dip on the figure.  The returned ``photometry`` block includes ``target_flux`` and
    ``comparison_flux`` (aperture sums, aligned with ``light_curve`` and with ``comparison_xy``) so a caller can
    put the series on a magnitude scale.
    """
    p = params or PhotometryParams()
    read = loader or load_image
    if not fits_paths:
        raise ValueError("analyze() needs at least one FITS path")
    paths = _sorted_paths(list(fits_paths))
    warnings: list[str] = []

    ref = _load_frame(paths[0], 0, p, read)
    read, ref, mosaic_note = _average_if_narrow(paths[0], ref, p, read)
    if mosaic_note:
        warnings.append(mosaic_note)
    wcs = wcs_from_header(ref.header)
    if p.target_xy is None and wcs is not None:
        hint = target_hint(ref.header, wcs)
        if hint is not None:
            p = replace(p, target_xy=hint)
    findings: dict[str, Any] = {
        "n_frames": len(paths),
        "n_frames_used": 0,
        "mosaic_averaged": mosaic_note is not None,
        "frame_paths": paths,
        "exposure_s": ref.exptime,
        "epoch_first_iso": ref.t_start.isoformat(),
        "object": str(ref.header.get("OBJECT") or ""),
        "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.__dict__.items()},
        "warnings": warnings,
        "detections": [],
        "figures": [],
        "n_comps": 0,
    }

    def finish(summary: str) -> dict[str, Any]:
        findings["summary"] = summary
        findings["agent_summary"] = summarize(findings)
        log.info("Photometry: %s", summary)
        return findings

    if len(paths) < p.min_frames:
        warnings.append(f"Only {len(paths)} frame(s); at least {p.min_frames} are needed for a light curve.")
        return finish(f"{len(paths)} frames: too few for a light curve.")

    if wcs is None:
        warnings.append("No pointing/WCS information in headers; Gaia identity check unavailable.")
    elif p.target_xy is not None:
        log.info("Photometry: target expected at pixel (%.0f, %.0f) from the TARGRA/TARGDEC header keywords", *p.target_xy)

    p, ref, widened = _widen_if_soft(ref, p, read)
    if widened:
        warnings.append(widened)
        findings["params"] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.__dict__.items()}

    sel, sel_warnings = _select_stars(ref, p)
    warnings.extend(sel_warnings)
    if sel is None:
        # Say why: the last warning is the reason (too few stars, no star near the target, too few comparisons).
        detail = next((w for w in reversed(sel_warnings) if w), "Could not select a target star and comparison stars in the first frame.")
        return finish(detail)

    ref_stars = _brightest(ref, p.registration_stars)
    target_ref, comps_ref = sel["target_xy"], sel["comps_xy"]
    all_ref = np.vstack([target_ref[None, :], comps_ref])  # row 0 = target

    rows: list[dict[str, Any]] = []
    failures = 0
    ny0, nx0 = ref.data.shape
    pivot = ((nx0 - 1) / 2.0, (ny0 - 1) / 2.0)
    corner_px = 0.5 * math.hypot(nx0, ny0)  # distance from the pivot to a frame corner
    angles: list[float] = []  # measured rotation of each registered frame relative to the first
    explained: float | None = None  # rotation that explains a failed registration, if one does
    for i, path in enumerate(paths):
        fr = ref if i == 0 else _load_frame(path, i, p, read)
        frame_stars = _brightest(fr, p.registration_stars)
        if i == 0:
            est = {"dx": 0.0, "dy": 0.0, "n_matches": len(ref_stars), "angle_deg": 0.0, "shift_only": True}
        else:
            est, why = _register_frame(frame_stars, ref_stars, pivot, corner_px, p)
            if explained is None and why is not None:
                explained = why
        if est is None:
            failures += 1
            log.warning("Photometry: registration failed for %s; frame excluded", Path(path).name)
            continue
        angles.append(est["angle_deg"])
        pos = _into_frame(all_ref, pivot, est)  # frame position of each first-frame star
        ny, nx = fr.data.shape
        valid = (pos[:, 0] > p.edge_margin_px / 2) & (pos[:, 0] < nx - p.edge_margin_px / 2) & \
                (pos[:, 1] > p.edge_margin_px / 2) & (pos[:, 1] < ny - p.edge_margin_px / 2)
        if p.refine_centroids:
            pos = _refine_centroids(fr.data, pos, p)
        flux, err = _aperture_photometry(fr.data, pos, fr.rms, p)
        flux, err = np.where(valid, flux, np.nan), np.where(valid, err, np.nan)
        rows.append({
            "t_mid": fr.t_mid, "flux": flux, "err": err, "dx": est["dx"], "dy": est["dy"],
            "sky": fr.sky, "rms": fr.rms, "n_match": est["n_matches"], "exptime": fr.exptime,
        })
    if failures:
        warnings.append(f"Registration failed for {failures} frame(s); they were excluded.")
    rot_deg = max([abs(a) for a in angles] + [abs(explained or 0.0)])
    edge_px = math.radians(rot_deg) * corner_px
    findings["rotation"] = {
        "max_abs_deg": rot_deg, "corner_displacement_px": edge_px, "explains_failures_deg": explained,
        "frames_measured": len(angles), "corrected": bool(p.correct_rotation),
    }
    if not p.correct_rotation and p.max_rotation_edge_px is not None and edge_px > p.max_rotation_edge_px:
        msg = (
            f"The field rotated by up to {rot_deg:.2f} deg over this sequence, which moves stars at the frame corner "
            f"by {edge_px:.1f} px (limit {p.max_rotation_edge_px:.1f} px).  Registration here is translation-only, so "
            "photometry from this sequence would be silently wrong and no light curve was produced.  Use shorter "
            "sequences, or turn rotation correction on (``correct_rotation``) before re-running."
        )
        warnings.append(msg)
        return finish(f"Field rotation of {rot_deg:.2f} deg exceeds what translation-only registration can handle; "
                      "no light curve was produced.")
    if edge_px > p.warn_rotation_edge_px:
        if p.correct_rotation:
            warnings.append(
                f"Field rotation of {rot_deg:.2f} deg ({edge_px:.1f} px at the frame corner) was measured and "
                "corrected by registering each frame with a rotation; this has not been checked on a real sequence."
            )
        else:
            warnings.append(
                f"Field rotation of {rot_deg:.2f} deg ({edge_px:.1f} px at the frame corner) was measured; comparison "
                "stars near the edge may be slightly mis-registered."
            )
    if len(rows) < p.min_frames:
        return finish(f"Only {len(rows)} frame(s) could be measured; too few for a light curve.")

    t0 = rows[0]["t_mid"]
    t = np.array([(r["t_mid"] - t0).total_seconds() for r in rows])
    F = np.vstack([r["flux"] for r in rows])  # (n_frames, 1 + n_comps)
    E = np.vstack([r["err"] for r in rows])

    # Drop comparison stars that are often unmeasurable (left the field), then frames missing anything.
    comp_ok = np.isfinite(F[:, 1:]).mean(axis=0) >= 1.0 - p.max_missing_fraction
    comp_ok &= (F[:, 1:] > 0).all(axis=0, where=np.isfinite(F[:, 1:]))
    if comp_ok.sum() < p.min_comps:
        warnings.append("Too few comparison stars were measurable throughout the sequence.")
        return finish("Too few comparison stars were measurable throughout the sequence.")
    if (~comp_ok).any():
        warnings.append(f"{int((~comp_ok).sum())} comparison star(s) dropped (left the frame or had non-positive flux).")
    cols = np.concatenate([[0], 1 + np.flatnonzero(comp_ok)])
    F, E = F[:, cols], E[:, cols]
    good = np.isfinite(F).all(axis=1) & np.isfinite(E).all(axis=1) & (F > 0).all(axis=1)
    if good.sum() < p.min_frames:
        return finish(f"Only {int(good.sum())} complete frame(s); too few for a light curve.")
    if (~good).any():
        warnings.append(f"{int((~good).sum())} frame(s) dropped (a star could not be measured).")
    t, F, E = t[good], F[good], E[good]
    rows = [r for r, g in zip(rows, good) if g]

    flux, err, keep, notes = differential_light_curve(F[:, 0], E[:, 0], F[:, 1:], E[:, 1:], p)
    warnings.extend(notes)
    findings["n_comps"] = int(keep.sum())

    cadence = float(np.median(np.diff(t)))
    findings.update(
        n_frames_used=int(len(t)), cadence_s=cadence, span_h=float(t[-1] - t[0]) / 3600.0,
        epoch_last_iso=(t0 + timedelta(seconds=float(t[-1]))).isoformat(),
        epoch_mid_iso=(t0 + timedelta(seconds=float(t[0] + t[-1]) / 2.0)).isoformat(),
    )
    t0_iso = t0.isoformat()

    fit = fit_transit(t, flux, err, p)
    detections: list[dict[str, Any]] = []
    fit_out: dict[str, Any] = {"ok": False, "reason": fit.get("reason")}
    detected = False
    if fit.get("ok"):
        tm_iso = (t0 + timedelta(seconds=fit["t_mid_s"])).isoformat()
        full = fit["n_pre"] >= p.min_baseline_points and fit["n_post"] >= p.min_baseline_points
        within = t[0] <= fit["t_mid_s"] <= t[-1]
        detected = bool(
            fit["snr"] >= p.min_snr and fit["delta_bic"] >= p.min_delta_bic
            and fit["n_in"] >= p.min_in_transit_points and within and fit["depth"] > 0
        )
        fit_out = {
            "ok": True,
            "significant": detected,
            "t_mid_s": fit["t_mid_s"], "t_mid_iso": tm_iso, "t_mid_err_min": fit["t_mid_err_s"] / 60.0,
            "duration_h": fit["duration_s"] / 3600.0, "duration_err_h": fit["duration_err_s"] / 3600.0,
            "ingress_h": fit["ingress_s"] / 3600.0,
            "depth_pct": 100.0 * fit["depth"], "depth_err_pct": 100.0 * fit["depth_err"],
            "snr": fit["snr"], "delta_bic": fit["delta_bic"], "chi2_red": fit["chi2_red"], "red_noise_beta": fit["beta"],
            "n_in_transit": fit["n_in"], "n_pre_transit": fit["n_pre"], "n_post_transit": fit["n_post"],
            "coverage": "full" if full else "partial",
            "baseline_order": p.baseline_order, "baseline_coeffs": fit["coeffs"],
        }
        if detected:
            quality = "good" if (full and fit["snr"] >= 8.0) else "marginal"
            if not full:
                warnings.append(
                    "The transit is not bracketed by out-of-transit data on both sides; the baseline fit can "
                    "absorb part of the dip, so depth and mid-time are less reliable."
                )
            detections.append({
                "id": "TR01",
                "kind": "transit",
                "epoch_mid_iso": tm_iso,
                "t_mid_err_min": fit_out["t_mid_err_min"],
                "depth_pct": fit_out["depth_pct"], "depth_err_pct": fit_out["depth_err_pct"],
                "duration_h": fit_out["duration_h"], "duration_err_h": fit_out["duration_err_h"],
                "snr_median": fit["snr"], "delta_bic": fit["delta_bic"],
                "n_points": fit["n_in"], "n_frames": int(len(t)),
                "coverage": fit_out["coverage"], "quality": quality,
            })

    findings["fit"] = fit_out
    findings["detections"] = detections
    findings["rms_ppt"] = 1e3 * float(fit["rms"]) if fit.get("ok") else 1e3 * float(np.std(flux, ddof=1))
    findings["light_curve"] = {
        "t0_iso": t0_iso,
        "t_s": [round(float(v), 3) for v in t],
        "flux": [round(float(v), 6) for v in flux],
        "err": [round(float(v), 6) for v in err],
        "model": [round(float(v), 6) for v in fit["model"]] if fit.get("ok") else None,
        "baseline": [round(float(v), 6) for v in fit["baseline"]] if fit.get("ok") else None,
        "dx_px": [round(float(r["dx"]), 3) for r in rows],
        "dy_px": [round(float(r["dy"]), 3) for r in rows],
    }
    findings["photometry"] = {
        "target_xy": [float(v) for v in target_ref],
        "comparison_xy": [[float(a), float(b)] for a, b in comps_ref[comp_ok][keep]],
        "target_flux": [round(float(v), 4) for v in F[:, 0]],
        "comparison_flux": [[round(float(v), 4) for v in row] for row in F[:, 1:][:, keep]],
        "n_comparison_stars_used": int(keep.sum()),
        "aperture_radius_px": p.aperture_radius_px,
        "annulus_px": list(p.annulus_px),
        "drift_px": [float(rows[-1]["dx"] - rows[0]["dx"]), float(rows[-1]["dy"] - rows[0]["dy"])],
    }
    if wcs is not None:
        findings["astrometry"] = {
            "wcs": wcs_to_dict(wcs),
            "reference_stars": _brightest(ref, 150).tolist(),
            "frame_shape": list(ref.data.shape),
            "target_xy": [float(v) for v in target_ref],
            "calibrated": False,
        }

    if figure_dir is not None:
        try:
            findings["figures"].append(
                _make_figure(t, flux, fit if fit.get("ok") else None, findings["object"] or "Target", t0_iso,
                             Path(figure_dir) / "light_curve.png", p, detected, dip_label)
            )
        except Exception:  # noqa: BLE001 - a plotting bug must not discard the science
            log.exception("Photometry: figure generation failed")
            warnings.append("Figure generation failed; see logs.")

    if detected:
        d = detections[0]
        summary = (
            f"Transit candidate: depth {d['depth_pct']:.2f} +/- {d['depth_err_pct']:.2f} %, duration "
            f"{d['duration_h']:.2f} h, mid-time {d['epoch_mid_iso'][:16].replace('T', ' ')} UTC "
            f"(SNR~{d['snr_median']:.0f}, dBIC {d['delta_bic']:.0f}, {d['coverage']} coverage) in "
            f"{len(t)} frames with {findings['n_comps']} comparison stars."
        )
    elif fit.get("ok"):
        summary = (
            f"No significant transit in {len(t)} frames (best dip {fit_out['depth_pct']:.2f} % at SNR "
            f"{fit_out['snr']:.1f}); light-curve rms {findings['rms_ppt']:.1f} ppt, "
            f"{findings['n_comps']} comparison stars."
        )
    else:
        summary = f"No dip-shaped feature in {len(t)} frames; light-curve rms {findings['rms_ppt']:.1f} ppt."
    return finish(summary)
