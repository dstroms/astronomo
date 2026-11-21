"""The output pipeline for the transit case: target lists from the networks, the AAVSO Exoplanet drafts and the anomaly
proposals.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core import anomaly
from core.graph import run_mission
from core.state import initial_state
from core.submission import STATUS_HELD, STATUS_SIMULATED
from plugins import load_plugin
from tests.conftest import Harness

NO_SKY_LIMITS = {"min_altitude_deg": -90.0, "min_sun_separation_deg": 0.0, "moon_avoid_deg": 0.0, "moon_floor_deg": 0.0}  # where the sky is on the day the suite runs must not decide a test


def test_transit_targets_come_from_the_network_lists_in_priority_order(settings, monkeypatch):
    """Whole path: ExoClock + Exoplanet Watch + the TTV flag -> ranked, scheduled targets carrying their network task."""
    from datetime import datetime, timezone

    from astropy.time import Time

    from core import networks
    from plugins.transit_finder import archive
    from plugins.transit_finder import networks as tn

    jd_now = float(Time(datetime.now(timezone.utc)).jd)

    def ec(name, hours_ahead, period_unc, priority, ra="06:30:00"):  # period 1 d: one transit in the next 24 h
        return {"name": name, "star": name[:-1], "period_days": 1.0, "period_unc": period_unc, "t0_bjd_tdb": jd_now - 10.0 + hours_ahead / 24.0,
                "t0_unc": 1e-4, "duration_hours": 3.0, "ra_j2000": ra, "dec_j2000": "+30:00:00", "v_mag": 11.0,
                "depth_r_mmag": 15.0, "priority": priority, "current_oc_min": 0.0, "total_observations_recent": 0}

    payload = json.dumps({
        "Stale-1b": ec("Stale-1b", 3.0, 1e-4, "high"),
        "Fresh-2b": ec("Fresh-2b", 9.0, 1e-9, "low"),
        "Ttv-3b": ec("Ttv-3b", 16.0, 1e-9, "low"),
    })
    watch = "name,RA,Dec,epoch,period,duration,depth,vmag,priority \n"

    def get(url, params, timeout):
        if url == tn.EXOCLOCK_URL:
            return payload
        if url == tn.EXOPLANET_WATCH_URL:
            return watch
        if url == archive.ARCHIVE_URL:
            return "pl_name\nTtv-3 b\n"
        raise networks.NetworkError(url)

    monkeypatch.setattr(networks, "_http_get_text", get)
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MIN_ALT_DEG", "-90")
    monkeypatch.delenv("ASTRONOMO_TRANSIT_TARGETS", raising=False)
    plugin = load_plugin("transit_finder", replace(settings, offline_services=False, **NO_SKY_LIMITS))
    targets = {t["name"]: t for t in plugin.generate_targets()}
    assert set(targets) == {"Stale-1 b", "Fresh-2 b", "Ttv-3 b"}
    assert targets["Stale-1 b"]["network"]["task"] == "ephemeris_refinement" and targets["Stale-1 b"]["network"]["sources"] == ["exoclock"]
    assert targets["Ttv-3 b"]["network"]["task"] == "ttv_monitoring" and "nasa_exoplanet_archive" in targets["Ttv-3 b"]["network"]["sources"]
    assert targets["Stale-1 b"]["network"]["score"] > targets["Fresh-2 b"]["network"]["score"]
    assert targets["Stale-1 b"]["network"]["retrieved_utc"] and targets["Stale-1 b"]["network"]["stale_cache"] is False


def test_transit_falls_back_to_a_labelled_built_in_list_when_no_network_list_is_reachable(settings, monkeypatch):
    from datetime import datetime, timezone

    from astropy.time import Time

    from plugins.transit_finder import archive

    jd_now = float(Time(datetime.now(timezone.utc)).jd)
    row = {"planet": "WASP-12 b", "host": "WASP-12", "ra_deg": 100.0, "dec_deg": 30.0, "v_mag": 11.5, "period_d": 1.0,
           "period_err_d": 1e-7, "t0_bjd_tdb": jd_now - 10.0 + 5.0 / 24.0, "t0_err_d": 1e-4, "duration_h": 3.0, "depth_pct": 1.4,
           "source": "test"}
    asked = []
    monkeypatch.setattr(archive, "fetch_planets", lambda names: asked.append(names) or [row])
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MIN_ALT_DEG", "-90")
    monkeypatch.delenv("ASTRONOMO_TRANSIT_TARGETS", raising=False)
    plugin = load_plugin("transit_finder", replace(settings, offline_services=False, **NO_SKY_LIMITS))  # the conftest makes every list unreachable
    (target,) = plugin.generate_targets()
    assert target["network"]["task"] == "manual" and target["network"]["sources"] == ["built_in"]
    assert "no network target list" in target["network"]["reason"]
    assert asked and "WASP-12 b" in asked[0]


def test_an_operator_transit_list_is_labelled_operator(settings, monkeypatch):
    from datetime import datetime, timezone

    from astropy.time import Time

    from plugins.transit_finder import archive

    jd_now = float(Time(datetime.now(timezone.utc)).jd)
    row = {"planet": "HAT-P-32 b", "host": "HAT-P-32", "ra_deg": 100.0, "dec_deg": 30.0, "v_mag": 11.5, "period_d": 1.0,
           "period_err_d": 1e-7, "t0_bjd_tdb": jd_now - 10.0 + 5.0 / 24.0, "t0_err_d": 1e-4, "duration_h": 3.0, "depth_pct": 1.4, "source": "t"}
    monkeypatch.setattr(archive, "fetch_planets", lambda names: [row])
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MIN_ALT_DEG", "-90")
    monkeypatch.setenv("ASTRONOMO_TRANSIT_TARGETS", "HAT-P-32 b")
    plugin = load_plugin("transit_finder", replace(settings, offline_services=False, **NO_SKY_LIMITS))
    (target,) = plugin.generate_targets()
    assert target["network"]["sources"] == ["operator"] and target["network"]["task"] == "manual"


# --------------------------------------------------------------------------- #
# Transit: the AAVSO Exoplanet drafts and the anomaly proposals
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def transit_mission(tmp_path_factory):
    import os

    from config import get_settings

    os.environ["ASTRONOMO_NO_DOTENV"] = "1"
    root = tmp_path_factory.mktemp("transit_out")
    s = get_settings(data_dir=root / "data", mock_mode=True, offline_services=True, mock_capture_seconds=0.0, sleep_interval_s=0.0,
                     max_sleep_cycles=5, mock_cloudy_checks=0, max_targets=3)
    s.ensure_dirs()
    h = Harness(s, load_plugin("transit_finder", s))
    try:
        h.db.create_mission("m_t", "transit_finder", True, {"cloud_cover_threshold_pct": 30.0})
        final = run_mission(h.app, "m_t", initial_state("m_t", "transit_finder", True))
    finally:
        h.close()
    return s, {"night": "2026-10-21", "simulated": True, "data_source": "telescope", "observations": final["observations"]}, final


PLANET, CONTROL = "WASP-12 b (sim)", "Control star (sim)"


def test_the_whole_simulated_transit_mission_confirms_the_planet_and_finds_nothing_on_the_control(transit_mission):
    _, _, final = transit_mission
    assert final["status"] == "COMPLETE"
    by_name = {o["target"]["name"]: o for o in final["observations"]}
    assert by_name[PLANET]["crossref"]["entries"][0]["classification"] == "known_transit_confirmed"
    assert by_name[CONTROL]["findings"]["detections"] == []
    text = Path(final["mission_log"]["path"]).read_text()
    assert "WASP-12 b" in text and "known transit confirmed" in text.lower()


def test_the_transit_mission_writes_aavso_exoplanet_files_in_bjd_tdb(transit_mission):
    s, mission, final = transit_mission
    files = [r for r in final["submissions"] if r["file"]]
    assert files and all(r["status"] == STATUS_SIMULATED and r["file"].startswith("SIMULATED_") for r in files)
    text = Path(files[0]["path"]).read_text()
    assert text.splitlines()[0] == "#TYPE=EXOPLANET" and "#DATE_TYPE=BJD_TDB" in text and "#MEASUREMENT_TYPE=Rnflux" in text
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(rows) > 20 and all(len(r.split(",")) == 3 for r in rows)
    jd = [float(r.split(",")[0]) for r in rows]
    assert jd == sorted(jd) and all(2.4e6 < x < 2.6e6 for x in jd)
    assert all(abs(float(r.split(",")[1]) - 1.0) < 0.5 for r in rows)  # normalised flux, not magnitudes
    values = sorted(float(r.split(",")[1]) for r in rows)
    assert 0.97 < values[len(values) * 3 // 4] < 1.03  # Rnflux: the out-of-transit level is nominally 1.0
    assert "normalised to the median of" in text
    names = files[0]["attachments"]
    assert names and all(a.endswith(".png") for a in names)
    assert any("finder" in a for a in names) and any("light_curve" in a for a in names)


def test_the_transit_control_star_is_held_not_uploaded(transit_mission):
    s, mission, final = transit_mission
    held = [r for r in final["submissions"] if r["status"] == STATUS_HELD]
    assert held and all(r["file"] is None and r["blockers"] for r in held)


def test_transit_drafts_are_blocked_until_an_observer_code_is_set(transit_mission):
    s, mission, _ = transit_mission
    plugin = load_plugin("transit_finder", replace(s, aavso_obs_code=""))
    drafts = [d for d in plugin.build_submissions(mission) if d.content]
    assert drafts and all(any("AAVSO observer code" in b for b in d.blockers) for d in drafts)
    coded = [d for d in load_plugin("transit_finder", replace(s, aavso_obs_code="ABC")).build_submissions(mission) if d.content]
    assert all(not any("observer code" in b for b in d.blockers) for d in coded)
    assert all(any("DETREND" in n or "detrend" in n for n in d.notes) for d in coded)
    assert all(any(Path(a).name.startswith("finder") for a in d.attachments) for d in coded)
    assert all(any("field image" in n for n in d.notes) for d in coded)

    stripped = json.loads(json.dumps(mission))
    for rec in stripped["observations"]:
        rec["findings"]["figures"] = [p for p in (rec["findings"].get("figures") or []) if not Path(p).name.startswith("finder")]
    bare = [d for d in load_plugin("transit_finder", replace(s, aavso_obs_code="ABC")).build_submissions(stripped) if d.content]
    assert bare and all(any("field image" in b for b in d.blockers) for d in bare)


def test_transit_anomalies_need_a_confirmed_identity(transit_mission):
    s, mission, _ = transit_mission
    plugin = load_plugin("transit_finder", s)
    base = plugin.detect_anomalies(mission)
    assert all(a["kind"] in ("timing_offset", "unexpected_dip", "missing_transit") for a in base)
    confirmed = [r for r in mission["observations"] if (r["crossref"].get("identity") or {}).get("status") == "confirmed" and r["crossref"].get("entries")
                 and r["findings"].get("detections")]
    assert confirmed, "the simulated planet is detected and identified against Gaia"

    # Pretend the timing is wildly off: with the identity confirmed that is a 50 sigma lead ...
    shifted = json.loads(json.dumps({**mission, "observations": confirmed}))
    for rec in shifted["observations"]:
        entry = rec["crossref"]["entries"][0]
        entry["t_mid_residual_min"], entry["t_mid_residual_sigma"] = 20.0, -50.0
    timing = [a for a in plugin.detect_anomalies(shifted) if a["kind"] == "timing_offset"]
    assert timing and timing[0]["significance_sigma"] == 50.0 and "+20.0 min" in timing[0]["summary"]

    # ... but measured on a star that is not the host, it is nothing, and the log can still show what was measured.
    for rec in shifted["observations"]:
        rec["crossref"]["identity"] = {"status": "mismatch"}
    timing = [a for a in plugin.detect_anomalies(shifted) if a["kind"] == "timing_offset"]
    assert timing and all(a["significance_sigma"] == 0.0 for a in timing)
    assert timing[0]["evidence"]["measured_sigma"] == 50.0 and "Not counted" in timing[0]["summary"]
    accepted, below, _ = anomaly.screen(timing, 5.0)
    assert accepted == [] and len(below) == len(timing)
