"""From a plan to a mission and back: the life cycle, the re-check, ``--from-plan``, and how the page shows it."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import config
import main
from core import interlocks, planning
from core.planning import PlanError, check_plan, settle_plan
from core.state import initial_state
from db.database import LIVE_PLAN_STATUSES, Database
from plugins import load_plugin
from plugins.asteroid_finder.plugin import AsteroidFinderPlugin
from tests.test_interlocks import _sky

PLUGIN = "asteroid_finder"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _plan_target(name="Field B (sim)", start=-1.0, end=2.0, **over):
    now = _now()
    target = next(t for t in AsteroidFinderPlugin.__new__(AsteroidFinderPlugin)._simulated_targets() if t["name"] == name)
    return {**target, "window_start": (now + timedelta(hours=start)).isoformat(), "window_end": (now + timedelta(hours=end)).isoformat(), **over}


@pytest.fixture()
def db(settings):
    d = Database(settings.db_path)
    yield d
    d.dispose()


# --------------------------------------------------------------------------- #
# The life cycle in the database
# --------------------------------------------------------------------------- #
def test_a_plan_runs_planned_started_done(db):
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    db.create_mission("m1", PLUGIN, True, {})
    assert db.start_planned(row["id"], "m1") is True
    started = db.get_planned(row["id"])
    assert started["status"] == "STARTED" and started["mission_id"] == "m1" and started["outcome"] is None
    assert db.planned_for_mission("m1")["id"] == row["id"] and db.planned_for_mission("nope") is None
    assert db.start_planned(row["id"], "m2") is False  # one mission per plan
    assert db.cancel_planned(row["id"]) is False  # a started plan can no longer be cancelled
    assert db.finish_planned(row["id"], "DONE", "Observed in mission m1.") is True
    done = db.get_planned(row["id"])
    assert done["status"] == "DONE" and done["outcome"] == "Observed in mission m1."
    assert db.finish_planned(row["id"], "MISSED", "again") is False  # finishing is once


def test_a_finished_window_can_be_planned_again_and_each_attempt_keeps_its_mission(db):
    target = _plan_target()
    first, created = db.plan_observation(PLUGIN, target)
    assert created
    db.create_mission("m1", PLUGIN, True, {})
    db.start_planned(first["id"], "m1")
    db.finish_planned(first["id"], "MISSED", "Not observed: telescope unreachable.")

    second, created_again = db.plan_observation(PLUGIN, target)
    assert created_again and second["id"] != first["id"]
    assert second["status"] == "PLANNED" and second["mission_id"] is None
    third, created_third = db.plan_observation(PLUGIN, target)
    assert not created_third and third["id"] == second["id"]  # one open note for the window

    assert db.start_planned(first["id"], "m2") is False  # the finished plan is not reused
    db.create_mission("m2", PLUGIN, True, {})
    assert db.start_planned(second["id"], "m2") is True
    assert db.get_planned(first["id"])["mission_id"] == "m1"
    assert db.planned_for_mission("m1")["id"] == first["id"]
    assert db.planned_for_mission("m2")["id"] == second["id"]

    blocked, created_blocked = db.plan_observation(PLUGIN, target)
    assert not created_blocked and blocked["id"] == second["id"] and blocked["status"] == "STARTED"

    db.finish_planned(second["id"], "DONE", "Observed in mission m2.")
    fourth, created_fourth = db.plan_observation(PLUGIN, target, note="retry")
    assert created_fourth and fourth["id"] not in {first["id"], second["id"]} and fourth["note"] == "retry"
    assert {p["status"] for p in db.list_planned(status=None)} == {"MISSED", "DONE", "PLANNED"}


def test_planning_again_revives_a_cancelled_retry_and_leaves_the_finished_mission(db):
    target = _plan_target()
    first, _ = db.plan_observation(PLUGIN, target)
    db.create_mission("m1", PLUGIN, True, {})
    db.start_planned(first["id"], "m1")
    db.finish_planned(first["id"], "DONE", "Observed in mission m1.")
    retry, created = db.plan_observation(PLUGIN, target)
    assert created and retry["id"] != first["id"]
    assert db.cancel_planned(retry["id"]) is True
    again, revived = db.plan_observation(PLUGIN, target)
    assert revived and again["id"] == retry["id"] and again["status"] == "PLANNED"
    kept = db.get_planned(first["id"])
    assert kept["status"] == "DONE" and kept["mission_id"] == "m1"


def test_only_done_or_missed_can_finish_a_plan(db):
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    with pytest.raises(ValueError):
        db.finish_planned(row["id"], "PLANNED", "x")
    assert db.finish_planned(row["id"], "DONE", "x") is False  # not started


def test_a_plan_whose_window_passed_unstarted_becomes_missed_and_started_ones_are_left_alone(db):
    late, _ = db.plan_observation(PLUGIN, _plan_target("433 Eros (sim)", -4.0, -1.0))
    open_, _ = db.plan_observation(PLUGIN, _plan_target("Field B (sim)", -1.0, 2.0))
    under_way, _ = db.plan_observation(PLUGIN, _plan_target("Field C control (sim)", -4.0, -1.0))
    db.create_mission("m1", PLUGIN, True, {})
    db.start_planned(under_way["id"], "m1")
    assert db.expire_planned(_now()) == 1
    assert db.get_planned(late["id"])["status"] == "MISSED" and "window passed" in db.get_planned(late["id"])["outcome"]
    assert db.get_planned(open_["id"])["status"] == "PLANNED"
    assert db.get_planned(under_way["id"])["status"] == "STARTED"
    assert db.expire_planned(_now()) == 0


def test_listing_plans_by_status(db):
    a, _ = db.plan_observation(PLUGIN, _plan_target("433 Eros (sim)"))
    b, _ = db.plan_observation(PLUGIN, _plan_target("Field B (sim)"))
    c, _ = db.plan_observation(PLUGIN, _plan_target("Field C control (sim)"))
    db.cancel_planned(c["id"])
    db.miss_planned(b["id"], "Not started: x.")
    assert db.miss_planned(b["id"], "again") is False
    assert [p["id"] for p in db.list_planned()] == [a["id"]]
    assert {p["id"] for p in db.list_planned(status=LIVE_PLAN_STATUSES)} == {a["id"], b["id"]}  # cancelled ones are not shown
    assert {p["status"] for p in db.list_planned(status=None)} == {"PLANNED", "MISSED", "CANCELLED"}


def test_a_refreshed_target_replaces_the_stored_one_only_while_the_plan_is_still_planned(db):
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    moved = _plan_target(start=3.0, end=6.0)
    assert db.update_planned_target(row["id"], moved) is True
    stored = db.get_planned(row["id"])
    assert stored["window_start"] == moved["window_start"] and stored["target"]["window_end"] == moved["window_end"]
    db.create_mission("m1", PLUGIN, True, {})
    db.start_planned(row["id"], "m1")
    assert db.update_planned_target(row["id"], _plan_target()) is False


# --------------------------------------------------------------------------- #
# Re-checking a plan before it is started
# --------------------------------------------------------------------------- #
def _plan(db, **kw):
    row, _ = db.plan_observation(PLUGIN, _plan_target(**kw))
    return row


def test_a_plan_whose_window_has_closed_cannot_be_started(settings, db):
    check = check_plan(_plan(db, start=-4.0, end=-1.0), load_plugin(PLUGIN, settings), settings)
    assert not check.ok and "window closed at" in check.blockers[0]


def test_a_plan_in_its_window_can_be_started_and_carries_the_weather_caveat(settings, db, monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    check = check_plan(_plan(db), load_plugin(PLUGIN, settings), settings)
    assert check.ok and any("Weather is not known" in w for w in check.warnings)
    assert not any("window opens" in w for w in check.warnings)


def test_a_plan_for_later_says_the_mission_will_wait(settings, db, monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    check = check_plan(_plan(db, start=5.0, end=8.0), load_plugin(PLUGIN, settings), settings)
    assert check.ok and any("opens in" in w and "waits for it" in w for w in check.warnings)


def test_a_poor_sky_in_the_window_is_a_warning_not_a_refusal(settings, db, monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(alt=8.0))
    check = check_plan(_plan(db), load_plugin(PLUGIN, settings), settings)
    assert check.ok and any("only 8 deg high" in w for w in check.warnings)


def test_a_moved_schedule_replaces_the_target_and_a_failing_recheck_is_only_a_warning(settings, db, monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    plugin = load_plugin(PLUGIN, settings)
    plan = _plan(db)
    moved = {**plan["target"], "window_start": (_now() + timedelta(hours=1)).isoformat(), "window_end": (_now() + timedelta(hours=4)).isoformat()}
    monkeypatch.setattr(plugin, "recheck_planned_target", lambda target: {"target": moved, "notes": ["The transit moved +9 min."]})
    check = check_plan(plan, plugin, settings)
    assert check.target["window_start"] == moved["window_start"] and "The transit moved +9 min." in check.warnings

    def boom(target):
        raise RuntimeError("lists unreadable")

    monkeypatch.setattr(plugin, "recheck_planned_target", boom)
    check = check_plan(plan, plugin, settings)
    assert check.ok and check.target == plan["target"] and any("could not be re-checked" in w for w in check.warnings)


def test_the_transit_recheck_says_so_when_there_is_nothing_to_check_it_against(settings):
    plugin = load_plugin("transit_finder", settings)
    out = plugin.recheck_planned_target({"name": "X b"})
    assert out["target"] is None and "no predicted mid-time" in out["notes"][0]
    out = plugin.recheck_planned_target({"name": "X b", "epoch_number": 5, "predicted_t_mid_iso": _now().isoformat()})
    assert out["target"] is None and out["notes"]  # the conftest makes every list unreachable: said, not raised


# --------------------------------------------------------------------------- #
# How a mission settles its plan
# --------------------------------------------------------------------------- #
def _started(db, name="Field B (sim)"):
    row, _ = db.plan_observation(PLUGIN, _plan_target(name))
    db.create_mission("m1", PLUGIN, True, {})
    db.start_planned(row["id"], "m1")
    return row["id"]


def test_a_mission_that_observed_the_target_settles_the_plan_done(db):
    pid = _started(db)
    status, outcome = settle_plan(db, pid, {"status": "COMPLETE", "mission_id": "m1", "observations": [{"target": {"name": "Field B (sim)"}}]})
    assert status == "DONE" and "Observed in mission m1" in outcome
    assert db.get_planned(pid)["status"] == "DONE"
    assert settle_plan(db, pid, {"status": "COMPLETE", "observations": []}) is None  # already settled: left alone


def test_a_mission_that_skipped_the_target_settles_the_plan_missed_with_the_reason(db):
    pid = _started(db)
    final = {"status": "ABORTED", "observations": [], "skipped": [{"name": "Field B (sim)", "reason": "its observing window closed at 04:00 UTC"}]}
    status, outcome = settle_plan(db, pid, final)
    assert status == "MISSED" and "window closed at 04:00 UTC" in outcome and db.get_planned(pid)["outcome"] == outcome


def test_a_mission_that_failed_without_observing_says_why(db):
    pid = _started(db)
    status, outcome = settle_plan(db, pid, {"status": "FAILED", "observations": [], "errors": ["telescope unreachable"]})
    assert status == "MISSED" and "telescope unreachable" in outcome


def test_data_taken_before_a_late_failure_still_counts_as_observed(db):
    pid = _started(db)
    status, outcome = settle_plan(db, pid, {"status": "FAILED", "mission_id": "m1", "observations": [{"target": {"name": "Field B (sim)"}}]})
    assert status == "DONE" and "ended FAILED" in outcome


def test_a_plan_that_is_not_started_is_not_settled(db):
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    assert settle_plan(db, row["id"], {"status": "COMPLETE", "observations": []}) is None
    assert settle_plan(db, 9999, {}) is None


# --------------------------------------------------------------------------- #
# --from-plan
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cli(tmp_path, monkeypatch):
    """A data directory for ``main`` to use, and a way to put a plan in it."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "cli")
    settings = config.get_settings()
    settings.ensure_dirs()
    d = Database(settings.db_path)
    yield settings, d
    d.dispose()


