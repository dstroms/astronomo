"""Opportunity helpers, held reports, first-light filters and forecast labels.

Windows in these tests are relative to a frozen clock, so they do not depend on the date or on astropy;
the report logic itself is tested in ``test_opportunities.py``.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest

from core import networks as core_networks
from core.networks import NetworkError, network_block
from core.opportunities import check_report
from dashboard import opportunities as view
from plugins.transit_finder import networks as tn

TZ = timezone(timedelta(hours=-6))  # Denver in October
NOW = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)  # 12:00 local on Wednesday the 28th: tonight is the night of the 28th


def _window(hours_from_now: float, *, err=5.0, base=NOW):
    mid = base + timedelta(hours=hours_from_now)
    return {
        "start": (mid - timedelta(hours=2.5)).isoformat(), "mid": mid.isoformat(), "end": (mid + timedelta(hours=2.5)).isoformat(),
        "epoch": 1000 + int(hours_from_now), "mid_err_min": err,
    }


def _op(name, *, task=core_networks.TASK_EPHEMERIS, score=3.0, level="high", windows=(), blockers=(), base=NOW):
    reachable = not blockers
    net = network_block(["exoclock"], task, f"{name} is overdue", score=score, terms={"staleness": 2.0})
    impact = None
    if reachable:
        impact = {
            "level": level, "headline": f"One timing of {name} would help.", "assumptions": ["Assumes +/-3 min."],
            "figures": {"gain_pct": 60.0, "sigma_now_min": 100.0},
        }
    return {
        "id": name.lower().replace(" ", ""), "name": name, "host": None, "network": net, "reachable": reachable,
        "blockers": list(blockers), "windows": [_window(h, base=base) for h in windows] if reachable else [],
        "impact": impact, "target": {"name": name, "ra_deg": 10.0, "dec_deg": 40.0, "period_d": 1.0, "duration_h": 3.0, "network": net},
        "facts": {"v_mag": 11.0, "depth_pct": 1.2, "duration_h": 3.0, "period_d": 1.0},
    }


def _report(ops, base=NOW, **data):
    return {
        "plugin": "transit_finder", "label": "Exoplanet transit timing", "generated_utc": base.isoformat(), "horizon_days": 30,
        "site": {"name": "Home observatory", "lat": 39.7, "lon": -105.0, "elev_m": 1609.0},
        "limits": {"min_alt_deg": 30.0, "baseline_min": 60.0, "max_vmag": 13.0, "min_depth_pct": 0.5, "max_sun_alt_deg": -12.0,
                   "assumed_timing_err_min": 3.0},
        "data": {"retrieved_utc": (base - timedelta(hours=3)).isoformat(), "stale_cache": False, "from_cache": True,
                 "problems": [], **data},
        "n_candidates": len(ops), "caveats": ["Weather is not checked.", "The Moon is not checked."], "opportunities": ops,
    }


def _three(base=NOW):
    return [
        _op("Alpha b", score=5.0, windows=(8, 32, 56), base=base),                                      # tonight, then 2 more nights
        _op("Beta b", score=4.0, task=core_networks.TASK_TTV, level="medium", windows=(9.5, 80), base=base),
        _op("Gamma b", score=3.0, level="low", windows=(200,), base=base),                              # 8 days out
        _op("Delta b", score=2.0, blockers=("Too faint for a 50 mm telescope: V = 14.5, and the limit is 13.",), base=base),
    ]


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def test_a_morning_after_belongs_to_the_night_before():
    assert view.night_of(datetime(2026, 10, 29, 7, 0, tzinfo=timezone.utc), TZ) == date(2026, 10, 28)  # 01:00 local on the 29th
    assert view.night_of(datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc), TZ) == date(2026, 10, 28)  # 20:00 local on the 28th
    assert view.night_of(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc), TZ) == date(2026, 10, 29)  # 14:00 local: the coming night
    assert view.tonight(NOW, TZ) == date(2026, 10, 28)


def test_tonight_flips_at_dawn_not_at_noon():
    local = lambda h, m=0, d=28: datetime(2026, 10, d, h, m, tzinfo=TZ)  # noqa: E731
    assert view.tonight(local(3), TZ) == date(2026, 10, 27)  # 03:00: the night that began on the 27th is still going
    assert view.tonight(local(6, 30), TZ) == date(2026, 10, 27)
    assert view.tonight(local(8, 16), TZ) == date(2026, 10, 28)  # 08:16: that night is over; tonight is the coming one
    assert view.tonight(local(23), TZ) == date(2026, 10, 28)
    assert view.night_of(local(3), TZ) == date(2026, 10, 27)  # a window at 03:00 is filed under the evening before


def test_times_are_local_and_the_technical_view_adds_utc():
    assert view.fmt_when("2026-10-29T07:15:00+00:00", TZ) == "Thu 29 Oct, 1:15 AM UTC-06:00"
    assert view.fmt_when("2026-10-29T07:15:00+00:00", TZ, simple=False) == "Thu 29 Oct, 1:15 AM UTC-06:00 (7:15 AM UTC)"
    assert view.fmt_when("2026-10-29T07:15:00+00:00", TZ, hour12=False) == "Thu 29 Oct, 01:15 UTC-06:00"
    assert view.fmt_when("2026-10-29T07:15:00+00:00", TZ, simple=False, hour12=False) == "Thu 29 Oct, 01:15 UTC-06:00 (07:15 UTC)"
    assert view.fmt_clock(datetime(2026, 10, 29, 0, 0, tzinfo=TZ)) == "12:00 AM"  # midnight
    assert view.fmt_clock(datetime(2026, 10, 28, 12, 0, tzinfo=TZ)) == "12:00 PM"  # noon
    assert view.fmt_clock(datetime(2026, 10, 28, 21, 5, tzinfo=TZ)) == "9:05 PM"
    assert view.when_phrase(_window(0), NOW) == "under way now"
    assert view.when_phrase(_window(8), NOW) == "in 5 h 30 m"
    assert view.when_phrase(_window(50), NOW) == "in 47 h 30 m"  # the span helper switches to days beyond two
    assert view.when_phrase(_window(80), NOW) == "in 3 d 5 h"


def test_a_window_shows_the_event_apart_from_the_longer_mission():
    window = _window(8)  # mid at 20:00 local; the stored window is 2.5 h either side, the dip is 3.0 h
    timing = view.timing_for_window(window, duration_h=3.0)
    assert timing["mission_hours"] == 5.0
    lines = view.timing_lines(timing, TZ)
    assert lines == [
        "Event: Wed 28 Oct, 6:30 PM UTC-06:00 to 9:30 PM UTC-06:00",
        "Proposed mission: Wed 28 Oct, 5:30 PM UTC-06:00 to 10:30 PM UTC-06:00 (5.0 h)",
    ]
    assert view.timing_lines(timing, TZ, hour12=False) == [
        "Event: Wed 28 Oct, 18:30 UTC-06:00 to 21:30 UTC-06:00",
        "Proposed mission: Wed 28 Oct, 17:30 UTC-06:00 to 22:30 UTC-06:00 (5.0 h)",
    ]
    technical = view.timing_lines(timing, TZ, simple=False)
    assert "6:30 PM UTC-06:00 (12:30 AM UTC)" in technical[0] and "9:30 PM UTC-06:00 (3:30 AM UTC)" in technical[0]
    assert "(5.0 h)" in technical[1]

    overnight = view.window_timing("2026-10-29T05:00:00+00:00", "2026-10-29T07:30:00+00:00")
    assert view.timing_lines(overnight, TZ) == [
        "Proposed mission: Wed 28 Oct, 11:00 PM UTC-06:00 to Thu 29 Oct, 1:30 AM UTC-06:00 (2.5 h)",
        "No separate event: this window is the time the mission would run.",
    ]
    night = view.window_timing("2026-10-29T02:00:00+00:00", "2026-10-29T10:00:00+00:00", mid=window["mid"], duration_h=3.0, night=True)
    assert night["event_start"] is None and "usable night" in view.timing_lines(night, TZ)[-1]
    assert view.timing_lines(night, TZ, noted=True) == [
        "Proposed mission: Wed 28 Oct, 8:00 PM UTC-06:00 to Thu 29 Oct, 4:00 AM UTC-06:00 (8.0 h)"
    ]

    plan = {
        "window_start": window["start"], "window_end": window["end"],
        "target": {"duration_h": 3.0, "predicted_t_mid_iso": window["mid"], "window_kind": "night"},
    }
    assert view.timing_for_plan(plan)["event_start"] is None  # a night is not given an event, even if a duration was stored
    plan["target"]["window_kind"] = None
    assert view.timing_for_plan(plan)["event_start"] == timing["event_start"]


def test_a_time_less_certain_than_the_baseline_is_flagged_and_so_is_an_unknown_one():
    assert view.is_uncertain({"mid_err_min": 90.0}, 60.0) and view.is_uncertain({"mid_err_min": None}, 60.0)
    assert not view.is_uncertain({"mid_err_min": 5.0}, 60.0) and not view.is_uncertain({"mid_err_min": 90.0}, None)
    assert "(time uncertain)" in view.window_label(_window(8, err=90.0), TZ, NOW, baseline_min=60.0)
    assert "(time uncertain)" not in view.window_label(_window(8, err=5.0), TZ, NOW, baseline_min=60.0)
    assert "+/-5 min" in view.window_label(_window(8, err=5.0), TZ, NOW, simple=False)


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def test_filtering_keeps_reachable_best_first_and_cuts_windows_to_the_horizon():
    ops = _three()
    out = view.filter_opportunities(ops, now=NOW, tz=TZ, days=30)
    assert [o["name"] for o in out] == ["Alpha b", "Beta b", "Gamma b"]  # Delta is unreachable
    week = view.filter_opportunities(ops, now=NOW, tz=TZ, days=7)
    assert [o["name"] for o in week] == ["Alpha b", "Beta b"]  # Gamma's only window is 8 days out
    assert [len(o["windows"]) for o in week] == [3, 2]
    assert len(ops[0]["windows"]) == 3  # the report itself is not modified


def test_windows_that_have_already_ended_are_dropped():
    op = _op("Old b", windows=(-10, 4))
    assert [w["mid"] for w in view.windows_within(op, NOW, 30)] == [op["windows"][1]["mid"]]


def test_filtering_by_what_the_network_wants_and_by_tonight():
    ops = _three()
    ttv = view.filter_opportunities(ops, now=NOW, tz=TZ, tasks={core_networks.TASK_TTV})
    assert [o["name"] for o in ttv] == ["Beta b"]
    assert view.filter_opportunities(ops, now=NOW, tz=TZ, tasks=set()) == []  # nothing selected means nothing shown
    assert len(view.filter_opportunities(ops, now=NOW, tz=TZ, tasks=None)) == 3
    tonight = view.filter_opportunities(ops, now=NOW, tz=TZ, tonight_only=True)
    assert [(o["name"], len(o["windows"])) for o in tonight] == [("Alpha b", 1), ("Beta b", 1)]  # 20:00 and 21:30 local tonight


def test_sorting_by_network_priority_soonest_or_biggest_gain():
    ops = [
        _op("Slow b", score=9.0, level="low", windows=(5,)),
        _op("Soon b", score=1.0, level="medium", windows=(2,)),
        _op("Late b", score=5.0, level="high", windows=(90,)),
        _op("Odd b", score=3.0, level="unknown", windows=(40,)),
    ]
    ops[2]["impact"]["figures"]["gain_pct"] = 80.0
    ops[1]["impact"]["figures"]["gain_pct"] = 30.0
    names = lambda how: [o["name"] for o in view.sort_opportunities(ops, how)]  # noqa: E731
    assert names("priority") == ["Slow b", "Soon b", "Late b", "Odd b"]  # as the report ranked them
    assert names("soonest") == ["Soon b", "Slow b", "Odd b", "Late b"]
    assert names("gain") == ["Late b", "Odd b", "Soon b", "Slow b"]  # high, then "no stated error", then medium, then little
    assert names("anything else") == names("priority")
    assert [o["name"] for o in ops] == ["Slow b", "Soon b", "Late b", "Odd b"]  # the input is untouched


def test_a_star_overhead_at_mid_window_is_highest_then_and_stays_above_70():
    import math

    mid = NOW + timedelta(hours=8)
    lat, lon = 40.0, -105.0
    op = _op("Zenith b", windows=(8,))
    op["target"]["ra_deg"] = view._lst_deg(mid.timestamp(), lon)  # transits at the middle of the window
    op["target"]["dec_deg"] = lat
    sky = view.window_altitude(op, op["windows"][0], {"lat": lat, "lon": lon})
    assert sky["peak_alt_deg"] == pytest.approx(90.0, abs=0.15)
    assert abs((sky["peak_at"] - mid).total_seconds()) < 90
    assert sky["peaks_at_start"] is False and sky["peaks_at_end"] is False
    cos_h = (math.sin(math.radians(70)) - math.sin(math.radians(40)) ** 2) / math.cos(math.radians(40)) ** 2
    half_h = math.degrees(math.acos(cos_h)) / (360.98564736629 / 24.0)
    assert sky["hours_above"] == pytest.approx(2 * half_h, abs=0.05)
    assert view.altitude_line(sky, TZ) == (
        f"Climbs to 90 deg at {view.fmt_clock(sky['peak_at'].astimezone(TZ))} UTC-06:00 and stays above 70 deg for {sky['hours_above']:.1f} h."
    )
    technical = view.altitude_line(sky, TZ, simple=False)
    assert "airmass 1.00" in technical and "90.0 deg" in technical


def test_the_curve_runs_from_now_up_to_the_highest_point_when_the_window_is_soon():
    mid = NOW + timedelta(hours=8)
    lat, lon = 40.0, -105.0
    op = _op("Zenith b", windows=(8,))
    op["target"]["ra_deg"] = view._lst_deg(mid.timestamp(), lon)
    op["target"]["dec_deg"] = lat
    track = view.altitude_track(op, op["windows"][0], {"lat": lat, "lon": lon}, NOW)
    assert track["now_on_curve"] and track["rising"]
    assert abs((track["samples"][0]["at"] - NOW).total_seconds()) < 1
    alts = [s["alt_deg"] for s in track["samples"]]
    assert max(alts) == pytest.approx(90.0, abs=0.2)
    assert alts[0] < alts[len(alts) // 2] < max(alts)
    assert view.altitude_now_line(track) == f"Now {track['now_alt_deg']:.0f} deg and climbing."
    names = [trace.name for trace in view.altitude_figure(track, TZ).data]
    assert names == ["Altitude", "Highest", "Now"]


def test_a_window_far_from_now_says_so_and_leaves_now_off_the_curve():
    later = _op("Later b", windows=(40,))
    site = {"lat": 40.0, "lon": -105.0}
    track = view.altitude_track(later, later["windows"][0], site, NOW)
    assert not track["now_on_curve"]
    assert track["samples"][0]["at"] == view.parse_time(later["windows"][0]["start"])
    assert view.altitude_now_line(track).endswith("before this window.")
    assert "Now" not in [trace.name for trace in view.altitude_figure(track, TZ).data]

    past = _op("Past b", windows=(-10,))
    ended = view.altitude_track(past, past["windows"][0], site, NOW)
    assert not ended["now_on_curve"]
    assert view.altitude_now_line(ended).endswith("This window has ended.")


def test_a_star_already_past_transit_is_highest_at_the_start():
    start = view.parse_time(_window(8)["start"])
    lat, lon = 40.0, -105.0
    op = _op("Setting b", windows=(8,))
    op["target"]["ra_deg"] = view._lst_deg((start - timedelta(hours=3)).timestamp(), lon)
    op["target"]["dec_deg"] = lat
    sky = view.window_altitude(op, op["windows"][0], {"lat": lat, "lon": lon})
    assert sky["peaks_at_start"] is True and sky["peak_alt_deg"] < 70 and sky["hours_above"] == 0
    line = view.altitude_line(sky, TZ)
    assert line.startswith("Highest at the start of the window") and "does not reach 70 deg" in line


def test_easiest_ranks_bright_and_high_ahead_of_faint_low_or_an_outburst_maximum():
    mid = NOW + timedelta(hours=8)
    lat, lon = 40.0, -105.0
    ra = view._lst_deg(mid.timestamp(), lon)
    site = {"lat": lat, "lon": lon}

    def placed(name, v, dec, **facts):
        op = _op(name, windows=(8,))
        op["target"]["ra_deg"] = ra
        op["target"]["dec_deg"] = dec
        op["facts"] = {"v_mag": v, **facts}
        return op

    steady = placed("Steady b", 8.0, lat)
    faint = placed("Faint b", 12.5, lat)
    nova = placed("Nova b", 0.2, lat, faintest_mag=14.0, only_when_bright=True)
    low = placed("Low b", 7.0, lat - 55)  # culminates near 35 deg
    saturating = placed("Bright b", 6.5, lat, faintest_mag=8.0, saturates_when_bright=True)
    given = [faint, nova, low, saturating, steady]
    order = [o["name"] for o in view.sort_opportunities(given, "easiest", site=site)]
    assert order == ["Steady b", "Bright b", "Low b", "Faint b", "Nova b"]
    assert [o["name"] for o in given] == ["Faint b", "Nova b", "Low b", "Bright b", "Steady b"]  # the input is untouched
    assert [o["name"] for o in view.sort_opportunities([faint, steady], "easiest")] == ["Faint b", "Steady b"]  # no site, no ranking


def test_the_card_altitude_matches_the_variable_star_night_grid():
    import numpy as np

    from plugins.variable_stars.opportunities import altaz_deg

    when = NOW
    ra, dec, lat, lon = 30.0, 45.0, 39.7, -105.0
    alt, _az = altaz_deg(np.array([ra]), np.array([dec]), lat, lon, np.array([when.timestamp()]))
    assert view._altitude_at(ra, dec, lat, lon, when) == pytest.approx(float(alt[0][0]), abs=0.02)


def test_the_table_carries_the_highest_point_when_the_site_is_known():
    ops = view.filter_opportunities(_three(), now=NOW, tz=TZ)
    rows = view.opportunity_rows(ops, now=NOW, tz=TZ, simple=True, site={"lat": 39.7, "lon": -105.0})
    assert "Highest (deg)" in rows[0] and "Above 70 deg (h)" in rows[0]
    assert "Highest (deg)" not in view.opportunity_rows(ops, now=NOW, tz=TZ, simple=True)[0]


def test_the_table_rows_differ_between_simple_and_technical():
    ops = view.filter_opportunities(_three(), now=NOW, tz=TZ)
    simple = view.opportunity_rows(ops, now=NOW, tz=TZ, simple=True, baseline_min=60.0)
    tech = view.opportunity_rows(ops, now=NOW, tz=TZ, simple=False, baseline_min=60.0)
    assert simple[0]["What the network wants"] == "Keep its schedule fresh" and tech[0]["What the network wants"] == "ephemeris refinement"
    assert simple[0]["Value"] == "High value" and tech[0]["Value"] == "high"
    assert simple[0]["Chances"] == 3 and "Rank score" not in simple[0] and tech[0]["Rank score"] == 5.0
    assert tech[0]["Lists"] == "exoclock" and tech[0]["Gain (%)"] == 60.0
    no = view.unreachable_rows(_three(), simple=True)
    assert [r["Planet"] for r in no] == ["Delta b"] and "Too faint" in no[0]["Why not"]


def test_the_calendar_is_monday_first_counts_nights_and_caps_each_day():
    ops = view.filter_opportunities(_three(), now=NOW, tz=TZ, days=30)
    weeks = view.calendar_weeks(ops, now=NOW, days=10, tz=TZ, per_day=1, baseline_min=60.0)
    assert all(len(w) == 7 for w in weeks)
    assert weeks[0][0]["date"] == date(2026, 10, 26) and not weeks[0][0]["in_range"]  # Monday before Wednesday the 28th
    days = [d for w in weeks for d in w]
    in_range = [d for d in days if d["in_range"]]
    assert [d["date"] for d in in_range] == [date(2026, 10, 28) + timedelta(days=i) for i in range(10)]
    assert in_range[0]["is_tonight"] and sum(d["is_tonight"] for d in days) == 1

    first = in_range[0]  # Alpha at 20:00 and Beta at 21:30 local tonight; the cap keeps the better-ranked one
    assert [e["name"] for e in first["entries"]] == ["Alpha b"] and first["more"] == 1
    assert [e["name"] for e in first["all_entries"]] == ["Alpha b", "Beta b"]
    assert view.calendar_entry_line(first["entries"][0]) == "8:00 PM UTC-06:00 Alpha b"
    assert view.night_target_list(first["all_entries"]) == "8:00 PM UTC-06:00 Alpha b\n9:30 PM UTC-06:00 Beta b"
    # Rank order is not time order, and a time after midnight is later in the night, not earlier in the day.
    crossed = [
        {"time": "12:51 AM MST", "name": "Late b", "uncertain": False, "sky": None, "mid": datetime(2026, 11, 18, 7, 51, tzinfo=timezone.utc)},
        {"time": "9:55 PM MST", "name": "Early b", "uncertain": False, "sky": None, "mid": datetime(2026, 11, 18, 4, 55, tzinfo=timezone.utc)},
        {"time": "10:10 PM MST", "name": "Mid b", "uncertain": False, "sky": None, "mid": datetime(2026, 11, 18, 5, 10, tzinfo=timezone.utc)},
    ]
    assert view.night_target_list(crossed) == "9:55 PM MST Early b\n10:10 PM MST Mid b\n12:51 AM MST Late b"
    assert view.calendar_entry_line(
        {"time": "9:30 PM UTC-06:00", "name": "Beta b", "uncertain": True, "sky": "cloudy"}
    ) == "9:30 PM UTC-06:00 Beta b (!) - cloudy"
    assert first["entries"][0]["time"] == "8:00 PM UTC-06:00" and not first["entries"][0]["uncertain"]
    assert view.calendar_weeks(ops, now=NOW, days=1, tz=TZ, hour12=False)[0][2]["entries"][0]["time"] == "20:00 UTC-06:00"
    by_date = {d["date"]: d for d in in_range}
    assert [e["name"] for e in by_date[date(2026, 11, 5)]["entries"]] == ["Gamma b"]  # 200 h out: 20:00 local on the 5th
    assert [e["name"] for e in by_date[date(2026, 10, 30)]["entries"]] == ["Alpha b"] and by_date[date(2026, 10, 30)]["more"] == 0
    assert by_date[date(2026, 10, 30)]["all_entries"] == by_date[date(2026, 10, 30)]["entries"]
    assert [e["name"] for e in by_date[date(2026, 10, 31)]["entries"]] == ["Beta b"]  # +80 h: 20:00 local on the 31st
    assert by_date[date(2026, 11, 1)]["entries"] == [] and by_date[date(2026, 11, 1)]["more"] == 0

    short = view.calendar_weeks(ops, now=NOW, days=3, tz=TZ)
    assert sum(d["in_range"] for w in short for d in w) == 3
    uncertain = view.calendar_weeks(ops, now=NOW, days=1, tz=TZ, baseline_min=1.0)  # every +/-5 min time is wider than a 1 min baseline
    assert all(e["uncertain"] for w in uncertain for d in w for e in d["entries"])


def test_planned_windows_are_recognised_despite_sub_second_noise():
    planned = [{"id": 7, "target_name": "Alpha b", "window_start": "2026-10-28T20:30:00.123456+00:00"}]
    keys = view.planned_keys(planned)
    window = {"start": "2026-10-28T20:30:00+00:00"}
    assert view.planned_id(keys, "Alpha b", window) == 7 and view.planned_id(keys, "Beta b", window) is None


def test_a_finished_attempt_does_not_block_planning_the_window_again():
    start = "2026-10-28T20:30:00+00:00"
    planned = [
        {"id": 7, "target_name": "Alpha b", "window_start": start, "status": "MISSED", "mission_id": "m_old"},
        {"id": 8, "target_name": "Alpha b", "window_start": start, "status": "DONE", "mission_id": "m_x"},
        {"id": 9, "target_name": "Beta b", "window_start": start, "status": "STARTED"},
        {"id": 4, "target_name": "Alpha b", "window_start": start, "status": "PLANNED"},
    ]
    keys = view.planned_keys(planned)
    window = {"start": start}
    assert view.planned_id(keys, "Alpha b", window) == 4  # the open note, not the finished attempts
    assert view.planned_id(keys, "Beta b", window) == 9
    assert view.earlier_attempt_line(planned[:2]) == "Earlier: done, mission m_x (2 attempts)."
    now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    assert view.closed_window_sentence({"window_end": "2026-11-16T18:00:00+00:00"}, now) is None
    assert "closed at 2026-11-16 06:00 UTC" in view.closed_window_sentence({"window_end": "2026-11-16T06:00:00+00:00"}, now)


def test_source_names_are_readable_with_a_fallback():
    assert view.source_names(["exoclock", "exoplanet_watch", "some_new_list"]) == "ExoClock, NASA Exoplanet Watch, Some New List"


# --------------------------------------------------------------------------- #
# Collecting reports
# --------------------------------------------------------------------------- #
HAVE_LISTS = {"label": "x", "n_candidates": 3, "data": {"retrieved_utc": "2026-10-28T05:00:00+00:00"}}
NO_LISTS = {"label": "x", "n_candidates": 0, "data": {"retrieved_utc": None}}


def test_the_first_open_downloads_once_when_nothing_is_on_the_computer(monkeypatch, settings):
    calls = []

    def fake(settings_, *, now, days, refresh):
        calls.append(refresh)
        return [NO_LISTS if not refresh else HAVE_LISTS], []

    monkeypatch.setattr(view, "collect_reports", fake)
    state: dict = {}
    reports, _ = view.get_reports(state, settings, now=NOW)
    assert calls == [False, True] and reports == [HAVE_LISTS]  # read the cache, found nothing, downloaded once


def test_a_failed_first_download_is_not_retried_every_run(monkeypatch, settings):
    calls = []

    def fake(settings_, *, now, days, refresh):
        calls.append(refresh)
        return [NO_LISTS], []

    monkeypatch.setattr(view, "collect_reports", fake)
    state: dict = {}
    view.get_reports(state, settings, now=NOW)
    view.get_reports(state, settings, now=NOW + timedelta(seconds=view.CACHE_SECONDS + 1))
    assert calls == [False, True, False]  # one attempt per session, then it only reads the cache


def test_reports_are_held_between_runs_and_recomputed_on_refresh_or_a_new_data_directory(monkeypatch, settings):
    calls = []

    def fake(settings_, *, now, days, refresh):
        calls.append(refresh)
        return [HAVE_LISTS], []

    monkeypatch.setattr(view, "collect_reports", fake)
    state: dict = {}
    assert view.get_reports(state, settings, now=NOW)[0] == [HAVE_LISTS] and calls == [False]
    view.get_reports(state, settings, now=NOW + timedelta(seconds=30))
    assert calls == [False]  # held
    view.get_reports(state, settings, now=NOW, force=True)
    assert calls == [False, True]  # a refresh asks the networks
    view.get_reports(state, settings, now=NOW + timedelta(seconds=view.CACHE_SECONDS + 1))
    assert calls == [False, True, False]  # a held report ages out (and then only reads the cache)
    view.get_reports(state, dataclasses.replace(settings, data_dir=settings.data_dir / "other"), now=NOW + timedelta(seconds=view.CACHE_SECONDS + 1))
    assert len(calls) == 4


def test_one_plugin_failing_does_not_hide_the_others(monkeypatch, settings):
    from plugins import transit_finder

    def broken(self, now=None, *, days=30, refresh=False):
        raise RuntimeError("list parser bug")

    monkeypatch.setattr(transit_finder.PLUGIN_CLASS, "opportunities", broken)
    reports, errors = view.collect_reports(settings, now=NOW, days=7, refresh=False)
    assert [r["plugin"] for r in reports] == ["variable_stars"]  # the other plugin that offers opportunities still shows
    assert len(errors) == 1 and "transit_finder" in errors[0] and "list parser bug" in errors[0]


GUIDE = {
    "title": "What is one timing worth?", "summary": "Worth most when the schedule is stale.",
    "sections": [
        {"heading": "When one timing helps", "body": "Stale schedules.", "technical": False},
        {"heading": "How the figures are worked out", "body": "Inverse variance.", "technical": True},
    ],
}


def test_the_guide_hides_technical_sections_in_the_simple_view():
    assert [s["heading"] for s in view.guide_sections(GUIDE, simple=True)] == ["When one timing helps"]
    assert [s["heading"] for s in view.guide_sections(GUIDE, simple=False)] == ["When one timing helps", "How the figures are worked out"]


def _with_schedule(op, verdict, headline, *, provisional=False):
    return {**op, "schedule": {"verdict": verdict, "headline": headline, "reference_utc": "2015-04-29T00:00:00+00:00",
                               "uncertainty_min": 382.0, "provisional": provisional}}


def test_the_table_has_a_plain_schedule_column_and_marks_a_provisional_verdict():
    ops = [
        _with_schedule(_op("Alpha b", windows=(8,)), "out_of_date", "Its schedule is counted from 2015.", provisional=True),
        _with_schedule(_op("Beta b", windows=(9,)), "up_to_date", "Known to +/-1 min."),
        _op("Gamma b", windows=(10,)),  # a plugin that gives no verdict
    ]
    for simple in (True, False):
        rows = view.opportunity_rows(ops, now=NOW, tz=TZ, simple=simple)
        assert [r["Schedule"] for r in rows] == ["Out of date (provisional)", "Up to date", ""]
    assert "Staleness (x half transit)" in view.opportunity_rows([_op("A b", windows=(8,))], now=NOW, tz=TZ, simple=False)[0]
    assert view.schedule_label(_with_schedule(_op("X b"), "unknown", "h")) == "Can't tell"


def test_a_case_label_names_the_list_and_how_many_this_telescope_can_reach():
    assert view.category_label(_report(_three())) == "Exoplanet transit timing (3 planets)"
    assert view.category_label(_report([_op("Only b", windows=(8,))])) == "Exoplanet transit timing (1 planet)"
    assert view.category_label(_report([])) == "Exoplanet transit timing"  # nothing downloaded yet: no "(0 planets)"
    stars = {
        **_report([_op("RR Lyrae", windows=(8,))]), "label": "Variable-star photometry",
        "words": {"item": "Star", "items": "stars", "chance": "night", "chances": "nights"},
    }
    assert view.category_label(stars) == "Variable-star photometry (1 star)"


# --------------------------------------------------------------------------- #
# First-light preset and the forecast overlay
# --------------------------------------------------------------------------- #
SITE = {"lat": 39.7, "lon": -105.0, "elev_m": 1609.0}


def _fl_op(name, *, v=9.5, depth=1.8, dur=3.0, verdict="up_to_date", err=4.0, dec=39.7, hours=(8,), base=NOW):
    op = _with_schedule(_op(name, windows=hours, base=base), verdict, "Known well.")
    op["facts"] = {"v_mag": v, "depth_pct": depth, "duration_h": dur, "period_d": 2.0}
    op["target"] = {**op["target"], "dec_deg": dec}
    for w in op["windows"]:
        w["mid_err_min"] = err
    return op


@pytest.fixture()
def altitude_by_dec(monkeypatch):
    """Altitude is a function of declination here (no astropy).

    dec 39.7 is 55 deg (inside the 40-70 first-light band), dec 10 is 85 (too near the zenith), dec -30 is low.
    """
    from tools import astrometry

    def altitude(ra, dec, lat, lon, elev, when):
        if dec >= 30:
            return 55.0
        return 85.0 if dec > 0 else 25.0

    monkeypatch.setattr(astrometry, "target_altitude_deg", altitude)


def test_first_light_keeps_only_bright_deep_high_well_known_targets(altitude_by_dec):
    ops = [
        _fl_op("Good b"),
        _fl_op("Faint b", v=13.0),
        _fl_op("Shallow b", depth=0.4),
        _fl_op("Stale b", verdict="out_of_date"),
        _fl_op("Unknown b", verdict="unknown"),
        _fl_op("Low b", dec=-30.0),
        _fl_op("Zenith b", dec=10.0),
        _fl_op("Long b", dur=9.0),
        _fl_op("Vague b", err=400.0),
    ]
    out = view.first_light_candidates(ops, site=SITE, now=NOW, baseline_min=60.0)
    assert [o["name"] for o in out] == ["Good b"]
    fl = out[0]["first_light"]
    assert fl["altitude_deg"] == 55.0 and any("bright" in r for r in fl["why"]) and any("well-known" in r for r in fl["why"])


def test_first_light_ranks_brighter_deeper_better_known_targets_first(altitude_by_dec):
    ops = [
        _fl_op("Ok b", v=11.0, depth=1.1, verdict="getting_stale"),
        _fl_op("Best b", v=8.0, depth=2.5, verdict="up_to_date"),
        _fl_op("Mid b", v=10.0, depth=1.5, verdict="up_to_date"),
    ]
    out = view.first_light_candidates(ops, site=SITE, now=NOW)
    assert [o["name"] for o in out] == ["Best b", "Mid b", "Ok b"]
    assert out[0]["first_light"]["score"] > out[1]["first_light"]["score"] > out[2]["first_light"]["score"]


def test_first_light_looks_a_week_ahead_and_keeps_only_the_windows_that_qualify(altitude_by_dec):
    op = _fl_op("Far b", hours=(8, 24 * 9))  # one tonight, one nine days out
    out = view.first_light_candidates([op], site=SITE, now=NOW, days=7)
    assert len(out[0]["windows"]) == 1


def _hours(start, clouds, rain=0.0):
    return [{"time": (start + timedelta(hours=i)).isoformat(), "cloud_cover_pct": c, "precipitation_mm": rain, "wind_kmh": 5.0}
            for i, c in enumerate(clouds)]


def test_forecast_verdicts_per_window_and_nothing_claimed_beyond_the_forecast():
    from tools.weather import summarise_window

    start = NOW.replace(minute=0)
    hours = _hours(start, [5, 10, 15, 20, 20, 90, 95, 100])
    clear = summarise_window(hours, start, start + timedelta(hours=4))
    assert clear["verdict"] == "clear" and clear["cloud_mean_pct"] == pytest.approx(12.5)
    assert summarise_window(hours, start + timedelta(hours=5), start + timedelta(hours=8))["verdict"] == "cloudy"
    assert summarise_window(_hours(start, [40, 45, 50, 55]), start, start + timedelta(hours=4))["verdict"] == "partly"
    assert summarise_window(_hours(start, [5, 5, 5, 5], rain=0.4), start, start + timedelta(hours=4))["verdict"] == "cloudy"
    assert summarise_window(hours, start + timedelta(hours=20), start + timedelta(hours=23)) is None  # not covered
    assert summarise_window(hours, start + timedelta(hours=6), start + timedelta(hours=14)) is None  # under half covered


def test_cloudy_windows_can_be_hidden_but_windows_beyond_the_forecast_stay():
    start = NOW.replace(minute=0)
    ops = [_op("Alpha b", windows=(8, 40), base=NOW)]
    forecast = {"hours": _hours(start, [100] * 24)}  # all of the first day is overcast
    out = view.drop_cloudy(ops, forecast)
    assert len(out[0]["windows"]) == 1 and out[0]["windows"][0]["mid"] == ops[0]["windows"][1]["mid"]
    assert view.drop_cloudy(ops, None) is ops


def test_window_labels_rows_and_calendar_carry_the_forecast():
    start = NOW.replace(minute=0)
    forecast = {"hours": _hours(start, [10] * 30)}
    op = _op("Alpha b", windows=(8,), base=NOW)
    assert view.window_label(op["windows"][0], TZ, NOW, forecast=forecast).endswith("[clear]")
    assert "[" not in view.window_label(op["windows"][0], TZ, NOW)
    rows = view.opportunity_rows([op], now=NOW, tz=TZ, simple=True, forecast=forecast)
    assert rows[0]["Sky forecast"].startswith("forecast clear")
    weeks = view.calendar_weeks([op], now=NOW, days=3, tz=TZ, forecast=forecast)
    assert [e["sky"] for w in weeks for d in w for e in d["entries"]] == ["clear"]

