"""Astronomo CLI.

Examples
--------
End-to-end mock mission (no hardware, no network)::

    python main.py --simulate

Same, but quick (sub-second "captures", short weather sleeps)::

    python main.py --simulate --fast

Show the SLEEP transition by starting under cloud::

    python main.py --simulate --fast --cloudy-checks 2

Real hardware (set ASTRONOMO_MOCK_MODE=false, SEESTAR_HOST, SEESTAR_SHARE_PATH, site coords)::

    ASTRONOMO_MOCK_MODE=false python main.py

Language model (optional; default is none = zero tokens).  Verify a LOCAL model first::

    ollama pull llama3.1
    python main.py --llm-provider ollama --llm-model llama3.1 --llm-check   # connectivity + model
    python main.py --simulate --fast --ollama llama3.1                      # full mock mission

``--ollama [MODEL]`` is a verification shortcut: provider=ollama, strict (any LLM failure fails the
mission) and cache off (so the model is really called).  Cloud, once the local run is clean::

    ANTHROPIC_API_KEY=... python main.py --simulate --fast --llm-provider anthropic --llm-model <model>

Observe something planned on the dashboard's Opportunities page (a person starts it; nothing starts it for you)::

    python main.py --from-plan 3 --simulate --fast     # rehearsal: same steps, simulated sky, the plan is left as it was
    ASTRONOMO_MOCK_MODE=false python main.py --from-plan 3   # the real run; waits for the window, then observes

Save the forecast before joining the telescope Wi-Fi (that network has no internet), then leave
``ASTRONOMO_ASSUME_CLEAR`` off::

    python main.py --fetch-forecast

Look at what is due. This does not start a mission. ``--confirm`` is the person saying yes.
Self-start stays off until the mount has been confirmed::

    python main.py --schedule --once
    python main.py --schedule --once --confirm 3

Park and leave a mission stopped (it will not resume)::

    python main.py --stop
    python main.py --stop m_20261114_030000_ab12

Replay a real public data set (no telescope needed; only the asteroid plugin can)::

    python main.py --plugin asteroid_finder --data-source archive            # ZTF via IRSA
    python main.py --plugin asteroid_finder --data-source archive --replay-speed 3600 --new
                                  # one survey hour per second: watch the multi-night cadence play out

Several missions can run at once -- start several processes (each claims its own mission; the
dashboard shows them all)::

    python main.py --plugin asteroid_finder --data-source archive --replay-speed 3600 --new &
    python main.py --plugin asteroid_finder --data-source archive --replay-speed 60   --new &

Interrupt with Ctrl-C at any time; running the same command again resumes the mission
from the last completed node.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import config
from core.graph import (
    AstronomoRuntime,
    attach_db_logging,
    build_graph,
    detach_db_logging,
    mission_config,
    run_mission,
)
from core.lease import Lease, LeaseError, liveness
from core.scheduler import blockers_for, due_plans, park_and_stop, wakeable
from core.llm import LLM_ERRORS, LLMConfigError, build_llm_client, format_usage
from core.planning import check_plan, settle_plan
from core.state import MissionState, initial_state
from db.database import Database
from plugins import PluginLoadError, available_plugins, draft_refusal, load_plugin, plugin_status
from tools.catalogs import CatalogClient
from tools.telescope import make_controller
from tools.weather import WeatherClient, WeatherUnavailable, save_forecast
from writer import LogWriter

log = logging.getLogger("main")

EXIT_OK, EXIT_FAILED, EXIT_ABORTED, EXIT_INTERRUPTED = 0, 1, 2, 130


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Autonomous astronomical-discovery agent.")
    p.add_argument("--simulate", action="store_true",
                   help="Mock telescope AND offline weather/catalogs: a full mission with no hardware or network.")
    p.add_argument("--fast", action="store_true", help="With --simulate: sub-second captures and short sleeps.")
    p.add_argument("--live-services", action="store_true",
                   help="With --simulate: still use live Open-Meteo / Horizons / Gaia (telescope stays mocked).")
    p.add_argument("--data-source", choices=["telescope", "archive"],
                   help=f"Where frames come from (default: config.DATA_SOURCE={config.DATA_SOURCE}). 'archive' replays a real public "
                        "survey through the plugin (asteroid_finder: ZTF).")
    p.add_argument("--replay-speed", type=float,
                   help="Archive replay: survey seconds per wall second (0 = jump between visits, 1 = real time, 3600 = 1 survey hour/s).")
    p.add_argument("--plugin", help=f"Plugin to run (default: config.ACTIVE_PLUGIN={config.ACTIVE_PLUGIN}).")
    hw = p.add_argument_group("first light")
    hw.add_argument("--hardware-check", action="store_true",
                    help="Smoke-test the real Seestar (connect, firmware, status, image share, park) and exit. "
                         "Moves and captures nothing.  With --simulate it exercises the mock instead.")
    hw.add_argument("--no-park", action="store_true", help="With --hardware-check: do not send the park command.")
    hw.add_argument("--focus", action="store_true",
                    help="Focus the telescope and exit: take short frames while sweeping the Seestar's Alpaca focuser, "
                         "fit the star width, and settle on the sharpest position (it goes back to where it started if "
                         "that is not clearly better).  Moves the focuser.  Needs ASTRONOMO_SEESTAR_TRANSPORT=alpaca.  "
                         "Point the scope at a star field first (or give --focus-at).  With --simulate it uses a mock focuser.")
    hw.add_argument("--focus-step", action="store_true",
                    help="Run the mission's own focus step on its own and exit: the same code, settings and 'Autofocus:' "
                         "log lines a mission uses before its first exposure, without the rest of the mission.  "
                         "Moves the focuser.  Combine with --focus-at to slew first.  Needs the Alpaca transport.")
    hw.add_argument("--reanalyze", metavar="MISSION_ID",
                    help="Re-run the reduction and the catalogue cross-reference on a finished mission's saved frames, "
                         "with the current code, and print the result.  Read-only: it changes nothing in the database "
                         "and writes data/missions/<id>/reanalysis_<time>.json plus the light-curve figures beside the frames "
                         "(replacing the earlier ones).  Needs the network for Gaia.")
    hw.add_argument("--focus-at", metavar="RA,DEC", help="With --focus or --focus-step: slew to this position (degrees) first.")
    hw.add_argument("--focus-target", type=float, metavar="PX",
                    help="With --focus or --focus-step: settle where the star width reaches this FWHM (pixels) instead of at "
                         f"the sharpest focus; 0 takes the sharpest (default: {config.FOCUS_TARGET_FWHM_PX:g}).")
    hw.add_argument("--focus-half-width", type=int, metavar="STEPS",
                    help="With --focus: half the sweep, in focuser steps (default: a fraction of the focuser's range).")
    night = p.add_argument_group("unattended night")
    night.add_argument("--fetch-forecast", action="store_true",
                       help="Save this site's forecast and exit. Run it while the computer still has internet, "
                            "before joining the telescope Wi-Fi. Leave ASTRONOMO_ASSUME_CLEAR off afterwards.")
    night.add_argument("--stop", nargs="?", const="", metavar="MISSION_ID",
                       help="Park the telescope and leave the mission stopped, so it will not resume. "
                            "With no id, the newest unfinished mission. A mission that is running parks itself.")
    night.add_argument("--schedule", action="store_true",
                       help="See which plans are in their window and which missions are waiting on weather. "
                            "Starts nothing unless --confirm ID is given. Self-start stays off.")
    night.add_argument("--once", action="store_true", help="With --schedule: look once and exit, instead of polling.")
    night.add_argument("--confirm", action="append", type=int, default=None, metavar="ID",
                       help="With --schedule: start this plan if its window is open. Repeat for more than one.")
    p.add_argument("--list-plugins", action="store_true", help="List available plugins and exit.")
    p.add_argument("--cycles", type=int, default=1, help="Number of missions to run back-to-back (default 1).")
    p.add_argument("--loop", action="store_true", help="Run missions forever (Ctrl-C to stop).")
    p.add_argument("--cycle-interval", type=float, default=None, help="Seconds to wait between missions.")
    p.add_argument("--new", action="store_true", help="Never resume; always start a fresh mission.")
    p.add_argument("--from-plan", type=int, metavar="ID",
                   help="Build the mission from planned observation ID (made on the dashboard's Opportunities page). It is "
                        "re-checked first; a real run is refused if its window has closed. With --simulate it is a rehearsal "
                        "that leaves the plan unchanged.")
    p.add_argument("--mission-id", help="Resume this specific mission id.")
    p.add_argument("--max-targets", type=int, help="Override max targets per mission.")
    p.add_argument("--mock-capture-seconds", type=float, help="Wall-clock seconds per mock frame (default 10).")
    p.add_argument("--cloudy-checks", type=int, help="Offline weather: number of initial cloudy checks.")
    p.add_argument("--sleep-interval", type=float, help="Seconds between weather re-checks while asleep.")
    llm = p.add_argument_group("language model (optional, advisory prose only)")
    llm.add_argument("--llm-provider", choices=["none", "ollama", "openai", "anthropic"],
                     help=f"Default: config.LLM_PROVIDER={config.LLM_PROVIDER}. 'openai' also covers OpenAI-compatible servers via --llm-base-url.")
    llm.add_argument("--llm-model", help="Model name (default for ollama: llama3.1; required for cloud providers).")
    llm.add_argument("--llm-base-url", help="Endpoint override, e.g. http://localhost:11434 (Ollama).")
    llm.add_argument("--llm-strict", action="store_true", help="An LLM failure fails the mission (default: fall back to deterministic text).")
    llm.add_argument("--llm-no-cache", action="store_true", help="Always call the model (disable the on-disk response cache).")
    llm.add_argument("--llm-check", action="store_true",
                     help="Verify the configured model with one tiny request (<=64 output tokens) and exit.")
    llm.add_argument("--ollama", nargs="?", const="", metavar="MODEL",
                     help="Verification shortcut: provider=ollama (+ MODEL), strict, cache off.")
    out = p.add_argument_group("outputs (the product is upload files plus a mission log)")
    out.add_argument("--anomaly-sigma", type=float,
                     help=f"Significance (sigma) an anomaly needs to be flagged in the mission log (default: {config.ANOMALY_MIN_SIGMA}).")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def build_settings(args: argparse.Namespace) -> config.Settings:
    overrides: dict = {}
    if args.plugin:
        overrides["active_plugin"] = args.plugin
    if args.data_source:
        overrides["data_source"] = args.data_source
    if args.replay_speed is not None:
        overrides["replay_speed"] = args.replay_speed
    if args.simulate and (args.data_source or config.DATA_SOURCE) == "archive":
        raise SystemExit("error: --simulate fabricates data; it cannot be combined with --data-source archive")
    if (args.hardware_check or args.focus or args.focus_step or args.reanalyze) and not args.simulate:
        overrides["mock_mode"] = False  # a hardware check that quietly used the mock would prove nothing
    if args.simulate:
        overrides["mock_mode"] = True
        overrides["offline_services"] = not args.live_services
        overrides["sleep_interval_s"] = 2.0 if args.fast else 5.0
        overrides["max_sleep_cycles"] = min(config.MAX_SLEEP_CYCLES, 20)
    if args.fast:
        overrides["mock_capture_seconds"] = 0.3
    if args.mock_capture_seconds is not None:
        overrides["mock_capture_seconds"] = args.mock_capture_seconds
    if args.cloudy_checks is not None:
        overrides["mock_cloudy_checks"] = args.cloudy_checks
    if args.sleep_interval is not None:
        overrides["sleep_interval_s"] = args.sleep_interval
    if args.max_targets is not None:
        overrides["max_targets"] = args.max_targets
    if args.anomaly_sigma is not None:
        overrides["anomaly_min_sigma"] = args.anomaly_sigma
    if getattr(args, "focus_target", None) is not None:
        overrides["focus_target_fwhm_px"] = args.focus_target
    if args.ollama is not None:  # verification shortcut; explicit --llm-* flags below still win
        overrides.update(llm_provider="ollama", llm_strict=True, llm_cache=False)
        if args.ollama:
            overrides["llm_model_name"] = args.ollama
    if args.llm_provider:
        overrides["llm_provider"] = args.llm_provider
    if args.llm_model:
        overrides["llm_model_name"] = args.llm_model
    if args.llm_base_url:
        overrides["llm_base_url"] = args.llm_base_url
    if args.llm_strict:
        overrides["llm_strict"] = True
    if args.llm_no_cache:
        overrides["llm_cache"] = False
    return config.get_settings(**overrides)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    # NOTE: do not touch the "astropy"/"astroquery" loggers here.  Those packages install their
    # own Logger subclass at import time; pre-creating a plain Logger under the same name
    # makes `import astroquery` crash with "'Logger' object has no attribute '_set_defaults'".
    for noisy in ("matplotlib", "urllib3", "httpx", "httpcore", "langgraph", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def new_mission_id() -> str:
    return f"m_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"


def choose_mission(
    args: argparse.Namespace, db: Database, app, plugin_name: str, simulated: bool,
    data_source: str = "telescope", lease_s: float = 45.0,
) -> tuple[str, MissionState | None]:
    """Return ``(mission_id, initial_state_or_None)``; ``None`` means "resume".

    Several processes may share one database, so a mission another live process is working on
    (fresh heartbeat) is never picked up; one whose owner died (stale heartbeat) is.
    """
    if args.mission_id:
        existing = db.get_mission(args.mission_id)
        if not existing:
            raise SystemExit(f"Unknown mission id: {args.mission_id}")
        if existing.get("status") == "STOPPED":
            raise SystemExit(
                f"Mission {args.mission_id} was stopped and the telescope was parked. It will not resume."
            )
        if existing.get("data_source", "telescope") != data_source:
            raise SystemExit(
                f"Mission {args.mission_id} uses data source '{existing.get('data_source')}'; "
                f"rerun with --data-source {existing.get('data_source')}"
            )
        return args.mission_id, None

    if not args.new:
        now = datetime.now(timezone.utc)
        for existing in db.list_missions(limit=200):
            if (
                existing["plugin_name"] != plugin_name or existing["simulated"] != simulated
                or existing.get("data_source", "telescope") != data_source
                or liveness(existing, lease_s, now) in ("finished", "running")
            ):
                continue
            snapshot = app.get_state(mission_config(existing["id"]))
            if snapshot.next:
                return existing["id"], None
            age = (now - datetime.fromisoformat(existing["created_at"])).total_seconds()
            if age < lease_s:
                continue  # another process is just starting this mission and has not checkpointed yet
            log.warning("Unfinished mission %s has no resumable checkpoint; marking it FAILED", existing["id"])
            db.update_mission(existing["id"], status="FAILED", error="no resumable checkpoint")

    mission_id = new_mission_id()
    return mission_id, initial_state(mission_id, plugin_name, simulated, data_source)


def print_progress(chunk: dict) -> None:
    for node, update in chunk.items():
        status = (update or {}).get("status", "")
        print(f"  -> {node:<20} {status}", flush=True)


def print_outputs(final: dict) -> None:
    """What the mission produced: upload files first (the point), then the mission log."""
    from core.submission import summarize

    records = final.get("submissions") or []
    if records:
        print(f"  upload files ({summarize(records)}); none are sent, a person reviews each:")
        for r in records:
            where = r.get("path") or "(not written)"
            why = f" -- {'; '.join(r['blockers'])}" if r.get("blockers") else ""
            print(f"   - [{r['status']}] {r['network']} {r['kind']}: {where}{why}")
    anomalies = final.get("anomalies") or []
    below = final.get("below_threshold") or []
    if anomalies:
        print(f"  ANOMALIES ({len(anomalies)}):")
        for a in anomalies:
            print(f"   - {a['target']} ({a['significance_sigma']:.1f} sigma): {a['summary']}")
    elif below:
        print(f"  {len(below)} candidate anomaly(ies) below the threshold; listed in the mission log")
    findings = final.get("findings")
    if findings:
        print(f"  findings: {findings['path']}")
    mission_log = final.get("mission_log")
    if mission_log:
        print(f"  mission log: {mission_log['path']}")


def run_llm_check(settings: config.Settings) -> int:
    """``--llm-check``: one tiny real request so config problems show up before a mission."""
    try:
        client = build_llm_client(settings)
        if not client.enabled:
            print("LLM_PROVIDER is 'none'. Set --llm-provider (e.g. ollama) to check a model.", file=sys.stderr)
            return EXIT_FAILED
        info = client.check()
    except LLM_ERRORS as exc:
        print(f"LLM check FAILED: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"LLM check OK: {info['provider']}:{info['model']} @ {info['base_url']}")
    if info.get("installed_models"):
        print(f"  installed models: {', '.join(info['installed_models'])}")
    print(f"  reply={info['reply']!r}  tokens={info['prompt_tokens']}+{info['completion_tokens']}  latency={info['latency_s']}s")
    return EXIT_OK


def _fetch_forecast(settings: config.Settings) -> int:
    """Save the hourly forecast while this computer can still reach it."""
    if settings.offline_services:
        print("error: --fetch-forecast needs the network. Do not combine it with --simulate.", file=sys.stderr)
        return EXIT_FAILED
    if settings.site_source == "default":
        print(
            "error: set the observing site before saving a forecast, or the file would be for the Denver placeholder.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    try:
        hours = WeatherClient(settings).get_forecast()
    except WeatherUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    path = save_forecast(settings, hours)
    print(f"Saved {len(hours)} forecast hours to {path}")
    print("Join the telescope Wi-Fi after this, and leave ASTRONOMO_ASSUME_CLEAR off.")
    return EXIT_OK


def _stop_mission(settings: config.Settings, mission_id: str | None) -> int:
    """Ask a running mission to park, or park now and mark an idle one stopped."""
    db = Database(settings.db_path)
    try:
        if not mission_id:
            row = db.latest_mission(unfinished_only=True)
            if row is None:
                print("error: there is no unfinished mission to stop", file=sys.stderr)
                return EXIT_FAILED
            mission_id = row["id"]
        else:
            row = db.get_mission(mission_id)
            if row is None:
                print(f"error: unknown mission {mission_id}", file=sys.stderr)
                return EXIT_FAILED
        if row.get("status") == "STOPPED":
            print(f"Mission {mission_id} is already stopped.")
            return EXIT_OK
        db.update_mission(mission_id, stop_requested=True)
        if liveness(row, settings.lease_timeout_s) == "running":
            print(f"Stop requested for {mission_id}. The running mission will park and will not resume.")
            return EXIT_OK
        message = park_and_stop(make_controller(settings), db, mission_id)
        print(f"Mission {mission_id}: {message}")
        return EXIT_ABORTED
    finally:
        db.dispose()


def _schedule(args: argparse.Namespace, settings: config.Settings) -> int:
    """One pass, or a poll, over due plans and missions that are waiting on weather.

    A pass starts a plan only when its id was passed to ``--confirm`` (or self-start is allowed, which it is not
    until the mount has been confirmed). Sleeping missions whose wake time has passed are resumed.
    """
    import time

    worst = EXIT_OK
    while True:
        db = Database(settings.db_path)
        try:
            now = datetime.now(timezone.utc)
            plans = db.list_planned(status="PLANNED")
            telescope = make_controller(settings)
            actions = due_plans(
                plans, now, confirm_ids=set(args.confirm or []),
                self_start=settings.self_start, unattended_confirmed=settings.unattended_confirmed,
                person_blockers=blockers_for(settings, telescope, unattended=False, now=now),
                auto_blockers=blockers_for(settings, telescope, unattended=True, now=now),
            )
            wakes = wakeable(db.list_missions(limit=200), now)
        finally:
            db.dispose()
        if not actions and not wakes:
            print("Nothing is due.")
        for action in [*actions, *wakes]:
            who = f"plan #{action.plan_id} ({action.target_name})" if action.plan_id else f"mission {action.mission_id}"
            print(f"{action.kind}: {who}: {action.reason}")
        for action in actions:
            if action.kind != "start" or action.plan_id is None:
                continue
            inner = argparse.Namespace(**vars(args))
            inner.schedule = False
            inner.fetch_forecast = False
            inner.stop = None
            inner.from_plan = action.plan_id
            inner.cycles = 1
            inner.loop = False
            inner.once = False
            worst = max(worst, run(inner))
        for action in wakes:
            if not action.mission_id:
                continue
            inner = argparse.Namespace(**vars(args))
            inner.schedule = False
            inner.fetch_forecast = False
            inner.stop = None
            inner.from_plan = None
            inner.mission_id = action.mission_id
            inner.cycles = 1
            inner.loop = False
            inner.new = False
            inner.once = False
            worst = max(worst, run(inner))
        if args.once:
            return worst
        time.sleep(max(float(settings.schedule_poll_s), 1.0))


def run_hardware_check_cli(settings: config.Settings, park: bool = True) -> int:
    from tools.hardware_check import format_report, run_hardware_check, save_report

    report = run_hardware_check(settings, park=park)
    print(format_report(report))
    print(f"report saved: {save_report(report, settings.data_dir)}")
    return EXIT_OK if report["ok"] else EXIT_FAILED


def run_focus_cli(settings: config.Settings, focus_at: str | None = None, half_width: int | None = None) -> int:
    """``--focus``: sweep the focuser, settle on the sharpest star width, save the run."""
    from core.setup_checks import measure_star_width
    from tools.autofocus import IMPROVED, NO_IMPROVEMENT, run_autofocus
    from tools.telescope import TelescopeError

    target = None
    if focus_at:
        try:
            ra, dec = (float(v) for v in focus_at.split(","))
        except ValueError:
            print("error: --focus-at wants RA,DEC in degrees, for example 347.24,38.92", file=sys.stderr)
            return EXIT_FAILED
        target = {"name": "Focus field", "ra_deg": ra, "dec_deg": dec}
    mock = settings.mock_mode
    ctl = make_controller(settings)
    try:
        ctl.connect()
    except TelescopeError as exc:
        print(f"error: could not connect to the telescope: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        if target is not None:
            ctl.slew_to(target)
        device = ctl.focuser(target)
        print("Focusing: this moves the focuser.  It goes back to where it started if the result is not clearly sharper."
              + ("  (simulation)" if mock else "  Focuser behaviour on a Seestar is unconfirmed: watch the first run."))
        report = run_autofocus(
            device, measure_star_width,
            exposure_s=settings.focus_exposure_s, n_points=settings.focus_steps, half_width=half_width,
            half_width_frac=settings.focus_half_width_frac, max_widenings=settings.focus_max_widenings,
            min_improvement=settings.focus_min_improvement, target_fwhm_px=settings.focus_target_fwhm_px,
            sleep=(lambda _s: None) if mock else time.sleep, settle_s=0.0 if mock else 1.0,
        )
    except (KeyboardInterrupt, TelescopeError) as exc:
        restored = getattr(exc, "focuser_restored", None)
        start = getattr(exc, "focuser_start", None)
        note = {
            True: f"The focuser was put back at {start}.",
            False: f"THE FOCUSER WAS NOT PUT BACK: it started at {start}; set it back in the Seestar app.",
        }.get(restored, "" if start is None else "Whether the focuser moved is unknown: check it in the Seestar app.")
        if isinstance(exc, KeyboardInterrupt):
            print(f"Interrupted. {note}".strip(), file=sys.stderr)
            return EXIT_INTERRUPTED
        print(f"error: {exc}\n{note}".strip(), file=sys.stderr)
        return EXIT_FAILED
    finally:
        ctl.disconnect()
    out = Path(settings.data_dir) / "focus_runs"
    out.mkdir(parents=True, exist_ok=True)
    saved = out / (report["when"].replace(":", "").replace("-", "") + ".json")
    saved.write_text(json.dumps(report, indent=2, default=str))
    for row in report["samples"]:
        width = "no star" if row["fwhm_px"] is None else f"{row['fwhm_px']:.2f} px ({row['n_stars']} stars)"
        print(f"  position {row['position']:>6}: {width}")
    print(f"Focus {report['status']}: {report['detail']}")
    print(f"report saved: {saved}")
    if report["status"] == IMPROVED:
        return EXIT_OK
    already = report["status"] == NO_IMPROVEMENT and (report.get("fwhm_before_px") or 99.0) < settings.focus_warn_fwhm_px
    return EXIT_OK if already else EXIT_FAILED


def run_focus_step_cli(settings: config.Settings, focus_at: str | None = None) -> int:
    """``--focus-step``: the mission's focus step by itself, so it can be watched without a whole mission."""
    from types import SimpleNamespace

    from core.graph import autofocus_step
    from tools.telescope import StopRequested, TelescopeError

    target = None
    if focus_at:
        try:
            ra, dec = (float(v) for v in focus_at.split(","))
        except ValueError:
            print("error: --focus-at wants RA,DEC in degrees, for example 347.24,38.92", file=sys.stderr)
            return EXIT_FAILED
        target = {"name": "Focus field", "ra_deg": ra, "dec_deg": dec}
    mock = settings.mock_mode
    ctl = make_controller(settings)
    try:
        ctl.connect()
    except TelescopeError as exc:
        print(f"error: could not connect to the telescope: {exc}", file=sys.stderr)
        return EXIT_FAILED

    class _NoMissionLog:  # there is no mission row to hang log lines on; the console shows them
        def add_log(self, *args, **kwargs) -> None:
            return None

    rt = SimpleNamespace(settings=settings, telescope=ctl, db=_NoMissionLog(),
                         sleep_fn=(lambda _s: None) if mock else time.sleep)
    try:
        if target is not None:
            ctl.slew_to(target)
        result = autofocus_step(rt, {"mission_id": "focus_step"}, target or {}, {"gain": 80})
    except (KeyboardInterrupt, StopRequested, TelescopeError) as exc:
        restored = getattr(exc, "focuser_restored", None)
        note = {True: "The focuser was put back.", False: "THE FOCUSER WAS NOT PUT BACK: set it in the Seestar app."}.get(restored, "")
        print(f"Stopped ({type(exc).__name__}). {note}".strip(), file=sys.stderr)
        return EXIT_INTERRUPTED if isinstance(exc, KeyboardInterrupt) else EXIT_FAILED
    finally:
        ctl.disconnect()
    print(f"Focus step: {result.get('status')}: {result.get('detail')}")
    print(f"report saved: {settings.missions_dir / 'focus_step' / 'autofocus.json'}")
    if result.get("status") == "improved":
        return EXIT_OK
    already = result.get("status") == "no_improvement" and (result.get("fwhm_before_px") or 99.0) < settings.focus_warn_fwhm_px
    return EXIT_OK if already else EXIT_FAILED


