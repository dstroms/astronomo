"""Home page: every mission at a glance, grouped by lifecycle, including the ones that are mostly *waiting*.

A long-cadence mission spends most of its life between visits, so "what is it doing?" needs more
than a status badge: whether a process still owns it (a live heartbeat) or it was orphaned by a
crash, which visit it is on, how far through that visit's frames it is, and when (in survey time
and in real time at the replay speed) it will wake up.  Several missions can be in progress at
once, one process each; this page reads them all from the shared database and writes nothing.

Missions are grouped *Needs attention* / *In progress* / *Completed* (``vocab.lifecycle``).  Each
card carries a plain-language state sentence (``describe_state``) and, from the plugin's
``summarise`` hook, a one-line outcome.

Helpers (``fleet_row``, ``visit_rows``, ``fmt_span``, ``describe_state``) are plain data so they can
be unit-tested.  The HTTP API serialises them; the React client draws them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.clock import parse_time
from core.lease import liveness
from dashboard.plugin_ui import PluginUI, Summary, mission_context, summary_for
from dashboard.vocab import state_label

ACTIVE_STATUSES_DONE = ("COMPLETE", "FAILED", "ABORTED", "STOPPED")


def fmt_span(seconds: float | None) -> str:
    """``3 d 4 h`` / ``2 h 05 m`` / ``45 s``: the two most significant units."""
    if seconds is None:
        return "-"
    s = max(0.0, float(seconds))
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 2 * 86400:
        return f"{int(s // 3600)} h {int(s % 3600 // 60):02d} m"
    return f"{int(s // 86400)} d {int(s % 86400 // 3600)} h"


def _maybe_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_time(value)  # tz-aware UTC from an ISO string or a (naive = UTC) datetime
    except (ValueError, TypeError):
        return None


def display_state(mission: dict, live: str) -> str:
    """The stored status, except for two unfinished-without-an-owner cases that are not "running".

    ``ORPHANED``: the owner stopped heartbeating (crash, kill, sleep).  ``PAUSED``: it released the
    mission cleanly part-way (Ctrl-C).  Either way nothing is working on it until someone resumes it.
    """
    if live == "orphaned":
        return "ORPHANED"
    if live == "idle":
        return "IDLE" if mission.get("status", "INIT") == "INIT" else "PAUSED"
    return mission.get("status", "INIT")


def fleet_row(mission: dict, targets: list[dict], *, now: datetime | None = None, lease_s: float = 45.0) -> dict[str, Any]:
    """Everything the fleet card shows for one mission, as plain data."""
    now = now or datetime.now(timezone.utc)
    progress = mission.get("progress") or {}
    clock = mission.get("clock") or {}
    live = liveness(mission, lease_s, now)
    n_visits = len(targets) or progress.get("visits_total") or 0
    done = sum(1 for t in targets if t.get("status") in ("OBSERVED", "SKIPPED"))  # a skipped target is finished with
    visit = progress.get("visit") or min(done + 1, n_visits) or None
    frames_done, frames_total = progress.get("frames_done"), progress.get("frames_total")

    # Overall completion: finished visits plus the fraction of the current visit's frames in hand.
    within = 0.0
    if frames_total and frames_done is not None and mission.get("status") not in ACTIVE_STATUSES_DONE:
        within = min(1.0, frames_done / frames_total) * 0.5  # acquisition is about half a visit (reduce + cross-ref follow)
    fraction = 1.0 if mission.get("status") == "COMPLETE" else ((done + within) / n_visits if n_visits else 0.0)

    sim_time = _maybe_time(mission.get("sim_time"))
    wake_at = _maybe_time(mission.get("wake_at"))
    speed = clock.get("speed") if clock.get("kind") == "replay" else None
    wait_survey_s = (wake_at - sim_time).total_seconds() if wake_at and sim_time else None
    wait_wall_s = None
    if wait_survey_s is not None and speed:
        wait_wall_s = wait_survey_s / speed

    beat = _maybe_time(mission.get("heartbeat_at"))
    return {
        "id": mission["id"],
        "plugin": mission.get("plugin_name"),
        "simulated": bool(mission.get("simulated")),
        "target_names": [t.get("name") for t in sorted(targets, key=lambda t: t.get("position", 0)) if t.get("name")],
        "created_at": _maybe_time(mission.get("created_at")),
        "updated_at": _maybe_time(mission.get("updated_at")),
        "data_source": mission.get("data_source") or "telescope",
        "status": mission.get("status"),
        "state": display_state(mission, live),
        "liveness": live,
        "active": mission.get("status") not in ACTIVE_STATUSES_DONE,
        "node": mission.get("current_node") or progress.get("node"),
        "target": progress.get("target"),
        "visit": visit, "visits_total": n_visits, "visits_done": done,
        "frames_done": frames_done, "frames_total": frames_total,
        "fraction": fraction,
        "detail": progress.get("detail") or mission.get("error"),
        "clock_kind": clock.get("kind") or "system",
        "speed": speed,
        "sim_time": sim_time, "wake_at": wake_at,
        "wait_survey_s": wait_survey_s, "wait_wall_s": wait_wall_s,
        "heartbeat_age_s": (now - beat).total_seconds() if beat else None,
        "worker": (f"pid {mission['worker_pid']}@{mission['worker_host']}" if mission.get("worker_pid") else None),
        "frame_time": _maybe_time(progress.get("frame_time")),
        "error": mission.get("error"),
    }


def visit_rows(targets: list[dict], current_index: int | None) -> list[dict[str, Any]]:
    """One row per scheduled visit: when it is due, its state, and the gap since the previous visit."""
    rows = []
    prev: datetime | None = None
    for i, t in enumerate(sorted(targets, key=lambda t: t.get("position", 0))):
        payload = t.get("payload") or {}
        due = _maybe_time(payload.get("not_before") or payload.get("window_start"))
        if t.get("status") == "OBSERVED":
            state = "done"
        elif t.get("status") == "SKIPPED":
            state = "skipped"
        elif current_index is not None and i == current_index:
            state = "current"
        else:
            state = "pending"
        rows.append({
            "index": i + 1, "name": t.get("name"), "due": due, "state": state,
            "gap_s": (due - prev).total_seconds() if due and prev else None,
        })
        prev = due or prev
    return rows


def cadence_summary(rows: list[dict[str, Any]]) -> str | None:
    gaps = sorted(r["gap_s"] for r in rows if r["gap_s"] is not None)
    if not gaps:
        return None
    median = gaps[len(gaps) // 2]
    return f"{len(rows)} visits over {fmt_span(sum(gaps))}; gaps {fmt_span(gaps[0])} to {fmt_span(gaps[-1])} (median {fmt_span(median)})"


def describe_state(row: dict[str, Any]) -> str:
    """One everyday sentence about what a mission is doing right now (or how it ended)."""
    state = row["state"]
    visits = f"{row['visits_done']} of {row['visits_total']}" if row["visits_total"] else None
    if state == "PAUSED":
        return (
            "Stopped part-way, for example with Ctrl-C. Nothing is lost: starting it again picks up from the last "
            "checkpoint."
        )
    if state == "ORPHANED":
        ago = f"{fmt_span(row['heartbeat_age_s'])} ago" if row["heartbeat_age_s"] is not None else "earlier"
        return (
            f"The program running this mission stopped {ago}. Nothing is lost: starting it again picks up from "
            "the last checkpoint."
        )
    if state == "WAITING" and row["wake_at"]:
        text = f"Waiting for visit {row['visit'] or '-'} of {row['visits_total'] or '-'}, due {row['wake_at']:%Y-%m-%d %H:%M} UTC"
        if row["wait_survey_s"] is not None:
            text += f" ({fmt_span(row['wait_survey_s'])} away"
            text += f", about {fmt_span(row['wait_wall_s'])} of real time at {row['speed']:g}x)" if row["wait_wall_s"] is not None else ")"
        return text + "."
    if state == "SLEEP":
        return "Waiting for the sky to clear before observing. " + (row["detail"] or "It checks again on a timer.")
    if state == "OBSERVING":
        what = f"Taking images of {row['target']}" if row["target"] else "Taking images"
        if row["frames_total"]:
            what += f" (image {row['frames_done'] or 0} of {row['frames_total']})"
        return what + "."
    if state == "COMPLETE":
        return f"Finished: {visits} visits observed." if visits else "Finished."
    if state == "FAILED":
        return "Stopped with an error: " + str(row["error"] or "no details were recorded")[:240]
    if state == "ABORTED":
        return "Stopped before it finished." + (f" {row['error']}" if row["error"] else "")
    if state == "STOPPED":
        return "Parked and stopped. It will not resume." + (f" {row['error']}" if row["error"] else "")
    if state == "IDLE":
        return "Created but not started yet."
    return str(row["detail"]) if row["detail"] else state_label(state, True) + "."


def technical_facts(row: dict[str, Any]) -> str:
    """The expert one-liner: node, clocks, worker and heartbeat."""
    facts = [f"node `{row['node'] or '-'}`"]
    if row["sim_time"]:
        facts.append(f"{'survey' if row['clock_kind'] == 'replay' else 'clock'} time {row['sim_time']:%Y-%m-%d %H:%M} UTC")
    if row["clock_kind"] == "replay":
        facts.append("replay " + ("jump" if not row["speed"] else f"{row['speed']:g}x"))
    if row["worker"]:
        facts.append(f"`{row['worker']}`")
    if row["heartbeat_age_s"] is not None:
        facts.append(f"heartbeat {fmt_span(row['heartbeat_age_s'])} ago")
    return " | ".join(facts)


def mission_title(row: dict[str, Any], plugin_label: str, simple: bool) -> str:
    """``Exoplanet transit: WASP-2 b`` (simple) or ``Exoplanet transit `m_2026...` `` (technical)."""
    if not simple:
        return f"{plugin_label} `{row['id']}`"
    names = row.get("target_names") or []
    if not names:
        return plugin_label
    shown = ", ".join(names[:2]) + (f" +{len(names) - 2} more" if len(names) > 2 else "")
    return f"{plugin_label}: {shown}"


def progress_text(row: dict[str, Any], simple: bool) -> str:
    bits = [f"{'Visit' if simple else 'visit'} {row['visit'] or '-'} of {row['visits_total'] or '-'}"]
    if row["target"]:
        bits.append(str(row["target"]))
    if row["frames_total"]:
        bits.append(f"{'image' if simple else 'frame'} {row['frames_done'] or 0}/{row['frames_total']}")
    return " - ".join(bits)


def fleet_rows(db, lease_s: float, limit: int = 60, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every mission as a ``fleet_row``, newest first."""
    now = now or datetime.now(timezone.utc)
    return [fleet_row(m, db.list_targets(m["id"]), now=now, lease_s=lease_s) for m in db.list_missions(limit=limit)]


# One-line outcomes are cheap to compute but need every observation of a mission: remember them per
# (mission, last update) so a finished mission is read once, not on every refresh.
_SUMMARY_CACHE: dict[tuple[str, str], Summary] = {}


def mission_summary(db, mission_id: str, ui: PluginUI, updated_at: datetime | None) -> Summary:
    key = (mission_id, updated_at.isoformat() if updated_at else "")
    hit = _SUMMARY_CACHE.get(key)
    if hit is not None:
        return hit
    mission = db.get_mission(mission_id)
    out = summary_for(ui, mission_context(db, mission, simple=True)) if mission else Summary("", "", "neutral")
    if len(_SUMMARY_CACHE) > 256:
        _SUMMARY_CACHE.clear()
    _SUMMARY_CACHE[key] = out
    return out


def describe_when(when: datetime, now: datetime | None = None) -> str:
    """``12 min ago`` / ``3 h 05 m ago`` / ``2 d 4 h ago``."""
    now = now or datetime.now(timezone.utc)
    return f"{fmt_span((now - when).total_seconds())} ago"

