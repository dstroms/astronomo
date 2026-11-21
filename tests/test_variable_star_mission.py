"""A variable-star visit: green-channel extraction, a magnitude, and a simulated night with a known answer.

The known answer is injected, the way the transit simulation injects WASP-12 b: an eclipsing binary of a stated
magnitude whose minimum is at a stated time, and a steady control.  The pipeline has to recover both.
"""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from dataclasses import replace

from core.graph import run_mission
from core.state import initial_state
from core.submission import STATUS_SIMULATED
from plugins import load_plugin
from plugins.variable_stars import photometry
from tests.conftest import Harness
from tools.imaging import measurement_channel

STAR, CONTROL = "V1234 Lac (sim)", "Control star (sim)"


def test_a_bayer_mosaic_keeps_the_green_samples_and_drops_a_red_only_pixel():
    mosaic = np.zeros((6, 6), dtype=np.float32)
    mosaic[0, 1] = 80.0  # green in RGGB
    mosaic[1, 0] = 40.0  # green
    mosaic[0, 0] = 1000.0  # red, and it must not survive as itself
    header = fits.Header()
    header["BAYERPAT"] = "RGGB"
    image, band, note = measurement_channel(mosaic, header)
    assert band == "green" and "RGGB" in note and image.shape == mosaic.shape
    assert image[0, 1] == 80.0 and image[1, 0] == 40.0
    assert image[0, 0] == pytest.approx((80.0 + 40.0) / 2.0)

    colour = np.zeros((3, 4, 4), dtype=np.float32)
    colour[1, 2, 2] = 12.0
    plane, band, note = measurement_channel(colour, fits.Header())
    assert band == "green" and plane.shape == (4, 4) and plane[2, 2] == 12.0 and "3-colour" in note

    plain, band, _note = measurement_channel(np.full((4, 4), 3.0), fits.Header())
    assert band == "clear" and plain[0, 0] == 3.0


def test_comparison_stars_put_the_target_on_their_magnitude_scale():
    # A mag-12 star has 10^(-0.4) the flux of a mag-11 star.
    faint = 1000.0 / 10 ** 0.4
    comps = np.column_stack([np.full(5, 1000.0), np.full(5, faint)])
    mags = photometry.magnitudes_from_comps(np.full(5, 1000.0), comps, [11.0, 12.0])
    assert mags is not None and mags == pytest.approx(11.0, abs=1e-6)
    point = photometry.night_point(mags)
    assert point["mag"] == pytest.approx(11.0) and point["mag_err"] >= photometry.SYSTEMATIC_MAG
    # Frames inside a minimum are left out of the night's brightness.
    dipped = np.array([11.0, 11.0, 11.0, 12.5, 12.5, 11.0])
    times = [0.0, 100.0, 200.0, 1000.0, 1100.0, 2000.0]
    kept = photometry.night_point(dipped, times, t_mid_s=1050.0, duration_s=400.0)
    assert kept["mag"] == pytest.approx(11.0) and kept["n"] == 4


def test_an_unconfirmed_identity_zeros_the_anomaly_and_a_mira_far_from_maximum_is_proposed(settings):
    plugin = load_plugin("variable_stars", settings)
    eclipse = {"t_min_iso": "2026-10-28T08:00:00+00:00", "t_min_err_min": 1.0}
    mission = {"observations": [
        {
            "target": {
                "name": "Too soon", "v_max_mag": 12.0, "var_type": "UG",
                "predicted_min_iso": "2026-10-28T06:00:00+00:00", "predicted_min_err_min": 1.0,
            },
            "crossref": {"identity": {"status": "mismatch"}, "measurement": {"mag": 9.0, "mag_err": 0.05, "eclipse": eclipse}},
        },
        {
            "target": {"name": "Far Mira", "var_type": "M", "predicted_mag": 8.0, "v_max_mag": 8.0},
            "crossref": {"identity": {"status": "confirmed"}, "measurement": {"mag": 11.0, "mag_err": 0.05}},
        },
    ]}
    proposed = plugin.detect_anomalies(mission)
    too_soon = [a for a in proposed if a["target"] == "Too soon"]
    assert too_soon and all(a["significance_sigma"] == 0.0 and a["evidence"]["measured_sigma"] > 5 for a in too_soon)
    mira = [a for a in proposed if a["kind"] == "mira_offset"]
    assert mira and mira[0]["target"] == "Far Mira" and mira[0]["significance_sigma"] > 5


