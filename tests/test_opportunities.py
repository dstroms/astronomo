"""Opportunities: what a person could take part in, which of it this telescope can reach, when, and what it is worth.

The planets are synthetic and the clock is fixed, so every expectation here is derivable by hand: a planet
with a 1.000 d period transits at the same time every day, so choosing that time puts it at local midnight
(dark) or local noon (never observable).
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from core import networks as core_networks
from core.base_plugin import BaseExperimentPlugin
from core.networks import NetworkError, network_block
from core.opportunities import OpportunityError, check_opportunity, check_report
from db.database import Database
from plugins import available_plugins, load_plugin
from plugins.transit_finder import networks as tn
from plugins.transit_finder import opportunities as opp
from tools import astrometry

# Denver (the config default site): local midnight on 2026-10-28 (MDT, UTC-6) is 06:00 UTC.
NOW = datetime(2026, 10, 28, 6, 0, 0, tzinfo=timezone.utc)
JD_NOW = NOW.timestamp() / 86400.0 + 2440587.5
MIDNIGHT_RA, HIGH_DEC = "02:16:00", "+40:00:00"  # on the meridian near local midnight at the end of October


def _planet(name, *, period=1.0, period_unc=1e-7, age_epochs=1000, t0_unc=1e-4, dur_h=3.0, v=11.5, depth_mmag=12.0,
            ra=MIDNIGHT_RA, dec=HIGH_DEC, hours_after_now=0.0, priority="medium"):
    """An ExoClock record whose transits fall ``hours_after_now`` after the current clock time, every ``period`` days."""
    return {
        "name": name, "star": name[:-1], "period_days": period, "period_unc": period_unc,
        "t0_bjd_tdb": JD_NOW - age_epochs * period + hours_after_now / 24.0, "t0_unc": t0_unc, "duration_hours": dur_h,
        "ra_j2000": ra, "dec_j2000": dec, "v_mag": v, "depth_r_mmag": depth_mmag, "priority": priority,
        "current_oc_min": 0.0, "total_observations_recent": 0,
    }


EXOCLOCK = {
    "Fresh-1b": _planet("Fresh-1b", period_unc=1e-8, priority="low"),                  # schedule known to ~0.1 min
    "Stale-2b": _planet("Stale-2b", period_unc=1e-4, priority="alert"),                # +/- ~144 min after 1000 epochs
    "Noon-3b": _planet("Noon-3b", period_unc=1e-4, hours_after_now=12.0),              # transits at local noon: never dark
    "South-4b": _planet("South-4b", dec="-60:00:00", period_unc=1e-4),                 # never climbs above 30 deg from Denver
    "Faint-5b": _planet("Faint-5b", v=14.5, period_unc=1e-4),                          # too faint
    "Shallow-6b": _planet("Shallow-6b", depth_mmag=2.0, period_unc=1e-4),              # 0.18 % deep
    # 400 d period, last transit 100 d ago, next in 300 d: no transit in the next 30 days
    "Longp-7b": _planet("Longp-7b", period=400.0, age_epochs=3, period_unc=1e-3, hours_after_now=-2400.0),
    "Nosig-8b": _planet("Nosig-8b", period_unc=None),                                  # period has no stated error
}


@pytest.fixture()
def live_settings(settings):
    # The fixtures put planets near the zenith of the test site; the maximum-altitude cap has its own test below.
    return dataclasses.replace(settings, offline_services=False, max_altitude_deg=90.0)


@pytest.fixture()
def served(monkeypatch):
    calls: list[str] = []

    def get(url, params, timeout):
        calls.append(url)
        if url == tn.EXOCLOCK_URL:
            return json.dumps(EXOCLOCK)
        raise NetworkError(f"unexpected URL {url}")

    monkeypatch.setattr(core_networks, "_http_get_text", get)
    return calls


@pytest.fixture()
def report(live_settings, served):
    """The report with the Moon rule switched off, so the windows depend only on darkness and altitude (the Moon has its own tests)."""
    moonless = dataclasses.replace(live_settings, moon_avoid_deg=0.0, moon_floor_deg=0.0)
    return opp.build_report(moonless, now=NOW, days=30, refresh=True)


@pytest.fixture()
def moon_report(live_settings, served):
    """The same sky with the default Moon rule.  NOW is two days after the full Moon, so the Moon matters."""
    return opp.build_report(live_settings, now=NOW, days=30, refresh=True)


def _by_name(report):
    return {o["name"]: o for o in report["opportunities"]}


# --------------------------------------------------------------------------- #
# Vectorised astrometry the report is built on
# --------------------------------------------------------------------------- #
def test_the_vector_helpers_agree_with_the_scalar_ones():
    lat, lon, elev = 39.7392, -104.9903, 1609.0
    times = [NOW + timedelta(hours=7 * i) for i in range(6)]
    ra, dec = np.array([97.6, 200.0, 10.0, 97.6, 200.0, 10.0]), np.array([29.6, -10.0, 60.0, 29.6, -10.0, 60.0])
    alt = astrometry.target_altitude_deg_many(ra, dec, lat, lon, elev, times)
    sun = astrometry.sun_altitude_deg_many(lat, lon, elev, times)
    for i, when in enumerate(times):
        assert alt[i] == pytest.approx(astrometry.target_altitude_deg(ra[i], dec[i], lat, lon, elev, when), abs=1e-6)
        assert sun[i] == pytest.approx(astrometry.sun_altitude_deg(lat, lon, elev, when), abs=1e-6)

    bjd = [2461000.123456, 2461010.5, 2461020.9]
    many = astrometry.bjd_tdb_to_utc_many(bjd, ra[:3], dec[:3], lat, lon, elev)
    for b, r, d, got in zip(bjd, ra[:3], dec[:3], many):
        assert abs((got - astrometry.bjd_tdb_to_utc(b, r, d, lat, lon, elev)).total_seconds()) < 1.0
        assert got.tzinfo is not None
    assert astrometry.bjd_tdb_to_utc_many([], [], [], lat, lon, elev) == []


# --------------------------------------------------------------------------- #
# Impact: an estimate that carries its assumptions
# --------------------------------------------------------------------------- #
def _row(period_err_d, *, epoch_age=1000, t0_err_d=1e-4, period=1.0):
    return {"period_d": period, "period_err_d": period_err_d, "t0_err_d": t0_err_d, "t0_bjd_tdb": 0.0, "staleness": 2.0}


def test_a_stale_schedule_is_worth_more_than_a_fresh_one_and_the_numbers_make_sense():
    limits = opp.Limits(assumed_timing_err_min=3.0)
    stale = opp.impact_estimate(_row(1e-4), 1000, limits)
    fresh = opp.impact_estimate(_row(1e-8), 1000, limits)
    assert stale["level"] == "high" and fresh["level"] == "low"
    f = stale["figures"]
    assert f["sigma_now_min"] == pytest.approx(math.sqrt((1000 * 1e-4) ** 2 + 2 * 1000 * 1e-4 * 1e-4 + 1e-8) * 1440, rel=1e-6)
    assert f["sigma_after_min"] < 3.0 < f["sigma_now_min"]  # combining with a +/-3 min timing beats the +/-144 min prediction
    assert f["sigma_year_after_min"] < f["sigma_year_before_min"]  # a timing never makes the future schedule worse
    assert f["gain_pct"] == pytest.approx(100 * (1 - f["sigma_year_after_min"] / f["sigma_year_before_min"]))
    assert "Little gain" in fresh["headline"] and "+/-" in stale["headline"] and "tighter" in stale["headline"]


def test_a_big_percentage_on_an_already_fine_schedule_is_still_little_gain():
    # +/-0.5 min a year on is finer than one night's data (+/-3 min), however many percent a timing would shave off it
    est = opp.impact_estimate(_row(3e-7), 1000, opp.Limits(assumed_timing_err_min=3.0))
    f = est["figures"]
    assert f["sigma_year_before_min"] < 3.0 and f["gain_pct"] > 0
    assert est["level"] == "low" and "Little gain" in est["headline"] and "a year on" in est["headline"]


def test_the_middle_level_exists_between_little_and_high():
    est = opp.impact_estimate(_row(1e-4), 1000, opp.Limits(assumed_timing_err_min=120.0))
    assert est["level"] == "medium" and 15 <= est["figures"]["gain_pct"] < 50


def test_a_worse_telescope_is_worth_less_and_the_assumption_is_stated():
    good = opp.impact_estimate(_row(1e-4), 1000, opp.Limits(assumed_timing_err_min=1.0))
    poor = opp.impact_estimate(_row(1e-4), 1000, opp.Limits(assumed_timing_err_min=60.0))
    assert good["figures"]["gain_pct"] > poor["figures"]["gain_pct"]
    assert any("+/-60 min" in a and "not measured" in a for a in poor["assumptions"])
    assert any("discovery" in a for a in poor["assumptions"])


def test_a_period_with_no_stated_error_has_an_impact_that_cannot_be_estimated():
    est = opp.impact_estimate(_row(None), 1000, opp.Limits())
    assert est["level"] == "unknown" and est["figures"]["gain_pct"] is None and est["assumptions"]
    assert "cannot be estimated" in est["headline"]


def test_minutes_are_formatted_for_a_glance():
    assert [opp.fmt_minutes(x) for x in (0.34, 9.9, 12.4, 179.0, 200.0)] == ["0.3 min", "9.9 min", "12 min", "179 min", "3.3 h"]


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def test_the_report_lists_every_planet_the_network_named_and_passes_its_own_contract(report):
    assert check_report(report) is report
    names = {o["name"] for o in report["opportunities"]}
    assert names == {"Fresh-1 b", "Stale-2 b", "Noon-3 b", "South-4 b", "Faint-5 b", "Shallow-6 b", "Longp-7 b", "Nosig-8 b"}
    assert report["n_candidates"] == 8 and report["horizon_days"] == 30
    assert report["site"]["lat"] == pytest.approx(39.7392)


def test_reachable_planets_have_windows_that_are_dark_high_and_inside_the_horizon(report):
    ops = _by_name(report)
    for name in ("Fresh-1 b", "Stale-2 b", "Nosig-8 b"):
        op = ops[name]
        assert op["reachable"] and op["blockers"] == [] and op["windows"], name
        starts = [w["start"] for w in op["windows"]]
        assert starts == sorted(starts)
        for w in op["windows"]:
            start, mid, end = (datetime.fromisoformat(w[k]) for k in ("start", "mid", "end"))
            assert end >= NOW and start <= NOW + timedelta(days=30)
            assert end - start == timedelta(hours=3 + 2)  # transit + one hour of baseline each side
            lat, lon, elev = 39.7392, -104.9903, 1609.0
            ra, dec = 34.0, 40.0
            for t in (start, mid, end):
                assert astrometry.target_altitude_deg(ra, dec, lat, lon, elev, t) >= 30.0
                assert astrometry.sun_altitude_deg(lat, lon, elev, t) <= -12.0
    first = datetime.fromisoformat(ops["Fresh-1 b"]["windows"][0]["mid"])
    assert abs((first - NOW).total_seconds()) < 15 * 60  # the transit that is under way right now is listed, not dropped


def _moon_facts(ra, dec, when, lim):
    """Moon altitude, its distance from (ra, dec) and the separation it demands, by an independent astropy calculation.

    Illumination is taken from the Sun-Moon elongation, ``(1 - cos E) / 2``, a standard approximation that does not
    use the code under test.
    """
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
    from astropy.time import Time

    site = EarthLocation(lat=39.7392 * u.deg, lon=-104.9903 * u.deg, height=1609.0 * u.m)
    t = Time(when)
    frame = AltAz(obstime=t, location=site)
    moon = get_body("moon", t, site)
    illum = (1.0 - math.cos(math.radians(float(get_sun(t).separation(moon).deg)))) / 2.0
    star = SkyCoord(ra * u.deg, dec * u.deg).transform_to(frame)
    alt, sep = float(moon.transform_to(frame).alt.deg), float(star.separation(moon.transform_to(frame)).deg)
    return alt, sep, max(lim["moon_floor_deg"], lim["moon_avoid_deg"] * illum)


def test_a_window_is_not_offered_when_the_moon_is_up_and_too_close(report, moon_report):
    """Checked against an independent astropy calculation, not against the code's own helper."""
    ra, dec = 34.0, 40.0
    lim = moon_report["limits"]
    n_without = sum(len(o["windows"]) for o in report["opportunities"])
    n_with = sum(len(o["windows"]) for o in moon_report["opportunities"])
    assert 0 < n_with < n_without  # the Moon takes some windows away and leaves others

    def facts(w):
        return [_moon_facts(ra, dec, datetime.fromisoformat(w[k]), lim) for k in ("start", "mid", "end")]

    for op in moon_report["opportunities"]:
        for w in op["windows"]:  # every window offered has the Moon down or far enough away (a degree of slack for the grid)
            assert all(alt <= 0.0 or sep >= need - 1.0 for alt, sep, need in facts(w)), (op["name"], facts(w))
    kept = {(o["name"], w["start"]) for o in moon_report["opportunities"] for w in o["windows"]}
    dropped = [(o["name"], w) for o in report["opportunities"] for w in o["windows"] if (o["name"], w["start"]) not in kept]
    assert dropped
    for name, w in dropped:  # every window the Moon removed really had the Moon up and too close at some point
        assert any(alt > 0.0 and sep < need + 1.0 for alt, sep, need in facts(w)), (name, facts(w))