def run_reanalyze_cli(settings: config.Settings, mission_id: str) -> int:
    """``--reanalyze``: the reduction and cross-reference again, on frames already on disk.  Changes nothing stored."""
    from core.state import to_jsonable

    db = Database(settings.db_path)
    try:
        mission = db.get_mission(mission_id)
        observations = db.list_observations(mission_id) if mission else []
        targets = db.list_targets(mission_id) if mission else []
    finally:
        db.dispose()
    if mission is None:
        print(f"error: there is no mission {mission_id} in {settings.db_path}", file=sys.stderr)
        return EXIT_FAILED
    plugin_name = mission.get("plugin_name") or mission.get("plugin") or settings.active_plugin
    try:
        plugin = load_plugin(plugin_name, settings)
    except PluginLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    catalogs = CatalogClient(settings)
    results, worst = [], EXIT_OK
    for obs in observations:
        by_id = {t["id"]: t for t in targets}
        row = by_id.get(obs.get("target_id")) or (targets[0] if len(targets) == 1 else None)
        paths = [p for p in (obs.get("fits_paths") or []) if Path(p).exists()]
        if row is None or not paths:
            print(f"observation {obs['id']}: skipped ({'no target record' if row is None else 'no frames on disk'})")
            worst = EXIT_FAILED
            continue
        target = dict(row.get("payload") or {})
        target.update(name=row["name"], ra_deg=row["ra_deg"], dec_deg=row["dec_deg"])
        print(f"observation {obs['id']}: {row['name']}, {len(paths)} frames")
        from core import frame_watch  # the same per-frame numbers a live sequence prints

        print("  per-frame watch (the stars of frame 1 followed through the sequence):")
        watched = frame_watch.watch_sequence(
            paths, progress=lambda r: print("   ", frame_watch.format_row(r, len(paths)),
                                           *[f"\n      ! {w}" for w in r.get("warnings", [])]),
            warn_fraction=settings.frame_watch_warn_fraction,
        )
        print(f"  watch:     {frame_watch.summary(watched)}")
        frame_watch.save_stats(watched, settings.missions_dir / mission_id)
        findings = to_jsonable(plugin.analyze_frames(paths))
        xref = to_jsonable(plugin.cross_reference(findings, catalogs, target))
        meas = xref.get("measurement") or {}
        print(f"  findings:  {findings.get('summary')}")
        print(f"  result:    {xref.get('summary')}")
        if meas.get("mag") is not None:
            print(f"  magnitude: {meas['mag']:.3f} +/- {meas['mag_err']:.3f} from {meas.get('n_frames')} frames, "
                  f"{meas.get('zp_n')} comparison stars (zero-point scatter {meas.get('zp_scatter')} mag)")
        for note in (xref.get("notes") or []):
            print(f"  note:      {note}")
        results.append({"observation_id": obs["id"], "target": row["name"], "findings": findings, "crossref": xref,
                        "frame_watch": watched})
    if results:
        out = settings.missions_dir / mission_id / f"reanalysis_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2, default=str))
        print(f"saved: {out}")
    return worst if results else EXIT_FAILED


