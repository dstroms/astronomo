"""Simulated sky for a variable-star night: one star that may eclipse, plus a realistic field.

The variable is a real catalogue star as far as the offline Gaia is concerned (its out-of-eclipse
magnitude).  Whole-field drift and transparency are common to every star, so differential photometry
can remove them.  Only the variable's own eclipse is not.

``target["sim"]`` keys (all optional)::

    "eclipse": {"dx_px", "dy_px", "mag", "depth", "t_mid_s", "duration_s", "ingress_s"}
               depth is a fractional flux drop (0.3 is about 0.4 mag); omit or use depth 0 for a steady star
    "drift_px_per_hr": [dx, dy]
    "transparency_trend_per_hr": float
    "transparency_jitter": float
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from plugins.transit_finder.transit_model import transit_flux
from tools.mock_sky import FrameContext, MockSky, SkyScene, counts_for_mag


class VariableScene(SkyScene):
    def __init__(
        self,
        eclipse: dict | None = None,
        drift_px_per_hr: tuple[float, float] = (0.0, 0.0),
        transparency_trend_per_hr: float = 0.0,
        transparency_jitter: float = 0.0,
    ):
        self.eclipse = eclipse or None
        self.drift_px_per_hr = (float(drift_px_per_hr[0]), float(drift_px_per_hr[1]))
        self.transparency_trend_per_hr = float(transparency_trend_per_hr)
        self.transparency_jitter = float(transparency_jitter)

    @classmethod
    def from_target(cls, target: dict) -> "VariableScene":
        sim = target.get("sim") or {}
        drift = sim.get("drift_px_per_hr") or (0.0, 0.0)
        return cls(
            eclipse=sim.get("eclipse"),
            drift_px_per_hr=(float(drift[0]), float(drift[1])),
            transparency_trend_per_hr=float(sim.get("transparency_trend_per_hr", 0.0)),
            transparency_jitter=float(sim.get("transparency_jitter", 0.0)),
        )

    def star_pixel(self, sky: MockSky) -> tuple[float, float] | None:
        if not self.eclipse:
            return None
        ny, nx = sky.shape
        return (
            (nx - 1) / 2.0 + float(self.eclipse.get("dx_px", 0.0)),
            (ny - 1) / 2.0 + float(self.eclipse.get("dy_px", 0.0)),
        )

    def relative_flux(self, sky: MockSky, when: datetime) -> float:
        """Flux relative to the out-of-eclipse level.  A steady star stays at 1."""
        if not self.eclipse or not float(self.eclipse.get("depth") or 0.0):
            return 1.0
        ec = self.eclipse
        dt = (when - sky.epoch0).total_seconds()
        return float(transit_flux(dt, float(ec["t_mid_s"]), float(ec["depth"]), float(ec["duration_s"]), float(ec["ingress_s"])))

    def pointing_offset_px(self, sky: MockSky, when: datetime) -> tuple[float, float]:
        dt_hr = (when - sky.epoch0).total_seconds() / 3600.0
        return self.drift_px_per_hr[0] * dt_hr, self.drift_px_per_hr[1] * dt_hr

    def transparency(self, sky: MockSky, when: datetime, frame_index: int) -> float:
        dt_hr = (when - sky.epoch0).total_seconds() / 3600.0
        factor = 1.0 + self.transparency_trend_per_hr * dt_hr
        if self.transparency_jitter:
            rng = np.random.default_rng(sky.seed * 1000 + frame_index + 7_000_003)
            factor *= 1.0 + self.transparency_jitter * float(rng.normal())
        return factor

    def catalog_stars(self, sky: MockSky) -> list[tuple[float, float, float]]:
        pix = self.star_pixel(sky)
        if pix is None:
            return []
        return [(pix[0], pix[1], float(self.eclipse["mag"]))]  # type: ignore[index]

    def draw(self, sky: MockSky, signal: np.ndarray, ctx: FrameContext) -> None:
        pix = self.star_pixel(sky)
        if pix is None:
            return
        ox, oy = ctx.offset_px
        counts = counts_for_mag(float(self.eclipse["mag"]), ctx.exposure_s)  # type: ignore[index]
        sky.stamp(signal, pix[0] + ox, pix[1] + oy, counts * ctx.transparency * self.relative_flux(sky, ctx.mid))
