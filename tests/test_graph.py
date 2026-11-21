from dataclasses import replace
from pathlib import Path

import pytest

from core.base_plugin import BaseExperimentPlugin
from core.graph import has_checkpoint, run_mission
from core.state import initial_state
from plugins.asteroid_finder.plugin import AsteroidFinderPlugin
from tests.conftest import Harness, make_settings, offline_world

MID = "m_test"


def run(h: Harness, mid: str = MID):
    h.db.create_mission(mid, h.runtime.plugin.name, True, {"cloud_cover_threshold_pct": 30.0})
    return run_mission(h.app, mid, initial_state(mid, h.runtime.plugin.name, True))


def node_sequence(h: Harness, mid: str = MID) -> list[str]:
    return [e["node"] for e in h.db.list_logs(mid, limit=2000, kinds=["node_start"])]


@pytest.fixture(scope="module")
def finished_mission(tmp_path_factory):
    """The default simulated mission, run once; the tests below only read what it left behind."""
    with offline_world():
        h = Harness(make_settings(tmp_path_factory.mktemp("graph") / "data"))
        try:
            final = run(h)
        except BaseException:
            h.close()
            raise
    try:
        yield h, final
    finally:
        h.close()


def test_full_simulated_mission(finished_mission):
    harness, final = finished_mission
    assert final["status"] == "COMPLETE"
    assert len(final["observations"]) == 3

    seq = node_sequence(harness)
    assert seq[:2] == ["generate_hypothesis", "check_weather"]
    assert seq.count("execute_observation") == 3 and seq[-1] == "write_log"

    mission = harness.db.get_mission(MID)
    assert mission["status"] == "COMPLETE" and mission["error"] is None
    assert [t["status"] for t in harness.db.list_targets(MID)] == ["OBSERVED"] * 3
    obs = harness.db.list_observations(MID)
    assert len(obs) == 3 and all(o["findings"] and o["crossref"] and o["ended_at"] for o in obs)
    assert len(harness.db.list_weather(MID)) == 3

    log = Path(final["mission_log"]["path"])
    assert log.suffix == ".md" and log.is_file() and "paper" not in final
    assert [a["path"] for a in harness.db.list_artifacts(MID, "mission_log")] == [str(log)]
    assert any(e["kind"] == "log" for e in harness.db.list_logs(MID))  # python logging mirrored to telemetry


def test_science_outcomes_of_the_simulated_mission(finished_mission):
    _, final = finished_mission
    by_name = {o["target"]["name"]: o for o in final["observations"]}

    eros = by_name["433 Eros (sim)"]
    assert len(eros["findings"]["detections"]) == 1
    entry = eros["crossref"]["entries"][0]
    assert entry["classification"] == "known_object_match"
    assert entry["match"]["designation"] == "433" and entry["separation_arcsec"] < 5.0
    cal = eros["crossref"]["calibration"]
    assert cal["status"] == "calibrated" and cal["n_matches"] > 50
    assert cal["offset_arcsec"] == pytest.approx(32.8, abs=1.0)  # the injected pointing error

    field_b = by_name["Field B (sim)"]["crossref"]["entries"][0]
    # Field B has no designation, so no catalogue was asked about it: "not checked", not "unidentified".
    assert field_b["classification"] == "unverified" and field_b["match"] is None
    assert "Not checked" in field_b["notes"][0]
    xref_b = by_name["Field B (sim)"]["crossref"]
    assert "1 not checked" in xref_b["summary"] and "0 unidentified" in xref_b["summary"]
    assert xref_b["services"]["jpl_horizons"] == "not queried (no designations)"
    text = Path(final["mission_log"]["path"]).read_text()
    assert "1 not checked" in text  # the mission log says what was not checked

    assert by_name["Field C control (sim)"]["findings"]["detections"] == []


def test_cloud_cover_sends_mission_to_sleep_then_observes(settings):
    h = Harness(replace(settings, mock_cloudy_checks=2, max_targets=1))
    try:
        final = run(h)
        assert final["status"] == "COMPLETE"
        seq = node_sequence(h)
        assert seq[:7] == ["generate_hypothesis", "check_weather", "sleep", "check_weather", "sleep", "check_weather", "execute_observation"]
        clear = [w["clear"] for w in h.db.list_weather(MID)]
        assert clear == [False, False, True]
    finally:
        h.close()


