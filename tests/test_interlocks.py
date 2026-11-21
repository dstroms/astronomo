"""Site interlocks: the rules (``core/interlocks.py``), the sky they read, and how the mission graph acts on them.

The rules are pure functions of a target, a time and the sky, so most tests give them a made-up sky (``_sky``) and
need no astropy.  Two tests use the real sky functions: the exact and the grid-interpolated paths must agree, because
the mission uses one and the Opportunities page the other.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from core import interlocks
from core.base_plugin import PluginContractError
from core.graph import _start_target, run_mission
from core.interlocks import SiteLimits, evaluate, required_moon_separation, window_notes
from core.state import initial_state
from plugins.asteroid_finder.plugin import AsteroidFinderPlugin
from tests.conftest import Harness

NOW = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)
SITE = (40.0, -105.0, 1600.0)
LIMITS = SiteLimits(min_altitude_deg=30.0, min_sun_separation_deg=30.0, moon_avoid_deg=60.0, moon_floor_deg=15.0)


def _sky(alt=60.0, sun_alt=-40.0, sun_sep=120.0, moon_alt=-20.0, moon_sep=100.0, moon_illum=0.5):
    """A stand-in for ``sky_conditions_many``: the same value at every requested time."""
    def fake(ra, dec, lat, lon, elev, times):
        n = len(times)
        return {k: np.full(n, float(v)) for k, v in (
            ("target_alt", alt), ("sun_alt", sun_alt), ("sun_sep", sun_sep),
            ("moon_alt", moon_alt), ("moon_sep", moon_sep), ("moon_illum", moon_illum))}
    return fake


def _target(**over):
    return {"name": "T", "ra_deg": 150.0, "dec_deg": 10.0, **over}


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #
def test_the_moon_needs_more_room_the_fuller_it_is_and_none_once_it_has_set():
    need = required_moon_separation([30.0, 30.0, 30.0, -5.0], [1.0, 0.5, 0.0, 1.0], LIMITS)
    assert list(need) == [60.0, 30.0, 15.0, 0.0]  # full: 60, half: 30, new: the floor, below the horizon: nothing


def test_a_clear_sky_passes_and_the_facts_are_recorded(monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    verdict = evaluate(_target(), NOW, SITE, LIMITS)
    assert verdict.ok and not verdict.skip and verdict.reasons == []
    assert verdict.facts["target_altitude_deg"] == 60.0 and verdict.facts["moon_required_separation_deg"] == 0.0


@pytest.mark.parametrize("sky, phrase", [
    ({"alt": 12.0}, "altitude 12 deg is below the minimum of 30"),
    ({"sun_sep": 10.0}, "10 deg from the Sun"),
    ({"moon_alt": 40.0, "moon_sep": 20.0, "moon_illum": 1.0}, "20 deg from the 100% Moon (needs 60 deg"),
])
def test_each_sky_limit_blocks_but_does_not_skip(monkeypatch, sky, phrase):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(**sky))
    verdict = evaluate(_target(), NOW, SITE, LIMITS)
    assert not verdict.ok and not verdict.skip  # it may rise, or the Moon may set: wait
    assert phrase in verdict.reason


def test_a_target_near_the_zenith_waits_until_it_has_moved_on(monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(alt=85.0))
    capped = replace(LIMITS, max_altitude_deg=70.0)
    verdict = evaluate(_target(), NOW, SITE, capped)
    assert not verdict.ok and not verdict.skip, "a wait, not a skip: it will come down"
    assert "above the maximum of 70 deg" in verdict.reason and "zenith" in verdict.reason
    assert evaluate(_target(), NOW, SITE, LIMITS).ok, "a limit built by hand has no cap"
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(alt=65.0))
    assert evaluate(_target(), NOW, SITE, capped).ok


def test_the_missions_limits_carry_the_maximum_altitude(settings):
    assert interlocks.limits_from_settings(settings).max_altitude_deg == settings.max_altitude_deg == 70.0
    assert interlocks.limits_from_settings(replace(settings, max_altitude_deg=90.0)).max_altitude_deg == 90.0


def test_window_notes_mention_the_zenith(monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(alt=85.0))
    target = _target(window_start=NOW.isoformat(), window_end=(NOW + timedelta(hours=2)).isoformat())
    notes = window_notes(target, NOW, SITE, replace(LIMITS, max_altitude_deg=70.0), max_sun_alt_deg=-12.0)
    assert len(notes) == 3 and "near the zenith (maximum 70 deg)" in notes[1]


def test_a_moon_below_the_horizon_does_not_block_however_close_it_is(monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(moon_alt=-3.0, moon_sep=2.0, moon_illum=1.0))
    assert evaluate(_target(), NOW, SITE, LIMITS).ok


def test_before_the_window_waits_after_the_window_skips(monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    early = evaluate(_target(window_start=(NOW + timedelta(hours=1)).isoformat(), window_end=(NOW + timedelta(hours=3)).isoformat()), NOW, SITE, LIMITS)
    assert not early.ok and not early.skip and "window opens at" in early.reason
    inside = evaluate(_target(window_start=(NOW - timedelta(hours=1)).isoformat(), window_end=(NOW + timedelta(hours=1)).isoformat()), NOW, SITE, LIMITS)
    assert inside.ok
    late = evaluate(_target(window_start=(NOW - timedelta(hours=3)).isoformat(), window_end=(NOW - timedelta(hours=1)).isoformat()), NOW, SITE, LIMITS)
    assert late.skip and not late.ok and "window closed at" in late.reason


def test_a_closed_window_is_skipped_without_looking_at_the_sky(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the sky is irrelevant once the window has closed")

    monkeypatch.setattr(interlocks, "sky_conditions_many", boom)
    assert evaluate(_target(window_end=(NOW - timedelta(minutes=1)).isoformat()), NOW, SITE, LIMITS).skip


def test_window_notes_describe_each_part_of_a_window_that_will_not_suit(monkeypatch):
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(alt=10.0, sun_alt=20.0))
    target = _target(window_start=NOW.isoformat(), window_end=(NOW + timedelta(hours=2)).isoformat())
    notes = window_notes(target, NOW, SITE, LIMITS, max_sun_alt_deg=-12.0)
    assert len(notes) == 3 and "at the start of the window" in notes[0] and "at the end of the window" in notes[2]
    assert "Sun is 20 deg up" in notes[0] and "only 10 deg high" in notes[0]
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky())
    assert window_notes(target, NOW, SITE, LIMITS, max_sun_alt_deg=-12.0) == []
    assert window_notes(_target(window_end=(NOW - timedelta(hours=1)).isoformat()), NOW, SITE, LIMITS, max_sun_alt_deg=-12.0) == []


# --------------------------------------------------------------------------- #
# The sky they read
# --------------------------------------------------------------------------- #
def test_the_grid_and_the_exact_sky_agree():
    from tools.astrometry import sky_conditions_many, sky_from_grid, sun_moon_grid

    times = [NOW + timedelta(hours=h) for h in (0.0, 1.7, 3.1, 5.9)]
    ras, decs = [150.0, 150.0, 20.0, 300.0], [10.0, 10.0, -30.0, 60.0]
    exact = sky_conditions_many(ras, decs, *SITE, times)
    grid = sun_moon_grid(*SITE, [NOW - timedelta(hours=1) + timedelta(minutes=10 * i) for i in range(60)])
    approx = sky_from_grid(grid, ras, decs, *SITE, times)
    for key in ("target_alt", "sun_alt", "sun_sep", "moon_alt", "moon_sep"):
        assert np.allclose(exact[key], approx[key], atol=0.3), key
    assert np.allclose(exact["moon_illum"], approx["moon_illum"], atol=0.01)


def test_a_target_on_top_of_the_sun_is_reported_as_close_to_it():
    from astropy.coordinates import get_sun
    from astropy.time import Time

    from tools.astrometry import sky_conditions_many

    sun = get_sun(Time(NOW))
    sky = sky_conditions_many([float(sun.ra.deg)], [float(sun.dec.deg)], *SITE, [NOW])
    assert float(sky["sun_sep"][0]) < 0.5


# --------------------------------------------------------------------------- #
# Target windows in the plugin contract
# --------------------------------------------------------------------------- #
def test_validate_targets_normalises_windows_and_rejects_bad_ones(settings):
    plugin = AsteroidFinderPlugin(settings)
    (ok,) = plugin.validate_targets([_target(window_start="2026-10-28T06:00:00Z", window_end="2026-10-28T08:00:00+00:00")])
    assert ok["window_start"] == "2026-10-28T06:00:00+00:00" and ok["window_end"] == "2026-10-28T08:00:00+00:00"
    (bare,) = plugin.validate_targets([_target(window_start="", window_end=None)])
    assert "window_start" not in bare and "window_end" not in bare
    with pytest.raises(PluginContractError, match="not after its window_start"):
        plugin.validate_targets([_target(window_start="2026-10-28T08:00:00Z", window_end="2026-10-28T06:00:00Z")])
    with pytest.raises(PluginContractError, match="invalid window_end"):
        plugin.validate_targets([_target(window_end="soon")])


def test_a_window_is_waited_for_only_where_windows_are_enforced():
    future = {"name": "T", "window_start": (NOW + timedelta(hours=1)).isoformat()}
    state = {"targets": [future], "target_index": 0}
    assert _start_target({**state, "windows_enforced": True}) == "wait_for_visit"
    assert _start_target(state) == "check_weather"  # simulation: plugins put now-to-2-hours windows on everything
    assert _start_target({**state, "windows_enforced": True, "data_source": "telescope", "targets": [{"name": "T"}]}) == "check_weather"


# --------------------------------------------------------------------------- #
# The mission graph acting on them (rehearsed with the mock telescope)
# --------------------------------------------------------------------------- #
class _Windowed(AsteroidFinderPlugin):
    """The simulated sky, with each target's window set by the test (``(start, end)`` offsets in hours from now)."""

    offsets: dict[str, tuple[float, float]] = {}

    def generate_targets(self):
        now = datetime.now(timezone.utc)
        out = []
        for t in super().generate_targets():
            if t["name"] in self.offsets:
                a, b = self.offsets[t["name"]]
                t = {**t, "window_start": (now + timedelta(hours=a)).isoformat(), "window_end": (now + timedelta(hours=b)).isoformat()}
            out.append(t)
        return out