def test_a_planet_whose_every_window_is_spoiled_by_the_moon_says_so(moon_report):
    gone = [o for o in moon_report["opportunities"] if not o["reachable"] and any("Moon" in b for b in o["blockers"])]
    for op in gone:
        assert "too close to the Moon" in op["blockers"][0] and op["windows"] == []


def test_the_report_says_the_moon_is_checked_and_the_horizon_is_not(moon_report):
    text = " ".join(moon_report["caveats"])
    assert "Moon is checked" in text and "horizon" in text and "not checked" in text
    assert moon_report["limits"]["moon_avoid_deg"] == 60.0 and moon_report["limits"]["min_sun_sep_deg"] == 30.0


def test_an_unreachable_planet_is_listed_with_the_reason_never_hidden(report):
    ops = _by_name(report)
    reasons = {
        "South-4 b": "never climbs high enough",
        "Faint-5 b": "Too faint for a 50 mm telescope: V = 14.5",
        "Shallow-6 b": "too shallow",
        "Noon-3 b": "twilight or daylight",
        "Longp-7 b": "no transit in the next 30 days",
    }
    for name, fragment in reasons.items():
        op = ops[name]
        assert not op["reachable"] and op["windows"] == [] and op["impact"] is None, name
        assert fragment.lower() in " ".join(op["blockers"]).lower(), (name, op["blockers"])
    assert "None of its 30 transits" in ops["Noon-3 b"]["blockers"][0] or "None of its 31 transits" in ops["Noon-3 b"]["blockers"][0]