def _run(*argv):
    return main.main(list(argv))


def test_from_plan_refuses_what_cannot_be_run(cli, capsys):
    settings, db = cli
    assert _run("--from-plan", "42", "--simulate") == main.EXIT_FAILED and "no planned observation #42" in capsys.readouterr().err
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    db.cancel_planned(row["id"])
    assert _run("--from-plan", str(row["id"]), "--simulate") == main.EXIT_FAILED and "is CANCELLED" in capsys.readouterr().err
    other, _ = db.plan_observation(PLUGIN, _plan_target("433 Eros (sim)"))
    assert _run("--from-plan", str(other["id"]), "--plugin", "transit_finder", "--simulate") == main.EXIT_FAILED
    assert "belongs to plugin 'asteroid_finder'" in capsys.readouterr().err
    assert _run("--from-plan", str(other["id"]), "--data-source", "archive") == main.EXIT_FAILED
    assert "archive" in capsys.readouterr().err


def test_a_plan_whose_window_passed_is_missed_not_started(cli, capsys):
    settings, db = cli
    row, _ = db.plan_observation(PLUGIN, _plan_target(start=-4.0, end=-1.0))
    assert _run("--from-plan", str(row["id"]), "--simulate") == main.EXIT_FAILED
    assert "is MISSED" in capsys.readouterr().err and db.get_planned(row["id"])["status"] == "MISSED"