@pytest.fixture(scope="module")
def variable_mission(tmp_path_factory):
    import os

    from config import get_settings

    os.environ["ASTRONOMO_NO_DOTENV"] = "1"
    root = tmp_path_factory.mktemp("varstar_out")
    s = get_settings(
        data_dir=root / "data", mock_mode=True, offline_services=True, mock_capture_seconds=0.0, sleep_interval_s=0.0,
        max_sleep_cycles=5, mock_cloudy_checks=0, max_targets=3,
    )
    s.ensure_dirs()
    plugin = load_plugin("variable_stars", s)
    h = Harness(s, plugin)
    try:
        h.db.create_mission("m_v", "variable_stars", True, {"cloud_cover_threshold_pct": 30.0})
        final = run_mission(h.app, "m_v", initial_state("m_v", "variable_stars", True))
    finally:
        h.close()
    mission = {"night": "2026-11-14", "simulated": True, "data_source": "telescope", "observations": final["observations"]}
    return s, mission, final


def _by_name(final):
    return {o["target"]["name"]: o for o in final["observations"]}


def test_the_simulated_night_recovers_the_eclipse_time_and_both_magnitudes(variable_mission):
    _s, _mission, final = variable_mission
    assert final["status"] == "COMPLETE", final.get("errors")
    by = _by_name(final)
    star, control = by[STAR], by[CONTROL]
    meas = star["crossref"]["measurement"]
    assert meas["classification"] == "eclipse_on_time"
    assert meas["mag"] == pytest.approx(11.3, abs=0.12)
    assert meas["mag_err"] >= photometry.SYSTEMATIC_MAG
    truth = star["target"]["sim"]["epoch0_iso"]
    from datetime import datetime
    true_min = datetime.fromisoformat(truth) + timedelta(seconds=star["target"]["sim"]["eclipse"]["t_mid_s"])
    got = datetime.fromisoformat(meas["eclipse"]["t_min_iso"])
    assert abs((got - true_min).total_seconds()) < 6 * 60
    steady = control["crossref"]["measurement"]
    assert steady["classification"] == "steady"
    assert steady["mag"] == pytest.approx(11.8, abs=0.12)
    assert control["findings"]["detections"] == []
    text = Path(final["mission_log"]["path"]).read_text()
    assert "V1234 Lac" in text and "eclipse on time" in text.lower()


def test_the_mission_writes_an_aavso_extended_file_for_each_star(variable_mission):
    s, mission, final = variable_mission
    files = [r for r in final["submissions"] if r["file"]]
    assert len(files) == 2
    assert all(r["status"] == STATUS_SIMULATED and r["file"].startswith("SIMULATED_") for r in files)
    text = Path(files[0]["path"]).read_text()
    assert text.splitlines()[0] == "#TYPE=EXTENDED"
    assert "#OBSTYPE=CCD" in text
    body = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(body) == 1
    cells = body[0].split(",")
    assert cells[4] == "TG" and cells[6] == "STD" and cells[7] == "ENSEMBLE"
    assert math.isfinite(float(cells[2])) and 10.0 < float(cells[2]) < 13.0
    plugin = load_plugin("variable_stars", replace(s, aavso_obs_code=""))
    drafts = [d for d in plugin.build_submissions(mission) if d.content]
    assert drafts and all(any("observer code" in b for b in d.blockers) for d in drafts)
    coded = load_plugin("variable_stars", replace(s, aavso_obs_code="ABC"))
    assert all(not any("observer code" in b for b in d.blockers) for d in coded.build_submissions(mission) if d.content)