def test_reachable_come_first_then_best_ranked_and_each_says_why_it_was_chosen(report):
    ops = report["opportunities"]
    flags = [o["reachable"] for o in ops]
    assert flags == sorted(flags, reverse=True)  # reachable first, a stable order
    reachable = [o for o in ops if o["reachable"]]
    scores = [o["network"]["score"] for o in reachable]
    assert scores == sorted(scores, reverse=True)
    # no stated period error counts as the stalest schedule (the ranking caps it at 4x), then ALERT + stale, then fresh
    assert [o["name"] for o in reachable] == ["Nosig-8 b", "Stale-2 b", "Fresh-1 b"]
    net = _by_name({"opportunities": reachable})["Stale-2 b"]["network"]
    assert net["task"] == core_networks.TASK_EPHEMERIS and "ALERT" in net["reason"] and "exoclock" in net["sources"]


def test_impact_is_attached_to_reachable_planets_and_follows_staleness(report):
    ops = _by_name(report)
    assert ops["Stale-2 b"]["impact"]["level"] == "high"
    assert ops["Fresh-1 b"]["impact"]["level"] == "low"
    assert ops["Nosig-8 b"]["impact"]["level"] == "unknown"
    for op in report["opportunities"]:
        if op["reachable"]:
            assert op["impact"]["assumptions"] and op["impact"]["headline"]
            assert op["impact"]["figures"]["epoch"] == op["windows"][0]["epoch"]
    mid_err = ops["Stale-2 b"]["windows"][0]["mid_err_min"]
    assert 100 < mid_err < 200 and ops["Nosig-8 b"]["windows"][0]["mid_err_min"] is None


