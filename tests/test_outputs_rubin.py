"""The output pipeline for the Rubin case: the mission log on a mission with an anomaly, AAVSO Extended drafts and the
outburst screen.  Whole missions here are shared through ``run_once`` (see ``tests/conftest.py``).
"""

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from plugins import load_plugin
from tests.conftest import run_graph


@pytest.fixture()
def rubin(monkeypatch):
    monkeypatch.setenv("ASTRONOMO_RUBIN_FRAMES", "8")


@pytest.fixture()
def mission(run_once):
    """The whole simulated mission at the default 5 sigma threshold (shared with the end-to-end test)."""
    return run_once("rubin_followup", anomaly_min_sigma=5.0)


def test_an_anomaly_above_threshold_is_flagged_in_the_log_and_nothing_is_written_up(mission):
    final, seq, arts, _ = mission
    assert [a["target"] for a in final["anomalies"]] == ["AT 2026snc (sim)"]
    assert final["anomalies"][0]["kind"] == "outburst" and final["anomalies"][0]["significance_sigma"] >= 5.0
    assert seq[-1] == "write_log" and final["status"] == "COMPLETE"
    assert "paper" not in final and "paper_decision" not in final
    assert len(arts["mission_log"]) == 1
    log_text = Path(final["mission_log"]["path"]).read_text()
    assert "AT 2026snc" in log_text and "outburst" in log_text  # flagged for a person to check


FADING, GONE, RISING = "AT 2026sna (sim)", "AT 2026snb (sim)", "AT 2026snc (sim)"


def test_the_whole_simulated_rubin_mission_classifies_each_alert_and_logs_it(mission):
    final, _, arts, _ = mission
    assert final["status"] == "COMPLETE"
    by_name = {o["target"]["name"]: o["crossref"]["entries"][0]["classification"] for o in final["observations"]}
    assert by_name == {FADING: "detected_consistent_with_rubin", GONE: "not_detected_expected_visible",
                       RISING: "detected_brighter_than_expected"}
    log_text = Path(final["mission_log"]["path"]).read_text()
    assert "AT 2026sna" in log_text and "SIMULATION" in log_text.upper()
    assert arts["observations"][0]["crossref"]["entries"]  # stored for the dashboard


def test_raising_the_threshold_moves_the_anomaly_below_it(settings, rubin):
    final, seq, _, _ = run_graph(settings, "rubin_followup", anomaly_min_sigma=9.0)
    assert final["anomalies"] == [] and [a["kind"] for a in final["below_threshold"]] == ["outburst"]
    assert seq[-1] == "write_log" and final["status"] == "COMPLETE"
    assert "outburst" in Path(final["mission_log"]["path"]).read_text()  # listed in the log so a person can check it


# --------------------------------------------------------------------------- #
# Rubin: AAVSO Extended drafts and the outburst screen
# --------------------------------------------------------------------------- #
@pytest.fixture()
def rubin_mission(run_once):
    final, _, _ = run_once("rubin_followup", anomaly_min_sigma=5.0)[:3]  # the same run the log test reads
    mission = {"night": "2026-10-21", "simulated": True, "data_source": "telescope", "observations": final["observations"]}
    return mission, final


def _plugin(settings, name, **over):
    return load_plugin(name, replace(settings, **over))


def test_rubin_drafts_report_a_magnitude_or_a_fainter_than_limit(settings, rubin_mission):
    mission, _ = rubin_mission
    drafts = {d.target: d for d in _plugin(settings, "rubin_followup", aavso_obs_code="ABC").build_submissions(mission)}
    assert len(drafts) == 3 and all(d.format == "AAVSO Extended v1.2" and d.content for d in drafts.values())
    assert all(d.blockers == [] for d in drafts.values())  # with an observer code nothing blocks the draft
    detected = drafts["AT 2026sna (sim)"].content.splitlines()[-1].split(",")
    assert detected[0] == "AT 2026sna (sim)" and detected[4] == "CV" and detected[5] == "NO" and not detected[2].startswith("<")
    assert detected[13] == "Gaia DR3 G" and "Gaia G zero point" in detected[14]
    gone = drafts["AT 2026snb (sim)"].content.splitlines()[-1].split(",")
    assert gone[2].startswith("<") and gone[3] == "na"  # a limit has no error
    assert any("not V-band standard" in n for n in drafts["AT 2026sna (sim)"].notes)
    assert drafts["AT 2026sna (sim)"].filename.endswith("_AAVSO_Extended.txt")


def test_rubin_drafts_are_blocked_without_an_observer_code(settings, rubin_mission):
    mission, _ = rubin_mission
    drafts = _plugin(settings, "rubin_followup", aavso_obs_code="").build_submissions(mission)
    assert all(d.content and any("AAVSO observer code" in b for b in d.blockers) for d in drafts)


def test_rubin_holds_a_target_with_no_measurement(settings, rubin_mission):
    mission, _ = rubin_mission
    obs = json.loads(json.dumps(mission["observations"]))
    obs[0]["crossref"]["entries"] = [{"classification": "not_measured"}]
    draft = _plugin(settings, "rubin_followup").build_submissions({**mission, "observations": obs[:1]})[0]
    assert draft.content is None and "no calibrated magnitude" in draft.blockers[0]


def test_rubin_outburst_significance_is_the_brightening_over_a_third_of_the_tolerance(settings, rubin_mission):
    mission, _ = rubin_mission
    found = _plugin(settings, "rubin_followup").detect_anomalies(mission)
    assert [a["target"] for a in found] == ["AT 2026snc (sim)"]  # the fader and the vanished source are not outbursts
    a = found[0]
    ev = a["evidence"]
    assert a["kind"] == "outburst" and ev["delta_mag"] < 0
    assert a["significance_sigma"] == pytest.approx(abs(ev["delta_mag"]) / (ev["tolerance_mag"] / 3.0))
    assert ev["tolerance_mag"] >= 0.5 and math.isfinite(a["significance_sigma"])
    assert "brighter than Rubin" in a["summary"]


def test_rubin_targets_from_fink_carry_a_transient_followup_network_block(settings, monkeypatch):
    from datetime import datetime, timezone

    from plugins.rubin_followup.fink import FinkClient

    now = datetime.now(timezone.utc)
    alert = {"dia_object_id": "123", "mag": 14.5, "mag_err": 0.05, "band": "r", "utc_iso": now.isoformat(), "reliability": 0.97,
             "is_negative": False, "mjd": 61000.0, "ra_deg": 10.0, "dec_deg": 5.0, "tns_name": "AT 2026abc", "simbad_otype": None}
    monkeypatch.setenv("ASTRONOMO_RUBIN_MIN_ALT_DEG", "-90")
    monkeypatch.setattr(FinkClient, "recent_alerts", lambda self, tag, n, since: [alert])
    plugin = load_plugin("rubin_followup", replace(settings, offline_services=False, mock_mode=True))
    target = plugin.generate_targets()[0]
    net = target["network"]
    assert net["sources"] == ["fink_lsst"] and net["task"] == "transient_followup"
    assert "AT 2026abc" in net["reason"] and "reliability 0.97" in net["reason"]
