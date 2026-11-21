"""Which plugins can replay an archive, the WCS registration / target hint it relies on, and the fleet view's logic."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from dashboard import fleet
from plugins import PluginLoadError, load_plugin
from plugins.asteroid_finder import reduction
from plugins.transit_finder import photometry


# --------------------------------------------------------------------------- #
# Which plugins can replay an archive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["rubin_followup", "transit_finder", "variable_stars"])
def test_a_plugin_without_an_archive_source_is_rejected_at_load(settings, name):
    with pytest.raises(PluginLoadError, match="archive"):
        load_plugin(name, replace(settings, data_source="archive"))


def test_the_asteroid_plugin_replays_the_ztf_archive(settings):
    assert load_plugin("asteroid_finder", replace(settings, data_source="archive")).name == "asteroid_finder"


# --------------------------------------------------------------------------- #
# Target hint (TARGRA/TARGDEC) and WCS registration
# --------------------------------------------------------------------------- #
def _wcs_header(crpix=(50.0, 40.0), shape=(80, 100)):
    hdr = fits.Header()
    hdr["NAXIS1"], hdr["NAXIS2"] = shape[1], shape[0]
    hdr["CTYPE1"], hdr["CTYPE2"] = "RA---TAN", "DEC--TAN"
    hdr["CRVAL1"], hdr["CRVAL2"] = 307.7, 6.4
    hdr["CRPIX1"], hdr["CRPIX2"] = crpix
    hdr["CD1_1"], hdr["CD1_2"], hdr["CD2_1"], hdr["CD2_2"] = -0.736 / 3600, 0.0, 0.0, 0.736 / 3600
    return hdr


def test_target_hint_projects_the_catalogue_position_through_the_wcs():
    hdr = _wcs_header()
    wcs = WCS(hdr)
    x, y = wcs.all_world2pix([307.7005], [6.4003], 0)
    hdr["TARGRA"], hdr["TARGDEC"] = 307.7005, 6.4003
    got = photometry.target_hint(hdr, wcs)
    assert got == pytest.approx((float(x[0]), float(y[0])), abs=1e-6)


def test_target_hint_is_none_when_absent_malformed_or_off_the_frame(caplog):
    hdr = _wcs_header()
    wcs = WCS(hdr)
    assert photometry.target_hint(hdr, wcs) is None  # no keywords: telescope frames centred on the target
    hdr["TARGRA"], hdr["TARGDEC"] = "not a number", 6.4
    assert photometry.target_hint(hdr, wcs) is None
    hdr["TARGRA"], hdr["TARGDEC"] = 310.0, 6.4  # ~2.3 degrees away: not on this frame
    with caplog.at_level(logging.WARNING):
        assert photometry.target_hint(hdr, wcs) is None
    assert "outside the frame" in caplog.text


def _frame(header, shape=(80, 100)):
    return reduction._Frame(
        path="f.fits", header=header, data=np.zeros(shape, dtype=np.float32), t_mid=datetime(2019, 1, 20, tzinfo=timezone.utc),
        t_start=datetime(2019, 1, 20, tzinfo=timezone.utc), exptime=30.0, rms=1.0, xy=np.zeros((0, 2)), flux=np.zeros(0),
    )


def test_wcs_registration_recovers_the_pointing_offsets_without_any_stars():
    # Frame k's WCS is the first one's with CRPIX moved by (5k, -3k): the same sky sits (-5k, +3k) px away.
    frames = [_frame(_wcs_header(crpix=(50.0 + 5 * k, 40.0 - 3 * k))) for k in range(3)]
    report = reduction.register_frames(frames, reduction.ReductionParams(registration="wcs"))
    assert [r["method"] for r in report] == ["wcs"] * 3 and all(r["ok"] for r in report)
    for k, fr in enumerate(frames):
        assert fr.shift == pytest.approx([-5.0 * k, 3.0 * k], abs=1e-6)


def test_wcs_registration_falls_back_to_stars_when_a_frame_has_no_solved_wcs(caplog):
    frames = [_frame(_wcs_header()), _frame(fits.Header())]  # the second only has a pointing guess
    with caplog.at_level(logging.WARNING):
        report = reduction.register_frames(frames, reduction.ReductionParams(registration="wcs"))
    assert "matching stars instead" in caplog.text
    assert "method" not in report[0] and report[1]["ok"] is False  # no stars to match: reported, not faked


# --------------------------------------------------------------------------- #
# Fleet view logic
# --------------------------------------------------------------------------- #
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def _mission(**over):
    m = {
        "id": "m1", "plugin_name": "asteroid_finder", "status": "WAITING", "current_node": "wait_for_visit",
        "data_source": "archive", "error": None, "worker_pid": 4242, "worker_host": "host",
        "heartbeat_at": (NOW - timedelta(seconds=3)).isoformat(),
        "clock": {"kind": "replay", "speed": 100000.0, "sim_now": "2019-01-23T05:50:00+00:00"},
        "sim_time": "2019-01-23T05:50:00+00:00", "wake_at": "2019-02-09T05:05:00+00:00",
        "progress": {"phase": "waiting", "visit": 2, "visits_total": 2, "target": "(8191) 2019-02-09", "node": "wait_for_visit"},
    }
    m.update(over)
    return m


def _targets(statuses=("OBSERVED", "PENDING")):
    base = datetime(2019, 1, 20, 7, 19, tzinfo=timezone.utc)
    due = [base, base + timedelta(days=19, hours=21)]
    return [
        {"position": i, "name": f"visit {i + 1}", "status": s, "payload": {"not_before": due[i].isoformat()}}
        for i, s in enumerate(statuses)
    ]


def test_fmt_span_uses_the_two_most_significant_units():
    assert fleet.fmt_span(None) == "-" and fleet.fmt_span(45) == "45 s" and fleet.fmt_span(600) == "10 min"
    assert fleet.fmt_span(7500) == "2 h 05 m" and fleet.fmt_span(3 * 86400 + 4 * 3600) == "3 d 4 h" and fleet.fmt_span(-5) == "0 s"


def test_a_waiting_replay_mission_shows_survey_gap_and_the_real_time_it_will_take():
    row = fleet.fleet_row(_mission(), _targets(), now=NOW)
    assert row["state"] == "WAITING" and row["liveness"] == "running" and row["active"] is True
    assert row["visit"] == 2 and row["visits_total"] == 2 and row["visits_done"] == 1
    gap = (datetime(2019, 2, 9, 5, 5, tzinfo=timezone.utc) - datetime(2019, 1, 23, 5, 50, tzinfo=timezone.utc)).total_seconds()
    assert row["wait_survey_s"] == pytest.approx(gap) and row["wait_wall_s"] == pytest.approx(gap / 100000.0)
    assert row["worker"] == "pid 4242@host" and row["heartbeat_age_s"] == pytest.approx(3.0)
    assert row["fraction"] == pytest.approx(0.5)  # one of two visits done


def test_a_mission_whose_owner_stopped_heartbeating_is_shown_as_orphaned_not_as_running():
    stale = _mission(status="OBSERVING", heartbeat_at=(NOW - timedelta(minutes=5)).isoformat(),
                     progress={"phase": "acquiring", "visit": 1, "visits_total": 1, "frames_done": 30, "frames_total": 74})
    row = fleet.fleet_row(stale, _targets(("PENDING",)), now=NOW, lease_s=45.0)
    assert row["liveness"] == "orphaned" and row["state"] == "ORPHANED" and row["active"] is True
    assert row["frames_done"] == 30 and row["frames_total"] == 74
    assert 0.0 < row["fraction"] < 1.0  # partial credit for the frames already in hand, never a full visit


def test_finished_and_never_started_missions_have_their_own_states():
    done = fleet.fleet_row(_mission(status="COMPLETE", heartbeat_at=None, progress={"phase": "complete"}), _targets(("OBSERVED", "OBSERVED")), now=NOW)
    assert done["state"] == "COMPLETE" and done["active"] is False and done["fraction"] == 1.0 and done["liveness"] == "finished"
    fresh = fleet.fleet_row(_mission(status="INIT", heartbeat_at=None, wake_at=None, sim_time=None, progress=None, clock=None), [], now=NOW)
    assert fresh["state"] == "IDLE" and fresh["liveness"] == "idle" and fresh["speed"] is None and fresh["clock_kind"] == "system"
    failed = fleet.fleet_row(_mission(status="FAILED", error="boom", heartbeat_at=None), _targets(), now=NOW)
    assert failed["state"] == "FAILED" and failed["detail"] == "boom"


def test_visit_rows_report_state_gaps_and_the_cadence():
    rows = fleet.visit_rows(_targets(), current_index=1)
    assert [r["state"] for r in rows] == ["done", "current"] and rows[0]["gap_s"] is None
    assert rows[1]["gap_s"] == pytest.approx((19 * 24 + 21) * 3600)
    assert fleet.cadence_summary(rows) == "2 visits over 19 d 21 h; gaps 19 d 21 h to 19 d 21 h (median 19 d 21 h)"
    assert fleet.cadence_summary(fleet.visit_rows(_targets(("PENDING",)), None)) is None  # one visit: no cadence
    undated = [{"position": 0, "name": "t", "status": "PENDING", "payload": {}}]  # telescope target, no schedule
    assert fleet.visit_rows(undated, 0)[0]["due"] is None
