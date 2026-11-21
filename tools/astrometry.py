"""Small astrometry helpers shared by the telescope, plugins and catalog code.

* ``make_tan_wcs`` / ``wcs_from_header``: build an astropy WCS (gnomonic projection).
* ``estimate_translation``: robust 2-D offset between two point sets using
  pair-difference voting (works with outliers and unmatched points, no rotation).

Pixel coordinates are 0-based (numpy convention) throughout this module.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from scipy.ndimage import uniform_filter
from scipy.spatial import cKDTree

# Seestar S50: IMX462 (2.9 um pixels) behind a 250 mm focal length => ~2.39 "/px.
SEESTAR_S50_PIXEL_SCALE_ARCSEC = 2.39
SEESTAR_S50_SHAPE = (1080, 1920)  # (rows, cols)
# S50 Pro frames are 2160 x 3840. A plate solve of a real frame measured 2.24
# arcsec/pixel, so the extra pixels are a wider field, not a finer sampling of the
# S50's field. Halving 2.39 made the local solver search the wrong quad size.
SEESTAR_S50_PRO_PIXEL_SCALE_ARCSEC = SEESTAR_S50_PIXEL_SCALE_ARCSEC


def nominal_pixel_scale_arcsec(shape: tuple[int, int] | None) -> float:
    """Arcsec per pixel when a frame did not record ``PIXSCALE``.

    A 2160x3840 frame is the S50 Pro and uses the same 2.39 arcsec/pixel as the
    1080x1920 S50. Any other size does too. Either orientation counts.
    """
    if shape is not None and tuple(sorted(int(v) for v in shape)) == (2160, 3840):
        return SEESTAR_S50_PRO_PIXEL_SCALE_ARCSEC
    return SEESTAR_S50_PIXEL_SCALE_ARCSEC


def make_tan_wcs(
    ra_deg: float,
    dec_deg: float,
    shape: tuple[int, int],
    pixel_scale_arcsec: float,
) -> WCS:
    """North-up, east-left TAN WCS centred on the image."""
    ny, nx = shape
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = [ra_deg, dec_deg]
    wcs.wcs.crpix = [(nx + 1) / 2.0, (ny + 1) / 2.0]  # FITS 1-based
    scale = pixel_scale_arcsec / 3600.0
    wcs.wcs.cd = [[-scale, 0.0], [0.0, scale]]
    wcs.wcs.cunit = ["deg", "deg"]
    wcs.pixel_shape = (nx, ny)
    return wcs


def wcs_from_header(header: fits.Header, shape: tuple[int, int] | None = None) -> WCS | None:
    """Best-effort WCS from a FITS header.

    Uses a real celestial WCS if present; otherwise falls back to a pointing-only TAN
    WCS built from ``RA``/``DEC`` (degrees) and ``PIXSCALE`` (arcsec/px).  Returns
    ``None`` when no pointing information exists.
    """
    ny = header.get("NAXIS2") or (shape[0] if shape else None)
    nx = header.get("NAXIS1") or (shape[1] if shape else None)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)  # e.g. 'datfix' MJD-OBS notes
            wcs = WCS(header)
        if wcs.has_celestial and wcs.wcs.ctype[0].startswith("RA"):
            return wcs.celestial
    except Exception:  # noqa: BLE001 - malformed WCS keywords are common in the wild
        pass

    ra, dec = header.get("RA"), header.get("DEC")
    if ra is None or dec is None or nx is None or ny is None:
        return None
    try:
        ra, dec = float(ra), float(dec)
    except (TypeError, ValueError):
        return None
    if "PIXSCALE" in header:
        scale = float(header["PIXSCALE"])
    else:
        scale = nominal_pixel_scale_arcsec((int(ny), int(nx)))
    return make_tan_wcs(ra, dec, (int(ny), int(nx)), scale)


def wcs_to_dict(wcs: WCS) -> dict[str, Any]:
    return {
        "crval": [float(v) for v in wcs.wcs.crval],
        "crpix": [float(v) for v in wcs.wcs.crpix],
        "cd": [[float(v) for v in row] for row in wcs.pixel_scale_matrix],
        "shape": [int(v) for v in wcs.pixel_shape[::-1]] if wcs.pixel_shape else None,
    }


def wcs_from_dict(d: dict[str, Any]) -> WCS:
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = d["crval"]
    wcs.wcs.crpix = d["crpix"]
    wcs.wcs.cd = d["cd"]
    wcs.wcs.cunit = ["deg", "deg"]
    if d.get("shape"):
        wcs.pixel_shape = (d["shape"][1], d["shape"][0])
    return wcs


def pixel_scale_arcsec(wcs: WCS) -> float:
    m = wcs.pixel_scale_matrix
    return float(math.sqrt(abs(np.linalg.det(m))) * 3600.0)


def shift_wcs(wcs: WCS, dx: float, dy: float) -> WCS:
    """Return a WCS where every sky position maps to ``pixel + (dx, dy)``."""
    new = wcs.deepcopy()
    new.wcs.crpix = [new.wcs.crpix[0] + dx, new.wcs.crpix[1] + dy]
    return new


def sun_altitude_deg(lat_deg: float, lon_deg: float, elevation_m: float = 0.0, when=None) -> float:
    """Geometric altitude of the Sun (degrees) at the site.  Pure computation, no network.

    Uses astropy's built-in solar ephemeris; IERS data is *not* downloaded and any
    shortfall in Earth-orientation data only costs arcseconds, irrelevant for a
    darkness check.
    """
    from datetime import datetime, timezone

    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_sun
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    when = when or datetime.now(timezone.utc)
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)  # "IERS data not valid for this date": arcsec-level only
        t = Time(when)
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        sun = get_sun(t).transform_to(AltAz(obstime=t, location=site))
        return float(sun.alt.deg)


def target_altitude_deg(
    ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, elevation_m: float = 0.0, when=None
) -> float:
    """Altitude (degrees) of a fixed ICRS position at the site.  Pure computation, no network."""
    from datetime import datetime, timezone

    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    when = when or datetime.now(timezone.utc)
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        altaz = SkyCoord(ra_deg * u.deg, dec_deg * u.deg).transform_to(AltAz(obstime=Time(when), location=site))
        return float(altaz.alt.deg)


def _utc_to_bjd_tdb(when, coord, site) -> float:
    from astropy.time import Time

    t = Time(when, scale="utc", location=site)
    return float((t.tdb + t.light_travel_time(coord, kind="barycentric")).jd)


def utc_to_bjd_tdb(when, ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, elevation_m: float = 0.0) -> float:
    """Convert a UTC ``datetime`` to BJD_TDB for a source at (ra, dec), observed from the site."""
    from astropy import units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        return _utc_to_bjd_tdb(when, SkyCoord(ra_deg * u.deg, dec_deg * u.deg), site)


def utc_to_bjd_tdb_many(
    times: list, ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, elevation_m: float = 0.0
) -> list[float]:
    """:func:`utc_to_bjd_tdb` for a whole light curve in one astropy call (same result, far faster than a loop)."""
    from astropy import units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    if not times:
        return []
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        t = Time(list(times), scale="utc", location=site)
        coord = SkyCoord(ra_deg * u.deg, dec_deg * u.deg)
        return [float(v) for v in (t.tdb + t.light_travel_time(coord, kind="barycentric")).jd]


def bjd_tdb_to_utc(bjd_tdb: float, ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, elevation_m: float = 0.0):
    """Inverse of :func:`utc_to_bjd_tdb`: the UTC ``datetime`` at which BJD_TDB equals ``bjd_tdb``.

    Archive mid-transit times are BJD_TDB; a telescope's frames are stamped in UTC.  The two differ
    by up to ~8 minutes of light-travel time plus a ~1 minute leap-second/TT offset, which is
    comparable to the timing precision of a good transit, so it is corrected, not ignored.
    """
    from datetime import timedelta, timezone

    from astropy import units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        coord = SkyCoord(ra_deg * u.deg, dec_deg * u.deg)
        guess = Time(bjd_tdb, format="jd", scale="tdb").utc.to_datetime(timezone=timezone.utc)
        for _ in range(3):  # the correction changes by milliseconds per iteration after the first
            err_days = _utc_to_bjd_tdb(guess, coord, site) - bjd_tdb
            guess = guess - timedelta(days=err_days)
        return guess


def bjd_tdb_to_utc_many(
    bjd_tdb: Any, ra_deg: Any, dec_deg: Any, lat_deg: float, lon_deg: float, elevation_m: float = 0.0
) -> list:
    """:func:`bjd_tdb_to_utc` for many events in one astropy call, each with its own sky position.

    ``bjd_tdb``, ``ra_deg`` and ``dec_deg`` are equal-length sequences.  The light-travel correction is
    applied once to a TT-based first guess; its rate of change is about a millisecond per second of error,
    so the answer agrees with the scalar version to well under a second.  Returns tz-aware UTC datetimes.
    """
    from datetime import timezone

    from astropy import units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    bjd = np.atleast_1d(np.asarray(bjd_tdb, dtype=float))
    if bjd.size == 0:
        return []
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        coord = SkyCoord(np.asarray(ra_deg, dtype=float) * u.deg, np.asarray(dec_deg, dtype=float) * u.deg)
        guess = Time(bjd, format="jd", scale="tdb").utc  # right to the light-travel term (up to ~8 minutes)
        stamped = Time(guess.jd1, guess.jd2, format="jd", scale="utc", location=site)
        err = ((stamped.tdb + stamped.light_travel_time(coord, kind="barycentric")).jd - bjd)  # days
        fixed = Time(guess.jd1, guess.jd2 - err, format="jd", scale="utc")
        return list(fixed.to_datetime(timezone=timezone.utc))


def sun_altitude_deg_many(lat_deg: float, lon_deg: float, elevation_m: float, times: list) -> np.ndarray:
    """:func:`sun_altitude_deg` for many instants (``times`` are tz-aware datetimes) in one astropy call."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_sun
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    if not times:
        return np.zeros(0)
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        t = Time(list(times))
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        return np.asarray(get_sun(t).transform_to(AltAz(obstime=t, location=site)).alt.deg, dtype=float)