@pytest.mark.slow
def test_a_rehearsal_observes_exactly_the_plan_and_leaves_the_plan_untouched(cli, capsys, monkeypatch):
    settings, db = cli
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    assert _run("--from-plan", str(row["id"]), "--simulate", "--mock-capture-seconds", "0", "--sleep-interval", "0") == main.EXIT_OK
    out = capsys.readouterr().out
    assert f"Plan #{row['id']}: Field B (sim)" in out and "Weather is not known" in out
    after = db.get_planned(row["id"])
    assert after["status"] == "PLANNED" and after["mission_id"] is None and after["outcome"] is None  # a rehearsal is not the real thing
    (mission,) = db.list_missions()
    assert [t["name"] for t in db.list_targets(mission["id"])] == ["Field B (sim)"]  # only what was planned, not the plugin's own list
    assert db.planned_for_mission(mission["id"]) is None


# The tracked path needs a real telescope to run end to end; its steps are tested one at a time.
def _real(settings):
    return replace(settings, mock_mode=False)


def test_a_real_run_links_the_plan_to_a_new_mission_with_its_id_in_the_state(cli, monkeypatch):
    settings, db = cli
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    s = _real(settings)
    mission_id, initial, tracked = main._prepare_plan_mission(db.get_planned(row["id"]), load_plugin(PLUGIN, s), s, db)
    assert tracked and initial["plan_id"] == row["id"] and initial["mission_id"] == mission_id and initial["simulated"] is False