def test_the_report_says_what_it_does_not_check(report):
    text = " ".join(report["caveats"]).lower()
    for word in ("weather", "moon", "horizon", "estimates"):
        assert word in text


def test_reading_the_cache_never_touches_the_network_and_matches_the_download(live_settings, served, monkeypatch):
    fresh = opp.build_report(live_settings, now=NOW, days=30, refresh=True)
    assert served == [tn.EXOCLOCK_URL] or tn.EXOCLOCK_URL in served
    assert fresh["data"]["from_cache"] is False

    def boom(url, params, timeout):
        raise AssertionError("the network was touched")

    monkeypatch.setattr(core_networks, "_http_get_text", boom)
    cached = opp.build_report(live_settings, now=NOW, days=30, refresh=False)
    assert cached["data"]["from_cache"] is True and cached["data"]["retrieved_utc"]
    assert [o["id"] for o in cached["opportunities"]] == [o["id"] for o in fresh["opportunities"]]
    assert any("not downloaded yet" in p for p in cached["data"]["problems"])  # the lists we never fetched say so


def test_with_nothing_cached_the_report_is_empty_and_says_to_refresh(live_settings):
    empty = opp.build_report(live_settings, now=NOW, days=30, refresh=False)
    assert empty["opportunities"] == [] and empty["n_candidates"] == 0
    assert any("not downloaded yet" in p or "could be read" in p for p in empty["data"]["problems"])
    check_report(empty)


def test_a_failed_refresh_falls_back_to_the_old_copy_and_flags_it(live_settings, served, monkeypatch):
    opp.build_report(live_settings, now=NOW, days=30, refresh=True)

    def outage(url, params, timeout):
        raise NetworkError("offline")

    monkeypatch.setattr(core_networks, "_http_get_text", outage)
    again = opp.build_report(live_settings, now=NOW, days=30, refresh=True)
    assert again["data"]["stale_cache"] is True and again["opportunities"]