def _mission(settings, monkeypatch, offsets, *, sky=None, **overrides):
    """Run one mission with the interlocks rehearsed in simulation; returns ``(final, node sequence, harness)``."""
    monkeypatch.setattr(interlocks, "sky_conditions_many", sky or _sky())
    s = replace(settings, interlocks_in_simulation=True, max_sun_altitude_deg=90.0, max_targets=2, **overrides)  # 90: the real Sun is not under test
    plugin = _Windowed(s)
    plugin.offsets = offsets
    h = Harness(s, plugin)
    try:
        h.db.create_mission("m_il", plugin.name, True, {"cloud_cover_threshold_pct": 30.0})
        final = run_mission(h.app, "m_il", initial_state("m_il", plugin.name, True))
        seq = [e["node"] for e in h.db.list_logs("m_il", limit=3000, kinds=["node_start"])]
        return final, seq, h
    finally:
        h.close()


@pytest.mark.slow
def test_a_target_whose_window_has_closed_is_skipped_and_the_next_is_observed(settings, monkeypatch):
    final, seq, h = _mission(settings, monkeypatch, {"433 Eros (sim)": (-3.0, -1.0), "Field B (sim)": (-1.0, 2.0)})
    assert final["status"] == "COMPLETE" and final["windows_enforced"] is True
    assert [o["target"]["name"] for o in final["observations"]] == ["Field B (sim)"]
    assert [s["name"] for s in final["skipped"]] == ["433 Eros (sim)"] and "window closed" in final["skipped"][0]["reason"]
    assert h.captures == 1  # the telescope was never pointed at the skipped target
    statuses = {t["name"]: t["status"] for t in h.db.list_targets("m_il")}
    assert statuses["433 Eros (sim)"] == "SKIPPED" and statuses["Field B (sim)"] == "OBSERVED"


