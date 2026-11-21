"""From a planned observation to a mission, and back to the plan.

A *plan* (``planned_observations``, made from the dashboard's Opportunities page) is a note of intent.  This
module is the thin, person-driven hand-off from that note to a mission:

1. :func:`check_plan` looks at the plan again just before it is started: has the window closed, has the
   schedule moved (the plugin's ``recheck_planned_target``), and will the sky suit it (Sun, altitude, Moon).
2. ``python main.py --from-plan ID`` builds the mission from the plan's target (the graph's
   ``generate_hypothesis`` reads it via ``state["plan_id"]``), links it with ``Database.start_planned`` and runs it.
3. :func:`settle_plan` records how it ended: ``DONE`` if the target was observed, ``MISSED`` if not.

A finished plan is not started again. Planning that same window once more records a new plan, and that new
plan is what ``--from-plan`` runs. Each attempt keeps its own mission.

Nothing here starts a mission on its own.  A person runs the command.  ``python main.py --schedule``
(:mod:`core.scheduler`) will start one only with ``--confirm``, and self-start stays off until the interlocks
and parking have been confirmed on the mount.  A *simulated* run (``--simulate``) is a
rehearsal of the same steps and leaves the plan exactly as it was.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import Settings
from core import interlocks
from core.base_plugin import BaseExperimentPlugin
from core.clock import parse_time
from db.database import Database

log = logging.getLogger(__name__)


class PlanError(Exception):
    """The plan cannot be started (unknown, cancelled, finished, or its window has closed)."""


@dataclass
class PlanCheck:
    target: dict[str, Any]
    #: Reasons the plan can no longer be observed.  A real run is refused if there are any.
    blockers: list[str] = field(default_factory=list)
    #: Things a person should know before starting: the schedule moved, the sky is poor, weather is unknown.
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def _site(settings: Settings) -> tuple[float, float, float]:
    return (settings.site_latitude_deg, settings.site_longitude_deg, settings.site_elevation_m)


def check_plan(plan: dict, plugin: BaseExperimentPlugin, settings: Settings, now: datetime | None = None) -> PlanCheck:
    """Re-check ``plan`` before a mission is built from it.  Reads the cached lists only; never the network."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target = dict(plan["target"])
    check = PlanCheck(target=target)

    try:
        result = plugin.recheck_planned_target(target) or {}
        if result.get("target"):
            check.target = target = dict(result["target"])
        check.warnings += [str(n) for n in result.get("notes") or []]
    except Exception as exc:  # noqa: BLE001 - a failed re-check must not stop a person starting a plan, only be said
        log.warning("Re-checking plan #%s failed: %s", plan.get("id"), exc)
        check.warnings.append(f"The schedule could not be re-checked ({exc}); the stored times are used.")

    end = parse_time(target["window_end"])
    if end <= now:
        check.blockers.append(f"its observing window closed at {end.strftime('%Y-%m-%d %H:%M UTC')}")
        return check
    start = parse_time(target["window_start"])
    if start > now:
        hours = (start - now).total_seconds() / 3600.0
        check.warnings.append(
            f"The window opens in {hours:.1f} h. A mission started now waits for it (and holds this process until then)."
        )
    check.warnings += interlocks.window_notes(
        target, now, _site(settings), interlocks.limits_from_settings(settings), float(settings.max_sun_altitude_deg)
    )
    check.warnings.append("Weather is not known until the night; the mission checks it before observing.")
    return check


def settle_plan(db: Database, plan_id: int, final: dict) -> tuple[str, str] | None:
    """Record how the mission built from plan ``plan_id`` ended.  Returns ``(status, outcome)``, or ``None`` if not applicable.

    ``DONE`` when the mission observed the planned target; otherwise ``MISSED`` with the reason (the window closed,
    the weather or site limits never cleared, or the mission failed).
    """
    plan = db.get_planned(plan_id)
    if plan is None or plan["status"] != "STARTED":
        return None
    name = plan["target_name"]
    observed = any((o.get("target") or {}).get("name") == name for o in final.get("observations") or [])
    if observed and final.get("status") == "COMPLETE":
        status, outcome = "DONE", f"Observed in mission {final.get('mission_id', plan['mission_id'])}."
    elif observed:  # observed, but the mission did not finish cleanly afterwards: the data exists, say so
        status, outcome = "DONE", f"Observed in mission {final.get('mission_id', plan['mission_id'])}, which ended {final.get('status')}."
    else:
        why = next((s["reason"] for s in final.get("skipped") or [] if s.get("name") == name), None)
        why = why or "; ".join(final.get("errors") or []) or f"the mission ended {final.get('status')} without observing it"
        status, outcome = "MISSED", f"Not observed: {why}."
    db.finish_planned(plan_id, status, outcome)
    return status, outcome