def test_offline_services_never_fetch_even_when_asked(settings, served):
    assert settings.offline_services
    out = opp.build_report(settings, now=NOW, days=30, refresh=True)
    assert served == [] and any("offline" in p.lower() for p in out["data"]["problems"])


def test_the_limits_are_the_missions_limits(live_settings, served, monkeypatch):
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MAX_VMAG", "10")
    out = opp.build_report(live_settings, now=NOW, days=30, refresh=True)
    assert out["limits"]["max_vmag"] == 10.0
    assert all(not o["reachable"] for o in out["opportunities"])  # nothing is brighter than V=10 here
    assert opp.Limits.from_env(live_settings).max_vmag == 10.0
    monkeypatch.setenv("ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN", "7")
    assert opp.Limits.from_env(live_settings).assumed_timing_err_min == 7.0


def test_the_transit_planner_keeps_the_missions_zenith_limit(settings, monkeypatch):
    limits = opp.Limits.from_env(settings)
    assert limits.max_alt_deg == settings.max_altitude_deg == 70.0
    assert limits.site_limits().max_altitude_deg == 70.0
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MAX_ALT_DEG", "55")
    assert opp.Limits.from_env(settings).max_alt_deg == 55.0
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MAX_ALT_DEG", "88")
    assert opp.Limits.from_env(settings).max_alt_deg == 70.0


def test_a_shorter_horizon_has_fewer_windows(live_settings, served):
    week = _by_name(opp.build_report(live_settings, now=NOW, days=7, refresh=True))
    month = _by_name(opp.build_report(live_settings, now=NOW, days=30, refresh=True))
    assert 0 < len(week["Fresh-1 b"]["windows"]) < len(month["Fresh-1 b"]["windows"])


# --------------------------------------------------------------------------- #
# The plugin hook and the plan record
# --------------------------------------------------------------------------- #
def test_only_plugins_with_a_network_list_offer_opportunities(live_settings):
    offered = {n: load_plugin(n, live_settings).opportunities(NOW, days=7) for n in available_plugins()}
    assert offered["asteroid_finder"] is None and offered["rubin_followup"] is None
    assert offered["transit_finder"] is not None and offered["transit_finder"]["plugin"] == "transit_finder"


def test_the_default_planned_target_is_the_base_target_plus_the_window():
    class Bare(BaseExperimentPlugin):
        name = "bare"

        def generate_targets(self): return []
        def get_exposure_plan(self): return {}
        def analyze_frames(self, fits_paths): return {}

    base = {"name": "X", "ra_deg": 1.0, "dec_deg": 2.0}
    out = Bare(None).planned_target({"target": base}, {"start": "a", "end": "b", "mid": "m"})  # type: ignore[arg-type]
    assert out == {**base, "window_start": "a", "window_end": "b"}


def test_a_planned_transit_target_carries_what_a_mission_target_does(live_settings, report):
    plugin = load_plugin("transit_finder", live_settings)
    op = _by_name(report)["Stale-2 b"]
    window = op["windows"][1]
    target = plugin.planned_target(op, window)
    assert target["window_start"] == window["start"] and target["window_end"] == window["end"]
    assert target["predicted_t_mid_iso"] == window["mid"] and target["epoch_number"] == window["epoch"]
    assert target["predicted_t_mid_err_min"] == window["mid_err_min"]
    assert {"name", "ra_deg", "dec_deg", "period_d", "duration_h", "network"} <= set(target)
    core_networks.check_network_block(target["name"], target["network"])
    json.dumps(target)  # storable


def test_planning_is_recorded_once_per_window_and_can_be_cancelled_and_revived(tmp_path, live_settings, report):
    plugin = load_plugin("transit_finder", live_settings)
    op = _by_name(report)["Stale-2 b"]
    db = Database(str(tmp_path / "x.sqlite"))
    try:
        t0 = plugin.planned_target(op, op["windows"][0])
        t1 = plugin.planned_target(op, op["windows"][1])
        row, created = db.plan_observation("transit_finder", t0, network=op["network"], impact=op["impact"], note="hi")
        assert created and row["status"] == "PLANNED" and row["window_start"] == op["windows"][0]["start"].replace("+00:00", "+00:00")
        assert row["target"]["name"] == "Stale-2 b" and row["network"]["task"] == core_networks.TASK_EPHEMERIS
        assert row["impact"]["level"] == "high" and row["mission_id"] is None  # nothing was started

        again, created_again = db.plan_observation("transit_finder", t0)
        assert not created_again and again["id"] == row["id"]
        other, _ = db.plan_observation("transit_finder", t1)
        planned = db.list_planned()
        assert [p["id"] for p in planned] == [row["id"], other["id"]]  # soonest window first

        assert db.cancel_planned(row["id"]) is True and db.cancel_planned(row["id"]) is False
        assert [p["id"] for p in db.list_planned()] == [other["id"]]
        assert {p["status"] for p in db.list_planned(status=None)} == {"PLANNED", "CANCELLED"}
        revived, created_revived = db.plan_observation("transit_finder", t0)
        assert created_revived and revived["id"] == row["id"] and revived["status"] == "PLANNED"
        assert db.cancel_planned(99999) is False
    finally:
        db.dispose()


