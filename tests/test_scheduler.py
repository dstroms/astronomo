"""The scheduler looks. It does not start a mission unless a person confirms it, and self-start stays off."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import main
from core.scheduler import SELF_START_REFUSAL, classify_plan, due_plans, wakeable
from db.database import Database
from plugins.asteroid_finder.plugin import AsteroidFinderPlugin

NOW = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
PLUGIN = "asteroid_finder"


def _plan(start_h=-1.0, end_h=2.0, status="PLANNED"):
    return {
        "id": 3, "status": status, "target_name": "Target",
        "window_start": (NOW + timedelta(hours=start_h)).isoformat(),
        "window_end": (NOW + timedelta(hours=end_h)).isoformat(),
    }


def test_a_due_plan_waits_for_a_person():
    action = classify_plan(_plan(), NOW, confirmed=False, self_start=False, unattended_confirmed=False, blockers=[])
    assert action is not None and action.kind == "awaiting" and "--confirm 3" in action.reason


def test_a_person_can_confirm_a_due_plan_and_a_closed_one_is_ignored():
    assert classify_plan(_plan(), NOW, confirmed=True, self_start=False, unattended_confirmed=False, blockers=[]).kind == "start"
    assert classify_plan(_plan(start_h=-5, end_h=-1), NOW, confirmed=True, self_start=False, unattended_confirmed=False, blockers=[]) is None
    refused = classify_plan(_plan(), NOW, confirmed=True, self_start=False, unattended_confirmed=False, blockers=["disk full"])
    assert refused.kind == "refused" and "disk full" in refused.reason


def test_self_start_stays_off_until_the_mount_is_confirmed():
    refused = classify_plan(_plan(), NOW, confirmed=False, self_start=True, unattended_confirmed=False, blockers=[])
    assert refused.kind == "refused" and refused.reason == SELF_START_REFUSAL
    still = classify_plan(
        _plan(), NOW, confirmed=False, self_start=True, unattended_confirmed=True,
        blockers=["battery is unknown; an unattended start will not guess (no reading)"],
    )
    assert still.kind == "refused" and "battery" in still.reason
    allowed = classify_plan(_plan(), NOW, confirmed=False, self_start=True, unattended_confirmed=True, blockers=[])
    assert allowed.kind == "start"


def test_a_sleeping_mission_is_woken_and_a_stopped_one_is_not():
    past = (NOW - timedelta(minutes=5)).isoformat()
    missions = [
        {"id": "m_sleep", "status": "SLEEP", "wake_at": past, "stop_requested": False},
        {"id": "m_stop", "status": "SLEEP", "wake_at": past, "stop_requested": True},
        {"id": "m_later", "status": "SLEEP", "wake_at": (NOW + timedelta(hours=1)).isoformat(), "stop_requested": False},
        {"id": "m_done", "status": "COMPLETE", "wake_at": past, "stop_requested": False},
    ]
    woken = wakeable(missions, NOW)
    assert [a.mission_id for a in woken] == ["m_sleep"]


def test_due_plans_uses_the_stricter_list_only_for_self_start():
    plans = [_plan()]
    actions = due_plans(
        plans, NOW, confirm_ids=set(), self_start=True, unattended_confirmed=True,
        person_blockers=[], auto_blockers=["battery is unknown"],
    )
    assert actions[0].kind == "refused"
    confirmed = due_plans(
        plans, NOW, confirm_ids={3}, self_start=True, unattended_confirmed=False,
        person_blockers=[], auto_blockers=["battery is unknown"],
    )
    assert confirmed[0].kind == "start"  # a person confirming wins, and an unknown battery does not block them


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "cli")
    settings = config.get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    yield settings, db
    db.dispose()


def _target():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    target = next(t for t in AsteroidFinderPlugin.__new__(AsteroidFinderPlugin)._simulated_targets() if t["name"] == "Field B (sim)")
    return {**target, "window_start": (now - timedelta(hours=1)).isoformat(), "window_end": (now + timedelta(hours=2)).isoformat()}


def test_schedule_once_does_not_start_a_due_plan(cli, capsys):
    _settings, db = cli
    row, _ = db.plan_observation(PLUGIN, _target())
    assert main.main(["--schedule", "--once", "--simulate"]) == main.EXIT_OK
    out = capsys.readouterr().out
    assert f"plan #{row['id']}" in out and "awaiting" in out
    assert db.get_planned(row["id"])["status"] == "PLANNED"


def test_stop_parks_an_idle_mission_and_only_flags_a_running_one(cli, capsys):
    _settings, db = cli
    db.create_mission("m_idle", PLUGIN, True, {})
    assert main.main(["--stop", "m_idle", "--simulate"]) == main.EXIT_ABORTED
    stopped = db.get_mission("m_idle")
    assert stopped["status"] == "STOPPED" and stopped["stop_requested"]
    assert "stay stopped" in capsys.readouterr().out

    db.create_mission("m_live", PLUGIN, True, {})
    db.update_mission("m_live", status="OBSERVING", heartbeat_at=datetime.now(timezone.utc))
    assert main.main(["--stop", "m_live", "--simulate"]) == main.EXIT_OK
    live = db.get_mission("m_live")
    assert live["status"] == "OBSERVING" and live["stop_requested"]
    assert "will park" in capsys.readouterr().out


def test_a_stopped_mission_is_refused_on_resume(cli):
    _settings, db = cli
    db.create_mission("m_idle", PLUGIN, True, {})
    main.main(["--stop", "m_idle", "--simulate"])
    with pytest.raises(SystemExit, match="will not resume"):
        main.main(["--mission-id", "m_idle", "--simulate", "--fast"])
