"""One night of differential photometry for a variable star.

The frame-to-frame work (registration, apertures, the comparison ensemble, field-rotation refusal)
is :func:`plugins.transit_finder.photometry.analyze`.  This module asks it for the green channel
when the frame is a colour mosaic, then turns the aperture sums into magnitudes.

A magnitude is ``zero point - 2.5 log10(target flux)``.  The zero point of a frame is the median of
``catalogue magnitude + 2.5 log10(comparison flux)`` over the comparison stars that were matched to a
catalogue.  Anything common to the field (cloud, airmass) cancels.  What remains, and is added in
quadrature to the quoted error, is a systematic floor: no flat frames, and a green channel that is
not Johnson V.

The series is one visit.  The number reported to the AAVSO is a single magnitude for the night: the
median outside a detected minimum, or the median of the whole series when there is no minimum.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from plugins.transit_finder.photometry import PhotometryParams, analyze as _analyze_curve
from tools.imaging import load_measurement

#: Added in quadrature to every quoted magnitude error.  Colour term and the lack of flat frames.
SYSTEMATIC_MAG = 0.03
MIN_COMPS = 2


def analyze(fits_paths: list[str], params: PhotometryParams | None = None, figure_dir: str | Path | None = None) -> dict[str, Any]:
    """Differential light curve, plus which channel the frames were measured through."""
    if not fits_paths:
        raise ValueError("analyze() needs at least one FITS path")
    findings = _analyze_curve(fits_paths, params, figure_dir, loader=load_measurement, dip_label="fitted minimum")
    try:
        _, header = load_measurement(fits_paths[0])
        findings["channel"] = str(header.get("PHOTBAND") or "")
        findings["channel_note"] = str(header.get("PHOTNOTE") or "")
    except (OSError, ValueError):
        findings["channel"] = ""
        findings["channel_note"] = ""
    det = (findings.get("detections") or [None])[0]
    if det:
        findings["summary"] = (
            f"A minimum: depth {det['depth_pct']:.1f} %, duration {det['duration_h']:.2f} h, "
            f"mid-time {det['epoch_mid_iso'][:16].replace('T', ' ')} UTC, "
            f"{findings.get('n_comps', 0)} comparison stars."
        )
    elif findings.get("light_curve"):
        findings["summary"] = (
            f"No minimum in {findings.get('n_frames_used') or 0} frames "
            f"(rms {findings.get('rms_ppt') or float('nan'):.1f} ppt). The night's brightness can still be measured."
        )
    findings["plugin"] = "variable_stars"
    return findings


def magnitudes_from_comps(
    target_flux: Sequence[float], comparison_flux: Sequence[Sequence[float]], comparison_mags: Sequence[float],
) -> np.ndarray | None:
    """Per-frame target magnitudes from comparison-star aperture sums and their catalogue magnitudes.

    ``comparison_flux`` is ``(n_frames, n_comps)``, aligned with ``comparison_mags``.  Returns ``None``
    when fewer than :data:`MIN_COMPS` comparisons have a positive flux and a finite magnitude.
    """
    target = np.asarray(target_flux, dtype=float)
    comps = np.asarray(comparison_flux, dtype=float)
    mags = np.asarray(comparison_mags, dtype=float)
    if target.ndim != 1 or comps.ndim != 2 or comps.shape[0] != target.shape[0] or comps.shape[1] != mags.shape[0]:
        return None
    usable = np.isfinite(mags) & (comps > 0).all(axis=0)
    if int(usable.sum()) < MIN_COMPS or not (target > 0).all():
        return None
    # One zero point per frame: the comparisons agree on how bright "1 count" is.
    zp = np.median(mags[usable] + 2.5 * np.log10(comps[:, usable]), axis=1)
    return zp - 2.5 * np.log10(target)


MIN_COLOUR_COMPS = 4  # a straight line through fewer than this says nothing about the colour
MIN_COLOUR_SPAN = 0.4  # the comparison stars must differ in Gaia BP-RP by at least this much
MAX_COLOUR_SLOPE = 1.5  # mag of zero point per mag of BP-RP; a larger slope is a blend or a saturated star, not colour


def fit_colour_term(
    comparison_flux: Sequence[Sequence[float]], comparison_mags: Sequence[float], comparison_colours: Sequence[float | None],
) -> dict[str, float] | None:
    """How the zero point depends on the comparison stars' colour (Gaia BP-RP), or None when it cannot be told.

    The camera's green channel is not Gaia G: a red star reads fainter against its Gaia G than a blue one does.
    Each comparison star gives ``zp = G + 2.5 log10(flux)`` (on its median flux over the night); a line through
    ``zp`` against ``BP-RP`` is the colour term.  Two passes of 2.5-sigma clipping keep one blended or saturated
    star from steering the line.  On 2026-11-16 the slope was -0.33 and -0.67 mag per mag on the two nights and
    the scatter about the line was 0.04 and 0.11 mag, against 0.14 and 0.64 without it.
    Returns ``{"slope", "intercept", "scatter", "scatter_before", "n", "n_dropped", "span"}``.
    """
    flux = np.asarray(comparison_flux, dtype=float)
    mags = np.asarray(comparison_mags, dtype=float)
    colours = np.array([float("nan") if c is None else float(c) for c in comparison_colours], dtype=float)
    if flux.ndim != 2 or flux.shape[1] != mags.shape[0] or mags.shape != colours.shape or flux.shape[0] == 0:
        return None
    usable = np.isfinite(mags) & np.isfinite(colours) & (flux > 0).all(axis=0)
    if int(usable.sum()) < MIN_COLOUR_COMPS:
        return None
    zp = mags[usable] + 2.5 * np.log10(np.median(flux[:, usable], axis=0))
    c = colours[usable]
    keep = np.ones(len(c), dtype=bool)
    for _ in range(2):
        if int(keep.sum()) < MIN_COLOUR_COMPS or np.ptp(c[keep]) < MIN_COLOUR_SPAN:
            return None
        slope, icpt = np.polyfit(c[keep], zp[keep], 1)
        resid = zp - (icpt + slope * c)
        sigma = max(1.4826 * float(np.median(np.abs(resid[keep] - np.median(resid[keep])))), 0.02)
        new_keep = np.abs(resid) <= 2.5 * sigma
        if int(new_keep.sum()) < MIN_COLOUR_COMPS or (new_keep == keep).all():
            break
        keep = new_keep
    slope, icpt = np.polyfit(c[keep], zp[keep], 1)  # the line for the stars finally kept
    if abs(slope) > MAX_COLOUR_SLOPE:
        return None
    resid = zp[keep] - (icpt + slope * c[keep])
    return {
        "slope": float(slope), "intercept": float(icpt),
        "scatter": float(np.std(resid, ddof=2)) if keep.sum() > 2 else 0.0,
        "scatter_before": float(np.std(zp, ddof=1)), "n": int(keep.sum()), "n_dropped": int((~keep).sum()),
        "span": float(np.ptp(c[keep])),
    }


def magnitudes_with_colour_term(
    target_flux: Sequence[float], comparison_flux: Sequence[Sequence[float]], comparison_mags: Sequence[float],
    comparison_colours: Sequence[float | None], target_colour: float, term: dict[str, float],
) -> np.ndarray | None:
    """Per-frame target magnitudes with the colour term applied: each frame's zero point is moved to the target's colour.

    Uses the comparison stars that fit the line (the ones with a colour and a positive flux); ``None`` when fewer than
    :data:`MIN_COLOUR_COMPS` remain.
    """
    target = np.asarray(target_flux, dtype=float)
    comps = np.asarray(comparison_flux, dtype=float)
    mags = np.asarray(comparison_mags, dtype=float)
    colours = np.array([float("nan") if c is None else float(c) for c in comparison_colours], dtype=float)
    if target.ndim != 1 or comps.ndim != 2 or comps.shape[0] != target.shape[0] or comps.shape[1] != mags.shape[0]:
        return None
    # Only the stars the fit kept: within 2.5 sigma of the line on the night's median flux.
    usable = np.isfinite(mags) & np.isfinite(colours) & (comps > 0).all(axis=0)
    if int(usable.sum()) < MIN_COLOUR_COMPS or not (target > 0).all():
        return None
    zp_night = mags + 2.5 * np.log10(np.where(usable, np.median(comps, axis=0), 1.0))
    resid = zp_night - (term["intercept"] + term["slope"] * np.where(usable, colours, 0.0))
    sigma = max(float(term["scatter"]), 0.02)
    use = usable & (np.abs(resid) <= 2.5 * sigma + 1e-9)
    if int(use.sum()) < MIN_COLOUR_COMPS:
        use = usable
    zp = np.median(mags[use] + 2.5 * np.log10(comps[:, use]) - term["slope"] * colours[use], axis=1)
    return zp + term["slope"] * float(target_colour) - 2.5 * np.log10(target)


def series_trend(mags: Sequence[float]) -> float | None:
    """Total change in magnitude over the sequence, from a straight-line fit (negative: the target brightened).

    A variable star can really change in a few minutes, but a steady slope across a whole visit is more often the
    measurement: on 2026-11-16 the target gained 0.15 mag on its comparison stars while the sky faded by 40 percent,
    and the brighter stars faded less than the fainter ones, which points at the sky and background subtraction.
    """
    values = np.array([float(m) for m in mags if m is not None and math.isfinite(float(m))])
    if len(values) < 8:
        return None
    slope, _ = np.polyfit(np.arange(len(values)), values, 1)
    return float(slope * (len(values) - 1))


def zp_scatter(comparison_flux: Sequence[Sequence[float]], comparison_mags: Sequence[float]) -> tuple[float, int]:
    """How far the comparison stars disagree on the zero point, in magnitudes, and how many were used.

    Computed on the middle frame.  A large scatter means the comparisons are a poor match (colour, blend).
    """
    comps = np.asarray(comparison_flux, dtype=float)
    mags = np.asarray(comparison_mags, dtype=float)
    if comps.ndim != 2 or comps.shape[1] != mags.shape[0] or comps.shape[0] == 0:
        return float("nan"), 0
    mid = comps[len(comps) // 2]
    usable = np.isfinite(mags) & (mid > 0)
    if int(usable.sum()) < MIN_COMPS:
        return float("nan"), int(usable.sum())
    points = mags[usable] + 2.5 * np.log10(mid[usable])
    scatter = float(np.std(points, ddof=1)) if usable.sum() > 2 else 0.0
    return scatter, int(usable.sum())


def night_point(
    mags: Sequence[float], t_s: Sequence[float] | None = None, *, t_mid_s: float | None = None, duration_s: float | None = None,
) -> dict[str, float]:
    """The one magnitude this visit reports, and its error.

    When a minimum was fitted, frames inside it are left out so the eclipse does not pull the
    night's brightness faint.  The error is the standard error of those frames, added in quadrature
    to :data:`SYSTEMATIC_MAG`.
    """
    values = [float(m) for m in mags if m is not None and math.isfinite(float(m))]
    times = list(t_s) if t_s is not None else []
    used = values
    if t_mid_s is not None and duration_s and len(times) == len(values):
        half = float(duration_s) / 2.0
        outside = [m for m, t in zip(values, times) if abs(float(t) - float(t_mid_s)) > half]
        if len(outside) >= 3:
            used = outside
    n = len(used)
    med = float(statistics.median(used))
    if n >= 3:
        stat = float(statistics.pstdev(used)) / math.sqrt(n)
    else:
        stat = SYSTEMATIC_MAG
    return {"mag": med, "mag_err": float(math.hypot(stat, SYSTEMATIC_MAG)), "n": float(n), "systematic_mag": SYSTEMATIC_MAG}
