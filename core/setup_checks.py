"""First-frame setup checks: is the telescope pointed, focused and sane before we trust the night?

Run once per mission, on the first frame of the first target, when the telescope is real
(or when the checks are being rehearsed in simulation).  Domain-agnostic: it looks at stars,
not at science.

* **Focus**: the median FWHM of the brighter, unsaturated stars.  Too soft refuses the
  mission, because a defocused night is hours of unusable photometry.
* **Pointing**: where the camera is actually looking, from a local plate solve when
  ``solve-field`` is installed and otherwise from a Gaia match of the star pattern.  The
  target is accepted when it lands on the chip.  A measured miss off the chip asks for one
  corrected slew (``Repoint``).  A field that will not solve, or a catalogue that cannot be
  reached, refuses the night.  Too few stars to try stays a warning.
* **Calibration**: no darks or flats are applied by Astronomo.  The check records the numbers
  that decide whether that is acceptable (background level, gradient across the frame,
  saturation) so the decision is made from data at first light and written into the log.

Every threshold is a *guess made before the telescope arrived*, set in ``config.py`` and
meant to be re-set from the first real frames.  Too few stars is a WARN -- "not verified" --
never a pass.  An unmatched field or an unreachable catalogue is a FAIL.

The same moment writes a stretched view of the frame (``first_frame.png``) so the operator
can see whether the field is sky.  The view is the saved pixels.  It decides nothing: a
FAIL still refuses the mission, and too few stars stays a warning.
"""

from __future__ import annotations

import json
import logging
import math
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import fit_wcs_from_points
from scipy.optimize import least_squares

from config import Settings
from tools.astrometry import (
    SEESTAR_S50_PIXEL_SCALE_ARCSEC,
    angular_separation_arcsec,
    estimate_translation,
    make_tan_wcs,
    match_rigid,
    nominal_pixel_scale_arcsec,
)
from tools.catalogs import CatalogClient, CatalogError
from tools.imaging import average_2x2, detect_stars, load_image, subtract_background
from tools.plate_solve import solve_frame

log = logging.getLogger(__name__)

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
SATURATION_ADU = 60000.0
PREVIEW_NAME = "first_frame.png"
LATEST_NAME = "latest_frame.png"
_DISPLAY_LO_PCT = 1.0
_DISPLAY_HI_PCT = 99.5
_DISPLAY_MAX_SIDE = 1200  # long edge of the operator's view; the FITS file is left whole
_HALF_BOX = 7  # cutout half-size (px) for the PSF fit
_MAX_STARS = 60
_MIN_STARS = 8
_GRADIENT_WARN = 0.15  # fractional background spread across the frame worth a flat-field discussion


class SetupCheckError(RuntimeError):
    """A first-frame check failed; the mission is refused with this message."""


class Repoint(Exception):
    """The target is off the chip and the measured miss says where to slew next.

    Not a setup failure by itself. The capture loop slews to ``ra_deg``, ``dec_deg`` and
    takes another frame, up to the configured number of corrections.

    ``optional`` is a re-centre: the target is on the chip, just far from its centre.  When the
    corrections are used up the sequence carries on where it is instead of being refused.
    """

    def __init__(self, ra_deg: float, dec_deg: float, detail: str = "", optional: bool = False) -> None:
        self.ra_deg = float(ra_deg)
        self.dec_deg = float(dec_deg)
        self.optional = bool(optional)
        super().__init__(detail or f"slew to RA {self.ra_deg:.4f} deg, Dec {self.dec_deg:.4f} deg")


