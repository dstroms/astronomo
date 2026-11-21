"""Long-cadence harness: clocks, wait_for_visit, resume, leases, concurrent missions, DB migration."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from core.clock import Clock, ReplayClock, SystemClock, clock_from_state, parse_time
from core.graph import has_checkpoint, run_mission
from core.lease import Lease, LeaseError, liveness
from core.state import initial_state
from db.database import Database
from tests.archive_stub import DEFAULT_OFFSETS, T0, StubArchivePlugin, StubSource
from tests.conftest import Harness

MID = "m_cadence"


def archive_settings(settings, **kw):
    return replace(settings, data_source="archive", offline_services=True, replay_speed=0.0, **kw)


def start(h: Harness, mid: str = MID, source: str = "archive"):
    h.db.create_mission(mid, h.runtime.plugin.name, False, {}, source)
    return initial_state(mid, h.runtime.plugin.name, False, source)


def nodes(h: Harness, mid: str = MID) -> list[str]:
    return [e["node"] for e in h.db.list_logs(mid, limit=3000, kinds=["node_start"])]


class FakeWall:
    """Wall clock that only moves when somebody sleeps."""

    def __init__(self):
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


# --------------------------------------------------------------------------- #
# Clocks
# --------------------------------------------------------------------------- #
def test_jump_clock_moves_without_waiting():
    clock = ReplayClock(T0, speed=0.0)
    slept: list[float] = []
    clock.wait_until(T0 + timedelta(days=19), sleep_fn=slept.append)
    assert clock.now() == T0 + timedelta(days=19) and slept == []
    clock.wait_until(T0, sleep_fn=slept.append)  # survey time never runs backwards
    assert clock.now() == T0 + timedelta(days=19)


def test_replay_clock_scales_waiting_by_speed():
    wall = FakeWall()
    clock = ReplayClock(T0, speed=3600.0, wall=wall)  # 1 survey hour per wall second
    ticks: list[datetime] = []
    clock.wait_until(T0 + timedelta(hours=5), sleep_fn=wall.sleep, tick_s=1.0, on_tick=ticks.append)
    assert sum(wall.slept) == pytest.approx(5.0, abs=1e-6)
    assert clock.now() == T0 + timedelta(hours=5)
    assert len(ticks) == 5


def test_system_clock_waits_in_slices():
    wall = FakeWall()
    clock = SystemClock(wall)
    target = datetime.fromtimestamp(wall.t + 25, tz=timezone.utc)
    clock.wait_until(target, sleep_fn=wall.sleep, tick_s=10.0)
    assert wall.slept == [10.0, 10.0, 5.0]


def test_replay_clock_state_resumes_paused_not_catching_up():
    wall = FakeWall()
    clock = ReplayClock(T0, speed=60.0, wall=wall)
    wall.t += 120  # two wall minutes = two survey hours
    saved = clock.state()
    assert parse_time(saved["sim_now"]) == T0 + timedelta(hours=2)
    wall.t += 10_000  # the process is down for ages
    restored = clock_from_state(saved, wall=wall)
    assert restored.now() == T0 + timedelta(hours=2)  # re-anchored: no catch-up for the downtime
    assert restored.speed == 60.0
    assert clock_from_state(None).kind == "system"


# --------------------------------------------------------------------------- #
# Missions that wait
# --------------------------------------------------------------------------- #
def test_archive_mission_visits_in_order_waits_and_skips_weather(settings):
    plugin = StubArchivePlugin(archive_settings(settings))
    h = Harness(archive_settings(settings), plugin)
    try:
        final = run_mission(h.app, MID, start(h))
        assert final["status"] == "COMPLETE"
        assert [o["target"]["name"] for o in final["observations"]] == ["visit-1", "visit-2", "visit-3"]
        seq = nodes(h)
        assert "check_weather" not in seq and seq.count("wait_for_visit") == 3 and seq.count("execute_observation") == 3
        assert h.captures == 0  # the telescope was never touched
        assert h.db.list_weather(MID) == []
        # the clock followed the survey: 3 days + the last burst
        mission = h.db.get_mission(MID)
        assert parse_time(mission["sim_time"]) == T0 + DEFAULT_OFFSETS[2] + timedelta(seconds=2 * 300)
        assert mission["wake_at"] is None and mission["data_source"] == "archive"
        assert mission["progress"]["phase"] == "complete"
        # provenance flows into the observation records handed to the mission log
        assert final["observations"][0]["archive"]["source"] == "stub-survey"
        assert final["simulated"] is False
    finally:
        h.close()


def test_wait_is_visible_in_the_database_while_it_happens(settings):
    s = archive_settings(settings)
    seen: list[dict] = []
    h = Harness(s, StubArchivePlugin(s, offsets=(timedelta(0), timedelta(days=2))))
    try:
        def spy(_seconds: float) -> None:  # only called by a non-jump clock
            seen.append(h.db.get_mission(MID))

        wall = FakeWall()
        h.runtime.clock = ReplayClock(T0, speed=86400.0, wall=wall)  # one survey day per second
        h.runtime.sleep_fn = lambda sec: (spy(sec), wall.sleep(sec))
        run_mission(h.app, MID, start(h))
        waiting = [m for m in seen if m["status"] == "WAITING"]
        assert waiting, "the mission never showed as WAITING"
        m = waiting[0]
        assert m["wake_at"] is not None and m["progress"]["phase"] in ("waiting", "acquiring")
        assert parse_time(m["wake_at"]) in (T0, T0 + timedelta(days=2)) or m["progress"]["phase"] == "acquiring"
        assert sum(wall.slept) > 1.5  # ~2 days at 1 day/s, plus frame pacing
    finally:
        h.close()


def test_crash_during_a_wait_resumes_the_wait_not_the_mission(settings, monkeypatch):
    s = archive_settings(settings)
    wall2 = FakeWall()
    # The restarted process rebuilds its clock from the DB (the real restore path), on a fake wall.
    import core.graph as graph_mod
    monkeypatch.setattr(graph_mod, "clock_from_state", lambda state: clock_from_state(state, wall=wall2))

    def make():
        h = Harness(s, StubArchivePlugin(s))
        h.runtime.sleep_fn = lambda sec: None
        return h

    h1 = make()
    wall = FakeWall()
    h1.runtime.clock = ReplayClock(T0, speed=3600.0, wall=wall)

    def die_in_the_long_wait(sec: float) -> None:
        wall.sleep(sec)
        if h1.runtime.clock.now() > T0 + timedelta(days=1):
            raise KeyboardInterrupt  # Ctrl-C / kill -9 stand-in, mid-wait for visit 3

    h1.runtime.sleep_fn = die_in_the_long_wait
    try:
        with pytest.raises(KeyboardInterrupt):
            run_mission(h1.app, MID, start(h1))
        snap = h1.app.get_state({"configurable": {"thread_id": MID}, "recursion_limit": 1000})
        assert snap.next == ("wait_for_visit",)
        assert len(snap.values["observations"]) == 2  # visits 1 and 2 are safe in the checkpoint
        saved = h1.db.get_mission(MID)
        assert saved["status"] == "WAITING" and parse_time(saved["wake_at"]) == T0 + DEFAULT_OFFSETS[2]
        sim_at_crash = parse_time(saved["sim_time"])
        # persisted every tick (1 wall s = 1 survey h): the last tick before the kill, not "the end"
        assert T0 + timedelta(hours=20) < sim_at_crash <= T0 + timedelta(days=1, hours=1)
    finally:
        h1.close()

    # "restart the process": brand-new runtime, brand-new clock object, same databases.
    h2 = make()
    h2.runtime.sleep_fn = wall2.sleep
    try:
        assert has_checkpoint(h2.app, MID)
        final = run_mission(h2.app, MID)
        assert final["status"] == "COMPLETE" and len(final["observations"]) == 3
        assert h2.runtime.plugin.source.acquired == ["visit-3"]  # visits 1-2 were not fetched again
        # The restored clock started at the saved survey time (not at the origin, not "caught up"),
        # so only the remainder of the wait was spent: (visit-3 - saved) / 3600 wall seconds + frame pacing.
        remaining_survey_s = (T0 + DEFAULT_OFFSETS[2] - sim_at_crash).total_seconds()
        assert sum(wall2.slept) == pytest.approx(remaining_survey_s / 3600.0 + 2 * 300 / 3600.0, abs=1.5)
        assert parse_time(h2.db.get_mission(MID)["sim_time"]) > T0 + DEFAULT_OFFSETS[2]
    finally:
        h2.close()


def test_telescope_mission_with_a_scheduled_visit_waits_then_checks_weather(settings):
    s = replace(settings, max_targets=2)
    soon = datetime.now(timezone.utc) + timedelta(hours=6)

    class Scheduled(StubArchivePlugin):
        def generate_targets(self):
            return [
                {"name": "now", "ra_deg": 10.0, "dec_deg": 5.0, "priority": 2.0},
                {"name": "later", "ra_deg": 11.0, "dec_deg": 5.0, "priority": 9.0, "not_before": soon.isoformat()},
            ]

        def analyze_frames(self, fits_paths):
            return {"summary": "ok", "detections": [], "figures": []}

    class ManualClock(Clock):
        kind, speed = "manual", 1.0

        def __init__(self):
            self.t = datetime.now(timezone.utc)

        def now(self):
            return self.t

        def wait_until(self, when, *, sleep_fn=None, tick_s=1.0, on_tick=None):
            self.t = max(self.t, parse_time(when))

        def state(self):
            return {"kind": "system"}

    h = Harness(s, Scheduled(s))
    h.runtime.clock = ManualClock()
    try:
        h.db.create_mission(MID, "stub_archive", True, {})
        final = run_mission(h.app, MID, initial_state(MID, "stub_archive", True))
        assert final["status"] == "COMPLETE"
        # unscheduled first (priority is irrelevant to scheduled targets: time order wins), then the wait
        assert [o["target"]["name"] for o in final["observations"]] == ["now", "later"]
        seq = nodes(h)
        assert seq[:3] == ["generate_hypothesis", "check_weather", "execute_observation"]
        i = seq.index("wait_for_visit")
        assert seq[i + 1:i + 3] == ["check_weather", "execute_observation"]  # a telescope still checks the weather
        assert len(h.db.list_weather(MID)) == 2
        assert h.runtime.clock.now() >= soon
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# Ownership, concurrency, migration
# --------------------------------------------------------------------------- #
def test_lease_blocks_a_second_owner_until_the_heartbeat_goes_stale(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    try:
        db.create_mission("m1", "p", False, {})
        assert db.claim_mission("m1", 111, "hostA", lease_s=30)
        assert not db.claim_mission("m1", 222, "hostB", lease_s=30)  # live owner
        assert db.claim_mission("m1", 111, "hostA", lease_s=30)  # re-claim by the owner is fine
        assert liveness(db.get_mission("m1"), 30) == "running"
        assert db.heartbeat("m1", 111, "hostA") and not db.heartbeat("m1", 222, "hostB")
        assert not db.claim_mission("m1", 222, "hostB", lease_s=30)
        # owner dies: heartbeat goes stale
        long_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.update_mission("m1", heartbeat_at=long_ago)
        assert liveness(db.get_mission("m1"), 30) == "orphaned"
        assert db.claim_mission("m1", 222, "hostB", lease_s=30)  # takeover
        assert not db.heartbeat("m1", 111, "hostA")  # the old owner has lost it
        db.release_mission("m1", 222, "hostB")
        assert liveness(db.get_mission("m1"), 30) == "idle"
        db.update_mission("m1", status="COMPLETE")
        assert liveness(db.get_mission("m1"), 30) == "finished"
    finally:
        db.dispose()


def test_lease_context_heartbeats_and_releases(tmp_path):
    db = Database(tmp_path / "b.sqlite")
    try:
        db.create_mission("m1", "p", False, {})
        with Lease(db, "m1", lease_s=3.0, interval_s=0.05) as lease:
            first = db.get_mission("m1")["heartbeat_at"]
            threading.Event().wait(0.3)
            assert db.get_mission("m1")["heartbeat_at"] > first
            with pytest.raises(LeaseError):
                other = Database(tmp_path / "b.sqlite")
                try:
                    # same pid/host would re-claim; fake another worker by claiming through the DB API
                    assert not other.claim_mission("m1", lease.pid + 1, lease.host, 3.0)
                    raise LeaseError("owned")
                finally:
                    other.dispose()
        assert db.get_mission("m1")["worker_pid"] is None
    finally:
        db.dispose()


def test_latest_mission_can_skip_missions_a_live_process_owns(tmp_path):
    db = Database(tmp_path / "c.sqlite")
    try:
        db.create_mission("old", "p", False, {}, "archive")
        db.create_mission("new", "p", False, {}, "archive")
        db.claim_mission("new", 1, "h", 30)
        assert db.latest_mission(plugin_name="p", unfinished_only=True)["id"] == "new"
        assert db.latest_mission(plugin_name="p", unfinished_only=True, unowned_only=True, lease_s=30)["id"] == "old"
        assert db.latest_mission(plugin_name="p", data_source="telescope") is None
    finally:
        db.dispose()


def test_two_missions_run_concurrently_in_one_database(settings):
    """Two missions share the DB while interleaving; each owns its own clock, rows and checkpoint."""
    s = archive_settings(settings)
    harnesses = [Harness(replace(s, data_dir=s.data_dir), StubArchivePlugin(s, source=StubSource(frames_per_visit=4)))
                 for _ in range(2)]
    results: dict[str, str] = {}
    errors: list[BaseException] = []
    progress_seen: dict[str, set[str]] = {"m_a": set(), "m_b": set()}

    def work(h: Harness, mid: str, offsets):
        try:
            h.runtime.plugin.offsets = offsets
            wall = FakeWall()
            h.runtime.clock = ReplayClock(T0, speed=7200.0, wall=wall)

            def tiny_sleep(sec):
                wall.sleep(sec)
                threading.Event().wait(0.002)  # real time passes, so the two threads interleave
                m = h.db.get_mission(mid)
                progress_seen[mid].add(((m or {}).get("progress") or {}).get("phase", ""))

            h.runtime.sleep_fn = tiny_sleep
            with Lease(h.db, mid, 30.0, interval_s=0.1):
                final = run_mission(h.app, mid, initial_state(mid, "stub_archive", False, "archive"))
            results[mid] = final["status"]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        for h, mid in zip(harnesses, ("m_a", "m_b")):
            h.db.create_mission(mid, "stub_archive", False, {}, "archive")
        threads = [
            threading.Thread(target=work, args=(harnesses[0], "m_a", (timedelta(0), timedelta(hours=6)))),
            threading.Thread(target=work, args=(harnesses[1], "m_b", (timedelta(minutes=30), timedelta(hours=9), timedelta(hours=20)))),
        ]
        [t.start() for t in threads]
        [t.join(timeout=60) for t in threads]
        assert not errors, errors
        assert results == {"m_a": "COMPLETE", "m_b": "COMPLETE"}
        a, b = (harnesses[0].db.get_mission(m) for m in ("m_a", "m_b"))
        assert a["worker_pid"] is None and b["worker_pid"] is None  # both released
        assert len(harnesses[0].db.list_observations("m_a")) == 2
        assert len(harnesses[0].db.list_observations("m_b")) == 3
        assert {"waiting", "acquiring"} <= progress_seen["m_a"] | progress_seen["m_b"]
    finally:
        for h in harnesses:
            h.close()


def test_old_databases_are_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE missions (id VARCHAR(64) PRIMARY KEY, plugin_name VARCHAR(64), status VARCHAR(32), "
        "current_node VARCHAR(64), simulated BOOLEAN, hypothesis TEXT, error TEXT, config JSON, "
        "created_at DATETIME, updated_at DATETIME)"
    )
    conn.execute("INSERT INTO missions (id, plugin_name, status, simulated, created_at) VALUES ('legacy','p','COMPLETE',1,'2025-01-01 00:00:00')")
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        legacy = db.get_mission("legacy")
        assert legacy["status"] == "COMPLETE"
        assert legacy["data_source"] == "telescope"  # defaulted, not NULL
        assert legacy["wake_at"] is None and legacy["worker_pid"] is None
        db.create_mission("fresh", "p", False, {}, "archive")
        db.update_progress("fresh", phase="waiting", visit=2)
        db.update_progress("fresh", visit=None, detail="x")
        assert db.get_mission("fresh")["progress"] == {"phase": "waiting", "detail": "x"}
    finally:
        db.dispose()
