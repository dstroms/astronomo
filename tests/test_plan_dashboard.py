"""Plan helpers: ordering, launch specs, and the two-step real start.

The list is pure data plus a process record.  Rehearse is one command; a real start does nothing
until it is confirmed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dashboard import plans
from dashboard.plans import (
    LaunchError, countdown_label, launch_plan, launch_spec, log_tail, mission_id_from_log, ordered_plans, plan_actions,
    stop_command,
)
from db.database import Database


def _target(name: str, *, hours: float = 2.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "name": name, "ra_deg": 10.0, "dec_deg": 20.0,
        "window_start": (now + timedelta(hours=1)).isoformat(),
        "window_end": (now + timedelta(hours=1 + hours)).isoformat(),
    }


def test_plans_are_ordered_with_the_live_ones_first_and_cancelled_ones_dropped():
    rows = [
        {"id": 3, "status": "DONE", "window_start": "2026-11-02"},
        {"id": 1, "status": "PLANNED", "window_start": "2026-11-04"},
        {"id": 9, "status": "CANCELLED", "window_start": "2026-11-01"},
        {"id": 2, "status": "STARTED", "window_start": "2026-11-05"},
        {"id": 4, "status": "MISSED", "window_start": "2026-11-01"},
        {"id": 5, "status": "PLANNED", "window_start": "2026-11-03"},
    ]
    assert [p["id"] for p in ordered_plans(rows)] == [2, 5, 1, 3, 4]


def test_the_countdown_is_the_time_until_the_window_opens():
    start = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)
    assert countdown_label(start, end, start - timedelta(hours=2, minutes=5)) == ("2 h 05 m", "until start")
    assert countdown_label(start, end, start + timedelta(minutes=10)) == ("Now", "window open")
    assert countdown_label(start, end, end + timedelta(minutes=1)) == ("Closed", "window ended")
    assert countdown_label(None, end, start) == ("-", "")


def test_a_finished_child_process_is_not_reported_as_running():
    """An exited child lingers as a zombie until this process collects it. That leftover is not 'running'."""
    import subprocess
    import sys
    import time

    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    deadline = time.time() + 5
    while time.time() < deadline:
        if plans.pid_alive(proc.pid) is False:
            assert plans.pid_alive(proc.pid) is False
            return
        time.sleep(0.05)
    raise AssertionError("the exited process was still reported as running")


def test_a_rehearsal_log_names_the_simulated_mission(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("Plan #4: Hat-P-23\n\n=== Mission m_20261115_120000_ab (new) ===\nobserving\n")
    assert mission_id_from_log(str(log)) == "m_20261115_120000_ab"
    assert mission_id_from_log(str(tmp_path / "missing.log")) is None


def test_a_running_mission_has_no_start_button_and_a_finished_plan_has_neither():
    assert plan_actions("PLANNED", mission_running=False) == {
        "rehearse": True, "start": True, "start_word": "Start", "again": False,
    }
    assert plan_actions("STARTED", mission_running=False)["start_word"] == "Resume"
    assert plan_actions("STARTED", mission_running=False)["again"] is False
    paused = plan_actions("STARTED", mission_running=True)
    assert paused["rehearse"] is False and paused["start"] is False and paused["again"] is False
    assert plan_actions("DONE", mission_running=False)["start"] is False
    assert plan_actions("DONE", mission_running=False)["again"] is True
    assert plan_actions("MISSED", mission_running=False)["rehearse"] is False
    assert plan_actions("MISSED", mission_running=False)["again"] is True


def test_rehearse_and_start_are_the_documented_commands_and_only_a_real_start_clears_mock_mode(tmp_path):
    rehearse = launch_spec(7, rehearse=True, data_dir=tmp_path, default_data_dir=tmp_path)
    assert rehearse.command == "python main.py --from-plan 7 --simulate --fast"
    assert rehearse.argv[2:] == ["--from-plan", "7", "--simulate", "--fast"]
    assert "ASTRONOMO_MOCK_MODE" not in rehearse.env
    assert rehearse.env["ASTRONOMO_DATA_DIR"] == str(tmp_path)

    real = launch_spec(7, rehearse=False, data_dir=tmp_path, default_data_dir=tmp_path)
    assert real.command == "ASTRONOMO_MOCK_MODE=false python main.py --from-plan 7"
    assert "--simulate" not in real.argv and "--fast" not in real.argv
    assert real.env["ASTRONOMO_MOCK_MODE"] == "false"
    assert stop_command(tmp_path, tmp_path, "m_1") == "python main.py --stop m_1"
    assert stop_command(tmp_path, tmp_path) == "python main.py --stop"


def test_a_data_directory_other_than_the_default_is_passed_through_on_the_command(tmp_path):
    other = tmp_path / "other data"
    real = launch_spec(3, rehearse=False, data_dir=other, default_data_dir=tmp_path)
    assert 'ASTRONOMO_DATA_DIR="' in real.command and "other data" in real.command
    assert real.env["ASTRONOMO_DATA_DIR"] == str(other)
    assert "ASTRONOMO_DATA_DIR=" in stop_command(other, tmp_path, "m9")


class _Proc:
    pid = 4321

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs


def test_a_second_start_is_refused_while_the_process_is_alive(tmp_path, monkeypatch):
    spec = launch_spec(4, rehearse=True, data_dir=tmp_path, default_data_dir=tmp_path)
    monkeypatch.setattr(plans, "pid_alive", lambda pid: pid == 4321)
    record = launch_plan(spec, data_dir=tmp_path, popen=_Proc)
    assert record["pid"] == 4321 and record["rehearse"] is True
    assert (tmp_path / "plan_runs" / "launches.json").is_file()
    log = next((tmp_path / "plan_runs").glob("4-*.log"))
    log.write_text("cannot start: its observing window closed\n")
    assert "cannot start" in log_tail(str(log))
    with pytest.raises(LaunchError, match="already has a process"):
        launch_plan(spec, data_dir=tmp_path, popen=_Proc)


def test_a_finished_process_can_be_started_again(tmp_path, monkeypatch):
    spec = launch_spec(4, rehearse=False, data_dir=tmp_path, default_data_dir=tmp_path)
    monkeypatch.setattr(plans, "pid_alive", lambda pid: False)
    first = launch_plan(spec, data_dir=tmp_path, popen=_Proc)
    second = launch_plan(spec, data_dir=tmp_path, popen=_Proc)
    assert first["pid"] == second["pid"] == 4321
    saved = plans.read_launches(tmp_path)
    assert saved["4"]["rehearse"] is False and "--simulate" not in saved["4"]["command"]


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.api.app import create_app

    return TestClient(create_app(data_dir=tmp_path, serve_frontend=False))


def test_every_kind_of_plan_is_listed_and_a_real_start_waits_for_confirmation(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "astronomo.sqlite"))
    try:
        transit, _ = db.plan_observation("transit_finder", _target("WASP-2 b"))
        db.plan_observation("variable_stars", _target("RR Lyrae"))
        missed, _ = db.plan_observation("asteroid_finder", _target("433 Eros"))
        db.miss_planned(missed["id"], "Not started: the window closed.")
    finally:
        db.dispose()

    launched: list = []

    def fake(spec, **kwargs):
        launched.append(spec)
        return {"pid": 99, "plan_id": spec.plan_id, "rehearse": spec.rehearse, "command": spec.command, "log": "", "started_at": "t"}

    monkeypatch.setattr("dashboard.api.queries.launch_plan", fake)
    client = _client(tmp_path)
    body = client.get("/api/v1/tonight").json()
    live = body["plans"]["live"]
    earlier = body["plans"]["earlier"]
    live_names = [p["target_name"] for p in live]
    assert "WASP-2 b" in live_names and "RR Lyrae" in live_names
    assert "433 Eros" not in live_names
    assert [p["target_name"] for p in earlier] == ["433 Eros"]
    labels = {p["plugin_label"] for p in live + earlier}
    assert labels == {"Exoplanet transit", "Variable star", "Asteroid search"}
    assert all(p["actions"]["rehearse"] and p["actions"]["start"] for p in live)
    assert earlier[0]["actions"]["again"] is True

    asking = client.post(f"/api/v1/plans/{transit['id']}/start", json={})
    assert asking.status_code == 200
    asked = asking.json()
    assert asked["ok"] is False and asked["needs_confirm"] is True
    assert "real telescope" in asked["warning"]
    assert launched == []

    rehearsed = client.post(f"/api/v1/plans/{transit['id']}/rehearse")
    assert rehearsed.status_code == 200 and rehearsed.json()["ok"] is True
    assert len(launched) == 1 and launched[0].rehearse is True and "--simulate" in launched[0].argv

    confirmed = client.post(f"/api/v1/plans/{transit['id']}/start", json={"confirm": True})
    assert confirmed.status_code == 200 and confirmed.json()["ok"] is True
    assert launched[-1].rehearse is False
    assert launched[-1].env["ASTRONOMO_MOCK_MODE"] == "false"
    assert "--simulate" not in launched[-1].argv
    assert launched[-1].plan_id == transit["id"] == launched[0].plan_id


def test_a_rehearsal_card_follows_the_simulated_mission(tmp_path):
    import json

    db = Database(str(tmp_path / "astronomo.sqlite"))
    try:
        plan, _ = db.plan_observation("variable_stars", _target("Hat-P-23"))
        db.create_mission("m_reh", "variable_stars", True)
        db.update_mission(
            "m_reh", status="OBSERVING", heartbeat_at=datetime.now(timezone.utc),
            progress={"visit": 1, "visits_total": 1, "target": "Hat-P-23", "frames_done": 4, "frames_total": 20},
        )
    finally:
        db.dispose()

    runs = tmp_path / "plan_runs"
    runs.mkdir()
    log = runs / "reh.log"
    log.write_text("=== Mission m_reh ===\n")
    (runs / "launches.json").write_text(json.dumps({
        str(plan["id"]): {
            "pid": 99999999, "plan_id": plan["id"], "rehearse": True, "command": "rehearse",
            "log": str(log), "started_at": "t",
        },
    }))

    client = _client(tmp_path)
    card = next(p for p in client.get("/api/v1/tonight").json()["plans"]["live"] if p["id"] == plan["id"])
    rehearsal = card["rehearsal"]
    assert rehearsal["mission_id"] == "m_reh"
    assert "image 4 of 20" in rehearsal["progress_text"] or "image 4/20" in rehearsal["progress_text"]
