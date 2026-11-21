"""Simulated sky content for the Rubin follow-up plugin: a transient plus a realistic night.

All follow-up-specific simulation lives here, not in ``tools/``.  ``target["sim"]`` keys read by
this scene (all optional)::

    "transient": {"dx_px": float, "dy_px": float,  # position relative to the image centre, epoch0
                  "mag": float | None,             # brightness *now* (G-like); None = nothing there
                  "in_gaia": bool}                 # True: it is an ordinary catalogued star
    "drift_px_per_hr": [dx, dy]                    # whole-field pointing drift
    "transparency_jitter": float                   # rms fractional flux change per frame, all stars

A transient is not in Gaia (that is what makes it a transient), so by default it is drawn on the
camera but *not* reported to the offline catalogue.  ``in_gaia`` flips that, which is how the
"this alert is just a known star" case is simulated.  ``mag: None`` simulates an alert whose source
has faded below what we can see (or was never real, or was an asteroid that has moved on).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from tools.mock_sky import FrameContext, MockSky, SkyScene, counts_for_mag


class TransientScene(SkyScene):
    def __init__(
        self,
        transient: dict | None = None,
        drift_px_per_hr: tuple[float, float] = (0.0, 0.0),
        transparency_jitter: float = 0.0,
    ):
        self.transient = transient or None
        self.drift_px_per_hr = (float(drift_px_per_hr[0]), float(drift_px_per_hr[1]))
        self.transparency_jitter = float(transparency_jitter)

    @classmethod
    def from_target(cls, target: dict) -> "TransientScene":
        sim = target.get("sim") or {}
        return cls(
            transient=sim.get("transient"),
            drift_px_per_hr=tuple(float(v) for v in sim.get("drift_px_per_hr", (0.0, 0.0))),  # type: ignore[arg-type]
            transparency_jitter=float(sim.get("transparency_jitter", 0.0)),
        )

    # -- the transient ------------------------------------------------------ #
    def pixel(self, sky: MockSky) -> tuple[float, float] | None:
        """True-frame pixel of the transient at epoch0, or ``None`` if the scene has none."""
        if not self.transient:
            return None
        ny, nx = sky.shape
        return (nx - 1) / 2.0 + float(self.transient.get("dx_px", 0.0)), (ny - 1) / 2.0 + float(self.transient.get("dy_px", 0.0))

    def radec(self, sky: MockSky) -> tuple[float, float] | None:
        pix = self.pixel(sky)
        return None if pix is None else sky.pixel_to_radec(*pix)

    def visible_mag(self) -> float | None:
        if not self.transient or self.transient.get("mag") is None:
            return None
        return float(self.transient["mag"])

    # -- SkyScene hooks ------------------------------------------------------ #
    def pointing_offset_px(self, sky: MockSky, when: datetime) -> tuple[float, float]:
        hours = (when - sky.epoch0).total_seconds() / 3600.0
        return self.drift_px_per_hr[0] * hours, self.drift_px_per_hr[1] * hours

    def transparency(self, sky: MockSky, when: datetime, frame_index: int) -> float:
        if not self.transparency_jitter:
            return 1.0
        jrng = np.random.default_rng(sky.seed * 1000 + frame_index + 7_000_003)
        return 1.0 + self.transparency_jitter * float(jrng.normal())

    def catalog_stars(self, sky: MockSky) -> list[tuple[float, float, float]]:
        pix, mag = self.pixel(sky), self.visible_mag()
        if pix is None or mag is None or not (self.transient or {}).get("in_gaia"):
            return []
        return [(pix[0], pix[1], mag)]

    def draw(self, sky: MockSky, signal: np.ndarray, ctx: FrameContext) -> None:
        pix, mag = self.pixel(sky), self.visible_mag()
        if pix is None or mag is None:
            return
        ox, oy = ctx.offset_px
        sky.stamp(signal, pix[0] + ox, pix[1] + oy, counts_for_mag(mag, ctx.exposure_s) * ctx.transparency)