def test_a_real_run_of_a_closed_window_marks_the_plan_missed(cli, capsys):
    settings, db = cli
    row, _ = db.plan_observation(PLUGIN, _plan_target(start=-4.0, end=-1.0))
    s = _real(settings)
    assert main._prepare_plan_mission(db.get_planned(row["id"]), load_plugin(PLUGIN, s), s, db) == main.EXIT_ABORTED
    stored = db.get_planned(row["id"])
    assert stored["status"] == "MISSED" and stored["outcome"].startswith("Not started: its observing window closed")
    assert "cannot start" in capsys.readouterr().err


def test_a_real_run_stores_a_moved_schedule_on_the_plan(cli, monkeypatch):
    settings, db = cli
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    row, _ = db.plan_observation(PLUGIN, _plan_target())
    moved = {**row["target"], "window_start": (_now() + timedelta(hours=1)).isoformat(), "window_end": (_now() + timedelta(hours=4)).isoformat()}
    s = _real(settings)
    plugin = load_plugin(PLUGIN, s)
    monkeypatch.setattr(plugin, "recheck_planned_target", lambda target: {"target": moved, "notes": []})
    main._prepare_plan_mission(db.get_planned(row["id"]), plugin, s, db)
    assert db.get_planned(row["id"])["window_start"] == moved["window_start"]


def test_a_real_run_of_a_started_plan_resumes_its_mission_even_after_the_window_closed(cli):
    settings, db = cli
    row, _ = db.plan_observation(PLUGIN, _plan_target(start=-4.0, end=-1.0))
    db.create_mission("m_old", PLUGIN, False, {})
    db.start_planned(row["id"], "m_old")
    s = _real(settings)
    assert main._prepare_plan_mission(db.get_planned(row["id"]), load_plugin(PLUGIN, s), s, db) == ("m_old", None, True)


def test_the_graph_builds_its_targets_from_the_plan_not_the_plugin(settings, db):
    from core.graph import run_mission
    from tests.conftest import Harness

    row, _ = db.plan_observation(PLUGIN, _plan_target())
    h = Harness(replace(settings, max_targets=3))
    try:
        h.db.create_mission("m_plan", PLUGIN, True, {})
        final = run_mission(h.app, "m_plan", initial_state("m_plan", PLUGIN, True, plan_id=row["id"]))
        assert [o["target"]["name"] for o in final["observations"]] == ["Field B (sim)"] and final["plan_id"] == row["id"]
    finally:
        h.close()


def test_a_plan_that_vanished_fails_the_mission_with_a_message(settings):
    from core.graph import run_mission
    from tests.conftest import Harness

    h = Harness(settings)
    try:
        h.db.create_mission("m_gone", PLUGIN, True, {})
        final = run_mission(h.app, "m_gone", initial_state("m_gone", PLUGIN, True, plan_id=777))
        assert final["status"] == "FAILED" and "planned observation #777" in h.db.get_mission("m_gone")["error"]
    finally:
        h.close()


def test_plan_error_exists_for_callers():
    assert issubclass(PlanError, Exception) and planning.PlanCheck(target={}).ok


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.api.app import create_app

    return TestClient(create_app(data_dir=tmp_path, serve_frontend=False))