def _find_plan(args: argparse.Namespace, settings: config.Settings) -> tuple[dict | None, config.Settings, int | None]:
    """``--from-plan``: the plan, the settings aimed at its plugin, and an exit code if it cannot be run."""
    if settings.archive_mode:
        print("error: --from-plan is for a telescope; it cannot be combined with --data-source archive", file=sys.stderr)
        return None, settings, EXIT_FAILED
    db = Database(settings.db_path)
    try:
        db.expire_planned(datetime.now(timezone.utc))  # a plan whose window passed unstarted is MISSED, not startable
        plan = db.get_planned(args.from_plan)
    finally:
        db.dispose()
    if plan is None:
        print(f"error: there is no planned observation #{args.from_plan} in {settings.db_path}", file=sys.stderr)
        return None, settings, EXIT_FAILED
    if plan["status"] in ("CANCELLED", "DONE", "MISSED"):
        detail = f": {plan['outcome']}" if plan.get("outcome") else ""
        print(f"error: plan #{plan['id']} ({plan['target_name']}) is {plan['status']}{detail}", file=sys.stderr)
        return None, settings, EXIT_FAILED
    if args.plugin and args.plugin != plan["plugin_name"]:
        print(f"error: plan #{plan['id']} belongs to plugin '{plan['plugin_name']}', not '{args.plugin}'", file=sys.stderr)
        return None, settings, EXIT_FAILED
    return plan, replace(settings, active_plugin=plan["plugin_name"]), None