def test_weather_that_never_clears_is_handed_back_and_can_resume(settings):
    """Bad weather releases the process with a wake time. It does not abort. A later run finishes the mission."""
    from datetime import datetime, timedelta, timezone

    from core.clock import SystemClock

    first = Harness(replace(settings, mock_cloudy_checks=100, max_sleep_cycles=3, max_targets=1))
    try:
        deferred = run(first)
        assert deferred["status"] == "SLEEP" and deferred.get("deferred_until")
        assert first.captures == 0
        mission = first.db.get_mission(MID)
        assert mission["status"] == "SLEEP" and mission["wake_at"] and "never cleared" not in (mission["error"] or "")
        assert "release" not in node_sequence(first)  # the process stopped before holding the wait
    finally:
        first.close()

    later = datetime.now(timezone.utc) + timedelta(hours=3)
    second = Harness(replace(settings, mock_cloudy_checks=0, max_targets=1))
    second.runtime.clock = SystemClock(wall=lambda: later.timestamp())
    try:
        final = run_mission(second.app, MID, initial=None)
        assert final["status"] == "COMPLETE" and second.captures == 1
        assert second.db.get_mission(MID)["status"] == "COMPLETE"
    finally:
        second.close()


def test_a_stop_request_parks_and_the_mission_will_not_resume(settings):
    import argparse

    from main import choose_mission

    h = Harness(replace(settings, max_targets=1))
    try:
        h.db.create_mission(MID, h.runtime.plugin.name, True, {})
        h.db.update_mission(MID, stop_requested=True)
        final = run_mission(h.app, MID, initial_state(MID, h.runtime.plugin.name, True))
        assert final["status"] == "STOPPED" and h.captures == 0 and h.telescope.park_calls >= 1
        assert "stay stopped" in (h.db.get_mission(MID)["error"] or "")
        with pytest.raises(SystemExit, match="will not resume"):
            choose_mission(
                argparse.Namespace(mission_id=MID, new=False), h.db, h.app, h.runtime.plugin.name, True,
            )
    finally:
        h.close()


def test_a_full_disk_fails_the_mission_before_a_capture(settings):
    h = Harness(replace(settings, storage_check_in_simulation=True, min_free_gb=1e9, max_targets=1))
    try:
        final = run(h)
        assert final["status"] == "FAILED" and h.captures == 0
        assert "free space" in (h.db.get_mission(MID)["error"] or "")
    finally:
        h.close()


class _Broken(AsteroidFinderPlugin):
    fail_in: str = ""

    def analyze_frames(self, fits_paths):
        if self.fail_in == "analyze":
            raise ValueError("reduction exploded")
        return super().analyze_frames(fits_paths)

    def cross_reference(self, findings, catalogs, target):
        if self.fail_in == "xref":
            raise RuntimeError("Horizons is down")
        return super().cross_reference(findings, catalogs, target)


def test_node_exception_fails_mission_and_stops(settings):
    plugin = _Broken(settings)
    plugin.fail_in = "analyze"
    h = Harness(settings, plugin)
    try:
        final = run(h)
        assert final["status"] == "FAILED"
        mission = h.db.get_mission(MID)
        assert mission["status"] == "FAILED" and "reduction exploded" in mission["error"]
        assert h.captures == 1 and h.db.list_artifacts(MID, "mission_log") == []
        assert "node_error" in [e["kind"] for e in h.db.list_logs(MID, kinds=["node_error"])]
    finally:
        h.close()


class _Flaky(AsteroidFinderPlugin):
    """Raises ``exc`` from the first ``failures`` calls to analyze_frames, then behaves."""

    def __init__(self, settings, exc, failures):
        super().__init__(settings)
        self.exc, self.failures, self.calls = exc, failures, 0

    def analyze_frames(self, fits_paths):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return super().analyze_frames(fits_paths)


def test_transient_failure_is_retried_with_backoff_and_logged(settings):
    settings = replace(settings, max_targets=1)
    plugin = _Flaky(settings, ConnectionError("link dropped"), failures=2)
    h = Harness(settings, plugin)
    slept = []
    h.runtime.sleep_fn = slept.append
    try:
        final = run(h)
        assert final["status"] == "COMPLETE" and plugin.calls == 3
        assert slept[:2] == [settings.node_backoff_s, settings.node_backoff_s * 2]
        retries = h.db.list_logs(MID, kinds=["node_retry"])
        assert len(retries) == 2 and "link dropped" in retries[0]["message"]
    finally:
        h.close()


def test_transient_failure_that_persists_fails_the_mission_after_the_retries(settings):
    settings = replace(settings, max_targets=1, node_retries=1)
    plugin = _Flaky(settings, TimeoutError("still down"), failures=99)
    h = Harness(settings, plugin)
    try:
        final = run(h)
        assert final["status"] == "FAILED" and plugin.calls == 2  # first try + one retry
        assert "still down" in h.db.get_mission(MID)["error"]
    finally:
        h.close()


