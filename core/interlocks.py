"""Site interlocks: is it safe and sensible to point at this target right now?

The weather gate (``check_weather`` in the graph) already answers "is the sky usable", and the Sun's altitude
keeps the optics out of daylight.  This module answers the questions that depend on the *target and the time*:

* **window**: a target may carry ``window_start`` and ``window_end`` (a planned transit, for example).  Before the
  window opens the mission waits.  After it closes the target is **skipped**, because a window that has closed
  is not coming back.  The check is made when a target is about to start; a sequence already under way is not
  cut off when its window ends (the telescope has no mid-sequence stop for that yet).
* **altitude**: at least ``min_altitude_deg``.  A single minimum altitude is the whole horizon model for now; a
  real obstruction mask is later work.  Also at most ``max_altitude_deg``: an alt-az mount turns the field
  fastest near the zenith, so a target there waits until it has moved on.
* **Sun**: at least ``min_sun_separation_deg`` from the Sun, whatever the darkness setting says.
* **Moon**: while the Moon is up, at least ``max(moon_floor_deg, moon_avoid_deg x illumination)`` degrees from
  it: a full Moon needs the most room, a thin one the floor.

The graph treats a failed check other than a closed window as *waiting*: the target may rise, or the Moon set,
and the mission sleeps and checks again.  Nothing here moves hardware.  The rules are pure functions of the
target, the time and the settings, so they are tested without a telescope.  The Opportunities page uses
:func:`limits_from_settings` and :func:`required_moon_separation` too, so a window it calls observable is one
the mission would observe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from config import Settings
from core.clock import parse_time
from core.site import horizon_altitude
from tools.astrometry import sky_conditions_many


@dataclass(frozen=True)
class SiteLimits:
    min_altitude_deg: float = 30.0
    min_sun_separation_deg: float = 30.0
    moon_avoid_deg: float = 60.0
    moon_floor_deg: float = 15.0
    #: ``((azimuth, altitude), ...)``: the lowest altitude the telescope can see at each bearing (``core/site.py``).
    horizon: tuple = ()
    #: Above this the field turns too fast on an alt-az mount.  90 (the default here) is no limit.
    max_altitude_deg: float = 90.0


def limits_from_settings(settings: Settings) -> SiteLimits:
    return SiteLimits(
        min_altitude_deg=float(settings.min_altitude_deg),
        max_altitude_deg=float(settings.max_altitude_deg),
        min_sun_separation_deg=float(settings.min_sun_separation_deg),
        moon_avoid_deg=float(settings.moon_avoid_deg),
        moon_floor_deg=float(settings.moon_floor_deg),
        horizon=tuple(settings.site_horizon),
    )


def minimum_altitude(azimuth_deg: Any, limits: SiteLimits) -> Any:
    """The lowest altitude usable at each azimuth: the site-wide minimum, or the horizon where that is higher."""
    floor = float(limits.min_altitude_deg)
    if not limits.horizon or azimuth_deg is None:
        return floor if np.ndim(azimuth_deg) == 0 else np.full(np.shape(azimuth_deg), floor)
    return np.maximum(floor, horizon_altitude(limits.horizon, azimuth_deg))


def required_moon_separation(moon_alt: Any, moon_illum: Any, limits: SiteLimits) -> Any:
    """Degrees the target must be from the Moon (0 while the Moon is below the horizon).  Works on numbers or arrays."""
    need = np.maximum(limits.moon_floor_deg, limits.moon_avoid_deg * np.asarray(moon_illum, dtype=float))
    return np.where(np.asarray(moon_alt, dtype=float) > 0.0, need, 0.0)


@dataclass
class Verdict:
    """The outcome of :func:`evaluate`.  ``ok`` means point at it now; ``skip`` means do not come back to it."""

    ok: bool
    skip: bool = False
    reasons: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _clock_text(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M UTC")


def evaluate(target: dict, now: datetime, site: tuple[float, float, float], limits: SiteLimits) -> Verdict:
    """Check ``target`` (a plugin target dict) at ``now`` (a timezone-aware instant) from ``site`` (lat, lon, elevation m)."""
    reasons: list[str] = []

    start = parse_time(target["window_start"]) if target.get("window_start") else None
    end = parse_time(target["window_end"]) if target.get("window_end") else None
    if end is not None and now > end:
        return Verdict(
            ok=False, skip=True,
            reasons=[f"its observing window closed at {_clock_text(end)}"],
            facts={"window_start": start.isoformat() if start else None, "window_end": end.isoformat()},
        )
    if start is not None and now < start:
        reasons.append(f"its observing window opens at {_clock_text(start)}")

    lat, lon, elev = site
    sky = sky_conditions_many([target["ra_deg"]], [target["dec_deg"]], lat, lon, elev, [now])
    alt = float(sky["target_alt"][0])
    az = float(sky["target_az"][0]) if "target_az" in sky else None
    need_alt = float(minimum_altitude(az, limits))
    sun_sep = float(sky["sun_sep"][0])
    moon_alt, moon_sep, moon_illum = (float(sky[k][0]) for k in ("moon_alt", "moon_sep", "moon_illum"))
    need = float(required_moon_separation(moon_alt, moon_illum, limits))

    if alt < need_alt:
        where = f" in the direction {az:.0f} deg" if need_alt > limits.min_altitude_deg and az is not None else ""
        reasons.append(f"target altitude {alt:.0f} deg is below the minimum of {need_alt:.0f} deg{where}")
    if alt > limits.max_altitude_deg:
        reasons.append(
            f"target altitude {alt:.0f} deg is above the maximum of {limits.max_altitude_deg:.0f} deg "
            "(the field turns too fast near the zenith)"
        )
    if sun_sep < limits.min_sun_separation_deg:
        reasons.append(f"target is {sun_sep:.0f} deg from the Sun (minimum {limits.min_sun_separation_deg:.0f} deg)")
    if moon_alt > 0.0 and moon_sep < need:
        reasons.append(
            f"target is {moon_sep:.0f} deg from the {moon_illum * 100:.0f}% Moon (needs {need:.0f} deg while the Moon is up)"
        )

    facts = {
        "target_altitude_deg": round(alt, 1), "target_azimuth_deg": None if az is None else round(az, 1),
        "required_altitude_deg": round(need_alt, 1), "sun_separation_deg": round(sun_sep, 1), "moon_altitude_deg": round(moon_alt, 1),
        "moon_separation_deg": round(moon_sep, 1), "moon_illumination": round(moon_illum, 2), "moon_required_separation_deg": round(need, 1),
    }
    return Verdict(ok=not reasons, reasons=reasons, facts=facts)


def window_notes(
    target: dict, now: datetime, site: tuple[float, float, float], limits: SiteLimits, max_sun_alt_deg: float, *, samples: int = 3
) -> list[str]:
    """What the sky will be like across the target's observing window, as sentences (empty if all is well).

    Looks at ``samples`` instants from the later of ``now`` and the window start to the window end, with the same
    rules :func:`evaluate` applies on the night.  Used to warn a person before a plan is started; weather cannot
    be known until the night, so it is not part of this.  A window that has already closed is not described here.
    """
    start = parse_time(target["window_start"]) if target.get("window_start") else now
    end = parse_time(target["window_end"]) if target.get("window_end") else None
    if end is None or end <= now:
        return []
    first = max(start, now)
    times = [first + (end - first) * (i / max(samples - 1, 1)) for i in range(samples)]
    lat, lon, elev = site
    sky = sky_conditions_many([target["ra_deg"]] * len(times), [target["dec_deg"]] * len(times), lat, lon, elev, times)
    need = required_moon_separation(sky["moon_alt"], sky["moon_illum"], limits)
    need_alt = minimum_altitude(sky.get("target_az"), limits) if "target_az" in sky else np.full(len(times), float(limits.min_altitude_deg))
    labels = ["at the start", "in the middle", "at the end"] if samples == 3 else [f"sample {i + 1}" for i in range(samples)]
    notes: list[str] = []
    for i, label in enumerate(labels):
        problems = []
        if sky["sun_alt"][i] > max_sun_alt_deg:
            problems.append(f"the Sun is {sky['sun_alt'][i]:.0f} deg up (limit {max_sun_alt_deg:.0f} deg)")
        if sky["target_alt"][i] < need_alt[i]:
            problems.append(f"the target is only {sky['target_alt'][i]:.0f} deg high (minimum {need_alt[i]:.0f} deg)")
        if sky["target_alt"][i] > limits.max_altitude_deg:
            problems.append(f"the target is {sky['target_alt'][i]:.0f} deg high, near the zenith (maximum {limits.max_altitude_deg:.0f} deg)")
        if sky["sun_sep"][i] < limits.min_sun_separation_deg:
            problems.append(f"the target is {sky['sun_sep'][i]:.0f} deg from the Sun")
        if sky["moon_alt"][i] > 0.0 and sky["moon_sep"][i] < need[i]:
            problems.append(f"the target is {sky['moon_sep'][i]:.0f} deg from the {sky['moon_illum'][i] * 100:.0f}% Moon (needs {need[i]:.0f} deg)")
        if problems:
            notes.append(f"{label} of the window ({times[i].strftime('%Y-%m-%d %H:%M UTC')}): " + "; ".join(problems))
    return notes
