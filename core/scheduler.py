"""When a plan's window is open, and when a sleeping mission should be woken.

Nothing here starts a mission by itself. A person confirms a plan (``python main.py --schedule --confirm ID``).
Starting on its own is a separate setting, off by default, and refused until ``ASTRONOMO_UNATTENDED_CONFIRMED``
is set, which is the record that weather, Sun, Moon, altitude and safe parking have been seen on this mount.
Dew, battery and free space are checked before either kind of start. An unknown battery blocks only the
unattended start: a person who is there can still confirm.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from config import Settings
from core.clock import parse_time
from core.readiness import Reading, battery_reading, dew_reading, disk_reading, start_blockers
from tools.telescope import SeestarController, TelescopeError
from tools.weather import conditions_from_saved

log = logging.getLogger(__name__)

SELF_START_REFUSAL = (
    "Self-start stays off until weather, Sun, Moon, altitude and safe parking have been confirmed on this mount."
)


@dataclass(frozen=True)
class Action:
    kind: str  # "start" | "awaiting" | "refused" | "resume"
    reason: str
    plan_id: int | None = None
    mission_id: str | None = None
    target_name: str = ""


def _window_open(plan: dict, now: datetime) -> bool:
    if plan.get("status") != "PLANNED":
        return False
    try:
        start, end = parse_time(plan["window_start"]), parse_time(plan["window_end"])
    except (KeyError, TypeError, ValueError):
        return False
    return start <= now < end


def classify_plan(
    plan: dict,
    now: datetime,
    *,
    confirmed: bool,
    self_start: bool,
    unattended_confirmed: bool,
    blockers: list[str],
) -> Action | None:
    """What to do with one plan that might be due. None means it is not in its window."""
    if not _window_open(plan, now):
        return None
    name = str(plan.get("target_name") or "")
    plan_id = int(plan["id"])
    if confirmed:
        if blockers:
            return Action("refused", "; ".join(blockers), plan_id=plan_id, target_name=name)
        return Action("start", "a person confirmed this start", plan_id=plan_id, target_name=name)
    if self_start and not unattended_confirmed:
        return Action("refused", SELF_START_REFUSAL, plan_id=plan_id, target_name=name)
    if self_start and unattended_confirmed:
        if blockers:
            return Action("refused", "; ".join(blockers), plan_id=plan_id, target_name=name)
        return Action("start", "self-start is on and this mount has been confirmed", plan_id=plan_id, target_name=name)
    return Action(
        "awaiting",
        f"the window is open; a person starts it with python main.py --schedule --confirm {plan_id}",
        plan_id=plan_id, target_name=name,
    )


def due_plans(
    plans: list[dict],
    now: datetime,
    *,
    confirm_ids: set[int],
    self_start: bool,
    unattended_confirmed: bool,
    person_blockers: list[str],
    auto_blockers: list[str],
) -> list[Action]:
    actions = []
    for plan in plans:
        confirmed = int(plan["id"]) in confirm_ids
        # An unknown battery blocks an unattended start only. A confirmed start uses the shorter list.
        blockers = auto_blockers if self_start and not confirmed else person_blockers
        action = classify_plan(
            plan, now, confirmed=confirmed, self_start=self_start,
            unattended_confirmed=unattended_confirmed, blockers=blockers,
        )
        if action is not None:
            actions.append(action)
    return actions


def wakeable(missions: list[dict], now: datetime) -> list[Action]:
    """Sleeping missions whose wake time has passed, and which nobody stopped."""
    actions = []
    for mission in missions:
        if mission.get("status") != "SLEEP" or mission.get("stop_requested"):
            continue
        wake = mission.get("wake_at")
        if not wake:
            continue
        try:
            when = parse_time(wake)
        except (TypeError, ValueError):
            continue
        if when <= now:
            actions.append(Action(
                "resume", f"the weather wait until {when.strftime('%Y-%m-%d %H:%M UTC')} has passed",
                mission_id=str(mission["id"]),
            ))
    return actions


def readiness(settings: Settings, telescope: SeestarController | None, now: datetime | None = None) -> list[Reading]:
    """Disk, battery and dew, in that order. Dew comes from the saved forecast when it covers ``now``."""
    now = now or datetime.now(timezone.utc)
    disk = disk_reading(settings.data_dir, int(settings.min_free_gb * 1e9))
    percent = None if telescope is None else telescope.battery_percent()
    battery = battery_reading(percent, settings.min_battery_pct)
    saved = conditions_from_saved(settings, settings.site_latitude_deg, settings.site_longitude_deg, now)
    if saved is None:
        dew = dew_reading(None, None, settings.dew_margin_c)
    else:
        dew = dew_reading(saved.temperature_c, saved.humidity_pct, settings.dew_margin_c)
    return [disk, battery, dew]


def blockers_for(settings: Settings, telescope: SeestarController | None, *, unattended: bool, now: datetime | None = None) -> list[str]:
    return start_blockers(readiness(settings, telescope, now), unattended=unattended)


def park_and_stop(telescope: SeestarController, db, mission_id: str) -> str:
    """Park, then mark the mission stopped so nothing resumes it. Used when no other process owns it."""
    try:
        if not telescope.mock and not telescope.connected:
            telescope.connect()
        telescope.park()
    except TelescopeError:
        log.exception("Could not park mission %s", mission_id)
    finally:
        if telescope.connected:
            try:
                telescope.disconnect()
            except TelescopeError:
                log.exception("Could not disconnect after stopping mission %s", mission_id)
    message = "Stopped by the operator. The telescope was parked and will stay stopped."
    db.update_mission(mission_id, status="STOPPED", error=message, wake_at=None, stop_requested=True)
    return message