@pytest.mark.slow
def test_a_mission_is_aborted_when_every_window_has_closed(settings, monkeypatch):
    final, seq, h = _mission(settings, monkeypatch, {"433 Eros (sim)": (-3.0, -1.0), "Field B (sim)": (-3.0, -1.0)})
    assert final["status"] == "ABORTED" and h.captures == 0
    assert len(final["skipped"]) == 2 and any("window closed" in e for e in final["errors"])
    assert "write_log" in seq  # it still leaves a log saying why nothing was observed


def test_a_target_that_never_clears_the_site_limits_is_waited_for_then_given_up_on(settings, monkeypatch):
    final, seq, h = _mission(settings, monkeypatch, {}, sky=_sky(alt=5.0), max_sleep_cycles=3)
    assert final["status"] == "ABORTED" and h.captures == 0
    assert any("site limits never cleared" in e for e in final["errors"])
    assert seq.count("sleep") >= 3


@pytest.mark.slow
def test_a_target_that_rises_into_limits_is_observed_after_waiting(settings, monkeypatch):
    calls = {"n": 0}
    good = _sky()

    def rising(ra, dec, lat, lon, elev, times):
        calls["n"] += 1
        return _sky(alt=10.0)(ra, dec, lat, lon, elev, times) if calls["n"] <= 2 else good(ra, dec, lat, lon, elev, times)

    final, seq, h = _mission(settings, monkeypatch, {}, sky=rising)
    assert final["status"] == "COMPLETE" and h.captures >= 1
    assert seq.count("sleep") == 2


