"""JSON payloads the HTTP API serves.  No FastAPI: helpers plus the database."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.clock import parse_time
from core.state import NODE_ORDER, to_jsonable
from dashboard.fleet import (
    describe_state, describe_when, fleet_row, fleet_rows, fmt_span, mission_summary, mission_title,
    progress_text, technical_facts,
)
from dashboard.opportunities import (
    IMPACT_STYLE, TASK_PLAIN, TASK_TECHNICAL, altitude_track, closed_window_sentence, earlier_attempt_line,
    finished_attempts, fmt_clock, get_reports, local_tz, planned_id, planned_keys, site_banner, source_names,
    timing_for_plan, timing_for_window, timing_lines, window_label, words_for, zone_name,
)
from dashboard.plans import (
    LaunchError, countdown_label, launch_plan, launch_spec, load_plans, log_tail, mission_id_from_log,
    pid_alive, plan_actions, read_launches, stop_command,
)
from dashboard.plugin_ui import evidence_for, load_plugin_ui, mission_context, summary_for
from dashboard.vocab import (
    GLOSSARY, NODE_STATE_PLAIN, PLAN_STATUS_LOOK, PLAN_STATUS_PLAIN,
    format_distance, group_by_lifecycle, lifecycle, node_label, plural, state_label, state_look, target_status,
)
from dashboard import first_frame
from db.database import LIVE_PLAN_STATUSES, Database, summarise_llm_calls

SUBMISSION_STATUS_PLAIN = {
    "simulated": "Simulated: practice data, never uploadable",
    "replay": "Replay: archive data, never uploadable",
    "draft": "Draft: something must be fixed first",
    "ready_for_review": "Ready for a person to review",
    "held": "Held back (not produced)",
}

_START_WARNING = (
    "Start {name} on the telescope. The plan is checked again first. A closed window "
    "is refused and the plan is marked missed. This commands the real telescope."
)


def jsonable(obj: Any) -> Any:
    return to_jsonable(obj)


def human_age(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return "-"
    when = iso if isinstance(iso, datetime) else datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (now or datetime.now(timezone.utc)) - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h {(secs % 3600) // 60}m ago"


def node_states(events: list[dict]) -> dict[str, dict]:
    out = {n: {"state": "idle", "runs": 0} for n in NODE_ORDER}
    for ev in events:
        node = ev.get("node")
        if node not in out:
            continue
        if ev["kind"] == "node_start":
            out[node]["state"] = "running"
            out[node]["runs"] += 1
        elif ev["kind"] == "node_end":
            out[node]["state"] = "done"
        elif ev["kind"] == "node_error":
            out[node]["state"] = "error"
    return out


def look_of(state: str) -> dict[str, Any]:
    slot, filled = state_look(state)
    return {"slot": slot, "filled": filled}


def resume_command(row: dict) -> str:
    extra = f" --data-source {row['data_source']}" if row.get("data_source") == "archive" else ""
    return f"python main.py --plugin {row['plugin']} --mission-id {row['id']}{extra}"


def origin_chip(row: dict, simple: bool) -> dict[str, str] | None:
    if row.get("simulated"):
        return {"label": "Simulated" if simple else "SIM", "slot": "violet"}
    if row.get("data_source") == "archive":
        return {"label": "Archive replay" if simple else "ARCHIVE", "slot": "violet"}
    if not simple:
        return {"label": "TELESCOPE", "slot": "gray"}
    return None


def mission_chips(row: dict, simple: bool) -> list[dict[str, Any]]:
    slot, filled = state_look(row["state"])
    chips = [{"label": state_label(row["state"], simple), "slot": slot, "filled": filled}]
    live = row.get("liveness")
    if simple:
        if live == "running" and row.get("active") and row["state"] not in ("ORPHANED", "PAUSED"):
            chips.append({"label": "Running now", "slot": "green", "filled": True})
    else:
        tech = {"running": "process alive", "orphaned": "process gone", "idle": "no process", "finished": "finished"}
        slot_live = {"running": "green", "orphaned": "orange", "idle": "gray", "finished": "gray"}
        if live in tech:
            chips.append({"label": tech[live], "slot": slot_live[live], "filled": live != "idle"})
    origin = origin_chip(row, simple)
    if origin:
        chips.append(origin)
    return chips


def deliverable_phrase(artifacts: list[dict], *, simulated: bool) -> str:
    if simulated:
        subs = [a for a in artifacts if a.get("kind") == "submission"]
        if subs:
            return "practice data, not uploadable"
    ready = [a for a in artifacts if a.get("status") == "ready_for_review"]
    draft = [a for a in artifacts if a.get("status") == "draft"]
    if ready:
        return f"{plural(len(ready), 'file')} ready"
    if draft:
        return "draft: fix blockers"
    if not artifacts:
        return "none yet"
    return "none"


def serialize_row(row: dict, ui, summary, *, simple: bool) -> dict[str, Any]:
    slot, filled = state_look(row["state"])
    return jsonable({
        "id": row["id"],
        "plugin": row["plugin"],
        "plugin_label": ui.label,
        "title": mission_title(row, ui.label, simple),
        "state": row["state"],
        "state_label": state_label(row["state"], simple),
        "look": {"slot": slot, "filled": filled},
        "chips": mission_chips(row, simple),
        "describe_state": describe_state(row),
        "progress_text": progress_text(row, simple),
        "fraction": row["fraction"],
        "active": row["active"],
        "liveness": row["liveness"],
        "simulated": row["simulated"],
        "data_source": row["data_source"],
        "visits_done": row["visits_done"],
        "visits_total": row["visits_total"],
        "visit": row.get("visit"),
        "frames_done": row.get("frames_done"),
        "frames_total": row.get("frames_total"),
        "wake_at": row.get("wake_at"),
        "updated_at": row.get("updated_at"),
        "updated_ago": describe_when(row["updated_at"]) if row.get("updated_at") else None,
        "resume_command": resume_command(row) if row["state"] in ("ORPHANED", "PAUSED") else None,
        "error": row.get("error"),
        "technical_facts": None if simple else technical_facts(row),
        "summary": {"found": summary.found, "meaning": summary.meaning, "verdict": summary.verdict},
        "lifecycle": None,
    })


def shell_payload(settings, *, have_db: bool, attention_count: int) -> dict[str, Any]:
    now = datetime.now().astimezone()
    tz = local_tz()
    source = getattr(settings, "site_source", "default")
    return jsonable({
        "site": {
            "name": settings.site_name,
            "lat": settings.site_latitude_deg,
            "lon": settings.site_longitude_deg,
            "elev_m": settings.site_elevation_m,
            "horizon": list(settings.site_horizon or ()),
            "source": source,
            "banner": site_banner(source),
            "line": (
                f"{settings.site_name} {abs(settings.site_latitude_deg):.2f}"
                f"{'N' if settings.site_latitude_deg >= 0 else 'S'} "
                f"{abs(settings.site_longitude_deg):.2f}"
                f"{'E' if settings.site_longitude_deg >= 0 else 'W'}"
            ),
        },
        "clock": {
            "iso": now.isoformat(),
            "display_12": f"{fmt_clock(now, hour12=True)} {zone_name(now)}",
            "display_24": f"{fmt_clock(now, hour12=False)} {zone_name(now)}",
            "zone": zone_name(now),
            "weekday": f"{now:%a %d %b}",
        },
        "data_dir": str(settings.data_dir),
        "db_path": str(Path(settings.data_dir) / "astronomo.sqlite"),
        "have_db": have_db,
        "attention_count": attention_count,
        "poll_s": 3,
    })


def _weekday(now: datetime) -> str:
    return f"{now:%a %d %b}"


def attention_count(db: Database, lease_s: float, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    rows = fleet_rows(db, lease_s, now=now) if db else []
    groups = group_by_lifecycle(rows, now)
    n = len(groups["attention"])
    for plan in load_plans(db, now):
        if plan.get("status") != "PLANNED":
            continue
        start, end = _window_bounds(plan)
        remaining, hint = countdown_label(start, end, now)
        if hint == "window open":
            n += 1
    return n


def _window_bounds(plan: dict) -> tuple[datetime | None, datetime | None]:
    timing = timing_for_plan(plan)
    try:
        return parse_time(timing["mission_start"]), parse_time(timing["mission_end"])
    except (TypeError, ValueError):
        return None, None


def _ui(name: str):
    return load_plugin_ui(name)


def tonight_payload(db: Database | None, settings, *, simple: bool, hour12: bool) -> dict[str, Any]:
    db_path = str(Path(settings.data_dir) / "astronomo.sqlite")
    if db is None:
        return jsonable({
            "have_db": False,
            "db_path": db_path,
            "empty": f"No database at {db_path} yet. Run python main.py --simulate first.",
            "counts": {"needs_you": 0, "running": 0, "waiting": 0, "planned": 0, "in_progress": 0},
            "attention": [], "running": [], "plans": {"live": [], "earlier": []},
            "conditions": None, "ready_for_review": [],
            "date": _weekday(datetime.now().astimezone()),
        })
    now = datetime.now(timezone.utc)
    rows = fleet_rows(db, settings.lease_timeout_s, now=now)
    groups = group_by_lifecycle(rows, now)
    active = [r for r in rows if r["active"]]
    waiting = sum(r["state"] == "WAITING" for r in active)

    def pack(row):
        ui = _ui(row["plugin"])
        return serialize_row(row, ui, mission_summary(db, row["id"], ui, row["updated_at"]), simple=simple)

    attention = [pack(r) for r in groups["attention"]]
    running = [pack(r) for r in groups["in_progress"]]
    plans = plans_payload(db, settings, simple=simple, hour12=hour12)
    live_n = len(plans["live"])
    return jsonable({
        "have_db": True,
        "db_path": db_path,
        "date": _weekday(datetime.now().astimezone()),
        "counts": {
            "needs_you": len(attention),
            "running": sum(r["liveness"] == "running" for r in active),
            "waiting": waiting,
            "planned": live_n,
            "in_progress": len(groups["in_progress"]) + sum(1 for r in groups["attention"] if r["active"]),
        },
        "helps": {
            "needs_you": "Interrupted or paused missions that need restarting, and failures from the last 24 hours.",
            "running": "Missions whose program is running right now.",
            "waiting": "Missions between scheduled observations. They are idle on purpose.",
            "planned": "Saved plans that have not finished.",
            "in_progress": "Missions that have not finished.",
        },
        "attention": attention,
        "running": running,
        "plans": plans,
        "conditions": conditions_line(db, rows),
        "ready_for_review": ready_for_review(db, rows, simple=simple),
        "attention_count": len(attention) + sum(
            1 for p in plans["live"] if p.get("countdown", {}).get("hint") == "window open"
        ),
    })


def conditions_line(db: Database, rows: list[dict]) -> dict[str, Any] | None:
    """Latest stored weather of a running or recent mission.  Never fetches."""
    candidates = [r for r in rows if r.get("active") or r.get("state") == "COMPLETE"]
    best = None
    best_ts = None
    for row in candidates[:12]:
        weather = db.list_weather(row["id"])
        if not weather:
            continue
        latest = weather[-1]
        ts = latest.get("ts")
        if best_ts is None or str(ts) > str(best_ts):
            best, best_ts = latest, ts
            best["mission_id"] = row["id"]
            best["mission_title"] = row.get("id")
    if best is None:
        return None
    return jsonable({
        "clear": best.get("clear"),
        "cloud_cover_pct": best.get("cloud_cover_pct"),
        "wind_kmh": best.get("wind_kmh"),
        "seeing_arcsec": best.get("seeing_arcsec"),
        "reason": best.get("reason"),
        "source": best.get("source"),
        "age": human_age(best.get("ts")),
        "mission_id": best.get("mission_id"),
        "verdict": "Clear enough" if best.get("clear") else "Not safe",
        "slot": "green" if best.get("clear") else "red",
    })


def ready_for_review(db: Database, rows: list[dict], *, simple: bool) -> list[dict[str, Any]]:
    out = []
    by_id = {r["id"]: r for r in rows}
    for art in db.list_artifacts(kind="submission"):
        if art.get("status") != "ready_for_review":
            continue
        row = by_id.get(art["mission_id"])
        ui = _ui(row["plugin"]) if row else load_plugin_ui("unknown")
        title = mission_title(row, ui.label, simple) if row else art["mission_id"]
        out.append(jsonable({
            "artifact_id": art["id"],
            "mission_id": art["mission_id"],
            "title": title,
            "file_title": art.get("title"),
            "status_label": SUBMISSION_STATUS_PLAIN.get(art.get("status") or "", art.get("status")),
            "path": art.get("path"),
            "filename": Path(art["path"]).name if art.get("path") else None,
            "finished_ago": describe_when(row["updated_at"]) if row and row.get("updated_at") else None,
        }))
    return out


def missions_payload(db: Database | None, settings, *, simple: bool) -> dict[str, Any]:
    db_path = str(Path(settings.data_dir) / "astronomo.sqlite")
    if db is None:
        return jsonable({"have_db": False, "db_path": db_path, "missions": [], "counts": {}})
    now = datetime.now(timezone.utc)
    rows = fleet_rows(db, settings.lease_timeout_s, now=now)
    groups = group_by_lifecycle(rows, now)
    artifacts_by_mission: dict[str, list] = {}
    for art in db.list_artifacts():
        artifacts_by_mission.setdefault(art["mission_id"], []).append(art)

    def pack(row):
        ui = _ui(row["plugin"])
        summary = mission_summary(db, row["id"], ui, row["updated_at"])
        body = serialize_row(row, ui, summary, simple=simple)
        lane = lifecycle(row, now)
        arts = artifacts_by_mission.get(row["id"], [])
        body["lifecycle"] = lane
        body["outcome"] = (
            describe_state(row) if row["state"] in ("FAILED", "ABORTED", "STOPPED", "ORPHANED", "PAUSED")
            else (summary.found or describe_state(row))
        )
        body["deliverables"] = deliverable_phrase(arts, simulated=bool(row["simulated"]))
        body["pinned"] = lane == "attention"
        return body

    packed = [pack(r) for r in rows]
    packed.sort(key=lambda m: (0 if m["pinned"] else 1, m.get("updated_at") or ""), reverse=False)
    # pinned first, then newest: rebuild
    attention_ids = {r["id"] for r in groups["attention"]}
    rest = [m for m in packed if m["id"] not in attention_ids]
    rest.sort(key=lambda m: str(m.get("updated_at") or ""), reverse=True)
    attention = [m for m in packed if m["id"] in attention_ids]
    ordered = attention + rest
    return jsonable({
        "have_db": True,
        "db_path": db_path,
        "missions": ordered,
        "counts": {
            "all": len(ordered),
            "needs_you": len(attention),
            "active": sum(1 for m in ordered if m["active"]),
            "finished": sum(1 for m in ordered if not m["active"]),
        },
    })


def pipeline_payload(db: Database, mission: dict, row: dict, simple: bool) -> list[dict[str, Any]]:
    events = db.list_logs(mission["id"], limit=2000, kinds=["node_start", "node_end", "node_error"])
    states = node_states(events)
    stalled = row["state"] in ("ORPHANED", "PAUSED")
    out = []
    for node in NODE_ORDER:
        info = states[node]
        state = info["state"]
        if mission["status"] in ("COMPLETE", "ABORTED", "FAILED", "STOPPED") and state == "running":
            state = "idle"
        label = NODE_STATE_PLAIN[state] if simple else state
        slot = {"running": "blue", "done": "green", "error": "red", "idle": "gray"}[state]
        filled = state != "idle"
        if stalled and info["state"] == "running" and state == "running":
            slot, filled, label = "orange", True, "stopped here" if simple else "stalled"
        out.append({
            "node": node,
            "name": node_label(node, simple),
            "state": state,
            "label": label,
            "slot": slot,
            "filled": filled,
            "runs": info["runs"],
        })
    return out


def artifacts_payload(db: Database, mission_id: str) -> dict[str, Any]:
    def one(art: dict) -> dict[str, Any]:
        meta = art.get("meta") or {}
        path = art.get("path")
        return jsonable({
            "id": art["id"],
            "kind": art.get("kind"),
            "title": art.get("title"),
            "status": art.get("status"),
            "status_label": SUBMISSION_STATUS_PLAIN.get(art.get("status") or "", art.get("status")),
            "path": path,
            "filename": Path(path).name if path else None,
            "downloadable": bool(path and Path(path).is_file()),
            "blockers": meta.get("blockers") or [],
            "notes": meta.get("notes") or [],
            "created_at": art.get("created_at"),
            "age": human_age(art.get("created_at")),
        })

    return jsonable({
        "submissions": [one(a) for a in db.list_artifacts(mission_id, "submission")],
        "mission_logs": [one(a) for a in db.list_artifacts(mission_id, "mission_log")],
        "notice": (
            "The product of a mission is a set of results files for the collaborative databases. "
            "Nothing is sent anywhere: you review each file and upload it yourself."
        ),
    })


def first_frame_payload(data_dir: Path, mission: dict) -> dict[str, Any] | None:
    found = first_frame.locate_first_frame(data_dir, mission["id"])
    pending = mission.get("status") == "OBSERVING" and first_frame.will_show_first_frame(mission)
    if found is None and not pending:
        return None
    live = first_frame.show_above_sections(mission, found)
    report = found.report if found else None
    return jsonable({
        "pending": found is None and pending,
        "pending_text": first_frame.PENDING if pending else None,
        "measuring": found is not None and found.report is None,
        "measuring_text": first_frame.MEASURING,
        "has_preview": bool(found and found.preview),
        "caption": first_frame.SAVED_FRAME if found and found.preview else first_frame.NO_PICTURE,
        "report": report,
        "continues": first_frame.CONTINUES if report and report.get("ok") else None,
        "stopped": first_frame.STOPPED if report and not report.get("ok") else None,
        "above_tabs": live,
        "preview_url": f"/api/v1/missions/{mission['id']}/first-frame" if found and found.preview else None,
    })


def live_frame_payload(data_dir: Path, mission: dict, *, simple: bool) -> dict[str, Any] | None:
    from core.frame_watch import FIGURE_NAME, STATS_NAME, format_row
    from core.setup_checks import LATEST_NAME
    from core.state import TERMINAL as TERM

    mission_dir = Path(data_dir) / "missions" / mission["id"]
    rows = first_frame._read_rows(mission_dir / STATS_NAME)
    latest = mission_dir / LATEST_NAME
    figure = mission_dir / FIGURE_NAME
    if not rows and not latest.is_file():
        return None
    live = mission.get("status") not in TERM
    last = rows[-1] if rows else None
    warnings = []
    if rows:
        recent = [(r.get("index", 0) + 1, w) for r in rows[-10:] for w in r.get("warnings") or []]
        warnings = [f"Frame {n}: {w}" for n, w in recent[-3:]]
    return jsonable({
        "live": live,
        "title": "Live view" if live else "Frame watch",
        "caption": first_frame.LIVE_CAPTION,
        "latest_url": f"/api/v1/missions/{mission['id']}/live-frame" if latest.is_file() else None,
        "figure_url": f"/api/v1/missions/{mission['id']}/frame-watch" if figure.is_file() else None,
        "stats": format_row(last) if last else None,
        "warnings": warnings,
        "row": last,
    })


def mission_payload(db: Database, settings, mission_id: str, *, simple: bool) -> dict[str, Any] | None:
    mission = db.get_mission(mission_id)
    if mission is None:
        return None
    ui = load_plugin_ui(mission["plugin_name"])
    ctx = mission_context(db, mission, simple)
    row = fleet_row(mission, ctx.targets, lease_s=settings.lease_timeout_s)
    summary = summary_for(ui, ctx)
    data_dir = Path(settings.data_dir)
    weather = db.list_weather(mission["id"])
    threshold = float((mission.get("config") or {}).get("cloud_cover_threshold_pct", 30.0))
    latest = weather[-1] if weather else None
    from_plan = db.planned_for_mission(mission["id"])
    logs = db.list_logs(mission_id, limit=250, kinds=["log", "node_error"])
    llm_calls = db.list_llm_calls(mission_id)
    usage = summarise_llm_calls(llm_calls) if llm_calls else None
    llm_cfg = (mission.get("config") or {}).get("llm") or {}
    dist = [format_distance((t.get("payload") or {}).get("distance_pc")) for t in ctx.targets]
    targets = []
    for i, t in enumerate(ctx.targets):
        item = {
            "position": t.get("position", i) + 1,
            "name": t.get("name"),
            "status": t.get("status"),
            "status_label": target_status(t.get("status"), simple),
            "ra_deg": t.get("ra_deg"),
            "dec_deg": t.get("dec_deg"),
            "priority": t.get("priority"),
            "distance": dist[i] if i < len(dist) else None,
        }
        targets.append(item)
    plan_caption = None
    if from_plan:
        plan_caption = (
            f"Started from plan #{from_plan['id']} on the Opportunities page "
            f"({PLAN_STATUS_PLAIN.get(from_plan['status'], from_plan['status'])}"
            + (f": {from_plan['outcome']}" if from_plan.get("outcome") else "") + ")."
        )
    return jsonable({
        "mission": {
            "id": mission["id"],
            "plugin": mission["plugin_name"],
            "plugin_label": ui.label,
            "description": ui.headline(),
            "plugin_status": ui.status,
            "status": mission["status"],
            "hypothesis": mission.get("hypothesis"),
            "error": mission.get("error"),
            "simulated": bool(mission.get("simulated")),
            "data_source": mission.get("data_source"),
            "updated_at": mission.get("updated_at"),
            "updated_ago": human_age(mission.get("updated_at")),
        },
        "row": serialize_row(row, ui, summary, simple=simple),
        "summary": {"found": summary.found, "meaning": summary.meaning, "verdict": summary.verdict},
        "plan": {
            "id": from_plan["id"] if from_plan else None,
            "caption": plan_caption,
            "status": from_plan["status"] if from_plan else None,
            "can_plan_again": bool(
                from_plan and from_plan["status"] in ("DONE", "MISSED") and not closed_window_sentence(from_plan)
            ),
            "closed": closed_window_sentence(from_plan) if from_plan else None,
            "text": mission.get("hypothesis"),
        },
        "targets": targets,
        "pipeline": pipeline_payload(db, mission, row, simple),
        "weather": {
            "readings": weather,
            "threshold_pct": threshold,
            "latest": latest,
            "verdict": None if not latest else (
                "CLEAR - observing allowed" if latest["clear"] else "NOT SAFE - waiting"
            ),
        },
        "first_frame": first_frame_payload(data_dir, mission),
        "live_frame": live_frame_payload(data_dir, mission, simple=simple),
        "artifacts": artifacts_payload(db, mission_id),
        "activity": {
            "logs": [
                {"ts": l["ts"], "level": l["level"], "node": l.get("node"), "message": l["message"]}
                for l in reversed(logs)
            ],
            "llm": {
                "calls": usage["calls"] if usage else 0,
                "usage": usage,
                "provider": llm_cfg.get("provider") or "none",
                "model": llm_cfg.get("model") or (llm_calls[0].get("model") if llm_calls else None),
                "items": [
                    {
                        "node": c.get("node"), "ok": c.get("ok"), "cached": c.get("cached"),
                        "error": c.get("error"), "prompt": c.get("prompt"), "response": c.get("response"),
                        "prompt_tokens": c.get("prompt_tokens"), "completion_tokens": c.get("completion_tokens"),
                        "latency_s": c.get("latency_s"),
                    }
                    for c in llm_calls
                ],
            },
        },
        "evidence": evidence_for(ui, ctx),
        "glossary": GLOSSARY if simple else None,
    })


def switcher_payload(db: Database, settings, *, simple: bool) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    rows = fleet_rows(db, settings.lease_timeout_s, now=now)
    groups = group_by_lifecycle(rows, now)
    out = []
    for lane, items in groups.items():
        for row in items:
            ui = _ui(row["plugin"])
            slot, filled = state_look(row["state"])
            out.append({
                "id": row["id"],
                "title": mission_title(row, ui.label, simple),
                "lane": lane,
                "state_label": state_label(row["state"], simple),
                "look": {"slot": slot, "filled": filled},
            })
    return jsonable(out)


def _plan_card(db, settings, plan, record, *, simple: bool, hour12: bool) -> dict[str, Any]:
    from dashboard.plans import _mission_running

    ui = _ui(plan["plugin_name"])
    running = _mission_running(db, plan, settings.lease_timeout_s)
    busy = bool(record) and pid_alive(record.get("pid"))
    actions = plan_actions(plan["status"], mission_running=running or busy)
    tz = local_tz()
    note = (plan.get("target") or {}).get("window_note")
    start, end = _window_bounds(plan)
    remaining, hint = countdown_label(start, end, datetime.now(timezone.utc))
    rehearsal_id = mission_id_from_log(record.get("log")) if record and record.get("rehearse") else None
    if rehearsal_id and rehearsal_id == plan.get("mission_id"):
        rehearsal_id = None
    rehearsal = None
    if rehearsal_id:
        mission = db.get_mission(rehearsal_id)
        if mission:
            row = fleet_row(mission, db.list_targets(rehearsal_id), lease_s=settings.lease_timeout_s)
            rehearsal = {
                "mission_id": rehearsal_id,
                "describe_state": describe_state(row),
                "progress_text": progress_text(row, simple),
                "fraction": row["fraction"],
                "note": "This plan stays planned.",
            }
        else:
            rehearsal = {"mission_id": rehearsal_id, "starting": True}
    slot, filled = PLAN_STATUS_LOOK.get(plan["status"], ("gray", True))
    real = launch_spec(plan["id"], rehearse=False, data_dir=settings.data_dir, default_data_dir=settings.data_dir)
    rehearse = launch_spec(plan["id"], rehearse=True, data_dir=settings.data_dir, default_data_dir=settings.data_dir)
    stop = stop_command(settings.data_dir, settings.data_dir, plan.get("mission_id"))
    return jsonable({
        "id": plan["id"],
        "target_name": plan["target_name"],
        "plugin": plan["plugin_name"],
        "plugin_label": ui.label,
        "status": plan["status"],
        "status_label": PLAN_STATUS_PLAIN.get(plan["status"], plan["status"]),
        "look": {"slot": slot, "filled": filled},
        "countdown": {"value": remaining, "hint": hint},
        "timing_lines": timing_lines(timing_for_plan(plan), tz, simple=simple, noted=bool(note), hour12=hour12),
        "note": note,
        "outcome": plan.get("outcome"),
        "mission_id": plan.get("mission_id"),
        "actions": {**actions, "cancel": plan["status"] == "PLANNED"},
        "commands": {"rehearse": rehearse.command, "run": real.command, "stop": stop},
        "confirm_warning": _START_WARNING.format(name=plan["target_name"]),
        "rehearsal": rehearsal,
        "busy": busy,
        "busy_pid": record.get("pid") if busy else None,
        "busy_kind": ("Rehearsal" if record.get("rehearse") else "Mission") if busy else None,
        "started_command": (record or {}).get("command") if busy else None,
        "mission_running": running,
        "log_tail": log_tail(record.get("log")) if record and not busy else "",
        "closed_window": closed_window_sentence(plan) if plan["status"] in ("DONE", "MISSED") else None,
    })


def plans_payload(db: Database, settings, *, simple: bool, hour12: bool) -> dict[str, Any]:
    plans = load_plans(db)
    launches = read_launches(settings.data_dir)
    live, earlier = [], []
    for plan in plans:
        card = _plan_card(db, settings, plan, launches.get(str(plan["id"])), simple=simple, hour12=hour12)
        if plan["status"] in ("PLANNED", "STARTED"):
            live.append(card)
        else:
            earlier.append(card)
    return {"live": live, "earlier": earlier}


def do_launch(settings, plan_id: int, *, rehearse: bool) -> dict[str, Any]:
    spec = launch_spec(
        plan_id, rehearse=rehearse, data_dir=settings.data_dir, default_data_dir=settings.data_dir,
    )
    try:
        record = launch_plan(spec, data_dir=settings.data_dir)
    except LaunchError as exc:
        return {"ok": False, "error": str(exc)}
    if rehearse:
        notice = (
            f"Rehearsal started (pid {record['pid']}). The plan is left as it is. "
            "This card follows the simulated mission as it runs."
        )
    else:
        notice = f"Started process {record['pid']}. The plan is checked again before anything is commanded."
    return {"ok": True, "pid": record["pid"], "command": record.get("command"), "notice": notice, "rehearse": rehearse}


def opportunities_payload(
    cache: dict, settings, *, force: bool, simple: bool, hour12: bool, db: Database | None,
) -> dict[str, Any]:
    reports, errors = get_reports(cache, settings, force=force)
    planned = []
    if db is not None:
        db.expire_planned(datetime.now(timezone.utc))
        planned = db.list_planned(status=LIVE_PLAN_STATUSES)
    kinds = []
    for report in reports:
        n = sum(1 for o in report["opportunities"] if o["reachable"])
        kinds.append({
            "plugin": report["plugin"],
            "label": report["label"],
            "reachable": n,
            "n_candidates": report.get("n_candidates") or 0,
            "words": words_for(report),
            "summary": (report.get("summary") or "").strip(),
            "planning": report.get("planning", True),
            "caveats": report.get("caveats") or [],
            "limits": report.get("limits") or {},
            "limits_text": report.get("limits_text"),
            "data": report.get("data") or {},
            "site": report.get("site") or {},
            "horizon_days": report.get("horizon_days") or 30,
            "has_transit_facts": any((o.get("facts") or {}).get("depth_pct") is not None for o in report["opportunities"]),
            "tasks": sorted({o["network"]["task"] for o in report["opportunities"] if o["reachable"]}),
        })
    compact = []
    for report in reports:
        for op in report["opportunities"]:
            compact.append(serialize_opportunity(op, report, planned, simple=simple, hour12=hour12))
    return jsonable({
        "errors": errors,
        "kinds": kinds,
        "opportunities": compact,
        "planned_live": sum(1 for p in planned if p.get("status") in ("PLANNED", "STARTED")),
        "site_banner": site_banner(getattr(settings, "site_source", "default")),
        "task_labels": TASK_PLAIN if simple else TASK_TECHNICAL,
    })


def serialize_opportunity(op: dict, report: dict, planned: list, *, simple: bool, hour12: bool) -> dict[str, Any]:
    tz = local_tz()
    now = datetime.now(timezone.utc)
    net = op.get("network") or {}
    impact = op.get("impact") or {}
    level = impact.get("level", "unknown")
    style = IMPACT_STYLE.get(level, IMPACT_STYLE["unknown"])
    facts = op.get("facts") or {}
    keys = planned_keys(planned)
    night = (report.get("words") or {}).get("chance") == "night"
    duration_h = None if night else (facts.get("duration_h"))
    windows = []
    for w in op.get("windows") or []:
        windows.append({
            **w,
            "label": window_label(w, tz, now, simple=simple, baseline_min=(report.get("limits") or {}).get("baseline_min"), hour12=hour12),
            "timing_lines": timing_lines(
                timing_for_window(w, duration_h=duration_h, night=night), tz, simple=simple, hour12=hour12,
            ),
        })
    existing = None
    if op.get("windows"):
        existing = planned_id(keys, op["name"], op["windows"][0])
    attempts = finished_attempts(planned, op["name"], op["windows"][0]) if op.get("windows") else []
    plan_status = next((p["status"] for p in planned if p["id"] == existing), None) if existing else None
    task = TASK_PLAIN.get(net.get("task"), net.get("task")) if simple else TASK_TECHNICAL.get(net.get("task"), net.get("task"))
    return {
        "id": op.get("id"),
        "plugin": report["plugin"],
        "plugin_label": report["label"],
        "name": op.get("name"),
        "host": op.get("host"),
        "reachable": bool(op.get("reachable")),
        "blockers": op.get("blockers") or [],
        "task": net.get("task"),
        "task_label": task,
        "reason": net.get("reason"),
        "score": net.get("score"),
        "sources": net.get("sources"),
        "source_line": source_names(net.get("sources") or []),
        "terms": net.get("terms") if not simple else None,
        "impact_level": level,
        "impact_label": style[1] if simple else style[2],
        "impact_slot": {"high": "green", "medium": "blue", "low": "gray", "unknown": "gray"}.get(level, "gray"),
        "headline": impact.get("headline"),
        "assumptions": impact.get("assumptions") or [],
        "figures": impact.get("figures") if not simple else None,
        "facts": facts,
        "detail": op.get("detail"),
        "schedule": op.get("schedule"),
        "first_light": op.get("first_light"),
        "windows": windows,
        "planned_id": existing,
        "plan_status": plan_status,
        "earlier": earlier_attempt_line(attempts) if attempts else None,
        "planning": report.get("planning", True),
        "target": op.get("target"),
    }


def opportunity_track(cache: dict, settings, plugin: str, op_id: str, window_index: int, *, hour12: bool) -> dict[str, Any] | None:
    reports, _errors = get_reports(cache, settings, force=False)
    report = next((r for r in reports if r["plugin"] == plugin), None)
    if report is None:
        return None
    op = next((o for o in report["opportunities"] if str(o.get("id")) == str(op_id)), None)
    if op is None or not op.get("windows"):
        return None
    idx = max(0, min(window_index, len(op["windows"]) - 1))
    window = op["windows"][idx]
    now = datetime.now(timezone.utc)
    track = altitude_track(op, window, report.get("site") or {}, now)
    if not track:
        return None
    tz = local_tz()
    from dashboard.opportunities import altitude_line, altitude_now_line
    samples = [
        {
            "at": s["at"].astimezone(tz).isoformat(),
            "alt_deg": s["alt_deg"],
        }
        for s in track["samples"]
    ]
    return jsonable({
        "samples": samples,
        "peak_alt_deg": track.get("peak_alt_deg"),
        "peak_at": track["peak_at"].astimezone(tz).isoformat() if track.get("peak_at") else None,
        "now_alt_deg": track.get("now_alt_deg"),
        "now_at": track["now_at"].astimezone(tz).isoformat() if track.get("now_at") else None,
        "now_on_curve": track.get("now_on_curve"),
        "rising": track.get("rising"),
        "now_line": altitude_now_line(track, simple=True),
        "altitude_line": altitude_line(track, tz, simple=True, hour12=hour12),
        "high_alt_deg": 70.0,
    })
