"""Small image-reduction helpers shared by plugins that work on raw FITS frames.

Everything here is plain Python on pixel data (astropy + photutils).  A plugin that needs
background subtraction, star detection or frame timing can use these instead of re-implementing
them; they do not know about any science case.

(The asteroid plugin predates this module and keeps its own copies in
``plugins/asteroid_finder/reduction.py``; the two are independent on purpose.)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from astropy.time import Time
from photutils.background import Background2D, MedianBackground
from photutils.detection import DAOStarFinder

log = logging.getLogger(__name__)


def load_image(path: str) -> tuple[np.ndarray, fits.Header]:
    """First 2-D (or 3-D, collapsed to a mean) image HDU as float32, plus its header."""
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
    if data.ndim == 3:  # e.g. an RGB stack: collapse to a luminance-like mean
        data = data.mean(axis=0)
    return data, header


def frame_times(header: fits.Header, fallback_index: int = 0) -> tuple[datetime, float]:
    """``(UTC exposure start, exposure seconds)`` from a header (spacing assumed if DATE-OBS is absent)."""
    exptime = float(header.get("EXPTIME", header.get("EXPOSURE", 10.0)))
    raw = header.get("DATE-OBS")
    if raw:
        start = Time(raw, scale="utc").to_datetime(timezone=timezone.utc)
    else:
        log.warning("Frame has no DATE-OBS; assuming %.0f s spacing", exptime)
        start = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=fallback_index * exptime)
    return start, exptime


def subtract_background(data: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Return ``(background-subtracted image, background rms, median sky level)``."""
    try:
        bkg = Background2D(
            data,
            box_size=(64, 64),
            filter_size=(3, 3),
            sigma_clip=SigmaClip(sigma=3.0, maxiters=5),
            bkg_estimator=MedianBackground(),
        )
        return (
            (data - bkg.background).astype(np.float32),
            float(bkg.background_rms_median),
            float(bkg.background_median),
        )
    except Exception:  # noqa: BLE001 - e.g. tiny test images
        log.warning("Background2D failed; falling back to global sigma-clipped median", exc_info=True)
        clipped = SigmaClip(sigma=3.0, maxiters=5)(data, masked=True)
        level = float(np.ma.median(clipped))
        return (data - level).astype(np.float32), float(np.ma.std(clipped)), level


def detect_stars(data: np.ndarray, rms: float, fwhm_px: float = 3.0, sigma: float = 5.0) -> tuple[np.ndarray, np.ndarray]:
    """DAOStarFinder on a background-subtracted image -> ``(xy (N, 2), flux (N,))``."""
    table = DAOStarFinder(threshold=sigma * rms, fwhm=fwhm_px, exclude_border=True)(data)
    if table is None or len(table) == 0:
        return np.empty((0, 2)), np.empty(0)
    xy = np.column_stack([np.asarray(table["xcentroid"], float), np.asarray(table["ycentroid"], float)])
    return xy, np.asarray(table["flux"], float)


# Green sites in a 2x2 Bayer cell, as (row, col) offsets.  The other two sites are the red and blue samples.
_GREEN_SITES = {
    "RGGB": ((0, 1), (1, 0)),
    "BGGR": ((0, 1), (1, 0)),
    "GRBG": ((0, 0), (1, 1)),
    "GBRG": ((0, 0), (1, 1)),
}


def measurement_channel(data: np.ndarray, header: fits.Header | None = None) -> tuple[np.ndarray, str, str]:
    """The image a brightness measurement should use, and how it was chosen.

    Returns ``(image, band, note)``.  ``band`` is ``"green"`` when a colour frame was reduced to its green
    samples, and ``"clear"`` when the frame was already a single channel.  A Bayer mosaic stays on the
    original pixel grid: green samples are kept and the other pixels are filled from their green neighbours,
    so a WCS written for the full frame still applies.  A 3-plane colour cube contributes its middle plane.
    """
    image = np.asarray(data, dtype=np.float32)
    pattern = ""
    if header is not None:
        pattern = str(header.get("BAYERPAT") or "").strip().upper()
    if image.ndim == 3:
        axis = next((i for i, n in enumerate(image.shape) if n == 3), None)
        if axis is not None:
            plane = np.moveaxis(image, axis, 0)[1]
            return np.ascontiguousarray(plane, dtype=np.float32), "green", "green plane of a 3-colour image"
    if image.ndim == 2 and pattern in _GREEN_SITES:
        return _green_from_mosaic(image, pattern), "green", f"green samples of a {pattern} mosaic"
    if image.ndim != 2:
        raise ValueError(f"an image must be 2-D or a 3-colour cube, not shape {image.shape}")
    return image, "clear", "single-channel image; no colour mosaic to split"


def _green_from_mosaic(mosaic: np.ndarray, pattern: str) -> np.ndarray:
    """Green samples kept; every other pixel filled with the mean of its neighbouring green samples."""
    from scipy.ndimage import convolve

    green = np.zeros(mosaic.shape, dtype=np.float32)
    mask = np.zeros(mosaic.shape, dtype=np.float32)
    for row, col in _GREEN_SITES[pattern]:
        green[row::2, col::2] = mosaic[row::2, col::2]
        mask[row::2, col::2] = 1.0
    kernel = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    neigh = convolve(green, kernel, mode="nearest")
    weight = convolve(mask, kernel, mode="nearest")
    filled = np.where(mask > 0, green, neigh / np.maximum(weight, 1.0))
    return np.ascontiguousarray(filled, dtype=np.float32)


#: A star narrower than this (FWHM, px) is sampled by only a few mosaic pixels of mixed colour.
NARROW_STAR_FWHM_PX = 4.0


def average_2x2(data: np.ndarray) -> np.ndarray:
    """A moving 2x2 mean on the original pixel grid.

    Every window holds one red, two green and one blue sample whatever the Bayer pattern is, so a star
    that is a ring of bright green pixels around a dim red or blue centre becomes one round blob, with no
    need to know the pattern.  The result is shifted by half a pixel, the same for every frame.  Noise
    between neighbouring pixels becomes correlated, so a photon-noise estimate from it is too small.
    """
    from scipy.ndimage import uniform_filter

    return uniform_filter(np.asarray(data, dtype=np.float32), size=2, mode="nearest").astype(np.float32)


def load_measurement(path: str) -> tuple[np.ndarray, fits.Header]:
    """Like :func:`load_image`, but a colour frame is reduced to its green channel first.

    The header gains ``PHOTBAND`` (``green`` or ``clear``) and a short ``PHOTNOTE``.
    """
    with fits.open(path) as hdul:
        for hdu in hdul:
            if hdu.data is not None and getattr(hdu.data, "ndim", 0) >= 2:
                raw = np.asarray(hdu.data)
                header = hdu.header.copy()
                if not header.get("DATE-OBS") and hdul[0].header.get("DATE-OBS"):
                    header["DATE-OBS"] = hdul[0].header["DATE-OBS"]
                break
        else:
            raise ValueError(f"{path}: no image data found")
    image, band, note = measurement_channel(raw, header)
    header["PHOTBAND"] = (band, "channel used for photometry")
    header["PHOTNOTE"] = note[:68]
    return image, header