# --------------------------------------------------------------------------- #
# The contract's validator
# --------------------------------------------------------------------------- #
def _good_op():
    net = network_block(["exoclock"], core_networks.TASK_EPHEMERIS, "because", score=1.0)
    return {
        "id": "x", "name": "X", "network": net, "reachable": True, "blockers": [], "facts": {}, "impact": None,
        "windows": [{"start": "2026-10-28T05:00:00+00:00", "mid": "2026-10-28T06:00:00+00:00", "end": "2026-10-28T07:00:00+00:00"}],
        "target": {"name": "X", "ra_deg": 1.0, "dec_deg": 2.0},
    }


@pytest.mark.parametrize("change, message", [
    ({"reachable": False}, "unreachable but says nothing|yet lists observable"),
    ({"reachable": "yes"}, "true or false"),
    ({"blockers": [""]}, "sentences"),
    ({"network": {"sources": []}}, "network"),
    ({"windows": [{"start": "x", "mid": "y", "end": "z"}]}, "ISO-8601"),
    ({"windows": [{"start": "2026-10-28T07:00:00+00:00", "mid": "2026-10-28T06:00:00+00:00", "end": "2026-10-28T08:00:00+00:00"}]}, "start <= mid <= end"),
    ({"impact": {"level": "huge", "headline": "h", "assumptions": ["a"]}}, "impact.level"),
    ({"impact": {"level": "high", "headline": "h", "assumptions": []}}, "assumptions"),
    ({"impact": {"level": "high", "headline": " ", "assumptions": ["a"]}}, "headline"),
    ({"target": {"name": "X"}}, "'target' needs"),
])
def test_a_malformed_opportunity_is_refused(change, message):
    import re

    assert check_opportunity(_good_op())
    with pytest.raises(OpportunityError) as err:
        check_opportunity({**_good_op(), **change})
    assert re.search(message, str(err.value)), str(err.value)


def test_windows_must_be_soonest_first_and_ids_unique(report):
    op = _good_op()
    later = {"start": "2026-10-29T05:00:00+00:00", "mid": "2026-10-29T06:00:00+00:00", "end": "2026-10-29T07:00:00+00:00"}
    with pytest.raises(OpportunityError, match="soonest first"):
        check_opportunity({**op, "windows": [later, op["windows"][0]]})
    bad = {**report, "opportunities": [report["opportunities"][0], report["opportunities"][0]]}
    with pytest.raises(OpportunityError, match="duplicate"):
        check_report(bad)
    with pytest.raises(OpportunityError, match="horizon_days"):
        check_report({**report, "horizon_days": 0})


# --------------------------------------------------------------------------- #
# The impact guide ("What is one timing worth?")
# --------------------------------------------------------------------------- #
def test_the_report_carries_a_valid_impact_guide(report):
    guide = report["impact_guide"]
    assert guide["title"] == "What is one timing worth?"
    assert any(s["technical"] for s in guide["sections"]) and any(not s["technical"] for s in guide["sections"])
    check_report(report)


def test_the_guide_is_honest_about_what_one_timing_is_and_is_not_worth(report):
    text = " ".join(s["heading"] + " " + s["body"] for s in report["impact_guide"]["sections"]).lower()
    for must_say in ("out of date", "not a discovery", "placeholder", "accepted", "years", "little gain"):
        assert must_say in text or must_say.replace("out of date", "off by") in text, must_say
    assert "discover" not in report["impact_guide"]["summary"].lower()  # the summary makes no discovery claim
    plain = " ".join(s["body"] for s in report["impact_guide"]["sections"] if not s["technical"])
    assert "sigma" not in plain and "inverse variance" not in plain  # jargon stays in the Technical sections


def test_the_guide_quotes_the_assumed_timing_error_that_is_actually_used(live_settings):
    limits = dataclasses.replace(opp.Limits(), assumed_timing_err_min=7.0)
    body = " ".join(s["body"] for s in opp.impact_guide(limits)["sections"])
    assert "7 minutes" in body and "7 min" in body


