"""Local plate solve of one frame, using Astrometry.net's ``solve-field`` when it is installed.

The mount's own coordinates are only a hint. The solve says where the camera was actually
pointed, and it does not need Gaia or a network connection. The pixel-scale window is taken
from the frame (an S50 and an S50 Pro are both about 2.4 arcsec/pixel), not from a
single telescope model. Returns None when the program is absent or the field will not solve,
so the caller can try another method or stop.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from astropy.io import fits
from astropy.wcs import WCS

log = logging.getLogger(__name__)


def solve_frame(
    path: str | Path,
    ra_deg: float,
    dec_deg: float,
    scale_arcsec: float,
    *,
    radius_deg: float = 5.0,
    timeout_s: float = 35.0,
) -> WCS | None:
    """Solve ``path`` near the hinted coordinates. None if ``solve-field`` is missing or fails."""
    binary = shutil.which("solve-field")
    if binary is None or scale_arcsec <= 0:
        return None
    fits_path = Path(path)
    low, high = 0.8 * scale_arcsec, 1.25 * scale_arcsec
    cmd = [
        binary,
        str(fits_path),
        "--dir", str(fits_path.parent),
        "--overwrite",
        "--no-plots",
        "--downsample", "2",
        "--ra", str(ra_deg),
        "--dec", str(dec_deg),
        "--radius", str(radius_deg),
        "--scale-units", "arcsecperpix",
        "--scale-low", f"{low:.4f}",
        "--scale-high", f"{high:.4f}",
        "--tweak-order", "1",
        "--cpulimit", str(max(1, int(timeout_s))),
    ]
    log.info("Plate solve: %s hinted at RA %.4f Dec %.4f, scale %.3f arcsec/px", fits_path.name, ra_deg, dec_deg, scale_arcsec)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Plate solve failed to run: %s", exc)
        return None
    wcs_path = fits_path.with_suffix(".wcs")
    if not wcs_path.is_file():
        log.info("Plate solve: no solution for %s", fits_path.name)
        return None
    try:
        wcs = WCS(fits.getheader(wcs_path, 0))
    except Exception as exc:  # noqa: BLE001 - a bad sidecar is "not solved"
        log.warning("Plate solve wrote an unreadable WCS (%s)", exc)
        return None
    if not wcs.has_celestial:
        return None
    return wcs.celestial
