"""Simulated sky content for the transit plugin: a transiting host plus a realistic night.

All transit-specific simulation lives here, not in ``tools/``:

* a bright *host star* whose flux follows :func:`~plugins.transit_finder.transit_model.transit_flux`
  (it is also reported to the offline Gaia, being a real star),
* whole-field systematics every photometric night has: tracking drift, an atmospheric
  transparency trend and frame-to-frame jitter, all common to every star so differential
  photometry can remove them,
* a small host-only baseline slope (differential extinction) that the fit must absorb.

``target["sim"]`` keys read by this scene (all optional)::

    "transit": {"dx_px": float, "dy_px": float,  # host offset from image centre at epoch0
                "mag": float,                    # host magnitude (stars here are G ~ 11-17.5)
                "depth": float,                  # fractional dip at mid-transit, e.g. 0.014
                "t_mid_s": float,                # mid-transit, seconds after epoch0
                "duration_s": float,             # first-to-fourth contact
                "ingress_s": float,              # ingress (= egress) duration
                "baseline_slope_per_hr": float}, # host-only fractional drift per hour
    "drift_px_per_hr": [dx, dy],                 # whole-field pointing drift
    "transparency_trend_per_hr": float,          # fractional flux change per hour, all stars
    "transparency_jitter": float                 # rms fractional flux change per frame, all stars

A simulated transit run is time-compressed: frames are spaced minutes apart on the *simulated*
clock (``interval_s`` in the exposure plan) while the wall clock runs fast.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from plugins.transit_finder.transit_model import transit_flux
from tools.mock_sky import FrameContext, MockSky, SkyScene, counts_for_mag


class TransitScene(SkyScene):
    def __init__(
        self,
        transit: dict | None = None,
        drift_px_per_hr: tuple[float, float] = (0.0, 0.0),
        transparency_trend_per_hr: float = 0.0,
        transparency_jitter: float = 0.0,
    ):
        self.transit = transit or None
        self.drift_px_per_hr = (float(drift_px_per_hr[0]), float(drift_px_per_hr[1]))
        self.transparency_trend_per_hr = float(transparency_trend_per_hr)
        self.transparency_jitter = float(transparency_jitter)

    @classmethod
    def from_target(cls, target: dict) -> "TransitScene":
        sim = target.get("sim") or {}
        return cls(
            transit=sim.get("transit"),
            drift_px_per_hr=tuple(float(v) for v in sim.get("drift_px_per_hr", (0.0, 0.0))),  # type: ignore[arg-type]
            transparency_trend_per_hr=float(sim.get("transparency_trend_per_hr", 0.0)),
            transparency_jitter=float(sim.get("transparency_jitter", 0.0)),
        )

    # -- the host star ------------------------------------------------------ #
    def host_pixel(self, sky: MockSky) -> tuple[float, float] | None:
        """Pixel position of the transiting host at epoch0 (true frame), or ``None``."""
        if not self.transit:
            return None
        ny, nx = sky.shape
        return (
            (nx - 1) / 2.0 + float(self.transit.get("dx_px", 0.0)),
            (ny - 1) / 2.0 + float(self.transit.get("dy_px", 0.0)),
        )

    def host_radec(self, sky: MockSky) -> tuple[float, float] | None:
        pix = self.host_pixel(sky)
        return None if pix is None else sky.pixel_to_radec(*pix)

    def host_relative_flux(self, sky: MockSky, when: datetime) -> float:
        """Host flux relative to its out-of-transit level at ``when`` (transit + baseline slope)."""
        if not self.transit:
            return 1.0
        tr = self.transit
        dt = (when - sky.epoch0).total_seconds()
        flux = float(
            transit_flux(dt, float(tr["t_mid_s"]), float(tr["depth"]), float(tr["duration_s"]), float(tr["ingress_s"]))
        )
        return flux * (1.0 + float(tr.get("baseline_slope_per_hr", 0.0)) * dt / 3600.0)

    # -- SkyScene hooks ------------------------------------------------------ #
    def pointing_offset_px(self, sky: MockSky, when: datetime) -> tuple[float, float]:
        dt_hr = (when - sky.epoch0).total_seconds() / 3600.0
        return self.drift_px_per_hr[0] * dt_hr, self.drift_px_per_hr[1] * dt_hr

    def transparency(self, sky: MockSky, when: datetime, frame_index: int) -> float:
        dt_hr = (when - sky.epoch0).total_seconds() / 3600.0
        t = 1.0 + self.transparency_trend_per_hr * dt_hr
        if self.transparency_jitter:
            jrng = np.random.default_rng(sky.seed * 1000 + frame_index + 7_000_003)
            t *= 1.0 + self.transparency_jitter * float(jrng.normal())
        return t

    def catalog_stars(self, sky: MockSky) -> list[tuple[float, float, float]]:
        host = self.host_pixel(sky)
        return [] if host is None else [(host[0], host[1], float(self.transit["mag"]))]  # type: ignore[index]

    def draw(self, sky: MockSky, signal: np.ndarray, ctx: FrameContext) -> None:
        host = self.host_pixel(sky)
        if host is None:
            return
        ox, oy = ctx.offset_px
        counts = counts_for_mag(float(self.transit["mag"]), ctx.exposure_s)  # type: ignore[index]
        sky.stamp(signal, host[0] + ox, host[1] + oy, counts * ctx.transparency * self.host_relative_flux(sky, ctx.mid))