@pytest.mark.parametrize("guide, message", [
    ("text", "must be a dict"),
    ({"title": "t", "summary": "", "sections": [{"heading": "h", "body": "b"}]}, "'summary'"),
    ({"title": "t", "summary": "s", "sections": []}, "'sections'"),
    ({"title": "t", "summary": "s", "sections": [{"heading": "h", "body": " "}]}, "'heading' and 'body'"),
    ({"title": "t", "summary": "s", "sections": [{"heading": "h", "body": "b", "technical": "yes"}]}, "true or false"),
])
def test_a_malformed_impact_guide_is_refused(report, guide, message):
    with pytest.raises(OpportunityError, match=message):
        check_report({**report, "impact_guide": guide})


def test_a_report_without_a_guide_is_still_valid(report):
    without = {k: v for k, v in report.items() if k != "impact_guide"}
    assert check_report(without)


# --------------------------------------------------------------------------- #
# Is the schedule out of date? A plain verdict, the date it is counted from, and an honest warning when a list is down
# --------------------------------------------------------------------------- #
def _sched_row(staleness, sigma=30.0, *, jd=2457142.0194, dur=2.5):
    return {"t0_bjd_tdb": jd, "staleness": staleness, "sigma_min": sigma, "duration_h": dur}


LIMITS = opp.Limits(assumed_timing_err_min=3.0)


@pytest.mark.parametrize("ratio, sigma, verdict", [
    (0.01, 0.5, "up_to_date"), (0.03, 3.0, "up_to_date"),            # known as well as one night's timing could
    (0.04, 3.1, "getting_stale"), (0.2, 15.0, "getting_stale"),      # drifting, but the transit can still be found
    (0.9999, 74.0, "getting_stale"), (1.0, 75.0, "out_of_date"),     # half the transit or more: it could be missed
    (5.1, 382.0, "out_of_date"), (math.inf, math.inf, "unknown"), (None, None, "unknown"),
])
def test_the_verdict_compares_the_uncertainty_with_one_timing_and_with_half_the_transit(ratio, sigma, verdict):
    got = opp.schedule_status(_sched_row(ratio, sigma), NOW, LIMITS)
    assert got["verdict"] == verdict
    assert got["headline"][0].isupper() and not got["headline"].startswith(("Out of date", "Up to date"))  # the page adds the label


def test_the_date_is_the_one_the_schedule_is_counted_from_and_says_so_without_claiming_a_last_observation():
    got = opp.schedule_status(_sched_row(5.1, 382.0), NOW, LIMITS)
    assert got["reference_utc"].startswith("2015-04-29")  # JD 2457142.0194
    assert "29 Apr 2015" in got["headline"] and "about 11 years ago" in got["headline"]
    assert "counted from a transit" in got["headline"] and "last observed" not in got["headline"].lower()
    assert "+/-6.4 h" in got["headline"] and "more than half the 2.5 h transit" in got["headline"]
    caveats = " ".join(opp.caveats(opp.Limits())).lower()
    assert "last observed" in caveats and "counted from" in caveats


def test_how_long_ago_reads_naturally():
    jd = lambda dt: dt.timestamp() / 86400.0 + 2440587.5  # noqa: E731
    assert "less than two months ago" in opp.schedule_status(_sched_row(0.1, jd=jd(NOW - timedelta(days=20))), NOW, LIMITS)["headline"]
    assert "about 6 months ago" in opp.schedule_status(_sched_row(0.1, jd=jd(NOW - timedelta(days=185))), NOW, LIMITS)["headline"]
    assert "about 3 years ago" in opp.schedule_status(_sched_row(0.1, jd=jd(NOW - timedelta(days=1100))), NOW, LIMITS)["headline"]


def test_a_schedule_with_no_stated_error_cannot_be_checked():
    got = opp.schedule_status(_sched_row(math.inf, math.inf), NOW, LIMITS)
    assert got["verdict"] == "unknown" and "no stated error" in got["headline"] and got["uncertainty_min"] is None


def test_every_opportunity_carries_a_verdict_and_the_planets_come_out_as_expected(report):
    ops = _by_name(report)
    assert all(o["schedule"]["verdict"] in ("up_to_date", "getting_stale", "out_of_date", "unknown") for o in ops.values())
    assert ops["Fresh-1 b"]["schedule"]["verdict"] == "up_to_date"
    assert ops["Stale-2 b"]["schedule"]["verdict"] == "out_of_date"   # +/- ~144 min against a 3 h transit
    assert ops["Nosig-8 b"]["schedule"]["verdict"] == "unknown"
    check_report(report)


def test_with_exoclock_loaded_nothing_is_provisional_and_only_the_missing_watch_list_is_flagged(report):
    assert not any(o["schedule"]["provisional"] for o in report["opportunities"])
    notice = report["data"].get("notice", "")
    assert "Exoplanet Watch" in notice and "ExoClock's list is not available" not in notice


