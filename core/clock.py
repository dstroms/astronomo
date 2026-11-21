"""Mission clocks: how the harness lives with surveys whose cadence is hours or days.

A telescope mission runs on the :class:`SystemClock` -- "now" is the wall clock and a visit that
is not due yet is simply waited for.  An *archive* mission replays history (a 2019 ZTF night, a
2008 transit), so "now" is **survey time**: a :class:`ReplayClock` that starts at the first visit
and advances ``speed`` survey-seconds per wall-second.

======  =====================================================================
speed   meaning
======  =====================================================================
0       *jump*: waiting for the next visit costs no real time (the clock just
        moves to it).  Fast reproducible runs and tests.
1       real time: a 20-day gap takes 20 days (what an unattended long-cadence
        mission on a real telescope does).
3600    one survey hour per second: a watchable demo of a multi-night mission.
======  =====================================================================

Clocks are persisted per mission (``Mission.clock``) so a resumed process continues from where the
survey clock stood.  A resumed ReplayClock is re-anchored to the *current* wall time: the replay
is paused while no process owns the mission, it does not "catch up" for the downtime.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

TickFn = Callable[[datetime], None]

#: A wait is over when it is this close (wall seconds); avoids spinning on float rounding.
_EPS_S = 1e-3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | datetime) -> datetime:
    """ISO-8601 (``Z`` or offset, or naive meaning UTC) -> timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class Clock:
    """Interface: ``now()``, ``wait_until()``, ``state()``."""

    kind = "base"
    speed = 1.0

    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError

    def wait_until(
        self, when: datetime, *, sleep_fn: Callable[[float], None] = time.sleep, tick_s: float = 1.0, on_tick: TickFn | None = None
    ) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def state(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    """Wall-clock time; waiting really waits (in ``tick_s`` slices so callers can heartbeat)."""

    kind = "system"
    speed = 1.0

    def __init__(self, wall: Callable[[], float] = time.time):
        self._wall = wall

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._wall(), tz=timezone.utc)

    def wait_until(self, when, *, sleep_fn=time.sleep, tick_s=1.0, on_tick=None) -> None:
        target = parse_time(when).timestamp()
        while True:
            remaining = target - self._wall()
            if remaining <= _EPS_S:
                return
            sleep_fn(min(remaining, max(tick_s, 0.01)))
            if on_tick:
                on_tick(self.now())

    def state(self) -> dict[str, Any]:
        return {"kind": "system"}


class ReplayClock(Clock):
    """Survey time for archive replays (see the module docstring for ``speed``)."""

    kind = "replay"

    def __init__(self, origin: datetime, speed: float = 0.0, wall: Callable[[], float] = time.time):
        self.speed = max(0.0, float(speed))
        self._wall = wall
        self._sim_ts = parse_time(origin).timestamp()  # survey time at ``_wall_ts``
        self._wall_ts = wall()

    def _now_ts(self) -> float:
        if self.speed == 0.0:
            return self._sim_ts
        return self._sim_ts + (self._wall() - self._wall_ts) * self.speed

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._now_ts(), tz=timezone.utc)

    def wait_until(self, when, *, sleep_fn=time.sleep, tick_s=1.0, on_tick=None) -> None:
        target = parse_time(when).timestamp()
        if self.speed == 0.0:  # jump
            if target > self._sim_ts:
                self._sim_ts = target
                if on_tick:
                    on_tick(self.now())
            return
        while True:
            remaining_survey = target - self._now_ts()
            if remaining_survey <= _EPS_S * self.speed:  # within a millisecond of wall time: due
                return
            sleep_fn(min(remaining_survey / self.speed, max(tick_s, 0.01)))
            if on_tick:
                on_tick(self.now())

    def state(self) -> dict[str, Any]:
        return {"kind": "replay", "speed": self.speed, "sim_now": self.now().isoformat()}


def clock_from_state(state: dict[str, Any] | None, *, fallback: Clock | None = None, wall: Callable[[], float] = time.time) -> Clock:
    """Rebuild a persisted clock (``ReplayClock`` is re-anchored to the current wall time)."""
    if not state:
        return fallback or SystemClock(wall)
    if state.get("kind") == "replay":
        return ReplayClock(parse_time(state["sim_now"]), float(state.get("speed", 0.0)), wall)
    return SystemClock(wall)


def describe_speed(speed: float) -> str:
    if speed <= 0:
        return "jump (no real waiting)"
    if speed == 1:
        return "real time"
    return f"{speed:g}x"
