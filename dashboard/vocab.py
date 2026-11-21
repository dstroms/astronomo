"""Plain-language vocabulary, colours and lifecycle grouping shared by every dashboard page.

One place decides what a mission *state* is called and which colour it gets, so Tonight, Missions
and the mission page never disagree.  Two vocabularies exist for the same data:

* **Simple** (default): what is happening, in everyday words ("Waiting for clear sky").
* **Technical**: the harness's own names (``SLEEP``, ``cross_reference``, ``dBIC`` ...).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# --------------------------------------------------------------------------- #
# Mission states
# --------------------------------------------------------------------------- #
#: Display state -> plain-language label.  "ORPHANED", "PAUSED" and "IDLE" are display states, not stored
#: statuses: "unfinished and its owning process died", "unfinished and its process let go cleanly (Ctrl-C)"
#: and "created but never started".
STATE_PLAIN = {
    "INIT": "Not started",
    "IDLE": "Not started",
    "HYPOTHESIS": "Planning targets",
    "WEATHER_CHECK": "Checking the weather",
    "SLEEP": "Waiting for clear sky",
    "WAITING": "Waiting for the next visit",
    "OBSERVING": "Taking images",
    "REDUCING": "Processing images",
    "CROSS_REFERENCING": "Checking against catalogues",
    "SCREENING": "Looking for anomalies",
    "PACKAGING": "Preparing upload files",
    "WRITING": "Writing the mission log",
    "COMPLETE": "Complete",
    "ABORTED": "Stopped",
    "STOPPED": "Parked",
    "FAILED": "Failed",
    "ORPHANED": "Interrupted",
    "PAUSED": "Paused",
}
#: Display state -> technical label (the harness's status names; the fleet page's older wording).
STATE_TECHNICAL = {
    "IDLE": "NOT STARTED",
    "OBSERVING": "ACQUIRING",
    "SLEEP": "SLEEPING",
}
STATE_COLORS = {
    "INIT": "#6c757d",
    "IDLE": "#6c757d",
    "HYPOTHESIS": "#0d6efd",
    "WEATHER_CHECK": "#0d6efd",
    "SLEEP": "#fd7e14",
    "WAITING": "#6f42c1",
    "OBSERVING": "#6610f2",
    "REDUCING": "#0d6efd",
    "CROSS_REFERENCING": "#0d6efd",
    "SCREENING": "#0d6efd",
    "PACKAGING": "#0d6efd",
    "WRITING": "#0d6efd",
    "COMPLETE": "#198754",
    "ABORTED": "#d39e00",
    "STOPPED": "#d39e00",
    "FAILED": "#dc3545",
    "ORPHANED": "#b02a37",
    "PAUSED": "#e0a800",
}
#: Named colours for a status dot in a label.
STATE_DOT = {
    "INIT": "gray", "IDLE": "gray", "WAITING": "violet", "SLEEP": "orange", "COMPLETE": "green",
    "ABORTED": "orange", "STOPPED": "orange", "FAILED": "red", "ORPHANED": "red", "PAUSED": "orange",
}
DEFAULT_COLOR = "#0d6efd"
DEFAULT_DOT = "blue"


def state_label(state: str, simple: bool = True) -> str:
    """Name of a display state in the chosen vocabulary."""
    if simple:
        return STATE_PLAIN.get(state, state.replace("_", " ").capitalize())
    return STATE_TECHNICAL.get(state, state)


def state_color(state: str) -> str:
    return STATE_COLORS.get(state, DEFAULT_COLOR)


def state_dot(state: str) -> str:
    return STATE_DOT.get(state, DEFAULT_DOT)


#: Colour slot and whether the status dot is filled.  Six looks, eighteen states.
STATE_LOOK = {
    "HYPOTHESIS": ("blue", True), "WEATHER_CHECK": ("blue", True), "OBSERVING": ("blue", True),
    "REDUCING": ("blue", True), "CROSS_REFERENCING": ("blue", True), "SCREENING": ("blue", True),
    "PACKAGING": ("blue", True), "WRITING": ("blue", True),
    "WAITING": ("blue", False), "SLEEP": ("blue", False),
    "ORPHANED": ("orange", True), "PAUSED": ("orange", True),
    "FAILED": ("red", True),
    "COMPLETE": ("green", True),
    "ABORTED": ("gray", True), "STOPPED": ("gray", True),
    "INIT": ("gray", False), "IDLE": ("gray", False),
}
PLAN_STATUS_LOOK = {
    "PLANNED": ("blue", False), "STARTED": ("blue", True), "DONE": ("green", True),
    "MISSED": ("gray", True), "CANCELLED": ("gray", True),
}


def state_look(state: str) -> tuple[str, bool]:
    """``(slot, filled)`` for a display state.  Unknown states are active blue."""
    return STATE_LOOK.get(state, ("blue", True))


def chip(text: str, bg: str, fg: str = "#ffffff") -> str:
    """A small rounded label as HTML."""
    return (
        f"<span style='background:{bg};color:{fg};padding:2px 10px;border-radius:10px;font-weight:600;"
        f"font-size:.8rem;margin-right:6px'>{text}</span>"
    )


# --------------------------------------------------------------------------- #
# Pipeline nodes and target status
# --------------------------------------------------------------------------- #
NODE_PLAIN = {
    "generate_hypothesis": "Plan",
    "wait_for_visit": "Wait for visit",
    "check_weather": "Check weather",
    "sleep": "Wait for clear sky",
    "release": "Hand back the wait",
    "execute_observation": "Take images",
    "reduce_data": "Process images",
    "cross_reference": "Check catalogues",
    "screen_anomalies": "Look for anomalies",
    "package_submission": "Prepare upload files",
    "write_log": "Write mission log",
}
NODE_TECHNICAL = {
    "generate_hypothesis": "Hypothesis",
    "wait_for_visit": "Wait",
    "check_weather": "Weather",
    "sleep": "Sleep",
    "release": "Release",
    "execute_observation": "Observe",
    "reduce_data": "Reduce",
    "cross_reference": "Cross-ref",
    "screen_anomalies": "Anomaly screen",
    "package_submission": "Package",
    "write_log": "Mission log",
}
NODE_STATE_PLAIN = {"running": "in progress", "done": "done", "error": "error", "idle": "not yet"}


def node_label(node: str, simple: bool = True) -> str:
    return (NODE_PLAIN if simple else NODE_TECHNICAL).get(node, node)


# A planned observation's life cycle (db/models.py: PlannedObservation): label, chip colours.
PLAN_STATUS_PLAIN = {"PLANNED": "Planned", "STARTED": "In progress", "DONE": "Done", "MISSED": "Missed", "CANCELLED": "Cancelled"}
PLAN_STATUS_COLOURS = {
    "PLANNED": ("#0d6efd", "#ffffff"), "STARTED": ("#fd7e14", "#ffffff"), "DONE": ("#198754", "#ffffff"),
    "MISSED": ("#6c757d", "#ffffff"), "CANCELLED": ("#adb5bd", "#000000"),
}


TARGET_STATUS_PLAIN = {"PENDING": "Not yet observed", "OBSERVED": "Observed", "SKIPPED": "Skipped", "FAILED": "Failed"}


def target_status(status: str | None, simple: bool = True) -> str:
    if not status:
        return "-"
    return TARGET_STATUS_PLAIN.get(status, status.capitalize()) if simple else status


# --------------------------------------------------------------------------- #
# Glossary (hover help for terms that survive into the simple view)
# --------------------------------------------------------------------------- #
GLOSSARY = {
    "SNR": "Signal-to-noise ratio: how far the signal stands above the random noise. Above about 5 is a firm detection; below 3 is easily noise.",
    "depth": "How much the star's light dips during the transit, as a percentage of its normal brightness.",
    "duration": "How long the dip lasts, from first to last contact.",
    "delta_bic": "How much better a transit model explains the data than a flat line (Bayesian Information Criterion). Bigger is stronger evidence; above 10 is strong.",
    "anomaly": "A result far enough from what the network's data predicts (a transit arriving late, a source brighter than expected) to be worth a closer look. Anything weaker is only listed in the mission log.",
    "upload_file": "A results file in the format a collaborative database (AAVSO, the Minor Planet Center ...) accepts. Astronomo never sends it: a person reviews it first.",
    "ephemeris": "The published schedule of when a planet should transit. Schedules drift as small period errors add up.",
    "cross_reference": "Comparing what we measured with catalogues of known objects to see whether it is already known.",
    "reduce": "Turning raw camera frames into measurements: removing background, finding stars, tracking brightness.",
    "unidentified": "A candidate the queried catalogues do not explain. That is a lead to check, never a discovery on its own.",
    "known_object": "A candidate whose position and speed match an object in the catalogue.",
    "visit": "One scheduled observation of one target. A long mission is made of many visits.",
}


# --------------------------------------------------------------------------- #
# Lifecycle grouping
# --------------------------------------------------------------------------- #
LIFECYCLES = ("attention", "in_progress", "completed")
LIFECYCLE_TITLES = {"attention": "Needs attention", "in_progress": "In progress", "completed": "Completed"}
#: A failure stays "needs attention" for this long, then it is history like any other finished mission.
FAILED_STAYS_FLAGGED_S = 24 * 3600


def lifecycle(row: dict[str, Any], now: datetime | None = None) -> str:
    """Which lane a ``fleet_row`` belongs in.

    There is no "proposed" lane: missions have no approval state yet, so the first state a
    mission can be seen in is *in progress* (or not started).
    """
    state = row.get("state")
    if state in ("ORPHANED", "PAUSED"):
        return "attention"
    if state == "FAILED":
        updated = row.get("updated_at")
        if updated is not None and (now or datetime.now(timezone.utc)) - updated <= timedelta(seconds=FAILED_STAYS_FLAGGED_S):
            return "attention"
        return "completed"
    return "in_progress" if row.get("active") else "completed"


def group_by_lifecycle(rows: list[dict[str, Any]], now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """``{lane: rows}`` in lane order, each lane keeping the input (newest first) order."""
    groups: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LIFECYCLES}
    for row in rows:
        groups[lifecycle(row, now)].append(row)
    return groups


def plural(n: int, noun: str, plural_form: str | None = None) -> str:
    """``1 field`` / ``3 fields``."""
    return f"{n} {noun if n == 1 else (plural_form or noun + 's')}"


_LY_PER_PC = 3.26156


def format_distance(parsecs: float | None) -> str | None:
    """``"153 pc (500 ly)"``, or ``None`` when there is no usable distance."""
    return f"{parsecs:.0f} pc ({parsecs * _LY_PER_PC:,.0f} ly)" if isinstance(parsecs, (int, float)) and parsecs > 0 else None