# --------------------------------------------------------------------------- #
# Measurements (pure functions of the pixels)
# --------------------------------------------------------------------------- #
def _fit_sigma(cut: np.ndarray) -> float | None:
    """Circular-Gaussian sigma (px) of a star cutout, or None if the fit is not credible."""
    n = cut.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    peak = float(cut.max())
    if peak <= 0:
        return None

    def model(p):
        amp, x0, y0, sig, bg = p
        return amp * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sig**2)) + bg

    start = [peak, n / 2.0 - 0.5, n / 2.0 - 0.5, 1.5, float(np.median(cut))]
    try:
        fit = least_squares(lambda p: (model(p) - cut).ravel(), start,
                            bounds=([0, 0, 0, 0.4, -np.inf], [np.inf, n, n, n / 2.0, np.inf]))
    except Exception:  # noqa: BLE001 - a bad star is skipped, not fatal
        return None
    return float(fit.x[3]) if fit.success else None


_KERNELS_PX = (3.0, 6.0, 10.0, 14.0)  # detection kernels: a matched filter that is too narrow misses soft stars


def detect_any_width(sub: np.ndarray, rms: float, sigma: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Detect stars with the kernel width that finds the most, so soft (defocused) stars are still seen."""
    best: tuple[np.ndarray, np.ndarray, float] | None = None
    for kernel in _KERNELS_PX:
        with warnings.catch_warnings():  # "no sources found" is an answer here, not a problem
            warnings.simplefilter("ignore")
            xy, flux = detect_stars(sub, rms, fwhm_px=kernel, sigma=sigma)
        if best is None or len(xy) > len(best[0]):
            best = (xy, flux, kernel)
    assert best is not None
    return best


def _bin2(data: np.ndarray) -> np.ndarray | None:
    """Average each 2x2 block. A colour mosaic's stars become round enough to measure."""
    ny, nx = data.shape[:2]
    if ny < 16 or nx < 16 or ny % 2 or nx % 2:
        return None
    return data[:ny, :nx].reshape(ny // 2, 2, nx // 2, 2).mean(axis=(1, 3))


def measure_focus(data: np.ndarray, pixel_scale: float = SEESTAR_S50_PIXEL_SCALE_ARCSEC) -> dict[str, Any] | None:
    """Median stellar FWHM of the frame, or None if too few usable stars were found.

    ``fwhm_px`` is in the pixels of ``data``. :func:`_focus_or_binned` averages a colour
    mosaic 2x2 first and converts the width back to the original pixels.
    """
    sub, rms, _level = subtract_background(data.astype(np.float32))
    xy, flux, kernel = detect_any_width(sub, rms, 8.0)
    if len(xy) < _MIN_STARS:
        return None
    ny, nx = sub.shape
    half = max(_HALF_BOX, int(round(1.5 * kernel)))  # the cutout must hold the whole profile, wings included
    order = np.argsort(flux)[::-1]
    sigmas: list[float] = []
    for i in order[: _MAX_STARS * 3]:
        x, y = int(round(xy[i, 0])), int(round(xy[i, 1]))
        if not (half <= x < nx - half and half <= y < ny - half):
            continue
        cut = sub[y - half : y + half + 1, x - half : x + half + 1].astype(float)
        raw_peak = float(data[y - 1 : y + 2, x - 1 : x + 2].max())
        if raw_peak >= SATURATION_ADU:
            continue  # a saturated core reads falsely sharp
        sigma = _fit_sigma(cut)
        if sigma is not None:
            sigmas.append(sigma)
        if len(sigmas) >= _MAX_STARS:
            break
    if len(sigmas) < _MIN_STARS:
        return None
    fwhm_px = 2.3548 * float(np.median(sigmas))
    return {"fwhm_px": fwhm_px, "fwhm_arcsec": fwhm_px * pixel_scale, "n_stars": len(sigmas)}


def _focus_or_binned(data: np.ndarray, pixel_scale: float) -> dict[str, Any] | None:
    found = measure_focus(data, pixel_scale)
    if found is not None:
        return found
    binned = _bin2(data)
    if binned is None:
        return None
    found = measure_focus(binned.astype(np.float32), pixel_scale * 2.0)
    if found is None:
        return None
    found["fwhm_px"] *= 2.0  # the arcsec width was already computed at the binned scale
    return found


def measure_star_width(data: np.ndarray) -> dict[str, Any] | None:
    """The first-frame focus measure (median star FWHM, colour mosaic handled), for a focus sweep.

    Returns ``{"fwhm_px", "fwhm_arcsec", "n_stars"}`` or None when too few stars can be measured.
    """
    return _focus_or_binned(data, nominal_pixel_scale_arcsec(data.shape))


def measure_frame_stats(data: np.ndarray) -> dict[str, float]:
    """Numbers that inform the dark/flat decision."""
    sub, rms, level = subtract_background(data.astype(np.float32))
    ny, nx = data.shape
    by, bx = ny // 8, nx // 8
    grid = np.array([[np.median(data[j * by : (j + 1) * by, i * bx : (i + 1) * bx]) for i in range(8)] for j in range(8)])
    spread = float((grid.max() - grid.min()) / max(float(np.median(grid)), 1.0))
    return {
        "background_adu": float(level),
        "noise_adu": float(rms),
        "gradient_fraction": spread,
        "saturated_fraction": float(np.mean(data >= SATURATION_ADU)),
    }


def _stars(data: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sub, rms, _ = subtract_background(data.astype(np.float32))
    xy, flux, _kernel = detect_any_width(sub, rms, 6.0)
    return xy, flux


_TARGET_SEARCH_PX = 45.0  # the photometry looks for the target this close to where the solve puts it


def target_star_seen(data: np.ndarray, x: float, y: float, radius_px: float = _TARGET_SEARCH_PX) -> dict[str, float | None]:
    """Distance (px) from ``(x, y)`` to the nearest detected star, as the photometry would find it.

    ``raw`` is the frame as saved, ``averaged`` the same frame after a 2x2 mean (what the photometry uses
    for sharp stars on a colour mosaic).  None means no star within ``radius_px``.
    """
    out: dict[str, float | None] = {}
    for key, image in (("raw", data), ("averaged", average_2x2(data))):
        sub, rms, _level = subtract_background(np.asarray(image, dtype=np.float32))
        xy, _flux, _kernel = detect_any_width(sub, rms, 5.0)
        if len(xy) == 0:
            out[key] = None
            continue
        nearest = float(np.min(np.hypot(xy[:, 0] - x, xy[:, 1] - y)))
        out[key] = nearest if nearest <= radius_px else None
    return out


def _wrap_deg(delta: float) -> float:
    return ((float(delta) + 180.0) % 360.0) - 180.0


def nudged_command(
    command_ra: float, command_dec: float, target_ra: float, target_dec: float, solved_ra: float, solved_dec: float,
) -> tuple[float, float]:
    """Next slew: add the miss (where the target is, minus where the camera was) to the last command."""
    ra = (float(command_ra) + _wrap_deg(target_ra - solved_ra)) % 360.0
    dec = min(90.0, max(-90.0, float(command_dec) + (float(target_dec) - float(solved_dec))))
    return ra, dec


def _on_chip(x: float, y: float, shape: tuple[int, int], margin_px: float) -> bool:
    ny, nx = shape
    margin = min(float(margin_px), max(0.0, min(nx, ny) / 2.0 - 1.0))
    return margin <= x < nx - margin and margin <= y < ny - margin


def wcs_from_match(
    commanded: WCS, shape: tuple[int, int], dx: float, dy: float, angle_deg: float, mirrored: bool,
) -> WCS:
    """WCS of the image, from the rigid match that carries the north-up chart onto the frame.

    The match model is ``image = R @ (catalog_pixel - center) + center + shift``, with an
    optional mirror about the center applied to the catalog pixel first.
    """
    ny, nx = int(shape[0]), int(shape[1])
    center = np.array([(nx - 1) / 2.0, (ny - 1) / 2.0])
    xs = np.linspace(nx * 0.25, nx * 0.75, 4)
    ys = np.linspace(ny * 0.25, ny * 0.75, 4)
    cat = np.array([(x, y) for y in ys for x in xs], dtype=float)
    sky_ra, sky_dec = commanded.all_pix2world(cat[:, 0], cat[:, 1], 0)
    pts = cat.copy()
    if mirrored:
        pts[:, 0] = 2.0 * center[0] - pts[:, 0]
    theta = math.radians(angle_deg)
    rotation = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    image = (pts - center) @ rotation.T + center + np.array([dx, dy])
    world = SkyCoord(np.asarray(sky_ra, dtype=float) * u.deg, np.asarray(sky_dec, dtype=float) * u.deg)
    return fit_wcs_from_points((image + 1.0).T, world, proj_point="center")


def _describe_solution(
    solved: WCS, shape: tuple[int, int], command_ra: float, command_dec: float,
    target_ra: float, target_dec: float, margin_px: float, *,
    n_matches: int | None = None, rms_px: float | None = None, angle_deg: float = 0.0, mirrored: bool = False,
) -> dict[str, Any]:
    """Offset, target pixel and the next slew, from a WCS that describes this frame."""
    ny, nx = shape
    cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0
    solved_ra, solved_dec = (float(v) for v in solved.all_pix2world([cx], [cy], 0))
    solved_ra = solved_ra % 360.0
    tx, ty = (float(v) for v in np.ravel(solved.all_world2pix([target_ra], [target_dec], 0)))
    next_ra, next_dec = nudged_command(command_ra, command_dec, target_ra, target_dec, solved_ra, solved_dec)
    return {
        "status": "matched",
        "offset_arcsec": angular_separation_arcsec(solved_ra, solved_dec, command_ra, command_dec),
        "target_offset_arcsec": angular_separation_arcsec(solved_ra, solved_dec, target_ra, target_dec),
        "solved_ra_deg": solved_ra, "solved_dec_deg": solved_dec,
        "target_x": tx, "target_y": ty, "on_chip": _on_chip(tx, ty, shape, margin_px),
        "next_ra_deg": next_ra, "next_dec_deg": next_dec,
        "n_matches": n_matches, "rms_px": rms_px, "angle_deg": angle_deg, "mirrored": mirrored,
        "wcs": solved,
    }


def measure_pointing(
    data: np.ndarray, header, target: dict, catalogs: CatalogClient, *, scene=None, max_shift_px: float = 400.0,
    margin_px: float = 64.0,
) -> dict[str, Any]:
    """Where this frame is, matched against Gaia, and whether ``target`` lands on the chip.

    The chart is north-up at the commanded header coordinates. A Seestar frame may be rotated
    by any angle and its pixels may be mirrored, and a colour mosaic is averaged 2x2 so the
    stars are round enough to find. The catalogue is rebuilt from the header coordinates, which
    are the slew that produced this frame, while ``target`` stays the star the night asked for.

    ``status`` is ``matched``, ``too_few_stars``, ``catalog`` or ``unmatched``.
    """
    try:
        ra, dec = float(header["RA"]), float(header["DEC"])
        target_ra, target_dec = float(target["ra_deg"]), float(target["dec_deg"])
    except (KeyError, TypeError, ValueError):
        return {"status": "unmatched", "detail": "the frame has no commanded coordinates"}
    native_scale = float(header["PIXSCALE"]) if "PIXSCALE" in header else nominal_pixel_scale_arcsec(data.shape)
    xy, flux = _stars(data)
    scale, shift_px = native_scale, max_shift_px
    if len(xy) < _MIN_STARS:
        binned = _bin2(data)
        if binned is None:
            return {"status": "too_few_stars", "detail": "too few stars to match a star pattern"}
        xy, flux = _stars(binned.astype(np.float32))
        if len(xy) < _MIN_STARS:
            return {"status": "too_few_stars", "detail": "too few stars to match a star pattern"}
        xy = xy * 2.0  # detections were made on the averaged image; the WCS stays on the original pixels
    xy = xy[np.argsort(flux)[::-1][:80]]
    ny, nx = data.shape[:2]
    commanded = make_tan_wcs(ra, dec, (int(ny), int(nx)), scale)
    radius = 0.5 * float(np.hypot(nx, ny)) * scale + shift_px * scale
    catalog_target = {**target, "ra_deg": ra, "dec_deg": dec}
    try:
        rows = catalogs.gaia_cone(ra, dec, radius, 15.5, target=catalog_target, scene=scene)
    except CatalogError as exc:
        log.warning("Pointing check: Gaia unavailable (%s)", exc)
        return {"status": "catalog", "detail": f"Gaia could not be reached ({exc})"}
    if len(rows) < _MIN_STARS:
        return {"status": "unmatched", "detail": "the catalogue returned too few stars to match"}
    px = np.array(commanded.all_world2pix([r["ra_deg"] for r in rows], [r["dec_deg"] for r in rows], 0)).T
    inside = (px[:, 0] > -50) & (px[:, 0] < nx + 50) & (px[:, 1] > -50) & (px[:, 1] < ny + 50)
    catalog = px[inside][:150]
    est = estimate_translation(catalog, xy, max_shift=shift_px, tol=2.5, min_matches=_MIN_STARS)
    rigid = None
    if est is None or est["n_matches"] < min(20, len(xy) // 2):
        rigid = match_rigid(catalog, xy, ((nx - 1) / 2.0, (ny - 1) / 2.0), tol=3.0, min_matches=_MIN_STARS, max_shift=shift_px)
    if rigid is not None and (est is None or rigid["n_matches"] > est["n_matches"]):
        est = rigid
    if est is None:
        return {"status": "unmatched", "detail": "the stars in the frame do not match the catalogue around the commanded position"}
    try:
        solved = wcs_from_match(
            commanded, (int(ny), int(nx)), float(est["dx"]), float(est["dy"]),
            float(est.get("angle_deg") or 0.0), bool(est.get("mirrored", False)),
        )
    except Exception as exc:  # noqa: BLE001 - a fit that will not converge is still a measured shift
        log.warning("Pointing check: could not fit a WCS to the match (%s); using the shift of the centre", exc)
        cx, cy = (nx - 1) / 2.0 - float(est["dx"]), (ny - 1) / 2.0 - float(est["dy"])
        solved_ra, solved_dec = (float(v) for v in commanded.all_pix2world([cx], [cy], 0))
        solved = make_tan_wcs(solved_ra % 360.0, solved_dec, (int(ny), int(nx)), scale)
    described = _describe_solution(
        solved, (int(ny), int(nx)), ra, dec, target_ra, target_dec, margin_px,
        n_matches=int(est["n_matches"]), rms_px=float(est["rms_px"]),
        angle_deg=float(est.get("angle_deg") or 0.0), mirrored=bool(est.get("mirrored", False)),
    )
    described["dx_px"] = float(est["dx"])
    described["dy_px"] = float(est["dy"])
    return described


def stamp_solution(path: str | Path, wcs: WCS, target_ra: float, target_dec: float) -> None:
    """Write the solved WCS and the science target's coordinates into a FITS header.

    Photometry uses these to find the star wherever it landed, instead of assuming the chip centre.
    The commanded ``RA`` and ``DEC`` are left as the slew that was sent.
    """
    fresh = wcs.to_header()
    drop = {key for key in fresh.keys() if key} | {
        "CTYPE1", "CTYPE2", "CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2",
        "CD1_1", "CD1_2", "CD2_1", "CD2_2", "CUNIT1", "CUNIT2",
        "PC1_1", "PC1_2", "PC2_1", "PC2_2", "CDELT1", "CDELT2", "CROTA2", "LONPOLE", "LATPOLE",
    }
    with fits.open(path, mode="update") as hdul:
        header = hdul[0].header
        for key in list(header.keys()):
            if key in drop:
                del header[key]
        header.update(fresh)
        header["TARGRA"] = (float(target_ra), "[deg] science target RA")
        header["TARGDEC"] = (float(target_dec), "[deg] science target Dec")


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def run_setup_checks(
    settings: Settings,
    frame_path: str,
    target: dict,
    catalogs: CatalogClient,
    *,
    scene=None,
    solved: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run every check on one frame.  Never raises; the caller decides what a FAIL means.

    ``solved`` is the scope's own plate solution (``{"ra_deg", "dec_deg"}``) if it gave one.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, **values: Any) -> None:
        checks.append({"name": name, "status": status, "detail": detail, **{k: v for k, v in values.items() if v is not None}})

    data, header = load_image(frame_path)
    scale = float(header["PIXSCALE"]) if "PIXSCALE" in header else nominal_pixel_scale_arcsec(data.shape)

    # Focus ---------------------------------------------------------------
    focus = _focus_or_binned(data, scale)
    if focus is None:
        add("focus", WARN, "Too few usable stars to measure focus: not verified.  Check the sky is clear and the scope is "
            "pointed at a star field.")
    else:
        fw = focus["fwhm_px"]
        text = f"median FWHM {fw:.1f} px ({focus['fwhm_arcsec']:.1f} arcsec) from {focus['n_stars']} stars"
        if fw >= settings.focus_fail_fwhm_px:
            add("focus", FAIL, f"{text}: at or above the {settings.focus_fail_fwhm_px:.0f} px limit.  The scope is out of "
                "focus (or the sky is very poor); refocus and run again.", **focus)
        elif fw >= settings.focus_warn_fwhm_px:
            add("focus", WARN, f"{text}: softer than the {settings.focus_warn_fwhm_px:.0f} px warning level.", **focus)
        else:
            add("focus", PASS, text, **focus)

    # Pointing ------------------------------------------------------------
    # Search the whole chip, so a star that is on the frame but far from the centre is measured
    # rather than reported as "no match".
    ny, nx = data.shape[:2]
    search_px = 0.5 * float(np.hypot(nx, ny))
    plate = None
    try:
        plate = solve_frame(
            frame_path, float(header["RA"]), float(header["DEC"]), scale,
            radius_deg=settings.pointing_solve_radius_deg, timeout_s=settings.pointing_solve_timeout_s,
        )
    except (KeyError, TypeError, ValueError):
        plate = None
    if plate is not None:
        point = _describe_solution(
            plate, (int(ny), int(nx)), float(header["RA"]), float(header["DEC"]),
            float(target["ra_deg"]), float(target["dec_deg"]), settings.pointing_edge_margin_px,
        )
    else:
        point = measure_pointing(
            data, header, target, catalogs, scene=scene, max_shift_px=search_px,
            margin_px=settings.pointing_edge_margin_px,
        )
    status_kind = point.get("status")
    if status_kind == "too_few_stars":
        add("pointing (star pattern)", WARN, "Too few stars to check the pointing: not verified.  "
            + point.get("detail", ""))
    elif status_kind != "matched":
        add("pointing (star pattern)", FAIL, point.get("detail") or "The field could not be solved, so the exposure will not continue.",
            action="stop")
    else:
        wcs = point.get("wcs")
        # After a correction slew the header command is the offset that was sent, not the star.
        # The miss that matters is how far the camera is from the star.
        off_arcmin = point.get("target_offset_arcsec", point["offset_arcsec"]) / 60.0
        where = "on the chip" if point["on_chip"] else "off the chip"
        text = (
            f"the camera centre is {off_arcmin:.1f} arcmin from the target; the target is {where} "
            f"at pixel ({point['target_x']:.0f}, {point['target_y']:.0f})"
        )
        if point.get("n_matches"):
            text += f" ({point['n_matches']} stars matched)"
        if abs(point.get("angle_deg") or 0.0) >= 1.0 or point.get("mirrored"):
            text += f", field rotated {point['angle_deg']:.0f} deg"
            if point.get("mirrored"):
                text += " and mirrored"
        public = {
            k: point[k] for k in (
                "offset_arcsec", "target_offset_arcsec", "dx_px", "dy_px", "n_matches", "rms_px", "angle_deg", "mirrored",
                "target_x", "target_y", "on_chip", "solved_ra_deg", "solved_dec_deg", "next_ra_deg", "next_dec_deg",
            ) if point.get(k) is not None
        }
        if point["on_chip"]:
            if wcs is not None:
                stamp_solution(frame_path, wcs, float(target["ra_deg"]), float(target["dec_deg"]))
            status = WARN if off_arcmin >= settings.pointing_warn_arcmin else PASS
            action = "accept"
            if status == WARN:
                text += f": above the {settings.pointing_warn_arcmin:.0f} arcmin warning level, and still on the chip."
            recentre = float(getattr(settings, "pointing_recentre_arcmin", 0.0) or 0.0)
            if recentre > 0.0 and off_arcmin >= recentre and point.get("next_ra_deg") is not None:
                # Drift and field rotation take the star off the chip sooner when it starts near the edge.
                action = "recentre"
                text += f"; that is above the {recentre:.0f} arcmin re-centre level, so the next slew puts it at the centre."
            add("pointing (star pattern)", status, text, action=action, **public)
            seen = target_star_seen(data, float(point["target_x"]), float(point["target_y"]))
            where_px = f"the predicted target position ({point['target_x']:.0f}, {point['target_y']:.0f})"
            if seen["raw"] is not None:
                add("target star", PASS, f"a star is detected {seen['raw']:.0f} px from {where_px}.", **seen)
            elif seen["averaged"] is not None:
                add("target star", INFO, f"a star is detected {seen['averaged']:.0f} px from {where_px} only after averaging "
                    "2x2 pixels: the colour mosaic hides a sharp star from the finder.  The photometry averages such frames.", **seen)
            else:
                add("target star", WARN, f"no star is detected within {_TARGET_SEARCH_PX:.0f} px of {where_px}, so the photometry "
                    "will not find the target.  A wrong solve, a target that is very faint or saturated, or a catalogue "
                    "position that is off would each do this.", **seen)
        else:
            add("pointing (star pattern)", FAIL,
                f"{text}. The target is off the chip; the next slew adds this miss to the command.",
                action="nudge", **public)

    if solved is not None:
        sep = angular_separation_arcsec(solved["ra_deg"], solved["dec_deg"], float(target["ra_deg"]), float(target["dec_deg"])) / 60.0
        text = f"the scope's own plate solve is {sep:.1f} arcmin from the commanded position"
        status = FAIL if sep >= settings.pointing_fail_arcmin else WARN if sep >= settings.pointing_warn_arcmin else PASS
        add("pointing (scope's solver)", status, text, offset_arcmin=sep)
    else:
        add("pointing (scope's solver)", INFO, "not available (mock, or the scope gave no readable solution)")

    # Calibration ---------------------------------------------------------
    stats = measure_frame_stats(data)
    notes = [
        f"background {stats['background_adu']:.0f} ADU, noise {stats['noise_adu']:.1f} ADU, "
        f"gradient {100 * stats['gradient_fraction']:.0f}% across the frame, "
        f"{100 * stats['saturated_fraction']:.3f}% of pixels saturated",
    ]
    status = INFO
    if stats["gradient_fraction"] > _GRADIENT_WARN:
        status = WARN
        notes.append("A strong gradient may be vignetting or sky glow; flat fielding may help if it persists across the night.")
    add(
        "calibration", status,
        "No darks or flats are applied by Astronomo: frames are used as the Seestar saved them.  " + "; ".join(notes),
        **stats,
    )

    ok = not any(c["status"] == FAIL for c in checks)
    return {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "frame": str(frame_path),
        "target": target.get("name"),
        "ok": ok,
        "checks": checks,
        "thresholds": {
            "focus_warn_fwhm_px": settings.focus_warn_fwhm_px, "focus_fail_fwhm_px": settings.focus_fail_fwhm_px,
            "pointing_warn_arcmin": settings.pointing_warn_arcmin, "pointing_fail_arcmin": settings.pointing_fail_arcmin,
        },
    }


def display_view(data: np.ndarray) -> np.ndarray:
    """Float image in 0..1, percentile-stretched, for a person looking at the frame.

    A tree, a roof, dew or a cloud is extended structure.  Star measurements miss it, so the
    stretch keeps the faint and the bright.  This is a view of the saved pixels, not a
    measurement.  Row 0 stays at the top, which is the order the file was written in.
    """
    image = np.asarray(data, dtype=np.float32)
    if image.ndim == 3:
        image = image.mean(axis=0)
    if image.ndim != 2 or image.size == 0:
        raise ValueError("first-frame view needs a 2-D image")
    step = max(1, int(math.ceil(max(image.shape) / _DISPLAY_MAX_SIDE)))
    if step > 1:
        # The brightest pixel of each block, not every step-th pixel: a Seestar star is a few pixels wide and
        # plain decimation drops most of them, leaving a view that looks like noise.
        ny, nx = image.shape
        padded = np.pad(image, ((0, (-ny) % step), (0, (-nx) % step)), mode="edge")
        shown = padded.reshape(padded.shape[0] // step, step, padded.shape[1] // step, step).max(axis=(1, 3))
    else:
        shown = image
    finite = shown[np.isfinite(shown)]
    if finite.size == 0:
        return np.zeros(shown.shape, dtype=np.float32)
    lo, hi = np.percentile(finite, [_DISPLAY_LO_PCT, _DISPLAY_HI_PCT])
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return np.full(shown.shape, 0.5, dtype=np.float32)
    scaled = (shown - lo) / (hi - lo)
    return np.clip(np.nan_to_num(scaled, nan=0.0), 0.0, 1.0).astype(np.float32)


def save_first_frame_view(frame_path: str, mission_dir: Path, name: str = PREVIEW_NAME) -> Path:
    """Write ``first_frame.png`` (or ``name``) beside the setup report, from the frame just saved.

    Called before the focus and pointing checks return, so the picture is on disk before
    the next exposure starts.  The per-frame watch writes ``latest_frame.png`` the same way.
    Orientation against the Seestar app is unconfirmed.
    """
    from matplotlib import image as mpimg

    data, _header = load_image(frame_path)
    mission_dir.mkdir(parents=True, exist_ok=True)
    dest = mission_dir / name
    tmp = dest.with_name(f".{dest.stem}.tmp{dest.suffix}")  # written whole, then moved: the dashboard never reads half a picture
    mpimg.imsave(str(tmp), display_view(data), cmap="gray", vmin=0.0, vmax=1.0, origin="upper")
    tmp.replace(dest)
    return dest


def failure_message(report: dict[str, Any]) -> str:
    bad = [f"{c['name']}: {c['detail']}" for c in report["checks"] if c["status"] == FAIL]
    return "First-frame setup check failed -- " + " | ".join(bad)


def format_report(report: dict[str, Any]) -> list[str]:
    """One line per check, for the log."""
    return [f"[{c['status']}] {c['name']}: {c['detail']}" for c in report["checks"]]


def save_report(report: dict[str, Any], mission_dir: Path) -> Path:
    mission_dir.mkdir(parents=True, exist_ok=True)
    path = mission_dir / "setup_check.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path
