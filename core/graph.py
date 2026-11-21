"""LangGraph orchestration state machine.

::

    START -> generate_hypothesis -> check_weather --clear--> execute_observation
                   |                    ^   |                       |
                   |                    |  cloudy                   v
                   |                    |   v                  reduce_data
                   |                    +- sleep                    |
                   |                                                v
                   |                                           cross_reference
                   |                                                |
                   v                          (more targets)--------+--(no targets left)
            wait_for_visit                                                  |
            (before check_weather, only for                                 v
             a target that carries                  screen_anomalies -> package_submission -> write_log -> END
             ``not_before``; archive missions
             skip the weather check)

What a mission produces
    The default product is **upload files** for the databases of the collaborative networks the
    plugin serves (``package_submission``, see :mod:`core.submission`) and a short **mission log**
    (``write_log``, :mod:`core.mission_log`, written as Markdown by :class:`writer.LogWriter`).
    ``screen_anomalies`` flags results at or above ``settings.anomaly_min_sigma`` (the bar is enforced
    here, in :mod:`core.anomaly`, not by the plugin); they appear in the log as leads for a person to
    review.  Nothing is ever sent anywhere: upload files are
    for a person to review.

Long cadences
    Targets may carry ``not_before`` (a survey visit that is not due yet).  ``wait_for_visit`` blocks
    on the mission clock (:mod:`core.clock`) until that instant: wall time for a telescope, replayed
    survey time for an archive.  Everything is checkpointed, so killing the process during a
    multi-day wait and starting it again resumes the wait.  See :mod:`core.lease` for how several
    processes run several such missions side by side.

The graph never mentions any science case: it delegates to the plugin
(``generate_targets``, ``get_exposure_plan``, ``analyze_frames``, ``cross_reference``,
``detect_anomalies``, ``build_submissions``) and to the tool adapters.

Persistence
    * LangGraph checkpoints (SQLite) make missions resumable: ``run_mission`` resumes from
      the last completed node after a crash/restart.
    * Every node start/end/error and every Astronomo log line is mirrored into the
      ``log_events`` table for the dashboard.

Failure policy
    A node that raises marks the mission ``FAILED`` and routes to ``END``.  Deliberate exceptions,
    all of which keep the data and say so in the log: cross-reference failures (catalog outages are
    recorded and the mission continues with unverified findings), a plugin whose
    ``detect_anomalies`` or ``build_submissions`` raises (recorded; the mission goes on without
    that output) and language-model failures (recorded; the text falls back to deterministic
    wording) -- the last unless ``settings.llm_strict`` is on.

Language model (optional, advisory)
    If ``LLM_PROVIDER`` is set, ``generate_hypothesis`` asks the model for a short observing
    rationale; the mission log and upload files never contain model text.  The model
    sees only a short digest of scalar fields (the target list), built with :mod:`core.digest`;
    it never receives ``findings``, FITS data or tables, and nothing it writes feeds back into
    weather gating, hardware control, detection or classification.  A plugin's
    ``summarize_for_llm`` is the digest contract for any later use of a model on results.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from config import Settings
from core import anomaly as anomaly_screen
from core import interlocks
from core import setup_checks
from core import submission as submission_files
from core.archive import ArchiveError
from core.base_plugin import BaseExperimentPlugin
from core.clock import Clock, ReplayClock, SystemClock, clock_from_state, parse_time
from core.digest import scalars_only, short
from core.frame_watch import FrameWatcher, format_row, save_stats
from core.llm import LLM_ERRORS, LLMClient, LLMResponse
from core import findings as findings_page
from core.mission_log import build_log, with_preview
from core.readiness import dew_point_c, disk_reading
from core.retry import backoff_delay, is_transient
from core.state import (
    NODE_STATUS,
    MissionState,
    MissionStatus,
    to_jsonable,
)
from db.database import Database
from tools.astrometry import sun_altitude_deg
from tools.autofocus import IMPROVED, NO_IMPROVEMENT, run_autofocus
from tools.catalogs import CatalogClient
from tools.telescope import SeestarController, StopRequested, TelescopeError, slugify
from tools.weather import WeatherClient, WeatherUnavailable, saved_hours
from writer import LogWriter, write_findings

log = logging.getLogger(__name__)

FAILED = MissionStatus.FAILED.value
ABORTED = MissionStatus.ABORTED.value
COMPLETE = MissionStatus.COMPLETE.value
STOPPED = MissionStatus.STOPPED.value

# --------------------------------------------------------------------------- #
# Runtime wiring
# --------------------------------------------------------------------------- #


@dataclass
class AstronomoRuntime:
    """Everything a node needs; built once in ``main.py`` and closed over by the graph."""

    settings: Settings
    plugin: BaseExperimentPlugin
    telescope: SeestarController
    weather: WeatherClient
    catalogs: CatalogClient
    writer: LogWriter
    db: Database
    sleep_fn: Callable[[float], None] = time.sleep
    llm: LLMClient | None = None  # None / provider "none" => no model, deterministic text only
    #: Force one clock for every mission (tests).  ``None``: a per-mission clock from the settings.
    clock: Clock | None = None
    #: Seconds between DB updates (heartbeat-friendly) while blocked on the mission clock.
    wait_tick_s: float = 1.0
    _clocks: dict[str, Clock] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- #
# Mirror python logging into the telemetry table
# --------------------------------------------------------------------------- #
_node_ctx: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("node_ctx", default=None)
_ASTRONOMO_LOGGERS = {"core", "tools", "plugins", "writer", "db", "main"}


class DBLogHandler(logging.Handler):
    """Persists Astronomo log records emitted *while a node is running*."""

    def __init__(self, db: Database, level: int = logging.INFO):
        super().__init__(level)
        self.db = db

    def emit(self, record: logging.LogRecord) -> None:
        ctx = _node_ctx.get()
        if ctx is None or record.name.split(".")[0] not in _ASTRONOMO_LOGGERS:
            return
        try:
            self.db.add_log(ctx[0], record.getMessage(), node=ctx[1], level=record.levelname)
        except Exception:  # noqa: BLE001 - never let telemetry break the mission
            self.handleError(record)


def attach_db_logging(db: Database) -> DBLogHandler:
    handler = DBLogHandler(db)
    logging.getLogger().addHandler(handler)
    return handler


def detach_db_logging(handler: DBLogHandler) -> None:
    logging.getLogger().removeHandler(handler)


# --------------------------------------------------------------------------- #
# Node implementations  (signature: (rt, state) -> partial state update)
# --------------------------------------------------------------------------- #


def _current_target(state: MissionState) -> dict:
    return state["targets"][state.get("target_index", 0)]


def _is_archive(state: MissionState) -> bool:
    return state.get("data_source") == "archive"


# --------------------------------------------------------------------------- #
# Mission clock
# --------------------------------------------------------------------------- #
def _mission_clock(rt: AstronomoRuntime, state: MissionState, *, create: bool = True) -> Clock | None:
    """The clock of this mission (built once per process, restored from the DB after a restart).

    Archive missions replay survey time from their first visit; telescope missions use wall time.
    ``create=False`` returns ``None`` instead of inventing an archive origin before targets exist.
    """
    mission_id = state["mission_id"]
    clock = rt._clocks.get(mission_id)
    if clock is not None:
        return clock
    if rt.clock is not None:
        clock = rt.clock
    else:
        saved = (rt.db.get_mission(mission_id) or {}).get("clock")
        if saved:
            clock = clock_from_state(saved)
            log.info("Restored the mission clock: %s", clock.state())
        elif _is_archive(state):
            starts = [parse_time(t["not_before"]) for t in state.get("targets", []) if t.get("not_before")]
            if not starts and not create:
                return None
            origin = min(starts) if starts else datetime.now(timezone.utc)
            clock = ReplayClock(origin, rt.settings.replay_speed)
        else:
            clock = SystemClock()
    rt._clocks[mission_id] = clock
    _save_clock(rt, mission_id, clock)
    return clock


def _save_clock(rt: AstronomoRuntime, mission_id: str, clock: Clock) -> None:
    try:
        rt.db.update_mission(mission_id, clock=clock.state(), sim_time=clock.now())
    except Exception:  # noqa: BLE001 - telemetry must never break the mission
        log.debug("could not persist the clock", exc_info=True)


# --------------------------------------------------------------------------- #
# Language-model helpers (advisory prose only)
# --------------------------------------------------------------------------- #
LLM_SYSTEM_PROMPT = (
    "You are the writing assistant of an automated observatory. You receive a short JSON summary "
    "produced by deterministic software; the raw images and catalogs are NOT available to you. "
    "Rules: use only facts and numbers present in the JSON; never invent measurements, object names, "
    "references or dates; never claim or imply a discovery; write plain text only (no markdown, no "
    "bullet lists); be concise."
)


def _llm_enabled(rt: AstronomoRuntime) -> bool:
    return rt.llm is not None and rt.llm.enabled


def _llm_ask(
    rt: AstronomoRuntime, state: MissionState, node: str, task: str, data: dict, max_tokens: int
) -> tuple[LLMResponse | None, str | None]:
    """One guarded model call.  Returns ``(response, None)`` or ``(None, error_message)``.

    Failures never propagate unless ``llm_strict``: the mission continues with deterministic text.
    """
    guidance = rt.plugin.llm_guidance().strip()
    system = LLM_SYSTEM_PROMPT + (f" {guidance}" if guidance else "")
    try:
        assert rt.llm is not None
        return rt.llm.ask(task, data, system=system, mission_id=state["mission_id"], node=node, max_tokens=max_tokens), None
    except LLM_ERRORS as exc:
        if rt.settings.llm_strict:
            raise
        log.warning("LLM call skipped (%s): %s", node, exc)
        return None, f"llm({node}): {exc}"


def generate_hypothesis(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Ask the plugin what to observe and why."""
    plan_id = state.get("plan_id")
    if plan_id is not None:  # built from a planned observation: observe exactly what the person planned
        plan = rt.db.get_planned(plan_id)
        if plan is None:
            raise ValueError(f"planned observation #{plan_id} no longer exists")
        targets = rt.plugin.validate_targets([plan["target"]])
        log.info("Mission built from plan #%s (%s)", plan_id, plan["target_name"])
    else:
        targets = rt.plugin.validated_targets()
    # Scheduled visits go in time order; unscheduled targets by priority (the original behaviour).
    targets.sort(key=lambda t: (t.get("not_before") or "", -float(t.get("priority", 0.0))))
    targets = targets[: rt.plugin.target_limit()]
    if not targets:
        log.warning("Plugin '%s' produced no targets; nothing to observe", rt.plugin.name)
        return {"status": ABORTED, "targets": [], "errors": ["plugin returned no targets"]}

    ids = rt.db.save_targets(state["mission_id"], targets)
    for tgt, db_id in zip(targets, ids):
        tgt["db_id"] = db_id
    hypothesis = rt.plugin.describe_hypothesis(targets)
    rt.db.update_mission(state["mission_id"], hypothesis=hypothesis)
    log.info("Hypothesis ready: %d target(s): %s", len(targets), ", ".join(t["name"] for t in targets))
    update: dict[str, Any] = {
        "targets": targets,
        "target_index": 0,
        "hypothesis": hypothesis,
        "sleep_cycles": 0,
        # Observing windows are enforced only where the site interlocks are, on a telescope (an archive has no sky).
        "windows_enforced": bool(rt.settings.interlocks_active and not _is_archive(state)),
        "status": MissionStatus.HYPOTHESIS.value,
    }

    if _llm_enabled(rt):
        data = {
            "experiment": rt.plugin.name,
            "description": short(rt.plugin.description, 200),
            "simulated": bool(state.get("simulated")),
            # Scalars only: drops nested simulation truth and anything array-like.  The schedule
            # window is "now"-relative noise to the model and would defeat the response cache.
            "targets": [scalars_only(t, exclude=("db_id", "window_start", "window_end")) for t in targets],
        }
        resp, err = _llm_ask(
            rt, state, "generate_hypothesis",
            "In at most 3 sentences, explain why these targets are worth observing and in what order "
            "(higher priority is observed first). Quote numbers exactly as given and add none.",
            data, max_tokens=160,
        )
        if resp:
            update["hypothesis_rationale"] = resp.text
        if err:
            update["errors"] = [err]
    return update