def _prepare_plan_mission(
    plan: dict, plugin, settings: config.Settings, db: Database
) -> tuple[str, MissionState | None, bool] | int:
    """Re-check ``plan`` and decide how its mission starts: ``(mission_id, initial_state_or_None, tracked)``, or an exit code.

    ``tracked`` means the plan's life cycle follows the mission.  That is only so on a real telescope: a rehearsal
    (mock telescope) walks through the same steps and leaves the plan as it was.
    """
    tracked = not settings.mock_mode
    if plan["status"] == "STARTED" and tracked and plan.get("mission_id"):
        # Already under way: the mission's own checks decide whether its remaining targets are still in window.
        print(f"Plan #{plan['id']}: {plan['target_name']}: resuming mission {plan['mission_id']}, started from this plan earlier")
        return plan["mission_id"], None, True
    check = check_plan(plan, plugin, settings)
    print(f"Plan #{plan['id']}: {plan['target_name']}, window {check.target['window_start']} to {check.target['window_end']}")
    for line in check.warnings:
        print(f"  note: {line}")
    if not check.ok:
        for line in check.blockers:
            print(f"  cannot start: {line}", file=sys.stderr if tracked else sys.stdout)
        if tracked:
            db.miss_planned(plan["id"], "Not started: " + "; ".join(check.blockers) + ".")
            return EXIT_ABORTED
        print("  (rehearsal: carrying on anyway)")
    if tracked and check.target != plan["target"]:
        db.update_planned_target(plan["id"], check.target)
    mission_id = new_mission_id()
    initial = initial_state(mission_id, plugin.name, settings.simulated, settings.data_source, plan_id=plan["id"])
    return mission_id, initial, tracked