def test_a_failed_mission_can_be_planned_again_from_its_page(tmp_path):
    db = Database(str(tmp_path / "astronomo.sqlite"))
    try:
        row, _ = db.plan_observation(PLUGIN, _plan_target())
        db.create_mission("m_fail", PLUGIN, True, {})
        db.start_planned(row["id"], "m_fail")
        db.finish_planned(row["id"], "MISSED", "Not observed: telescope unreachable.")
        db.update_mission("m_fail", status="FAILED", error="telescope unreachable")
        failed_id = row["id"]
    finally:
        db.dispose()

    client = _client(tmp_path)
    mission = client.get("/api/v1/missions/m_fail").json()
    caption = (mission.get("plan") or {}).get("caption") or ""
    assert "Started from plan" in caption and "Missed" in caption
    again = client.post("/api/v1/missions/m_fail/plan-again")
    assert again.status_code == 200 and again.json()["ok"] is True
    db = Database(str(tmp_path / "astronomo.sqlite"))
    try:
        rows = [p for p in db.list_planned(status=None) if p["target_name"] == "Field B (sim)"]
        assert {p["status"] for p in rows} == {"MISSED", "PLANNED"}
        assert db.get_planned(failed_id)["mission_id"] == "m_fail"
        assert again.json()["plan"]["id"] == next(p["id"] for p in rows if p["status"] == "PLANNED")
    finally:
        db.dispose()


def test_the_page_shows_the_exact_commands_for_a_plan():
    from dashboard.opportunities import plan_chip, plan_commands

    cmds = plan_commands({"id": 7})
    assert cmds["rehearse"] == "python main.py --from-plan 7 --simulate --fast"
    assert cmds["run"] == "ASTRONOMO_MOCK_MODE=false python main.py --from-plan 7"
    assert "In progress" in plan_chip("STARTED") and "Missed" in plan_chip("MISSED")


def test_the_planned_tab_shows_each_stage_and_only_planned_ones_can_be_cancelled(tmp_path):
    d = Database(str(tmp_path / "astronomo.sqlite"))
    try:
        ids = {}
        for name in ("Alpha b", "Beta b", "Gamma b", "Delta b"):
            row, _ = d.plan_observation("transit_finder", {**_plan_target("Field B (sim)", 1.0, 3.0), "name": name})
            ids[name] = row["id"]
        d.create_mission("m_beta", "transit_finder", True, {})
        d.start_planned(ids["Beta b"], "m_beta")
        d.create_mission("m_gamma", "transit_finder", True, {})
        d.start_planned(ids["Gamma b"], "m_gamma")
        d.finish_planned(ids["Gamma b"], "DONE", "Observed in mission m_gamma.")
        d.miss_planned(ids["Delta b"], "Not started: x.")
    finally:
        d.dispose()

    client = _client(tmp_path)
    plans_body = client.get("/api/v1/tonight").json()["plans"]
    by_name = {p["target_name"]: p for p in plans_body["live"] + plans_body["earlier"]}
    assert set(by_name) == {"Alpha b", "Beta b", "Gamma b", "Delta b"}
    assert {p["target_name"] for p in plans_body["live"]} == {"Alpha b", "Beta b"}
    assert by_name["Beta b"]["status_label"] == "In progress"
    assert by_name["Gamma b"]["status_label"] == "Done"
    assert by_name["Delta b"]["status_label"] == "Missed"
    assert "Observed in mission m_gamma." in (by_name["Gamma b"].get("outcome") or "")
    assert "Not started: x." in (by_name["Delta b"].get("outcome") or "")
    assert by_name["Beta b"]["mission_id"] == "m_beta"
    assert by_name["Alpha b"]["actions"]["cancel"] is True
    assert by_name["Beta b"]["actions"]["cancel"] is False
    assert by_name["Gamma b"]["actions"]["again"] is True
    assert by_name["Delta b"]["actions"]["again"] is True
    assert "--from-plan" in by_name["Alpha b"]["commands"]["run"]

    again = client.post(f"/api/v1/plans/{ids['Gamma b']}/again")
    assert again.status_code == 200 and again.json()["ok"] is True
    d = Database(str(tmp_path / "astronomo.sqlite"))
    try:
        gamma = [p for p in d.list_planned(status=None) if p["target_name"] == "Gamma b"]
        assert {p["status"] for p in gamma} == {"DONE", "PLANNED"}
        assert d.get_planned(ids["Gamma b"])["mission_id"] == "m_gamma"
    finally:
        d.dispose()