WATCH_HEAD = "name,RA,Dec,vmag,epoch,epoch_uncertainty,period,period_uncertainty,duration,comments,depth,priority"


def _watch_csv():
    def line(name, period_unc, rank):
        return f'"{name}",{MIDNIGHT_RA},{HIGH_DEC},11.5,{JD_NOW - 1000.0},0.0001,1.0,{period_unc},3.0,,12.0,{rank}'
    return "\n".join([WATCH_HEAD, line("Stale-2 b", "0.0001", 1), line("Fresh-1 b", "0.00000001", 2)])


@pytest.fixture()
def exoclock_down(monkeypatch):
    """ExoClock fails (as its server did: a broken TLS reply) and only Exoplanet Watch answers."""
    def get(url, params, timeout):
        if url == tn.EXOPLANET_WATCH_URL:
            return _watch_csv()
        raise NetworkError("SSL: WRONG_VERSION_NUMBER" if url == tn.EXOCLOCK_URL else "not served")

    monkeypatch.setattr(core_networks, "_http_get_text", get)


def test_when_exoclock_is_down_the_report_says_so_in_a_notice_on_every_estimate_and_on_the_verdict(live_settings, exoclock_down):
    rep = opp.build_report(live_settings, now=NOW, days=30, refresh=True)
    check_report(rep)
    assert "ExoClock's list is not available" in rep["data"]["notice"]
    assert "provisional" in rep["data"]["notice"] and "Exoplanet Watch" in rep["data"]["notice"]
    ops = _by_name(rep)
    stale, fresh = ops["Stale-2 b"], ops["Fresh-1 b"]
    assert stale["schedule"]["verdict"] == "out_of_date" and stale["schedule"]["provisional"] is True
    assert "may overstate how out of date it is" in stale["schedule"]["headline"]
    assert fresh["schedule"]["verdict"] == "up_to_date" and fresh["schedule"]["provisional"] is False  # fresh stays fresh
    for op in (stale, fresh):
        assert any("ExoClock's list was not available" in a for a in op["impact"]["assumptions"])


def test_the_notice_only_appears_when_a_list_is_missing(live_settings, monkeypatch):
    def get(url, params, timeout):
        if url == tn.EXOCLOCK_URL:
            return json.dumps(EXOCLOCK)
        if url == tn.EXOPLANET_WATCH_URL:
            return _watch_csv()
        raise NetworkError("not served")

    monkeypatch.setattr(core_networks, "_http_get_text", get)
    rep = opp.build_report(live_settings, now=NOW, days=30, refresh=True)
    assert "notice" not in rep["data"]
    assert not any(o["schedule"]["provisional"] for o in rep["opportunities"])
    assert not any("was not available" in a for o in rep["opportunities"] if o["impact"] for a in o["impact"]["assumptions"])


def test_the_guide_explains_the_verdict(report):
    text = " ".join(s["body"] for s in report["impact_guide"]["sections"]).lower()
    assert "out of date" in text and "getting stale" in text and "last observed" in text


@pytest.mark.parametrize("schedule, message", [
    ({"verdict": "ancient", "headline": "h"}, "schedule.verdict"),
    ({"verdict": "out_of_date", "headline": " "}, "schedule.headline"),
    ({"verdict": "out_of_date", "headline": "h", "reference_utc": "long ago"}, "ISO-8601"),
    ("out_of_date", "schedule.verdict"),
])
def test_a_malformed_schedule_is_refused(schedule, message):
    import re

    assert check_opportunity({**_good_op(), "schedule": {"verdict": "up_to_date", "headline": "h", "reference_utc": None}})
    with pytest.raises(OpportunityError) as err:
        check_opportunity({**_good_op(), "schedule": schedule})
    assert re.search(message, str(err.value)), str(err.value)


def test_a_blank_notice_is_refused(report):
    with pytest.raises(OpportunityError, match="notice"):
        check_report({**report, "data": {**report["data"], "notice": "  "}})


def test_up_to_date_and_little_gain_go_together_and_a_stale_schedule_is_never_little_gain(report):
    """The verdict is about the schedule now, the level about a year on; they must not tell opposite stories."""
    for op in report["opportunities"]:
        if not op["reachable"]:
            continue
        verdict, level = op["schedule"]["verdict"], op["impact"]["level"]
        if verdict == "out_of_date":
            assert level in ("high", "medium")  # a schedule that could miss the transit is worth a timing
        if level == "high":
            assert verdict != "up_to_date" or op["schedule"]["uncertainty_min"] <= LIMITS.assumed_timing_err_min