def _fmt_span(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds >= 172800:
        return f"{seconds / 86400:.1f} d"
    if seconds >= 7200:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 120:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


def wait_for_visit(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Block on the mission clock until the current target's ``not_before`` instant.

    The wait is the long-cadence primitive: a survey that revisits a field every few days is a
    mission that spends most of its life here.  The DB row shows ``WAITING`` with ``wake_at``;
    the lease heartbeat (a separate thread) keeps the mission owned while this blocks.  If the
    process dies, the checkpoint still points at this node and a restart simply waits again.
    """
    mission_id = state["mission_id"]
    idx = state.get("target_index", 0)
    target = _current_target(state)
    not_before = target.get("not_before")
    if not not_before and state.get("windows_enforced"):
        not_before = target.get("window_start")  # an observing window that has not opened yet
    if not not_before:
        return {"status": MissionStatus.WAITING.value}

    when = parse_time(not_before)
    clock = _mission_clock(rt, state)
    assert clock is not None
    now = clock.now()
    if now >= when:
        log.info("Visit %d/%d (%s) is already due (%s)", idx + 1, len(state["targets"]), target["name"], when.isoformat(timespec="seconds"))
        _save_clock(rt, mission_id, clock)
        return {"status": MissionStatus.WAITING.value}

    gap_s = (when - now).total_seconds()
    log.info(
        "WAITING %s for visit %d/%d (%s) at %s [%s clock, speed %s]",
        _fmt_span(gap_s), idx + 1, len(state["targets"]), target["name"], when.isoformat(timespec="seconds"),
        clock.kind, f"{clock.speed:g}x" if clock.speed else "jump",
    )
    rt.db.update_mission(mission_id, wake_at=when, clock=clock.state(), sim_time=now)
    rt.db.update_progress(
        mission_id, phase="waiting", wake_at=when.isoformat(), wait_started=now.isoformat(), wait_gap_s=gap_s,
        frames_done=None, frames_total=None, detail=f"waiting for visit {idx + 1}/{len(state['targets'])}: {target['name']}",
    )
    clock.wait_until(when, sleep_fn=rt.sleep_fn, tick_s=rt.wait_tick_s, on_tick=lambda _t: _save_clock(rt, mission_id, clock))
    rt.db.update_mission(mission_id, wake_at=None, clock=clock.state(), sim_time=clock.now())
    log.info("Visit %d/%d is due; survey clock now %s", idx + 1, len(state["targets"]), clock.now().isoformat(timespec="seconds"))
    return {"status": MissionStatus.WAITING.value}


def _evaluate_weather(report: dict, settings: Settings) -> tuple[bool, str]:
    cloud = report.get("cloud_cover_pct")
    assumed = False
    if cloud is None:
        if not settings.assume_clear_sky:
            return False, "weather data unavailable (fail-safe: not observing)"
        cloud = 0.0
        assumed = True
    if cloud > settings.cloud_cover_threshold_pct:
        return False, f"cloud cover {cloud:.0f}% exceeds {settings.cloud_cover_threshold_pct:.0f}%"
    if (report.get("wind_kmh") or 0.0) > settings.max_wind_kmh:
        return False, f"wind {report['wind_kmh']:.0f} km/h exceeds {settings.max_wind_kmh:.0f} km/h"
    if (report.get("precipitation_mm") or 0.0) > 0.0:
        return False, f"precipitation {report['precipitation_mm']:.1f} mm"
    temp, humid = report.get("temperature_c"), report.get("humidity_pct")
    if temp is not None and humid is not None:
        margin = float(temp) - dew_point_c(float(temp), float(humid))
        if margin < settings.dew_margin_c:
            return False, f"dew margin {margin:.1f} C is under {settings.dew_margin_c:.0f} C"
    sun = report.get("sun_altitude_deg")
    if sun is not None and sun > settings.max_sun_altitude_deg:
        return False, f"Sun altitude {sun:+.0f} deg above {settings.max_sun_altitude_deg:+.0f} deg (not dark enough)"
    if assumed:
        return True, "operator asserted a clear sky; no cloud, wind, or rain measurement was made"
    return True, f"cloud cover {cloud:.0f}% within limit"


def _skip_target(rt: AstronomoRuntime, state: MissionState, verdict: interlocks.Verdict) -> dict:
    """Give up on the current target for good (its window closed) and move on to the next one."""
    idx = state.get("target_index", 0)
    target = _current_target(state)
    log.warning("Skipping %s: %s", target["name"], verdict.reason)
    if target.get("db_id"):
        rt.db.update_target(target["db_id"], status="SKIPPED")
    update: dict[str, Any] = {
        "target_index": idx + 1, "target_decision": "skip", "sleep_cycles": 0,
        "skipped": [{"name": target["name"], "kind": "window_closed", "reason": verdict.reason}],
        "status": MissionStatus.WEATHER_CHECK.value,
    }
    if idx + 1 >= len(state["targets"]) and not state.get("observations"):
        update["status"] = ABORTED
        update["errors"] = [f"nothing was observed: {target['name']} was skipped because {verdict.reason}"]
    return update


def check_weather(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Cloud cover above the threshold (or any unsafe condition) transitions to SLEEP.

    On real hardware this is also where the site interlocks (:mod:`core.interlocks`) are enforced for the
    target about to be observed: a target that is too low, too near the Sun or the Moon, or not yet in its window
    is waited for like bad weather, and one whose window has closed is skipped.
    """
    verdict: interlocks.Verdict | None = None
    if rt.settings.interlocks_active and state.get("targets"):
        clock = _mission_clock(rt, state)
        assert clock is not None
        verdict = interlocks.evaluate(
            _current_target(state), clock.now(),
            (rt.settings.site_latitude_deg, rt.settings.site_longitude_deg, rt.settings.site_elevation_m),
            interlocks.limits_from_settings(rt.settings),
        )
        if verdict.skip:
            return _skip_target(rt, state, verdict)
    try:
        report = rt.weather.get_conditions().as_dict()
    except WeatherUnavailable as exc:
        log.error("Weather unavailable: %s", exc)
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cloud_cover_pct": None, "wind_kmh": None, "precipitation_mm": None,
            "seeing_arcsec": None, "source": "unavailable", "error": str(exc),
        }
    if rt.settings.interlocks_active:  # the darkness interlock protects real optics; pointless for the mock
        report["sun_altitude_deg"] = sun_altitude_deg(
            rt.settings.site_latitude_deg, rt.settings.site_longitude_deg, rt.settings.site_elevation_m
        )
    clear, reason = _evaluate_weather(report, rt.settings)
    if report.get("cloud_cover_pct") is None and rt.settings.assume_clear_sky:
        log.warning("ASTRONOMO_ASSUME_CLEAR is set: %s", reason)
        if clear:
            report["source"] = "operator"
            report["cloud_cover_pct"] = 0.0
            report["override"] = "assume_clear"
    if verdict is not None and not verdict.ok:
        report["interlocks"] = verdict.facts
        reason = verdict.reason if clear else f"{reason}; {verdict.reason}"
        clear = False
    rt.db.add_weather(state["mission_id"], report, clear, reason)
    log.info("Weather [%s]: %s -> %s", report.get("source"), reason, "CLEAR" if clear else "SLEEP")
    update: dict[str, Any] = {
        "weather": {**report, "clear": clear, "reason": reason},
        "status": MissionStatus.WEATHER_CHECK.value if clear else MissionStatus.SLEEP.value,
        "target_decision": "observe" if clear else "wait",
    }
    if clear:
        update["sleep_cycles"] = 0
    return update


def _defer_until(rt: AstronomoRuntime, state: MissionState) -> datetime:
    """When to look at the weather again: the next saved clear hour, or one sleep interval if the file has none."""
    clock = _mission_clock(rt, state)
    assert clock is not None
    now = clock.now()
    gap = rt.settings.sleep_interval_s if rt.settings.sleep_interval_s > 0 else 60.0
    fallback = now + timedelta(seconds=gap)
    hours = saved_hours(rt.settings) or []
    for hour in hours:
        if not isinstance(hour, dict) or hour.get("cloud_cover_pct") is None:
            continue
        try:
            start = parse_time(hour["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < now:
            continue  # this hour already began; the live check just failed it
        if float(hour["cloud_cover_pct"]) > rt.settings.cloud_cover_threshold_pct:
            continue
        if float(hour.get("precipitation_mm") or 0.0) > 0.0 or float(hour.get("wind_kmh") or 0.0) > rt.settings.max_wind_kmh:
            continue
        return start
    return fallback


def sleep(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Wait, then loop back to ``check_weather``.

    Site limits that never clear still give up on the mission. Weather that never clears does not: the mission
    is handed back with a wake time (``release``) so no process is held, and a later run checks again.
    """
    cycles = int(state.get("sleep_cycles", 0)) + 1
    if cycles > rt.settings.max_sleep_cycles:
        if (state.get("weather") or {}).get("interlocks"):
            msg = f"site limits never cleared after {rt.settings.max_sleep_cycles} sleep cycles"
            log.error("Aborting mission: %s", msg)
            return {"status": ABORTED, "sleep_cycles": cycles, "errors": [msg]}
        when = _defer_until(rt, state)
        target = _current_target(state) if state.get("targets") else {}
        end = target.get("window_end") if state.get("windows_enforced") else None
        if end and parse_time(end) <= when:
            verdict = interlocks.Verdict(
                ok=False, skip=True, reasons=["the sky stayed closed until the observing window ended"],
            )
            return _skip_target(rt, state, verdict)
        log.warning("Weather still closed after %d checks; resuming at %s", cycles, when.isoformat(timespec="seconds"))
        rt.db.update_mission(state["mission_id"], wake_at=when, status=MissionStatus.SLEEP.value)
        return {
            "status": MissionStatus.SLEEP.value, "sleep_cycles": cycles,
            "deferred_until": when.isoformat(), "target_decision": "defer",
        }
    reason = (state.get("weather") or {}).get("reason", "unsafe conditions")
    if rt.settings.assume_clear_sky and "weather data unavailable" in reason:
        log.warning("Skipping the weather wait because ASTRONOMO_ASSUME_CLEAR is set.")
        return {"status": MissionStatus.SLEEP.value, "sleep_cycles": cycles}
    log.info("SLEEP %.0f s (cycle %d/%d): %s", rt.settings.sleep_interval_s, cycles, rt.settings.max_sleep_cycles, reason)
    rt.sleep_fn(rt.settings.sleep_interval_s)
    return {"status": MissionStatus.SLEEP.value, "sleep_cycles": cycles}


def release(rt: AstronomoRuntime, state: MissionState) -> dict:
    """The weather wait, once the process has been handed back.

    If the wake time is still ahead, say so and let the runner stop. The checkpoint stays on this node, so the
    next run of the mission checks the time again instead of starting the wait from zero.
    """
    until = state.get("deferred_until")
    clock = _mission_clock(rt, state)
    assert clock is not None
    if until and clock.now() < parse_time(until):
        when = parse_time(until)
        rt.db.update_mission(state["mission_id"], status=MissionStatus.SLEEP.value, wake_at=when)
        log.info("Still waiting on weather until %s", when.isoformat(timespec="seconds"))
        return {"status": MissionStatus.SLEEP.value, "deferred_until": until, "target_decision": "defer"}
    log.info("Weather wait is over; checking the sky again")
    rt.db.update_mission(state["mission_id"], wake_at=None)
    return {
        "status": MissionStatus.WEATHER_CHECK.value, "sleep_cycles": 0,
        "deferred_until": None, "target_decision": "observe",
    }


def _execute_archive(rt: AstronomoRuntime, state: MissionState, idx: int, target: dict, frames_dir: Path) -> dict:
    """Archive counterpart of the capture step: pull one visit's frames from the plugin's source.

    Frames are released at the pace of the mission clock (``on_frame`` blocks until the frame's
    real observation time), so a real-time replay shows frames "arriving" exactly as the survey
    took them, and the dashboard's progress bar is meaningful.
    """
    mission_id = state["mission_id"]
    source = rt.plugin.archive_source()
    clock = _mission_clock(rt, state)
    assert clock is not None

    stale = rt.db.discard_incomplete_observations(mission_id, target.get("db_id"))
    if stale:
        log.warning("Discarded %d incomplete observation record(s) from an interrupted run", stale)
    obs_id = rt.db.create_observation(mission_id, target.get("db_id"), exposure_plan={"source": source.name, "status": "acquiring"})
    n_visits = len(state["targets"])
    log.info("Acquiring visit %d/%d (%s) from %s", idx + 1, n_visits, target["name"], source.name)
    rt.db.update_progress(mission_id, phase="acquiring", frames_done=0, frames_total=None, wake_at=None, source=source.name)

    def on_frame(i: int, total: int, obs_time: datetime | None) -> None:
        if obs_time is not None:
            clock.wait_until(obs_time, sleep_fn=rt.sleep_fn, tick_s=rt.wait_tick_s, on_tick=lambda _t: _save_clock(rt, mission_id, clock))
        _save_clock(rt, mission_id, clock)
        rt.db.update_progress(
            mission_id, phase="acquiring", frames_done=i, frames_total=total,
            frame_time=obs_time.isoformat() if obs_time else None,
        )

    try:
        batch = source.acquire(target, frames_dir, on_frame=on_frame)
    except ArchiveError as exc:
        log.error("Archive acquisition failed for %s: %s", target["name"], exc)
        raise
    if not batch.paths:
        raise ArchiveError(f"{source.name} supplied no usable frames for {target['name']}")

    plan = {"gain": 0, "exposure_s": 0.0, "filter": "", **batch.plan, "count": len(batch.paths)}
    rt.db.update_observation(obs_id, fits_paths=batch.paths, exposure_plan=plan, ended_at=datetime.now(timezone.utc))
    rt.db.update_progress(mission_id, phase="acquired", frames_done=len(batch.paths), frames_total=len(batch.paths))
    log.info("Acquired %d frame(s) for %s", len(batch.paths), target["name"])
    return {
        "fits_paths": batch.paths, "exposure_plan": plan, "observation_id": obs_id, "archive": batch.meta,
        "weather": None, "status": MissionStatus.OBSERVING.value,
    }


def execute_observation(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Capture FITS for the current target using the plugin's exposure plan."""
    idx = state.get("target_index", 0)
    target = _current_target(state)
    frames_dir = rt.settings.missions_dir / state["mission_id"] / "frames" / f"{idx + 1:02d}_{slugify(target['name'])}"
    if _is_archive(state):
        return _execute_archive(rt, state, idx, target, frames_dir)

    plan = rt.plugin.validated_exposure_plan()
    if rt.settings.storage_check_active:
        space = disk_reading(rt.settings.data_dir, int(rt.settings.min_free_gb * 1e9))
        if not space.ok:
            log.error("Not observing: %s", space.detail)
            return {"status": FAILED, "errors": [space.detail]}
    stale = rt.db.discard_incomplete_observations(state["mission_id"], target.get("db_id"))
    if stale:
        log.warning("Discarded %d incomplete observation record(s) from an interrupted run", stale)
    obs_id = rt.db.create_observation(
        state["mission_id"], target.get("db_id"), exposure_plan=plan, weather=state.get("weather")
    )
    log.info(
        "Observing %s (%d/%d): %d x %.0f s, gain %s, filter %s",
        target["name"], idx + 1, len(state["targets"]), plan["count"], plan["exposure_s"], plan["gain"], plan["filter"],
    )
    # The simulated sky is the plugin's business; real hardware never sees a scene.
    scene = rt.plugin.mock_scene(target) if rt.telescope.mock else None
    check_setup = rt.settings.setup_checks_active and not state.get("setup")
    saved: list[str] = []
    report: dict[str, Any] | None = None
    rt.telescope.stop_check = lambda: bool((rt.db.get_mission(state["mission_id"]) or {}).get("stop_requested"))

    focus_result: dict[str, Any] | None = None
    focus_due = rt.settings.autofocus_active and not state.get("autofocus")

    def before_exposures() -> None:
        # Once per mission, on target and before the first exposure.  A pointing correction runs the
        # capture again; the result already in hand keeps it from focusing twice.
        nonlocal focus_result
        if focus_due and focus_result is None:
            focus_result = autofocus_step(rt, state, target, plan)

    watcher = FrameWatcher(warn_fraction=rt.settings.frame_watch_warn_fraction) if rt.settings.frame_watch_active else None
    recentre_allowed = True  # off once the corrections are used up: a target on the chip is then accepted where it is

    def after_frame(index: int, path: str) -> None:
        nonlocal report
        saved.append(path)
        if index == 0 and check_setup:
            solved = None
            try:
                solved = rt.telescope.solve_pointing()
            except TelescopeError as exc:  # the scope's own solver is a bonus check, never a reason to stop
                log.warning("Telescope plate solve unavailable: %s", exc)
            report = _first_frame_checks(rt, state, target, path, scene, solved, recentre_allowed=recentre_allowed)
        if watcher is not None:
            _watch_frame(rt, state, watcher, index, path, int(plan["count"]))

    try:
        with rt.telescope:
            command = dict(target)
            nudges = rt.settings.pointing_max_nudges
            attempt = 0
            while True:
                try:
                    paths = rt.telescope.capture_sequence(
                        command, plan, frames_dir, scene=scene, after_frame=after_frame, before_exposures=before_exposures,
                    )
                    break
                except setup_checks.Repoint as exc:
                    if attempt >= nudges:
                        if exc.optional:  # on the chip, only off-centre: keep it, and do not ask again
                            recentre_allowed = False
                            msg = f"Re-centring stopped after {nudges} correction(s); the target is on the chip, so the sequence goes ahead where it is."
                            log.warning("%s", msg)
                            rt.db.add_log(state["mission_id"], msg, node="execute_observation", kind="setup_check", level="WARNING")
                            continue
                        raise setup_checks.SetupCheckError(
                            f"The target is still off the chip after {nudges} pointing correction(s). {exc}"
                        )
                    attempt += 1
                    what = "Re-centring" if exc.optional else "Pointing correction"
                    log.info(
                        "%s %d/%d for %s: slewing to RA %.4f deg, Dec %.4f deg",
                        what, attempt, nudges, target["name"], exc.ra_deg, exc.dec_deg,
                    )
                    rt.db.add_log(
                        state["mission_id"],
                        f"{what} {attempt}/{nudges}: slew to RA {exc.ra_deg:.4f} deg, Dec {exc.dec_deg:.4f} deg. {exc}",
                        node="execute_observation", kind="setup_check",
                    )
                    command = {**command, "ra_deg": exc.ra_deg, "dec_deg": exc.dec_deg}
    except StopRequested:
        log.warning("Stop requested during the exposure of %s; the telescope was parked", target["name"])
        return {
            "status": STOPPED,
            "errors": ["Stopped by the operator. The telescope was parked and will stay stopped."],
        }
    except setup_checks.SetupCheckError:
        if saved:
            rt.db.update_observation(obs_id, fits_paths=saved, ended_at=datetime.now(timezone.utc))
        raise
    rt.db.update_observation(obs_id, fits_paths=paths, ended_at=datetime.now(timezone.utc))
    update: dict[str, Any] = {
        "fits_paths": paths, "exposure_plan": plan, "observation_id": obs_id, "status": MissionStatus.OBSERVING.value,
    }
    if report is not None:
        update["setup"] = report
    if focus_result is not None:
        update["autofocus"] = focus_result
    return update


def _watch_frame(rt: AstronomoRuntime, state: MissionState, watcher: FrameWatcher, index: int, path: str, total: int) -> None:
    """Measure the frame just saved, say so in the console and the mission log, and refresh what the dashboard shows.

    Runs between two exposures, so it is quick (about 2 s) and can never raise: a failure here is a log line, not a
    lost night.
    """
    mission_dir = rt.settings.missions_dir / state["mission_id"]
    try:
        row = watcher.update(index, path)
        line = format_row(row, total)
        log.info("Frame watch: %s", line)
        rt.db.add_log(state["mission_id"], line, node="execute_observation", kind="frame_stats")
        for warning in row.get("warnings", []):
            log.warning("Frame watch: frame %d: %s", index + 1, warning)
            rt.db.add_log(state["mission_id"], f"Frame {index + 1}: {warning}", node="execute_observation", kind="frame_stats", level="WARNING")
        save_stats(watcher.rows, mission_dir)
        setup_checks.save_first_frame_view(path, mission_dir, setup_checks.LATEST_NAME)
    except Exception as exc:  # noqa: BLE001 - the watch is advice; the exposures matter more
        log.warning("Frame watch failed on frame %d: %s", index + 1, exc)


def autofocus_step(rt: AstronomoRuntime, state: MissionState, target: dict, plan: dict) -> dict:
    """Sweep the focuser once, before the first exposure.  Never fatal.

    ``main.py --focus-step`` calls this same function on its own, so a focus run can be tried without a mission.
    It reads only ``rt.settings``, ``rt.telescope``, ``rt.sleep_fn`` and ``rt.db.add_log``.

    Every step goes to the log and to the mission's log table (kind ``autofocus``), so a run shows what the
    focuser did and what the sharpness was at each position.  A failure is logged and the mission carries on:
    the first-frame focus check, which runs next, is what decides whether the night goes ahead.  A stop
    request is not swallowed.
    """
    mission_id = state["mission_id"]

    def say(line: str, level: int = logging.INFO) -> None:
        log.log(level, "Autofocus: %s", line)
        rt.db.add_log(mission_id, line, node="execute_observation", kind="autofocus", level=logging.getLevelName(level))

    s = rt.settings
    say("starting before the first exposure; this moves the Seestar's focuser (unconfirmed on a real scope)"
        + (" [simulated focuser]" if rt.telescope.mock else ""))
    try:
        device = rt.telescope.focuser(target)
        report = run_autofocus(
            device, setup_checks.measure_star_width,
            exposure_s=s.focus_exposure_s, gain=int(plan["gain"]), n_points=s.focus_steps,
            half_width_frac=s.focus_half_width_frac, max_widenings=s.focus_max_widenings,
            min_improvement=s.focus_min_improvement, target_fwhm_px=s.focus_target_fwhm_px, sleep=rt.sleep_fn,
            settle_s=0.0 if rt.telescope.mock else 1.0, stop_check=rt.telescope.stop_check,
        )
    except StopRequested:
        raise
    except Exception as exc:  # noqa: BLE001 - focusing is an aid; the first-frame check decides the night
        restored = getattr(exc, "focuser_restored", None)
        tail = {True: " The focuser was put back where it started.", False: " THE FOCUSER WAS NOT PUT BACK: check it in the Seestar app."}.get(restored, "")
        say(f"skipped after an error: {exc}.{tail}  The first-frame focus check will say how sharp the stars are.", logging.WARNING)
        return {"status": "error", "detail": str(exc), "focuser_restored": restored}
    for row in report["samples"]:
        width = "no star measured" if row["fwhm_px"] is None else f"FWHM {row['fwhm_px']:.2f} px ({row['n_stars']} stars)"
        say(f"position {row['position']}: {width}")
    say(f"{report['status']}: {report['detail']}", logging.INFO if report["status"] in (IMPROVED, NO_IMPROVEMENT) else logging.WARNING)
    try:
        mission_dir = rt.settings.missions_dir / mission_id
        mission_dir.mkdir(parents=True, exist_ok=True)
        (mission_dir / "autofocus.json").write_text(json.dumps(report, indent=2, default=str))
    except OSError as exc:
        log.warning("Autofocus: could not save the report: %s", exc)
    return {k: report.get(k) for k in ("status", "detail", "start_position", "final_position", "fwhm_before_px", "fwhm_after_px")}


def _first_frame_checks(
    rt: AstronomoRuntime, state: MissionState, target: dict, frame: str, scene: Any, solved: dict | None,
    recentre_allowed: bool = True,
) -> dict:
    """Focus, pointing and calibration on the first frame; refuse the mission if it is bad.

    The operator's view of the frame is written first, before the checks (which may wait on
    a catalogue) and before this function returns.  The capture loop calls this from
    ``after_frame``, so the picture is on disk before the next exposure.  A missing picture
    does not change the result: the checks still decide the hard stop.
    """
    mission_dir = rt.settings.missions_dir / state["mission_id"]
    preview_name = None
    try:
        preview_name = setup_checks.save_first_frame_view(frame, mission_dir).name
    except Exception as exc:  # noqa: BLE001 - the picture is for the eye; the checks still decide
        log.warning("Could not write the first-frame view: %s", exc)
    report = setup_checks.run_setup_checks(rt.settings, frame, target, rt.catalogs, scene=scene, solved=solved)
    if preview_name:
        report["preview"] = preview_name
    setup_checks.save_report(report, mission_dir)
    for line in setup_checks.format_report(report):
        log.info("Setup check: %s", line)
        rt.db.add_log(state["mission_id"], line, node="execute_observation", kind="setup_check")
    if preview_name:
        log.info("First-frame view written for the operator before the rest of the sequence (%s)", preview_name)
    focus = next((c for c in report["checks"] if c["name"] == "focus"), None)
    pointing = next((c for c in report["checks"] if c["name"] == "pointing (star pattern)"), None)
    if focus is not None and focus["status"] == setup_checks.FAIL:
        raise setup_checks.SetupCheckError(setup_checks.failure_message(report))
    if pointing is not None and pointing.get("action") == "nudge":
        raise setup_checks.Repoint(pointing["next_ra_deg"], pointing["next_dec_deg"], pointing["detail"])
    if recentre_allowed and pointing is not None and pointing.get("action") == "recentre" and report["ok"]:
        raise setup_checks.Repoint(pointing["next_ra_deg"], pointing["next_dec_deg"], pointing["detail"], optional=True)
    if not report["ok"]:
        raise setup_checks.SetupCheckError(setup_checks.failure_message(report))
    return report


def reduce_data(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Hand the FITS frames to the plugin and store its structured findings."""
    findings = rt.plugin.analyze_frames(list(state["fits_paths"]))
    if not isinstance(findings, dict):
        raise TypeError("analyze_frames() must return a dict")
    findings = to_jsonable(findings)
    findings.setdefault("summary", "No summary provided by plugin.")
    findings.setdefault("detections", [])
    findings.setdefault("figures", [])
    rt.db.update_observation(state["observation_id"], findings=findings)
    log.info("Findings: %s", findings["summary"])
    return {"findings": findings, "status": MissionStatus.REDUCING.value}


def cross_reference(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Let the plugin check findings against catalogs, then advance to the next target."""
    idx = state.get("target_index", 0)
    target = _current_target(state)
    errors: list[str] = []
    try:
        xref = to_jsonable(rt.plugin.cross_reference(state["findings"], rt.catalogs, target))
    except Exception as exc:  # noqa: BLE001 - deliberate: catalog outages must not lose the data
        log.exception("Cross-reference failed for %s", target["name"])
        xref = {"summary": f"Cross-reference failed: {exc}", "entries": [], "services": {}, "error": str(exc)}
        errors.append(f"cross_reference({target['name']}): {exc}")
    log.info("Cross-reference: %s", xref.get("summary", ""))

    record = {
        "target": target,
        "weather": state.get("weather"),
        "exposure_plan": state.get("exposure_plan"),
        "fits_paths": state.get("fits_paths"),
        "findings": state["findings"],
        "crossref": xref,
        "observation_id": state.get("observation_id"),
        "archive": state.get("archive") if _is_archive(state) else None,
    }
    rt.db.update_observation(state["observation_id"], crossref=xref)
    if target.get("db_id"):
        rt.db.update_target(target["db_id"], status="OBSERVED")
    update: dict[str, Any] = {
        "crossref": xref,
        "observations": [record],
        "target_index": idx + 1,
        "status": MissionStatus.CROSS_REFERENCING.value,
    }
    if errors:
        update["errors"] = errors
    return update


def _mission_record(rt: AstronomoRuntime, state: MissionState) -> dict:
    """The mission as the plugin hooks and the log writer see it (the ``mission`` argument)."""
    clock = _mission_clock(rt, state, create=False)
    try:
        night = (clock.now() if clock is not None else datetime.now(timezone.utc)).date().isoformat()
    except Exception:  # noqa: BLE001 - a date label must never fail a mission
        night = datetime.now(timezone.utc).date().isoformat()
    return {
        "mission_id": state["mission_id"],
        "plugin_name": state["plugin_name"],
        "hypothesis": state.get("hypothesis"),
        "simulated": state.get("simulated", False),
        "data_source": state.get("data_source", "telescope"),
        "night": night,
        "status": state.get("status"),
        "targets": state.get("targets", []),
        "observations": state.get("observations", []),
        "skipped": state.get("skipped", []),
        "anomalies": state.get("anomalies", []),
        "below_threshold": state.get("below_threshold", []),
        "anomaly_min_sigma": rt.settings.anomaly_min_sigma,
        "submissions": state.get("submissions", []),
        "errors": list(state.get("errors") or []),
        "setup": state.get("setup"),
    }


def _aborted_update(state: MissionState, update: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """Finish an output node's update: an ABORTED mission stays ABORTED, and its problems are only
    logged, because `errors` on an ABORTED mission would overwrite the real abort reason (see
    :func:`_instrument`)."""
    if state.get("status") == ABORTED:
        return {**update, "status": ABORTED}
    return {**update, **({"errors": errors} if errors else {})}


def screen_anomalies(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Ask the plugin for anomalies and keep only those that reach ``anomaly_min_sigma``.

    The threshold is applied here, not by the plugin, so a plugin cannot talk itself into raising an alarm.
    """
    errors: list[str] = []
    try:
        raw = to_jsonable(rt.plugin.detect_anomalies(_mission_record(rt, state)) or [])
    except Exception as exc:  # noqa: BLE001 - a screening bug must not lose the data or the upload files
        log.exception("detect_anomalies failed")
        raw = []
        errors.append(f"screen_anomalies: detect_anomalies failed: {exc}")
    accepted, below, problems = anomaly_screen.screen(raw, rt.settings.anomaly_min_sigma)
    for problem in problems:
        log.warning("Anomaly screen: %s", problem)
    errors.extend(f"screen_anomalies: {p}" for p in problems)
    log.info(
        "Anomaly screen (>= %.1f sigma): %d accepted, %d below threshold",
        rt.settings.anomaly_min_sigma, len(accepted), len(below),
    )
    for item in accepted:
        log.warning("ANOMALY: %s (%.1f sigma): %s", item["target"], item["significance_sigma"], item["summary"])
    update = {"anomalies": accepted, "below_threshold": below, "status": MissionStatus.SCREENING.value}
    return _aborted_update(state, update, errors)


def _results_dir(rt: AstronomoRuntime, state: MissionState) -> Path:
    """The one folder a person opens after a mission: upload files, findings summary and mission log together."""
    return rt.settings.missions_dir / state["mission_id"] / "results"


def package_submission(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Write the plugin's upload files (the default product) plus a manifest.  Never sends anything.

    A plugin that raises costs the mission its upload files and nothing else: the error is recorded
    and the log says no file was produced.
    """
    errors: list[str] = []
    mission = _mission_record(rt, state)
    try:
        drafts = list(rt.plugin.build_submissions(mission) or [])
        for draft in drafts:
            if not isinstance(draft, submission_files.SubmissionDraft):
                raise TypeError(f"build_submissions must return SubmissionDraft objects, got {type(draft).__name__}")
    except Exception as exc:  # noqa: BLE001
        log.exception("build_submissions failed")
        drafts = []
        errors.append(f"package_submission: build_submissions failed: {exc}")
    out_dir = _results_dir(rt, state)
    records = submission_files.write_submissions(
        drafts, out_dir,
        mission_id=state["mission_id"],
        simulated=bool(state.get("simulated")),
        data_source=state.get("data_source", "telescope"),
    )
    rt.db.replace_artifacts(
        state["mission_id"], "submission",
        [
            {
                "title": f"{r['network']} {r['kind']}: {r['target']}" if r.get("target") else f"{r['network']} {r['kind']}",
                "path": r.get("path"),
                "status": r["status"],
                "meta": {k: r.get(k) for k in ("network", "kind", "format", "target", "blockers", "notes")},
            }
            for r in records
        ],
    )
    log.info("Submissions: %s", submission_files.summarize(records))
    update = {
        "submissions": records,
        "results_dir": str(out_dir),
        "status": MissionStatus.PACKAGING.value,
    }
    return _aborted_update(state, update, errors)


def write_log(rt: AstronomoRuntime, state: MissionState) -> dict:
    """Write the automated mission log (Markdown).  The last node: it sets the final status."""
    aborted = state.get("status") == ABORTED
    final = ABORTED if aborted else COMPLETE
    mission = with_preview(  # the log says how the mission ended, not the status it had a moment before
        {**_mission_record(rt, state), "status": final}, rt.settings.missions_dir / state["mission_id"]
    )
    llm_meta = None
    if _llm_enabled(rt):  # recorded for provenance; no model-written text goes into the log itself
        llm_meta = {"provider": rt.llm.provider, "model": rt.llm.model, **rt.llm.usage(state["mission_id"])}  # type: ignore[union-attr]
    meta = {
        **mission,
        "llm": llm_meta,
        "plugin_status": rt.plugin.status,
        "plugin_status_note": rt.plugin.status_note,
        "site": {
            "name": rt.settings.site_name,
            "lat": rt.settings.site_latitude_deg,
            "lon": rt.settings.site_longitude_deg,
        },
    }
    basename = f"mission_log_{mission['night']}"
    out_dir = _results_dir(rt, state)
    result = rt.writer.write(build_log(mission), meta, out_dir, basename=basename)
    rt.db.replace_artifacts(
        state["mission_id"], "mission_log",
        [{"title": result.title, "path": result.path, "status": "written", "meta": {"notes": result.notes}}],
    )
    for note in result.notes:
        log.warning("Mission log: %s", note)
    update: dict[str, Any] = {"mission_log": result.as_dict(), "status": final, "results_dir": str(out_dir)}
    # The findings summary sits beside the log. It must never cost the mission its log, so a failure is recorded, not raised.
    try:
        rows, problems = findings_page.collect_rows(rt.plugin, mission.get("observations") or [])
        caveats, caveat_problems = findings_page.collect_caveats(rt.plugin)
        spec = findings_page.build_findings(mission, rows=rows, caveats=caveats, log_name=Path(result.path).name)
        page = write_findings(spec, out_dir, basename=f"findings_{mission['night']}")
        rt.db.replace_artifacts(
            state["mission_id"], "findings",
            [{"title": page.title, "path": page.path, "status": "written",
              "meta": {"notes": page.notes, **findings_page.summary_payload(spec)}}],
        )
        update["findings"] = page.as_dict()
        errors = [*problems, *caveat_problems]
    except Exception as exc:  # noqa: BLE001
        log.exception("findings summary failed")
        errors = [f"write_log: the findings summary could not be written: {exc}"]
    if errors and not aborted:  # on an ABORTED mission `errors` would overwrite the real abort reason
        update["errors"] = errors
    # An aborted mission (e.g. weather) still writes its log, but stays ABORTED.
    return update


NODE_FUNCS: dict[str, Callable[[AstronomoRuntime, MissionState], dict]] = {
    "generate_hypothesis": generate_hypothesis,
    "wait_for_visit": wait_for_visit,
    "check_weather": check_weather,
    "sleep": sleep,
    "release": release,
    "execute_observation": execute_observation,
    "reduce_data": reduce_data,
    "cross_reference": cross_reference,
    "screen_anomalies": screen_anomalies,
    "package_submission": package_submission,
    "write_log": write_log,
}

# --------------------------------------------------------------------------- #
# Instrumentation wrapper
# --------------------------------------------------------------------------- #


def _announce(rt: AstronomoRuntime, state: MissionState, node: str) -> None:
    """Publish "what is this mission doing right now" for the dashboard (never raises)."""
    try:
        targets = state.get("targets") or []
        idx = int(state.get("target_index", 0))
        fields: dict[str, Any] = {
            "phase": NODE_STATUS[node].lower(),
            "node": node,
            "visits_total": len(targets) or None,
            "visit": min(idx + 1, len(targets)) if targets else None,
            "target": targets[idx]["name"] if idx < len(targets) else None,
            "detail": None,
        }
        if node != "execute_observation":
            fields.update(frames_done=None, frames_total=None, frame_time=None)
        if node == "reduce_data":
            fields["detail"] = f"reducing {len(state.get('fits_paths') or [])} frame(s)"
        elif node == "sleep":
            fields["detail"] = (state.get("weather") or {}).get("reason")
        if node not in ("wait_for_visit", "sleep"):
            fields["wake_at"] = None
        rt.db.update_progress(state["mission_id"], **fields)
        clock = _mission_clock(rt, state, create=False)
        if clock is not None:
            _save_clock(rt, state["mission_id"], clock)
    except Exception:  # noqa: BLE001
        log.debug("could not publish progress", exc_info=True)


def _run_with_retry(rt: AstronomoRuntime, name: str, fn: Callable, state: MissionState) -> dict | None:
    """Run a node; re-run it after a transient network/link failure, with exponential backoff.

    Any other exception, and a transient one that outlasts ``settings.node_retries``, propagates
    to ``_instrument`` and fails the mission as before.
    """
    attempt = 0
    while True:
        try:
            return fn(rt, state)
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is transient and retries remain
            if attempt >= rt.settings.node_retries or not is_transient(exc):
                raise
            attempt += 1
            delay = backoff_delay(attempt, rt.settings.node_backoff_s)
            msg = (
                f"{name}: transient failure ({type(exc).__name__}: {exc}); "
                f"retry {attempt}/{rt.settings.node_retries} in {delay:.0f}s"
            )
            log.warning(msg)
            rt.db.add_log(state["mission_id"], msg, node=name, level="WARNING", kind="node_retry")
            rt.sleep_fn(delay)


def _park_for_stop(rt: AstronomoRuntime) -> None:
    tel = rt.telescope
    try:
        if not tel.mock and not tel.connected:
            tel.connect()
        tel.park()
    except TelescopeError:
        log.exception("Could not park after a stop")
    finally:
        if tel.connected:
            try:
                tel.disconnect()
            except TelescopeError:
                log.exception("Could not disconnect after a stop")


def _stopped_update(rt: AstronomoRuntime, mission_id: str, name: str) -> dict:
    _park_for_stop(rt)
    msg = "Stopped by the operator. The telescope was parked and will stay stopped."
    rt.db.add_log(mission_id, msg, node=name, level="WARNING", kind="node_error")
    rt.db.update_mission(mission_id, status=STOPPED, error=msg, wake_at=None, current_node=name)
    rt.db.update_progress(mission_id, phase="stopped", detail=msg, wake_at=None, frames_done=None, frames_total=None)
    return {"status": STOPPED, "current_node": name, "errors": [msg]}


def _instrument(rt: AstronomoRuntime, name: str, fn: Callable) -> Callable[[MissionState], dict]:
    def node(state: MissionState) -> dict:
        mission_id = state["mission_id"]
        token = _node_ctx.set((mission_id, name))
        try:
            if (rt.db.get_mission(mission_id) or {}).get("stop_requested"):
                return _stopped_update(rt, mission_id, name)
            rt.db.add_log(mission_id, f"{name} started", node=name, kind="node_start")
            rt.db.update_mission(mission_id, current_node=name, status=NODE_STATUS[name])
            _announce(rt, state, name)
            try:
                update = to_jsonable(_run_with_retry(rt, name, fn, state) or {})
            except Exception as exc:  # noqa: BLE001 - converted into a FAILED mission below
                log.exception("Node %s failed", name)
                msg = f"{name}: {type(exc).__name__}: {exc}"
                rt.db.add_log(mission_id, msg, node=name, level="ERROR", kind="node_error")
                rt.db.update_mission(mission_id, status=FAILED, error=msg, wake_at=None)
                rt.db.update_progress(mission_id, phase="failed", detail=msg, wake_at=None, frames_done=None, frames_total=None)
                return {"status": FAILED, "current_node": name, "errors": [msg]}

            update["current_node"] = name
            fields: dict[str, Any] = {}
            if "status" in update:
                fields["status"] = update["status"]
            if update.get("errors") and update.get("status") in (ABORTED, FAILED, STOPPED):
                fields["error"] = "; ".join(update["errors"])
            if fields:
                rt.db.update_mission(mission_id, **fields)
            if fields.get("status") in (COMPLETE, ABORTED, FAILED, STOPPED):
                rt.db.update_progress(
                    mission_id, phase=fields["status"].lower(), detail=None, wake_at=None, frames_done=None, frames_total=None
                )
            rt.db.add_log(mission_id, f"{name} finished", node=name, kind="node_end")
            return update
        finally:
            _node_ctx.reset(token)

    node.__name__ = name
    return node


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def _failed(state: MissionState) -> bool:
    return state.get("status") == FAILED


def _start_target(state: MissionState) -> str:
    """Where the current (next) target starts: wait for its visit time, then weather (or, for an
    archive, straight to acquisition: the weather over a telescope is irrelevant to a replay)."""
    targets = state.get("targets") or []
    idx = state.get("target_index", 0)
    if idx < len(targets) and (targets[idx].get("not_before") or (state.get("windows_enforced") and targets[idx].get("window_start"))):
        return "wait_for_visit"
    return "execute_observation" if _is_archive(state) else "check_weather"


def _route_after_hypothesis(state: MissionState) -> str:
    if _failed(state):
        return END
    # Nothing to observe (e.g. the network had nothing suitable): still leave a log saying so.
    return "write_log" if state.get("status") == ABORTED else _start_target(state)


def _route_after_wait(state: MissionState) -> str:
    if _failed(state):
        return END
    return "execute_observation" if _is_archive(state) else "check_weather"


def _route_after_weather(state: MissionState) -> str:
    if _failed(state):
        return END
    if state.get("status") == ABORTED:  # every target was skipped: leave a log saying so
        return "write_log"
    if state.get("target_decision") == "skip":  # the target's window closed: on to the next one
        if state.get("target_index", 0) < len(state.get("targets", [])):
            return _start_target(state)
        return "screen_anomalies"
    return "sleep" if state.get("status") == MissionStatus.SLEEP.value else "execute_observation"


def _route_after_sleep(state: MissionState) -> str:
    if _failed(state):
        return END
    if state.get("status") == ABORTED:
        return "screen_anomalies" if state.get("observations") else "write_log"
    if state.get("deferred_until"):
        return "release"
    if state.get("target_decision") == "skip":
        if state.get("target_index", 0) < len(state.get("targets", [])):
            return _start_target(state)
        return "screen_anomalies"
    return "check_weather"


def _route_after_release(state: MissionState) -> str:
    if _failed(state):
        return END
    if state.get("deferred_until"):
        return "release"
    return "check_weather"


def _guard(route: Callable[[MissionState], str]) -> Callable[[MissionState], str]:
    """A stopped mission ends. It does not resume, and it does not write the night up as if it had finished."""

    def wrapped(state: MissionState) -> str:
        if state.get("status") == STOPPED:
            return END
        return route(state)

    wrapped.__name__ = getattr(route, "__name__", "route")
    return wrapped


def _continue_to(next_node: str) -> Callable[[MissionState], str]:
    def route(state: MissionState) -> str:
        if _failed(state) or state.get("status") == STOPPED:
            return END
        return next_node

    return route


def _route_after_crossref(state: MissionState) -> str:
    if _failed(state):
        return END
    if state.get("target_index", 0) < len(state.get("targets", [])):
        return _start_target(state)
    return "screen_anomalies"


def build_graph(rt: AstronomoRuntime, checkpointer: Any = None):
    """Compile the mission state machine for the runtime's plugin and tools."""
    g = StateGraph(MissionState)
    for name, fn in NODE_FUNCS.items():
        g.add_node(name, _instrument(rt, name, fn))

    g.add_edge(START, "generate_hypothesis")
    g.add_conditional_edges(
        "generate_hypothesis", _guard(_route_after_hypothesis),
        ["wait_for_visit", "check_weather", "execute_observation", "write_log", END],
    )
    g.add_conditional_edges("wait_for_visit", _guard(_route_after_wait), ["check_weather", "execute_observation", END])
    g.add_conditional_edges(
        "check_weather", _guard(_route_after_weather),
        ["sleep", "execute_observation", "wait_for_visit", "check_weather", "screen_anomalies", "write_log", END],
    )
    g.add_conditional_edges(
        "sleep", _guard(_route_after_sleep),
        ["check_weather", "release", "wait_for_visit", "execute_observation", "screen_anomalies", "write_log", END],
    )
    g.add_conditional_edges("release", _guard(_route_after_release), ["check_weather", "release", END])
    g.add_conditional_edges("execute_observation", _continue_to("reduce_data"), ["reduce_data", END])
    g.add_conditional_edges("reduce_data", _continue_to("cross_reference"), ["cross_reference", END])
    g.add_conditional_edges(
        "cross_reference", _guard(_route_after_crossref),
        ["wait_for_visit", "check_weather", "execute_observation", "screen_anomalies", END],
    )
    g.add_conditional_edges("screen_anomalies", _continue_to("package_submission"), ["package_submission", END])
    g.add_conditional_edges("package_submission", _continue_to("write_log"), ["write_log", END])
    g.add_edge("write_log", END)
    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# Running / resuming a mission
# --------------------------------------------------------------------------- #


def mission_config(mission_id: str) -> dict:
    # Sleep loops and multi-target missions need far more than the default 25 steps.
    return {"configurable": {"thread_id": mission_id}, "recursion_limit": 1000}


def has_checkpoint(app: Any, mission_id: str) -> bool:
    return bool(app.get_state(mission_config(mission_id)).values)


def run_mission(
    app: Any,
    mission_id: str,
    initial: MissionState | None = None,
    on_update: Callable[[dict], None] | None = None,
) -> MissionState:
    """Run (or resume) a mission to completion and return its final state.

    If a checkpoint with pending work exists for ``mission_id`` the run resumes from the last
    completed node; otherwise ``initial`` is required.
    """
    cfg = mission_config(mission_id)
    snapshot = app.get_state(cfg)
    if snapshot.next:
        log.info("Resuming mission %s before node(s): %s", mission_id, ", ".join(snapshot.next))
        payload = None
    elif snapshot.values:
        return snapshot.values  # already finished
    else:
        if initial is None:
            raise ValueError(f"No checkpoint for mission {mission_id} and no initial state given")
        payload = initial

    for chunk in app.stream(payload, cfg, stream_mode="updates"):
        if on_update:
            on_update(chunk)
        if _handed_back(chunk):
            log.info("Mission %s is waiting on the weather; this process is stopping until it is run again", mission_id)
            break
    return app.get_state(cfg).values


def _handed_back(chunk: dict) -> bool:
    """True when a weather wait has been given back to the scheduler instead of held in this process."""
    for node, update in (chunk or {}).items():
        if node in ("sleep", "release") and (update or {}).get("target_decision") == "defer":
            return True
    return False