def test_non_transient_failure_is_not_retried(settings):
    settings = replace(settings, max_targets=1)
    plugin = _Flaky(settings, ValueError("a real bug"), failures=99)
    h = Harness(settings, plugin)
    try:
        assert run(h)["status"] == "FAILED" and plugin.calls == 1
    finally:
        h.close()


def test_catalog_outage_is_nonfatal_and_recorded(settings):
    settings = replace(settings, max_targets=2)  # two targets are enough to show every failed lookup is recorded
    plugin = _Broken(settings)
    plugin.fail_in = "xref"
    h = Harness(settings, plugin)
    try:
        final = run(h)
        assert final["status"] == "COMPLETE"
        assert len(final["observations"]) == 2 and len(final["errors"]) == 2
        assert all("Horizons is down" in o["crossref"]["error"] for o in final["observations"])
    finally:
        h.close()


def test_no_targets_aborts_cleanly(settings):
    class Empty(AsteroidFinderPlugin):
        def generate_targets(self):
            return []

    h = Harness(settings, Empty(settings))
    try:
        assert run(h)["status"] == "ABORTED" and h.captures == 0
    finally:
        h.close()


def test_invalid_plugin_targets_fail_with_clear_message(settings):
    class Bad(AsteroidFinderPlugin):
        def generate_targets(self):
            return [{"name": "x", "ra_deg": 10.0}]  # no dec

    h = Harness(settings, Bad(settings))
    try:
        final = run(h)
        assert final["status"] == "FAILED" and "dec_deg" in h.db.get_mission(MID)["error"]
    finally:
        h.close()


# ---------------------------------------------------------------------------- #
# Persistence
# ---------------------------------------------------------------------------- #
class _Bomb(AsteroidFinderPlugin):
    """Simulates the process dying (not an Exception) during the first reduction."""

    armed = True

    def analyze_frames(self, fits_paths):
        if _Bomb.armed:
            _Bomb.armed = False
            raise KeyboardInterrupt
        return super().analyze_frames(fits_paths)


def test_mission_resumes_from_checkpoint_after_restart(settings):
    _Bomb.armed = True
    settings = replace(settings, max_targets=2)  # two targets are enough to show the first is not captured again
    first = Harness(settings, _Bomb(settings))
    first.db.create_mission(MID, "asteroid_finder", True, {})
    with pytest.raises(KeyboardInterrupt):
        run_mission(first.app, MID, initial_state(MID, "asteroid_finder", True))
    assert first.captures == 1
    assert first.db.get_mission(MID)["status"] not in {"COMPLETE", "FAILED", "ABORTED"}
    first.close()

    # "Restart": brand-new objects over the same on-disk state.
    second = Harness(settings)
    try:
        assert has_checkpoint(second.app, MID)
        final = run_mission(second.app, MID, initial=None)  # resume: no initial state needed
        assert final["status"] == "COMPLETE" and len(final["observations"]) == 2
        assert second.captures == 1, "target 1 must not be re-captured after resume"
        assert len(second.db.list_observations(MID)) == 2, "no orphaned observation rows"
        assert second.db.get_mission(MID)["status"] == "COMPLETE"
    finally:
        second.close()


def test_finished_mission_is_not_rerun(finished_mission):
    harness, _ = finished_mission
    captures = harness.captures
    again = run_mission(harness.app, MID, initial=None)
    assert again["status"] == "COMPLETE" and harness.captures == captures


# ---------------------------------------------------------------------------- #
# Domain-agnosticism: a plugin that has nothing to do with asteroids
# ---------------------------------------------------------------------------- #
class ToyTransitPlugin(BaseExperimentPlugin):
    name = "toy_transit"
    description = "Test double: one target, trivial analysis."

    def generate_targets(self):
        return [{"name": "Toy-1", "ra_deg": 12.0, "dec_deg": -30.0}]

    def get_exposure_plan(self):
        return {"gain": 100, "exposure_s": 10.0, "filter": "LP", "count": 2}

    def analyze_frames(self, fits_paths):
        return {"summary": f"Measured {len(fits_paths)} frames; depth 0.4%.", "depth_pct": 0.4}


def test_core_runs_an_unrelated_plugin_with_default_hooks(settings):
    h = Harness(settings, ToyTransitPlugin(settings))
    try:
        final = run(h)
        assert final["status"] == "COMPLETE"
        obs = final["observations"][0]
        assert obs["findings"]["depth_pct"] == 0.4 and obs["findings"]["detections"] == []
        assert obs["crossref"]["summary"].startswith("No cross-referencing")
        text = Path(final["mission_log"]["path"]).read_text()
        assert "Toy-1" in text and "depth 0.4" in text
    finally:
        h.close()