def test_nothing_is_enforced_in_plain_simulation(settings, monkeypatch):
    """Without the flag the same impossible sky is ignored, so every existing simulated mission is unchanged."""
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky(alt=-30.0))
    s = replace(settings, max_targets=1)
    assert not s.interlocks_active
    h = Harness(s, _Windowed(s))
    try:
        h.db.create_mission("m_plain", "asteroid_finder", True, {})
        final = run_mission(h.app, "m_plain", initial_state("m_plain", "asteroid_finder", True))
        assert final["status"] == "COMPLETE" and final["windows_enforced"] is False and not final.get("skipped")
    finally:
        h.close()


def test_a_real_telescope_enforces_the_interlocks_and_a_simulated_one_does_not(settings):
    assert replace(settings, mock_mode=False).interlocks_active
    assert not settings.interlocks_active and replace(settings, interlocks_in_simulation=True).interlocks_active
    assert settings.public_dict()["interlocks"]["active"] is False


# --------------------------------------------------------------------------- #
# Skipped targets in the records people read
# --------------------------------------------------------------------------- #
def test_the_mission_log_lists_targets_that_were_not_observed():
    from core.mission_log import build_log

    log = build_log({
        "mission_id": "m1", "plugin_name": "x", "status": "COMPLETE", "night": "2026-10-28", "targets": [{"name": "A"}, {"name": "B"}],
        "observations": [], "skipped": [{"name": "A", "kind": "window_closed", "reason": "its observing window closed at 04:00 UTC"}],
    })
    text = str(log)
    assert "Targets not observed" in text and "its observing window closed at 04:00 UTC" in text
    assert "1 target(s) were not observed" in text
    assert "Targets not observed" not in str(build_log({"mission_id": "m1", "status": "COMPLETE", "targets": [], "observations": []}))


def test_a_skipped_visit_counts_as_finished_on_the_fleet_page():
    from dashboard.fleet import visit_rows

    rows = visit_rows(
        [{"position": 0, "name": "A", "status": "SKIPPED", "payload": {"window_start": "2026-10-28T04:00:00+00:00"}},
         {"position": 1, "name": "B", "status": "PENDING", "payload": {"window_start": "2026-10-28T09:00:00+00:00"}}], 1)
    assert rows[0]["state"] == "skipped" and rows[1]["state"] != "skipped"