def target_altitude_deg_many(
    ra_deg: Any, dec_deg: Any, lat_deg: float, lon_deg: float, elevation_m: float, times: list
) -> np.ndarray:
    """:func:`target_altitude_deg` for equal-length ``ra_deg``, ``dec_deg`` and ``times`` in one astropy call."""
    return target_altaz_many(ra_deg, dec_deg, lat_deg, lon_deg, elevation_m, times)[0]


def target_altaz_many(
    ra_deg: Any, dec_deg: Any, lat_deg: float, lon_deg: float, elevation_m: float, times: list
) -> tuple[np.ndarray, np.ndarray]:
    """Altitude and azimuth (degrees, azimuth from north through east) for equal-length ``ra_deg``, ``dec_deg`` and ``times``."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    if len(times) == 0:
        return np.zeros(0), np.zeros(0)
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        coord = SkyCoord(np.asarray(ra_deg, dtype=float) * u.deg, np.asarray(dec_deg, dtype=float) * u.deg)
        altaz = coord.transform_to(AltAz(obstime=Time(list(times)), location=site))
        return np.asarray(altaz.alt.deg, dtype=float), np.asarray(altaz.az.deg, dtype=float)


def angular_separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    a = SkyCoord(ra1 * u.deg, dec1 * u.deg)
    b = SkyCoord(ra2 * u.deg, dec2 * u.deg)
    return float(a.separation(b).arcsec)


def sky_motion(
    ra1: float, dec1: float, ra2: float, dec2: float, dt_minutes: float
) -> tuple[float, float]:
    """Return ``(rate_arcsec_per_min, position_angle_deg_east_of_north)``."""
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    a = SkyCoord(ra1 * u.deg, dec1 * u.deg)
    b = SkyCoord(ra2 * u.deg, dec2 * u.deg)
    rate = float(a.separation(b).arcsec) / dt_minutes if dt_minutes else float("nan")
    pa = float(a.position_angle(b).deg)
    return rate, pa


def estimate_translation(
    src: np.ndarray,
    dst: np.ndarray,
    *,
    max_shift: float = 100.0,
    tol: float = 2.0,
    min_matches: int = 5,
) -> dict[str, Any] | None:
    """Estimate ``d`` such that ``dst ~= src + d``.

    Votes over all pairwise differences, takes the densest 2-D bin, then refines with a
    nearest-neighbour match.  Returns ``None`` if fewer than ``min_matches`` points
    agree, so callers must handle failure explicitly.
    """
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    if len(src) < min_matches or len(dst) < min_matches:
        return None

    diffs = (dst[None, :, :] - src[:, None, :]).reshape(-1, 2)
    keep = (np.abs(diffs[:, 0]) <= max_shift) & (np.abs(diffs[:, 1]) <= max_shift)
    diffs = diffs[keep]
    if len(diffs) < min_matches:
        return None

    bin_px = max(tol, 1.0)
    n_bins = int(math.ceil(2 * max_shift / bin_px))
    hist, xe, ye = np.histogram2d(
        diffs[:, 0], diffs[:, 1], bins=n_bins, range=[[-max_shift, max_shift]] * 2
    )
    # Sum over 3x3 neighbourhoods so a true peak split across bins is still found.
    smooth = uniform_filter(hist, size=3, mode="constant") * 9.0
    ix, iy = np.unravel_index(int(np.argmax(smooth)), smooth.shape)
    # The 3x3 sums tie whenever the true peak sits in one bin, so the block *centre* can be
    # up to a bin away from the votes.  Use the median of the votes inside the block instead.
    x_lo, x_hi = xe[max(ix - 1, 0)], xe[min(ix + 2, n_bins)]
    y_lo, y_hi = ye[max(iy - 1, 0)], ye[min(iy + 2, n_bins)]
    in_block = (diffs[:, 0] >= x_lo) & (diffs[:, 0] <= x_hi) & (diffs[:, 1] >= y_lo) & (diffs[:, 1] <= y_hi)
    guess = np.median(diffs[in_block], axis=0)

    shift = guess
    tree = cKDTree(dst)
    matches = 0
    rms = float("nan")
    for _ in range(3):  # a few refinement passes
        dist, idx = tree.query(src + shift, distance_upper_bound=tol)
        ok = np.isfinite(dist)
        matches = int(ok.sum())
        if matches < min_matches:
            return None
        resid = dst[idx[ok]] - src[ok]
        shift = np.median(resid, axis=0)
        rms = float(np.sqrt(np.mean(np.sum((resid - shift) ** 2, axis=1))))

    return {"dx": float(shift[0]), "dy": float(shift[1]), "n_matches": matches, "rms_px": rms}


def estimate_rotation(
    src: np.ndarray,
    dst: np.ndarray,
    shift: tuple[float, float],
    center: tuple[float, float],
    *,
    tol: float = 2.0,
    capture_px: float = 8.0,
    min_matches: int = 8,
    angle0_deg: float = 0.0,
) -> dict[str, Any] | None:
    """Rotation (degrees) of ``dst`` relative to ``src`` about ``center``, given a translation guess.

    ``estimate_translation`` assumes no rotation, so a rotated field makes it match only the
    stars near the pivot.  This starts from its shift, pairs stars within ``capture_px`` (wide, so
    the outer stars that a rotation has moved are still caught), fits a rotation + translation
    to the pairs by least squares, re-pairs with the fitted model within ``tol`` and refits.
    Positive angles are counter-clockwise in pixel (x right, y up) coordinates.

    Returns ``{"angle_deg", "n_matches", "rms_px"}`` or None if too few stars pair up.
    """
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    if len(src) < min_matches or len(dst) < min_matches:
        return None
    c = np.asarray(center, dtype=float)
    tree = cKDTree(dst)
    theta, t = math.radians(angle0_deg), np.asarray(shift, dtype=float)
    n = 0
    rms = float("nan")
    for radius in (capture_px, capture_px, tol, tol):
        cos, sin = math.cos(theta), math.sin(theta)
        moved = (src - c) @ np.array([[cos, sin], [-sin, cos]]) + c + t  # rotate by theta, then shift
        dist, idx = tree.query(moved, distance_upper_bound=radius)
        ok = np.isfinite(dist)
        n = int(ok.sum())
        if n < min_matches:
            return None
        a, b = src[ok] - c, dst[idx[ok]] - c
        # Least-squares rotation + translation (Kabsch / Procrustes in 2-D).
        ma, mb = a.mean(axis=0), b.mean(axis=0)
        a0, b0 = a - ma, b - mb
        theta = math.atan2(float(np.sum(a0[:, 0] * b0[:, 1] - a0[:, 1] * b0[:, 0])),
                           float(np.sum(a0[:, 0] * b0[:, 0] + a0[:, 1] * b0[:, 1])))
        cos, sin = math.cos(theta), math.sin(theta)
        rot = np.array([[cos, sin], [-sin, cos]])
        t = mb - ma @ rot
        resid = b - (a @ rot + t)
        rms = float(np.sqrt(np.mean(np.sum(resid**2, axis=1))))
    # ``dx``/``dy`` complete the model: dst ~ R(angle) (src - center) + center + (dx, dy).
    return {"angle_deg": math.degrees(theta), "n_matches": n, "rms_px": rms, "dx": float(t[0]), "dy": float(t[1])}


def match_rigid(
    src: np.ndarray,
    dst: np.ndarray,
    center: tuple[float, float],
    *,
    tol: float = 3.0,
    min_matches: int = 8,
    min_sep: float = 15.0,
    max_shift: float | None = None,
) -> dict[str, Any] | None:
    """Rotation, optional mirror and translation that carries ``src`` onto ``dst``.

    ``src`` is the catalogue drawn north-up. ``dst`` is the stars measured in the frame,
    which on an alt-az telescope can be rotated by any angle and whose pixel axes may be
    mirrored. The model is ``dst ~= R @ (src - center) + center + shift``. ``shift`` is the
    pointing offset in pixels. Returns None when fewer than ``min_matches`` stars agree.
    """
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    if len(src) > 30:
        src = src[:30]
    if len(dst) > 40:
        dst = dst[:40]
    if len(src) < min_matches or len(dst) < min_matches:
        return None
    center_xy = np.asarray(center, dtype=float)

    def search(points: np.ndarray, mirrored: bool) -> dict[str, Any] | None:
        tree = cKDTree(dst)
        ii, jj = np.triu_indices(len(dst), 1)
        separations = np.hypot(dst[ii, 0] - dst[jj, 0], dst[ii, 1] - dst[jj, 1])
        order = np.argsort(separations)
        separations, ii, jj = separations[order], ii[order], jj[order]
        best: dict[str, Any] | None = None
        for a in range(len(points)):
            for b in range(a + 1, len(points)):
                span = float(np.hypot(*(points[b] - points[a])))
                if span < min_sep:
                    continue
                lo = int(np.searchsorted(separations, span - tol))
                hi = int(np.searchsorted(separations, span + tol))
                if hi - lo > 15:
                    continue
                for k in range(lo, hi):
                    for i, j in ((int(ii[k]), int(jj[k])), (int(jj[k]), int(ii[k]))):
                        found = _rigid_from_pair(points, dst, a, b, i, j, center_xy, tree, tol, min_matches)
                        if found is None:
                            continue
                        if max_shift is not None and math.hypot(found["dx"], found["dy"]) > max_shift:
                            continue
                        found["mirrored"] = mirrored
                        if best is None or (found["n_matches"], -found["rms_px"]) > (best["n_matches"], -best["rms_px"]):
                            best = found
                            if best["n_matches"] >= len(points) - 1:
                                return best
        return best

    direct = search(src, False)
    flipped = src.copy()
    flipped[:, 0] = 2.0 * center_xy[0] - flipped[:, 0]
    mirror = search(flipped, True)
    if direct is None:
        return mirror
    if mirror is None or (direct["n_matches"], -direct["rms_px"]) >= (mirror["n_matches"], -mirror["rms_px"]):
        return direct
    return mirror


def _rigid_from_pair(src, dst, a, b, i, j, center, tree, tol, min_matches) -> dict[str, Any] | None:
    """One hypothesis: the vector ``src[a] -> src[b]`` lands on ``dst[i] -> dst[j]``."""
    v, w = src[b] - src[a], dst[j] - dst[i]
    angle = math.atan2(float(w[1]), float(w[0])) - math.atan2(float(v[1]), float(v[0]))
    cos, sin = math.cos(angle), math.sin(angle)
    rotation = np.array([[cos, -sin], [sin, cos]])
    shift = dst[i] - ((src[a] - center) @ rotation.T + center)
    mapped = (src - center) @ rotation.T + center + shift
    dist, idx = tree.query(mapped, distance_upper_bound=tol)
    ok = dist < tol
    if int(ok.sum()) < min_matches:
        return None
    claimed: dict[int, tuple[float, int]] = {}
    for n in np.flatnonzero(ok):
        star = int(idx[n])
        if star not in claimed or dist[n] < claimed[star][0]:
            claimed[star] = (float(dist[n]), int(n))
    if len(claimed) < min_matches:
        return None
    residuals = np.array([mapped[n] - dst[star] for star, (_dist, n) in claimed.items()])
    rms = float(np.sqrt(np.mean(np.sum(residuals**2, axis=1))))
    return {
        "angle_deg": math.degrees(angle),
        "dx": float(shift[0]),
        "dy": float(shift[1]),
        "n_matches": len(claimed),
        "rms_px": rms,
    }


def find_rotation(
    src: np.ndarray,
    dst: np.ndarray,
    center: tuple[float, float],
    *,
    max_deg: float = 6.0,
    step_deg: float = 0.25,
    max_shift: float = 250.0,
    tol: float = 2.0,
    min_matches: int = 8,
) -> dict[str, Any] | None:
    """Explain a failed translation match: which rotation makes the two star lists agree?

    Tries rotations of ``src`` about ``center`` on a grid, scores each by how many stars
    ``estimate_translation`` can then match, and refines the best with ``estimate_rotation``.
    Returns ``{"angle_deg", "dx", "dy", "n_matches"}`` or None if no rotation helps.
    """
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    c = np.asarray(center, dtype=float)
    best: tuple[int, float, dict[str, Any]] | None = None
    for deg in np.arange(-max_deg, max_deg + step_deg / 2, step_deg):
        th = math.radians(float(deg))
        rotated = (src - c) @ np.array([[math.cos(th), math.sin(th)], [-math.sin(th), math.cos(th)]]) + c
        est = estimate_translation(rotated, dst, max_shift=max_shift, tol=tol, min_matches=min_matches)
        if est is not None and (best is None or est["n_matches"] > best[0]):
            best = (est["n_matches"], float(deg), est)
    if best is None:
        return None
    _, deg, est = best
    fine = estimate_rotation(src, dst, (est["dx"], est["dy"]), center, tol=tol, min_matches=min_matches, angle0_deg=deg)
    if fine is None:
        return {"angle_deg": deg, "dx": est["dx"], "dy": est["dy"], "n_matches": est["n_matches"]}
    return {"angle_deg": fine["angle_deg"], "dx": est["dx"], "dy": est["dy"], "n_matches": fine["n_matches"]}


def sky_conditions_many(
    ra_deg: Any, dec_deg: Any, lat_deg: float, lon_deg: float, elevation_m: float, times: list
) -> dict[str, np.ndarray]:
    """Where the target, the Sun and the Moon are, for equal-length ``ra_deg``, ``dec_deg`` and ``times``.

    One astropy pass, no network (the built-in ephemeris is accurate to a few arcminutes, far better than any
    interlock needs).  Returns arrays named ``target_alt``, ``target_az`` (from north through east), ``sun_alt``, ``sun_sep`` (degrees from the target),
    ``moon_alt``, ``moon_sep`` and ``moon_illum`` (illuminated fraction, 0 new to 1 full).  This is the one place
    the site interlocks (``core/interlocks.py``) and the Opportunities page get their sky from, so a window the
    page calls observable is one the mission would observe.
    """
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    if len(times) == 0:
        return {k: np.zeros(0) for k in ("target_alt", "target_az", "sun_alt", "sun_sep", "moon_alt", "moon_sep", "moon_illum")}
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        t = Time(list(times))
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        frame = AltAz(obstime=t, location=site)
        target = SkyCoord(np.asarray(ra_deg, dtype=float) * u.deg, np.asarray(dec_deg, dtype=float) * u.deg)
        sun = get_sun(t)
        moon = get_body("moon", t, site)
        elongation = sun.separation(moon)  # the Moon's phase follows from its angle to the Sun
        phase_angle = np.arctan2(sun.distance * np.sin(elongation), moon.distance - sun.distance * np.cos(elongation))
        # Separations are taken in the observer's own (AltAz) frame: comparing an ICRS direction with a Sun or Moon
        # position directly would mix a barycentric and a topocentric direction.
        target_aa, sun_aa, moon_aa = target.transform_to(frame), sun.transform_to(frame), moon.transform_to(frame)
        return {
            "target_alt": np.asarray(target_aa.alt.deg, dtype=float),
            "target_az": np.asarray(target_aa.az.deg, dtype=float),
            "sun_alt": np.asarray(sun_aa.alt.deg, dtype=float),
            "sun_sep": np.asarray(target_aa.separation(sun_aa).deg, dtype=float),
            "moon_alt": np.asarray(moon_aa.alt.deg, dtype=float),
            "moon_sep": np.asarray(target_aa.separation(moon_aa).deg, dtype=float),
            "moon_illum": np.asarray((1.0 + np.cos(phase_angle.to_value(u.rad))) / 2.0, dtype=float),
        }


def sun_moon_grid(lat_deg: float, lon_deg: float, elevation_m: float, times: list) -> dict[str, np.ndarray]:
    """The Sun and the Moon on a time grid (``times`` are tz-aware datetimes), for :func:`sky_from_grid`.

    Neither body depends on the target, so a report covering thousands of transits computes them once on a coarse
    grid and interpolates, instead of once per transit.  Returns ``t`` (POSIX seconds), ``sun_alt``, ``moon_alt``,
    ``moon_illum`` and the unit vectors ``sun_xyz`` and ``moon_xyz`` (shape ``(n, 3)``, ICRS).
    """
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_body, get_sun
    from astropy.time import Time
    from astropy.utils import iers
    from astropy.utils.exceptions import AstropyWarning

    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp(
        "iers_degraded_accuracy", "ignore"
    ):
        warnings.simplefilter("ignore", AstropyWarning)
        t = Time(list(times))
        site = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
        frame = AltAz(obstime=t, location=site)
        sun, moon = get_sun(t), get_body("moon", t, site)
        elongation = sun.separation(moon)
        phase_angle = np.arctan2(sun.distance * np.sin(elongation), moon.distance - sun.distance * np.cos(elongation))

        def xyz(body) -> np.ndarray:
            ra, dec = np.radians(body.ra.deg), np.radians(body.dec.deg)
            return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=1)

        return {
            "t": np.array([x.timestamp() for x in times], dtype=float),
            "sun_alt": np.asarray(sun.transform_to(frame).alt.deg, dtype=float),
            "moon_alt": np.asarray(moon.transform_to(frame).alt.deg, dtype=float),
            "moon_illum": np.asarray((1.0 + np.cos(phase_angle.to_value(u.rad))) / 2.0, dtype=float),
            "sun_xyz": xyz(sun), "moon_xyz": xyz(moon),
        }


def sky_from_grid(
    grid: dict[str, np.ndarray], ra_deg: Any, dec_deg: Any, lat_deg: float, lon_deg: float, elevation_m: float, times: list
) -> dict[str, np.ndarray]:
    """Like :func:`sky_conditions_many`, but the Sun and Moon are interpolated from ``grid`` (:func:`sun_moon_grid`).

    The target's own altitude is exact; the Sun and Moon move slowly enough that a grid of a few minutes is far
    finer than any interlock needs.
    """
    if len(times) == 0:
        return {k: np.zeros(0) for k in ("target_alt", "target_az", "sun_alt", "sun_sep", "moon_alt", "moon_sep", "moon_illum")}
    q = np.array([x.timestamp() for x in times], dtype=float)

    def at(name: str) -> np.ndarray:
        return np.interp(q, grid["t"], grid[name])

    def vec(name: str) -> np.ndarray:
        v = np.stack([np.interp(q, grid["t"], grid[name][:, k]) for k in range(3)], axis=1)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    ra, dec = np.radians(np.asarray(ra_deg, dtype=float)), np.radians(np.asarray(dec_deg, dtype=float))
    star = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=1)

    def sep(v: np.ndarray) -> np.ndarray:
        return np.degrees(np.arccos(np.clip(np.sum(star * v, axis=1), -1.0, 1.0)))

    alt, az = target_altaz_many(ra_deg, dec_deg, lat_deg, lon_deg, elevation_m, times)
    return {
        "target_alt": alt, "target_az": az,
        "sun_alt": at("sun_alt"), "sun_sep": sep(vec("sun_xyz")),
        "moon_alt": at("moon_alt"), "moon_sep": sep(vec("moon_xyz")), "moon_illum": at("moon_illum"),
    }
