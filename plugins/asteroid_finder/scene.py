"""Simulated sky content for the asteroid plugin: one linearly moving object.

Everything the simulator needs to know about "an asteroid" lives here, not in ``tools/``:

* :meth:`AsteroidScene.draw` renders the mover into each frame (a short trail, since it moves
  during the exposure),
* :meth:`AsteroidScene.ephemeris` is the plugin's *offline stand-in for JPL Horizons*: the
  position and rate of the same mover, in the same sky the camera and offline Gaia use.

``target["sim"]["mover"]``::

    {"dx_px": float, "dy_px": float,    # offset from image centre at epoch0
     "vx_px_s": float, "vy_px_s": float,
     "mag": float}

The speed is deliberately exaggerated (tens of arcsec/min) so motion is visible within a few
10-second frames; real main-belt asteroids need minutes to hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from tools.astrometry import sky_motion
from tools.mock_sky import FrameContext, MockSky, SkyScene, counts_for_mag

DEFAULT_MOVER_MAG = 14.5
TRAIL_SUBSTEPS = 8


class AsteroidScene(SkyScene):
    def __init__(self, mover: dict | None):
        self.mover = mover or None

    @classmethod
    def from_target(cls, target: dict) -> "AsteroidScene":
        return cls((target.get("sim") or {}).get("mover"))

    # -- geometry --------------------------------------------------------- #
    def mover_pixel(self, sky: MockSky, when: datetime) -> tuple[float, float] | None:
        if not self.mover:
            return None
        ny, nx = sky.shape
        dt = (when - sky.epoch0).total_seconds()
        x = (nx - 1) / 2.0 + float(self.mover["dx_px"]) + float(self.mover["vx_px_s"]) * dt
        y = (ny - 1) / 2.0 + float(self.mover["dy_px"]) + float(self.mover["vy_px_s"]) * dt
        return x, y

    def mover_radec(self, sky: MockSky, when: datetime) -> tuple[float, float] | None:
        pix = self.mover_pixel(sky, when)
        return None if pix is None else sky.pixel_to_radec(*pix)

    # -- SkyScene hook ---------------------------------------------------- #
    def draw(self, sky: MockSky, signal, ctx: FrameContext) -> None:
        if not self.mover:
            return
        ox, oy = ctx.offset_px
        total = counts_for_mag(float(self.mover.get("mag", DEFAULT_MOVER_MAG)), ctx.exposure_s)
        for k in range(TRAIL_SUBSTEPS):  # integrate the trail across the exposure
            t = ctx.start + timedelta(seconds=ctx.exposure_s * (k + 0.5) / TRAIL_SUBSTEPS)
            px, py = self.mover_pixel(sky, t)  # type: ignore[misc]
            sky.stamp(signal, px + ox, py + oy, total / TRAIL_SUBSTEPS)

    # -- offline stand-in for JPL Horizons ---------------------------------- #
    def ephemeris(self, sky: MockSky, designation: str, epochs: Sequence[datetime]) -> list[dict[str, Any]]:
        """What Horizons would answer if the mover were a catalogued body (``[]`` if there is none)."""
        if not self.mover:
            return []
        out: list[dict[str, Any]] = []
        for e in epochs:
            pos = self.mover_radec(sky, e)
            ahead = self.mover_radec(sky, e + timedelta(seconds=30))
            behind = self.mover_radec(sky, e - timedelta(seconds=30))
            rate, pa = sky_motion(*behind, *ahead, dt_minutes=1.0)  # type: ignore[misc]
            out.append(
                {
                    "designation": designation,
                    "epoch_iso": e.isoformat(),
                    "ra_deg": pos[0],  # type: ignore[index]
                    "dec_deg": pos[1],  # type: ignore[index]
                    "rate_arcsec_per_min": rate,
                    "pa_deg": pa,
                    "v_mag": float(self.mover.get("mag", DEFAULT_MOVER_MAG)),
                    "delta_au": None,
                    "source": "simulated",
                }
            )
        return out