def run(args: argparse.Namespace) -> int:
    if args.list_plugins:
        for name in available_plugins():
            status, note = plugin_status(name)
            default = " (default)" if name == config.ACTIVE_PLUGIN else ""
            print(f"{name}{default}" if status == "ready" else f"{name}{default}  [draft: not ready for use; {note}]")
        return EXIT_OK

    try:
        settings = build_settings(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    settings.ensure_dirs()
    plan = None
    if args.from_plan is not None:
        plan, settings, failure = _find_plan(args, settings)
        if failure is not None:
            return failure
        args.cycles, args.loop = 1, False  # one plan, one mission
    simulated = settings.simulated

    if args.fetch_forecast:
        return _fetch_forecast(settings)
    if args.stop is not None:
        return _stop_mission(settings, args.stop or None)
    if args.schedule and args.from_plan is not None:
        print("error: --schedule looks up plans on its own; do not also pass --from-plan", file=sys.stderr)
        return EXIT_FAILED

    if args.llm_check:
        return run_llm_check(settings)
    if args.hardware_check:
        return run_hardware_check_cli(settings, park=not args.no_park)
    if args.focus:
        return run_focus_cli(settings, args.focus_at, args.focus_half_width)
    if args.focus_step:
        return run_focus_step_cli(settings, args.focus_at)
    if args.reanalyze:
        return run_reanalyze_cli(settings, args.reanalyze)

    if not settings.mock_mode and not settings.archive_mode and settings.site_source == "default":
        print(
            "error: no observing site is set, so the Sun, Moon and altitude checks would be worked out for the placeholder "
            f"({settings.site_name}, {settings.site_latitude_deg:.2f}, {settings.site_longitude_deg:.2f}) and not for your telescope.\n"
            "Set it in the dashboard (Opportunities > Observing site) or with ASTRONOMO_SITE_LAT, ASTRONOMO_SITE_LON and "
            "ASTRONOMO_SITE_ELEV_M, then run again.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    if args.schedule:
        return _schedule(args, settings)

    try:
        llm = build_llm_client(settings)
    except LLMConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if not settings.mock_mode and not settings.archive_mode:
        log.warning("MOCK_MODE is OFF: this run will command real Seestar hardware at %s:%s (%s)", settings.seestar_host, settings.seestar_control_port, settings.seestar_transport)

    try:
        plugin = load_plugin(settings.active_plugin, settings)
    except PluginLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    refusal = draft_refusal(plugin, settings)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return EXIT_FAILED
    if plugin.status == "draft":
        log.warning("Plugin '%s' is a DRAFT and not ready for use (%s). Treat its results as a rehearsal.",
                    plugin.name, plugin.status_note or "no reason given")

    from langgraph.checkpoint.sqlite import SqliteSaver

    db = Database(settings.db_path)
    llm.db = db  # per-mission token ledger/budget persist in Astronomo DB
    log_handler = attach_db_logging(db)
    checkpoint_conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
    try:
        runtime = AstronomoRuntime(
            settings=settings,
            plugin=plugin,
            telescope=make_controller(settings),
            weather=WeatherClient(settings),
            catalogs=CatalogClient(settings),
            writer=LogWriter(settings),
            db=db,
            llm=llm,
        )
        app = build_graph(runtime, SqliteSaver(checkpoint_conn))

        if settings.archive_mode:
            from core.clock import describe_speed

            source_line = f"data=ARCHIVE (replay {describe_speed(settings.replay_speed)}; a resumed mission keeps its saved clock) | "
        else:
            source_line = f"telescope={'MOCK' if settings.mock_mode else 'REAL'} | "
        print(
            f"Astronomo | plugin={plugin.name} | {source_line}"
            f"services={'offline' if settings.offline_services else 'live'} | llm={llm.describe()}"
            f"{' (strict)' if llm.enabled and settings.llm_strict else ''} | data={settings.data_dir}"
        )

        worst = EXIT_OK
        cycle = 0
        while args.loop or cycle < args.cycles:
            cycle += 1
            if plan is not None:
                prepared = _prepare_plan_mission(plan, plugin, settings, db)
                if isinstance(prepared, int):
                    return prepared
                mission_id, initial, tracked = prepared
            else:
                mission_id, initial = choose_mission(
                    args, db, app, plugin.name, simulated, settings.data_source, settings.lease_timeout_s
                )
            if initial is not None:
                db.create_mission(mission_id, plugin.name, simulated, settings.public_dict(), settings.data_source)
                if plan is not None and tracked:
                    db.start_planned(plan["id"], mission_id)
            print(f"\n=== Mission {mission_id} ({'new' if initial is not None else 'resumed'}) ===", flush=True)

            try:
                with Lease(db, mission_id, settings.lease_timeout_s):
                    final = run_mission(app, mission_id, initial, on_update=print_progress)
            except LeaseError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_FAILED
            status = final.get("status")
            linked = db.planned_for_mission(mission_id)  # a mission built from a plan settles that plan, however it was resumed
            if linked and linked["status"] == "STARTED":
                settled = settle_plan(db, linked["id"], final)
                if settled:
                    print(f"\nPlan #{linked['id']} is now {settled[0]}: {settled[1]}")
            print(f"\nMission {mission_id} finished: {status}")
            print(f"  observations: {len(final.get('observations', []))}")
            for obs in final.get("observations", []):
                print(f"   - {obs['target']['name']}: {obs['findings'].get('summary', '')}")
                print(f"       cross-ref: {obs['crossref'].get('summary', '')}")
            print_outputs(final)
            if llm.enabled:
                print(f"  llm: {format_usage(llm.usage(mission_id))}")
            for err in final.get("errors", []):
                print(f"  ! {err}")

            if status == "FAILED":
                worst = max(worst, EXIT_FAILED)
            elif status in ("ABORTED", "STOPPED"):
                worst = max(worst, EXIT_ABORTED)
            args.mission_id = None  # only applies to the first cycle
            if args.loop or cycle < args.cycles:
                wait = args.cycle_interval if args.cycle_interval is not None else (1.0 if args.simulate else 60.0)
                runtime.sleep_fn(wait)
        return worst
    except KeyboardInterrupt:
        print("\nInterrupted. Mission state is checkpointed; run the same command again to resume.", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        detach_db_logging(log_handler)
        checkpoint_conn.close()
        db.dispose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    return run(args)


def cli() -> None:
    """Console entry so ``astronomo`` is ``python main.py`` after an install."""
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
