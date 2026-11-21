"""Deterministic synthetic sky used by the mock telescope and the offline catalogs.

This module is *science-case agnostic*.  One :class:`MockSky` describes a single pointing:

* a fixed star field (positions in the *true* sky frame, so an offline Gaia query can
  return exactly the stars the mock camera "sees"),
* a pointing error (the telescope lands slightly off its commanded position, as real
  mounts do), so astrometric calibration against Gaia has something to fix,
* sky background with a gentle gradient, Poisson + read noise and fixed hot pixels.

Everything specific to an experiment -- a moving asteroid, a transiting exoplanet host,
a variable star, pointing drift, an atmospheric transparency trend -- is supplied by the
*plugin* as a :class:`SkyScene` (``BaseExperimentPlugin.mock_scene``).  A scene can

* add sources to every frame (:meth:`SkyScene.draw`),
* shift the whole field (:meth:`SkyScene.pointing_offset_px`) or dim it
  (:meth:`SkyScene.transparency`),
* add real stars to the catalogue (:meth:`SkyScene.catalog_stars`), so the offline Gaia
  agrees with what the camera saw.

The telescope, the offline Gaia and any plugin-side stand-ins (e.g. an offline ephemeris
service) are all built from the same ``(target, scene)`` pair, so the simulated ecosystem
stays self-consistent without this module knowing about any plugin.

``target["sim"]`` keys understood here (all optional)::

    {
      "seed": int,
      "epoch0_iso": "2026-10-20T04:00:00+00:00",   # sim time of frame 0
      "pointing_error_arcsec": [dRA*cos(dec), dDec]
    }

Plugins may put further keys in ``sim`` for their own scene to read.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from astropy.io import fits

from tools.astrometry import (
    SEESTAR_S50_PIXEL_SCALE_ARCSEC,
    SEESTAR_S50_SHAPE,
    make_tan_wcs,
)

FWHM_PX = 3.0
SIGMA_PX = FWHM_PX / 2.3548
SKY_BACKGROUND_ADU = 1000.0
READ_NOISE_ADU = 12.0
# Total counts of a G=16 star in a 10 s exposure.
G16_COUNTS_10S = 4000.0
STAR_DENSITY_PER_PX2 = 2.4e-4
STAR_MAG_RANGE = (11.0, 17.5)
FRAME_MARGIN_PX = 500  # stars are generated beyond the frame so Gaia cones have neighbours


def _stable_seed(*parts: Any) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16)


def counts_for_mag(mag: float, exposure_s: float) -> float:
    return G16_COUNTS_10S * 10 ** (-0.4 * (mag - 16.0)) * (exposure_s / 10.0)


@dataclass(frozen=True)
class FrameContext:
    """What a :class:`SkyScene` is told about the frame being rendered."""

    start: datetime  # exposure start (simulated clock)
    mid: datetime  # exposure mid-point
    exposure_s: float
    frame_index: int
    offset_px: tuple[float, float]  # whole-field pointing offset applied to this frame
    transparency: float  # whole-field flux factor applied to the stars of this frame


class SkyScene:
    """The science-case-specific content of a simulated field.  The base class adds nothing.

    A plugin subclasses this and returns an instance from ``BaseExperimentPlugin.mock_scene``.
    All hooks are optional, deterministic, and receive the :class:`MockSky` they extend (for
    its seed, epoch, geometry and WCS).  Pixel coordinates are in the *true* frame at epoch0.
    """

    def pointing_offset_px(self, sky: "MockSky", when: datetime) -> tuple[float, float]:
        """Whole-field shift at ``when`` (e.g. tracking drift).  Stars and scene sources move together."""
        return 0.0, 0.0

    def transparency(self, sky: "MockSky", when: datetime, frame_index: int) -> float:
        """Whole-field flux factor for this frame (e.g. an extinction trend); ``1.0`` is clear."""
        return 1.0

    def catalog_stars(self, sky: "MockSky") -> list[tuple[float, float, float]]:
        """Extra real stars as ``(x_px, y_px, G_mag)`` the offline Gaia should also report."""
        return []

    def draw(self, sky: "MockSky", signal: np.ndarray, ctx: FrameContext) -> None:
        """Add this scene's sources to the noiseless ``signal`` image (ADU) in place.

        Use :meth:`MockSky.stamp` and :func:`counts_for_mag`; add ``ctx.offset_px`` to positions
        that should move with the field and multiply by ``ctx.transparency`` for flux that
        should dim with it.
        """


@dataclass
class MockSky:
    commanded_ra: float
    commanded_dec: float
    seed: int
    pointing_error_arcsec: tuple[float, float]
    epoch0: datetime
    shape: tuple[int, int] = SEESTAR_S50_SHAPE
    pixel_scale: float = SEESTAR_S50_PIXEL_SCALE_ARCSEC
    scene: SkyScene = field(default_factory=SkyScene)
    fwhm_px: float = FWHM_PX  # star width; a larger value simulates a defocused scope
    _stars: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_target(cls, target: dict, now: datetime | None = None, scene: SkyScene | None = None) -> "MockSky":
        sim = target.get("sim") or {}
        seed = int(sim.get("seed", _stable_seed(target["name"], target["ra_deg"], target["dec_deg"])))
        rng = np.random.default_rng(seed + 17)
        if "pointing_error_arcsec" in sim:
            perr = tuple(float(v) for v in sim["pointing_error_arcsec"])
        else:
            perr = tuple(float(v) for v in rng.uniform(-45.0, 45.0, size=2))
        if sim.get("epoch0_iso"):
            epoch0 = datetime.fromisoformat(sim["epoch0_iso"])
            if epoch0.tzinfo is None:
                epoch0 = epoch0.replace(tzinfo=timezone.utc)
        else:
            epoch0 = now or datetime.now(timezone.utc)
        return cls(
            fwhm_px=float(sim.get("fwhm_px", FWHM_PX)),
            commanded_ra=float(target["ra_deg"]),
            commanded_dec=float(target["dec_deg"]),
            seed=seed,
            pointing_error_arcsec=perr,  # type: ignore[arg-type]
            epoch0=epoch0,
            scene=scene or SkyScene(),
        )

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #
    @property
    def true_center(self) -> tuple[float, float]:
        d_ra, d_dec = self.pointing_error_arcsec
        dec = self.commanded_dec + d_dec / 3600.0
        ra = self.commanded_ra + d_ra / 3600.0 / max(math.cos(math.radians(self.commanded_dec)), 1e-6)
        return ra % 360.0, dec

    def true_wcs(self):
        ra, dec = self.true_center
        return make_tan_wcs(ra, dec, self.shape, self.pixel_scale)

    def commanded_wcs(self):
        return make_tan_wcs(self.commanded_ra, self.commanded_dec, self.shape, self.pixel_scale)

    # ------------------------------------------------------------------ #
    # Stars
    # ------------------------------------------------------------------ #
    def stars(self) -> dict[str, np.ndarray]:
        """Pixel positions (true frame) and G magnitudes of every star, cached."""
        if self._stars:
            return self._stars
        ny, nx = self.shape
        rng = np.random.default_rng(self.seed)
        area = (nx + 2 * FRAME_MARGIN_PX) * (ny + 2 * FRAME_MARGIN_PX)
        n = int(STAR_DENSITY_PER_PX2 * area)
        x = rng.uniform(-FRAME_MARGIN_PX, nx + FRAME_MARGIN_PX, n)
        y = rng.uniform(-FRAME_MARGIN_PX, ny + FRAME_MARGIN_PX, n)
        lo, hi = STAR_MAG_RANGE
        c0, c1 = 10 ** (0.35 * lo), 10 ** (0.35 * hi)  # dN/dm ~ 10^(0.35 m)
        mag = np.log10(c0 + rng.uniform(0, 1, n) * (c1 - c0)) / 0.35
        self._stars = {"x": x, "y": y, "mag": mag}
        return self._stars

    def star_catalog(self) -> dict[str, np.ndarray]:
        """The same stars as ICRS coordinates -- what an offline Gaia query returns.

        Real stars the scene adds (e.g. a variable or transit host) are included: as far as a
        catalogue is concerned they are ordinary stars.
        """
        s = self.stars()
        x, y, mag = s["x"], s["y"], s["mag"]
        extra = self.scene.catalog_stars(self)
        if extra:
            ex, ey, em = (np.array(col, dtype=float) for col in zip(*extra))
            x, y, mag = np.append(x, ex), np.append(y, ey), np.append(mag, em)
        ra, dec = self.true_wcs().all_pix2world(x, y, 0)
        return {"ra": np.asarray(ra) % 360.0, "dec": np.asarray(dec), "mag": mag}

    def pixel_to_radec(self, x: float, y: float) -> tuple[float, float]:
        """True-frame pixel -> ICRS degrees (helper for scenes and their offline stand-ins)."""
        ra, dec = self.true_wcs().all_pix2world([x], [y], 0)
        return float(ra[0]) % 360.0, float(dec[0])

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    @property
    def sigma_px(self) -> float:
        return self.fwhm_px / 2.3548

    def stamp(self, img: np.ndarray, x: float, y: float, counts: float, radius: int | None = None) -> None:
        """Add a Gaussian star of ``counts`` total ADU centred on (x, y) to ``img`` in place."""
        sigma = self.sigma_px
        if radius is None:
            radius = max(8, int(math.ceil(3.0 * sigma)))
        ny, nx = img.shape
        x0, x1 = int(math.floor(x)) - radius, int(math.floor(x)) + radius + 1
        y0, y1 = int(math.floor(y)) - radius, int(math.floor(y)) + radius + 1
        if x1 <= 0 or y1 <= 0 or x0 >= nx or y0 >= ny:
            return
        x0c, x1c, y0c, y1c = max(x0, 0), min(x1, nx), max(y0, 0), min(y1, ny)
        xs = np.arange(x0c, x1c)[None, :]
        ys = np.arange(y0c, y1c)[:, None]
        r2 = (xs - x) ** 2 + (ys - y) ** 2
        img[y0c:y1c, x0c:x1c] += counts / (2 * math.pi * sigma**2) * np.exp(-r2 / (2 * sigma**2))

    def render(self, start: datetime, exposure_s: float, frame_index: int) -> np.ndarray:
        ny, nx = self.shape
        signal = np.full((ny, nx), SKY_BACKGROUND_ADU, dtype=np.float64)

        # Gentle vignetting-like gradient so background modelling is exercised.
        yy, xx = np.mgrid[0:ny, 0:nx]
        signal *= 1.0 + 0.08 * ((xx - nx / 2) / nx) + 0.05 * ((yy - ny / 2) / ny)

        mid = start + timedelta(seconds=exposure_s / 2.0)
        ox, oy = self.scene.pointing_offset_px(self, mid)
        transparency = self.scene.transparency(self, mid, frame_index)

        s = self.stars()
        for x, y, m in zip(s["x"], s["y"], s["mag"]):
            x, y = x + ox, y + oy
            if -10 < x < nx + 10 and -10 < y < ny + 10:
                self.stamp(signal, x, y, counts_for_mag(m, exposure_s) * transparency)

        self.scene.draw(
            self,
            signal,
            FrameContext(
                start=start,
                mid=mid,
                exposure_s=exposure_s,
                frame_index=frame_index,
                offset_px=(ox, oy),
                transparency=transparency,
            ),
        )

        rng = np.random.default_rng(self.seed * 1000 + frame_index)
        img = rng.poisson(np.clip(signal, 0, None)).astype(np.float64)
        img += rng.normal(0.0, READ_NOISE_ADU, size=img.shape)

        # Fixed hot pixels (same in every frame) -- should be rejected, not tracked.
        hrng = np.random.default_rng(self.seed + 99)
        hx = hrng.integers(20, nx - 20, 15)
        hy = hrng.integers(20, ny - 20, 15)
        img[hy, hx] += 3000.0
        return np.clip(img, 0, 65535).astype(np.uint16)

    def header(
        self,
        start: datetime,
        exposure_s: float,
        gain: int,
        filt: str,
        frame_index: int,
        object_name: str,
    ) -> fits.Header:
        ny, nx = self.shape
        scale = self.pixel_scale / 3600.0
        hdr = fits.Header()
        hdr["DATE-OBS"] = (start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], "UTC exposure start")
        hdr["EXPTIME"] = (float(exposure_s), "[s] exposure time")
        hdr["GAIN"] = (int(gain), "camera gain setting")
        hdr["FILTER"] = (str(filt), "filter")
        hdr["OBJECT"] = (object_name, "target name")
        hdr["TELESCOP"] = "Seestar S50 (MOCK)"
        hdr["INSTRUME"] = "Synthetic IMX462"
        hdr["RA"] = (self.commanded_ra, "[deg] commanded pointing")
        hdr["DEC"] = (self.commanded_dec, "[deg] commanded pointing")
        hdr["PIXSCALE"] = (self.pixel_scale, "[arcsec/px] nominal")
        hdr["CTYPE1"], hdr["CTYPE2"] = "RA---TAN", "DEC--TAN"
        hdr["CRVAL1"], hdr["CRVAL2"] = self.commanded_ra, self.commanded_dec
        hdr["CRPIX1"], hdr["CRPIX2"] = (nx + 1) / 2.0, (ny + 1) / 2.0
        hdr["CD1_1"], hdr["CD1_2"], hdr["CD2_1"], hdr["CD2_2"] = -scale, 0.0, 0.0, scale
        hdr["CUNIT1"], hdr["CUNIT2"] = "deg", "deg"
        hdr["FRAMENUM"] = (frame_index, "frame index within sequence")
        hdr["SIMULATD"] = (True, "synthetic data -- not a real observation")
        hdr["SIM_PERA"] = (self.pointing_error_arcsec[0], "[arcsec] simulated pointing error (truth)")
        hdr["SIM_PEDE"] = (self.pointing_error_arcsec[1], "[arcsec] simulated pointing error (truth)")
        return hdr
