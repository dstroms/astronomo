"""Rubin follow-up plugin: Fink client, alert selection, stacking/calibration/photometry, classification, graph.

Network is never used: the Fink client gets a fake HTTP session, and live-target selection gets a fake client.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.time import Time

from config import get_settings
from core.digest import LLMPayloadError, render_payload
from plugins import available_plugins, load_plugin
from plugins.rubin_followup import fink, photometry
from plugins.rubin_followup.fink import FinkClient, FinkError
from plugins.rubin_followup.plugin import RubinFollowupPlugin
from plugins.rubin_followup.scene import TransientScene
from tools.catalogs import CatalogClient, CatalogError
from tools.mock_sky import MockSky
from tools.telescope import SeestarController

HEAVY_KEYS = ("sources", "reference_stars", "wcs", "transparency", "frame_paths", "light_curve")
FADING, GONE, RISING = "AT 2026sna (sim)", "AT 2026snb (sim)", "AT 2026snc (sim)"


# --------------------------------------------------------------------------- #
# Fink client (fake HTTP)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status=200, text=None):
        self._payload, self.status_code = payload, status
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _row(oid=170000000000000001, flux=15849.0, mjd=61235.4, **over):
    row = {"r:diaObjectId": oid, "r:diaSourceId": 7, "r:ra": 62.7, "r:dec": -48.7, "r:psfFlux": flux, "r:psfFluxErr": 158.0,
           "r:band": "r", "r:midpointMjdTai": mjd, "r:reliability": 0.99, "r:isNegative": False,
           "f:xm_simbad_otype": "Fail", "f:xm_tns_fullname": "AT 2026tgx"}
    row.update(over)
    return row


def test_flux_to_ab_magnitude():
    # 3631 Jy = 3.631e12 nJy is AB magnitude 0; the LSST convention rounds the constant to 31.4 (exact: 31.39993)
    assert fink.ab_mag(3631e9) == pytest.approx(0.0, abs=1e-3)
    assert fink.ab_mag(10 ** ((31.4 - 20.0) / 2.5)) == pytest.approx(20.0)
    assert fink.ab_mag(15849.0) == pytest.approx(31.4 - 2.5 * np.log10(15849.0))
    assert fink.ab_mag(0.0) is None and fink.ab_mag(-5.0) is None and fink.ab_mag(None) is None
    assert fink.ab_mag_err(10000.0, 100.0) == pytest.approx(0.01086, rel=1e-3)
    assert fink.ab_mag_err(-1.0, 1.0) is None


def test_mjd_tai_converts_to_utc_a_few_tens_of_seconds_earlier():
    utc = fink.mjd_tai_to_utc(61235.0)
    assert utc.tzinfo is not None and utc.date().isoformat() == "2026-07-13"
    tai_as_utc = datetime(2026, 7, 14, tzinfo=timezone.utc)
    assert 30.0 < (tai_as_utc - utc).total_seconds() < 40.0  # TAI - UTC = 37 s in 2026


def test_parse_row_normalises_and_rejects_unusable_rows():
    a = fink.parse_row(_row())
    assert a["dia_object_id"] == "170000000000000001" and a["band"] == "r" and a["tns_name"] == "AT 2026tgx"
    assert a["mag"] == pytest.approx(31.4 - 2.5 * np.log10(15849.0)) and a["simbad_otype"] is None  # "Fail" is no value
    assert a["utc_iso"].startswith("2026-07-14") or a["utc_iso"].startswith("2026-07-13")
    assert fink.parse_row(_row(**{"r:ra": None})) is None and fink.parse_row({}) is None
    neg = fink.parse_row(_row(flux=-50.0, **{"r:isNegative": True}))
    assert neg["mag"] is None and neg["is_negative"] is True


def test_recent_alerts_builds_the_query_and_sorts_newest_first():
    sess = _Session(_Resp([_row(oid=1, mjd=61230.0), _row(oid=2, mjd=61235.0), {"r:diaObjectId": 3}]))  # last has no position
    since = datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)
    out = FinkClient(session=sess).recent_alerts("in_tns", n=50, since=since)
    assert [a["dia_object_id"] for a in out] == ["2", "1"]
    url, params, headers, timeout = sess.calls[0]
    assert url == "https://api.lsst.fink-portal.org/api/v1/tags"
    assert params["tag"] == "in_tns" and params["n"] == 50 and params["startdate"] == "2026-10-01 12:30:00"
    assert "r:psfFlux" in params["columns"] and "User-Agent" in headers and timeout > 0


@pytest.mark.parametrize(
    "resp,msg",
    [
        (_Resp(None, status=500, text="boom"), "HTTP 500"),
        (_Resp(ValueError("x"), text="<html>"), "not JSON"),
        (_Resp({"status": "error", "text": "bad name"}), "bad name"),
        (_Resp({"not": "a list"}), "expected a list"),
    ],
)
def test_fink_failures_are_loud(resp, msg):
    with pytest.raises(FinkError, match=msg):
        FinkClient(session=_Session(resp)).recent_alerts("in_tns")


def test_a_network_failure_is_a_fink_error_not_a_raw_requests_error():
    import requests

    with pytest.raises(FinkError, match="request to /api/v1/tags failed"):
        FinkClient(session=_Session(requests.ConnectionError("no network"))).recent_alerts("in_tns")


def test_fink_refuses_odd_tags_and_ids_without_a_request():
    sess = _Session(_Resp([]))
    client = FinkClient(session=sess)
    with pytest.raises(FinkError, match="odd tag"):
        client.recent_alerts("x; drop")
    with pytest.raises(FinkError, match="odd diaObjectId"):
        client.objects_history(["12", "1 OR 1=1"])
    assert sess.calls == [] and client.objects_history([]) == {}


def test_objects_history_groups_by_object_and_sorts_oldest_first():
    rows = [_row(oid=11, mjd=61236.0), _row(oid=12, mjd=61230.0), _row(oid=11, mjd=61233.0), _row(oid=99, mjd=61231.0)]
    sess = _Session(_Resp(rows))
    out = FinkClient(session=sess).objects_history(["11", "12"])
    assert [a["mjd"] for a in out["11"]] == [61233.0, 61236.0] and len(out["12"]) == 1 and "99" not in out
    assert sess.calls[0][1]["diaObjectId"] == "11,12"


def test_base_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("ASTRONOMO_RUBIN_FINK_URL", "http://localhost:9999/")
    assert FinkClient().base_url == "http://localhost:9999"


def test_latest_per_object_and_light_curve_points():
    a1, a2, b = (fink.parse_row(_row(oid=o, mjd=m)) for o, m in ((1, 61230.0), (1, 61234.0), (2, 61232.0)))
    assert [(a["dia_object_id"], a["mjd"]) for a in fink.latest_per_object([a1, b, a2])] == [("1", 61234.0), ("2", 61232.0)]
    neg = fink.parse_row(_row(flux=-5.0, **{"r:isNegative": True}))
    pts = fink.light_curve_points([a2, neg, a1])
    assert [p["mjd"] for p in pts] == [61230.0, 61234.0] and set(pts[0]) == {"utc_iso", "mjd", "band", "mag", "mag_err"}


# --------------------------------------------------------------------------- #
# What Rubin's light curve implies
# --------------------------------------------------------------------------- #
WHEN = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)


def _pts(spec):
    """spec: (days before WHEN, band, mag) -> light-curve points."""
    out = []
    for days, band, mag in spec:
        t = WHEN - timedelta(days=days)
        out.append({"utc_iso": t.isoformat(), "mjd": float(Time(t).tai.mjd), "band": band, "mag": mag, "mag_err": 0.03})
    return sorted(out, key=lambda p: p["mjd"])


expected = RubinFollowupPlugin.expected_from_history


def test_expected_is_the_last_detection_without_a_trend():
    e = expected(_pts([(2.0, "r", 15.8), (2.4, "r", 15.6)]), WHEN)  # only two points
    assert e["expected_mag"] == 15.8 and e["trend_mag_per_day"] is None and e["basis"] == "last Rubin detection"
    assert e["age_d"] == pytest.approx(2.0, abs=0.01) and expected([], WHEN) is None


def test_expected_extrapolates_a_clear_trend_but_not_too_far():
    pts = _pts([(9.0, "r", 15.4), (7.0, "r", 15.7), (5.0, "r", 16.0), (3.0, "r", 16.3)])  # +0.15 mag/day
    e = expected(pts, WHEN)
    assert e["trend_mag_per_day"] == pytest.approx(0.15, abs=0.005) and e["expected_mag"] == pytest.approx(16.75, abs=0.03)  # 16.3 + 3 d * 0.15
    assert "linear trend of 4" in e["basis"] and e["expected_err"] > 0.03
    far = expected(pts, WHEN + timedelta(days=20))  # age 23 d >> 2x the 6 d baseline: do not extrapolate
    assert far["trend_mag_per_day"] is None and far["expected_mag"] == 16.3
    wild = expected(_pts([(1.0, "r", 15.0), (0.7, "r", 16.0), (0.4, "r", 17.0)]), WHEN)  # 3.3 mag/day: noise, not a trend
    assert wild["trend_mag_per_day"] is None


def test_expected_prefers_r_but_never_a_stale_band():
    both = expected(_pts([(2.0, "g", 16.0), (1.0, "r", 16.5)]), WHEN)
    assert both["band"] == "r" and both["last_mag"] == 16.5
    stale_r = expected(_pts([(200.0, "r", 16.0), (1.0, "g", 17.5)]), WHEN)  # the r-band point is 200 d older
    assert stale_r["band"] == "g" and stale_r["last_mag"] == 17.5


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
classify = RubinFollowupPlugin._classify
EXP = {"expected_mag": 16.0, "expected_err": 0.05}
CAL = {"zero_point": 25.0}


def _meas(**kw):
    return {"status": "ok", "detected": False, "marginal": False, **kw}


@pytest.mark.parametrize(
    "meas,calib,exp,klass",
    [
        (_meas(detected=True, mag=16.2, mag_err=0.05), CAL, EXP, "detected_consistent_with_rubin"),
        (_meas(detected=True, mag=15.2, mag_err=0.05), CAL, EXP, "detected_brighter_than_expected"),
        (_meas(detected=True, mag=17.0, mag_err=0.05), CAL, EXP, "detected_fainter_than_expected"),
        (_meas(detected=True, mag=16.2, mag_err=0.05), CAL, None, "detected_no_rubin_reference"),
        (_meas(marginal=True, mag=17.0, mag_err=0.3), CAL, EXP, "marginal_detection"),
        (_meas(limit_mag_5sigma=18.5), CAL, EXP, "not_detected_expected_visible"),
        (_meas(limit_mag_5sigma=16.3), CAL, EXP, "not_detected_uninformative"),  # we only reach 0.3 mag below it
        (_meas(limit_mag_5sigma=18.5), CAL, None, "not_detected"),
        (_meas(detected=True), {"zero_point": None}, EXP, "detected_uncalibrated"),
        (_meas(), {"zero_point": None}, EXP, "not_detected_uncalibrated"),
        ({"status": "edge"}, CAL, EXP, "not_covered"),
        ({"status": "outside_frame"}, CAL, EXP, "not_covered"),
    ],
)
def test_classification(meas, calib, exp, klass):
    assert classify(meas, calib, exp)[0] == klass


def test_tolerance_grows_with_the_uncertainty_but_never_below_the_floor():
    _, delta, tol = classify(_meas(detected=True, mag=16.4, mag_err=0.02), CAL, EXP)
    assert delta == pytest.approx(0.4) and tol == pytest.approx(0.5)
    _, _, tol_big = classify(_meas(detected=True, mag=16.4, mag_err=0.4), CAL, EXP)
    assert tol_big == pytest.approx(3 * np.hypot(0.4, 0.05)) and tol_big > 1.0


# --------------------------------------------------------------------------- #
# Transient scene (plugin-owned simulation)
# --------------------------------------------------------------------------- #
EPOCH0 = "2026-10-21T04:00:00+00:00"


def _target(mag=16.0, in_gaia=False, **extra):
    sim = {"seed": 5, "epoch0_iso": EPOCH0, "pointing_error_arcsec": [10.0, -5.0],
           "transient": {"dx_px": 12.0, "dy_px": -7.0, "mag": mag, "in_gaia": in_gaia}}
    sim.update(extra)
    return {"name": "T", "ra_deg": 120.0, "dec_deg": 5.0, "sim": sim}


def _counts(img, xy):
    x, y = (int(round(v)) for v in xy)
    box = img[y - 8:y + 9, x - 8:x + 9].astype(float)
    return float(box.sum() - np.median(img) * box.size)


def test_scene_draws_the_transient_but_does_not_tell_gaia_unless_it_is_a_catalogued_star():
    target = _target()
    scene = TransientScene.from_target(target)
    sky = MockSky.from_target(target, scene=scene)
    pix = scene.pixel(sky)
    img = sky.render(sky.epoch0, 10.0, 0)
    assert _counts(img, pix) > 1500  # a G=16 star is ~4000 counts
    assert len(sky.star_catalog()["ra"]) == len(sky.stars()["x"])  # a transient is not in Gaia
    catalogued = TransientScene.from_target(_target(in_gaia=True))
    assert len(MockSky.from_target(_target(in_gaia=True), scene=catalogued).star_catalog()["ra"]) == len(sky.stars()["x"]) + 1


def test_scene_with_no_visible_source_draws_and_reports_nothing():
    target = _target(mag=None, in_gaia=True)  # even "in Gaia" cannot report a star that is not there
    scene = TransientScene.from_target(target)
    sky = MockSky.from_target(target, scene=scene)
    plain = MockSky.from_target(target)
    assert np.array_equal(sky.render(sky.epoch0, 10.0, 0), plain.render(plain.epoch0, 10.0, 0))
    assert len(sky.star_catalog()["ra"]) == len(sky.stars()["x"])
    assert TransientScene.from_target({"name": "x", "ra_deg": 1, "dec_deg": 1}).pixel(MockSky.from_target(_target())) is None


def test_scene_drift_moves_the_source_with_the_field():
    target = _target(drift_px_per_hr=[60.0, -40.0])
    scene = TransientScene.from_target(target)
    sky = MockSky.from_target(target, scene=scene)
    x, y = scene.pixel(sky)
    later = sky.epoch0 + timedelta(minutes=30)
    assert _counts(sky.render(later, 10.0, 3), (x + 30.0, y - 20.0)) > 1500


# --------------------------------------------------------------------------- #
# Frames -> stack -> calibration -> measurement (shared, built once)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    root = tmp_path_factory.mktemp("rubin")
    settings = get_settings(data_dir=root / "data", mock_mode=True, offline_services=True, mock_capture_seconds=0.0)
    settings.ensure_dirs()
    plugin = load_plugin("rubin_followup", settings)
    targets = {t["name"]: t for t in plugin.validated_targets()}
    plan = plugin.validated_exposure_plan()
    out = {"settings": settings, "plugin": plugin, "targets": targets, "plan": plan, "findings": {}, "paths": {}, "xref": {}}
    catalogs = CatalogClient(settings)
    with SeestarController(settings, sleep_fn=lambda s: None) as tel:
        for name, target in targets.items():
            paths = tel.capture_sequence(target, plan, root / name.split()[1], scene=plugin.mock_scene(target))
            out["paths"][name] = paths
            out["findings"][name] = plugin.analyze_frames(paths)
            out["xref"][name] = plugin.cross_reference(out["findings"][name], catalogs, target)
    return out


def test_plugin_contract_and_simulated_targets(sim):
    plugin, targets, plan = sim["plugin"], sim["targets"], sim["plan"]
    assert plugin.name == "rubin_followup" and "rubin_followup" in available_plugins()
    assert set(targets) == {FADING, GONE, RISING}
    assert plan["count"] >= 10 and plan["interval_s"] == 0.0 and plan["filter"] == "IRCUT"
    for t in targets.values():
        assert t["rubin"]["source"] == "simulated" and t["rubin"]["history"] and "T" not in t["rubin"]["dia_object_id"]


def test_the_alert_position_is_where_the_simulated_transient_is_drawn(sim):
    """Alert RA/Dec (the target position) and the scene's pixel agree: the mount's pointing error is consistent."""
    for name, target in sim["targets"].items():
        scene = TransientScene.from_target(target)
        sky = MockSky.from_target(target, scene=scene)
        ra, dec = scene.radec(sky)
        sep = np.hypot((ra - target["ra_deg"]) * np.cos(np.radians(dec)), dec - target["dec_deg"]) * 3600.0
        assert sep < 1.0, name


def test_frames_are_registered_and_stacked(sim):
    f = sim["findings"][FADING]
    assert f["n_frames_used"] == f["n_frames"] == sim["plan"]["count"]
    assert f["total_exposure_s"] == pytest.approx(sim["plan"]["count"] * sim["plan"]["exposure_s"])
    assert Path(f["stack"]["path"]).is_file() and f["stack"]["rms_adu"] < 12.0  # mean of 20 frames: well below one frame's ~34
    # the field drifted by (60, -40) px/hr: registration undoes it ("reference = frame + shift")
    span_h = f["span_s"] / 3600.0
    assert f["drift_px"] == pytest.approx([-60.0 * span_h, 40.0 * span_h], abs=0.8)
    assert f["n_sources"] > 100 and f["min_transparency"] > 0.9 and f["hot_pixels_repaired"] > 0
    assert not f["detections"]  # the target-specific result lives in the cross-reference, not here


def test_gaia_calibration_recovers_the_pointing_error_and_zero_point(sim):
    cal = sim["xref"][FADING]["calibration"]
    assert cal["status"] == "calibrated" and cal["n_matches"] > 50
    assert cal["offset_arcsec"] == pytest.approx(np.hypot(28.0, -17.0), abs=1.5)  # the injected pointing error
    assert cal["zero_point"] == pytest.approx(25.0, abs=0.05)  # 16 + 2.5 log10(4000 counts per frame)
    assert cal["zp_scatter"] < 0.06 and cal["zp_n"] > 30 and cal["zp_err"] >= 0.02


def test_a_faded_supernova_is_measured_and_agrees_with_rubin(sim):
    x = sim["xref"][FADING]
    entry, meas = x["entries"][0], x["measurement"]
    assert entry["classification"] == "detected_consistent_with_rubin"
    assert entry["mag"] == pytest.approx(16.8, abs=0.12) and 0.0 < entry["mag_err"] < 0.15
    assert meas["snr"] > 15 and meas["detected"] and meas["limit_mag_5sigma"] > entry["mag"] + 1.0
    assert x["rubin"]["trend_mag_per_day"] == pytest.approx(0.155, abs=0.02)
    assert abs(entry["delta_mag"]) < 0.2 and "consistent" in x["summary"]
    # the measurement position is the scene's transient (astrometry and alert coordinates agree to <1 px)
    scene = TransientScene.from_target(sim["targets"][FADING])
    px, py = scene.pixel(MockSky.from_target(sim["targets"][FADING], scene=scene))
    assert np.hypot(meas["x"] - px, meas["y"] - py) < 1.0


def test_an_alert_that_vanished_gives_a_limit_not_a_measurement(sim):
    x = sim["xref"][GONE]
    entry = x["entries"][0]
    assert entry["classification"] == "not_detected_expected_visible"
    assert entry["mag"] is None and 18.0 < entry["limit_mag_5sigma"] < 19.5
    assert entry["delta_mag"] > 2.0 and x["measurement"]["snr"] < 3.0
    assert "should have been visible" in x["summary"] and x["light_curve"]["ours"]["detected"] is False


def test_a_source_rising_faster_than_rubin_implied_is_flagged_brighter(sim):
    x = sim["xref"][RISING]
    entry = x["entries"][0]
    assert entry["classification"] == "detected_brighter_than_expected"
    assert entry["mag"] == pytest.approx(15.7, abs=0.12) and entry["delta_mag"] < -0.5
    assert x["rubin"]["trend_mag_per_day"] < -0.5  # Rubin saw it rising


def test_cross_reference_writes_a_figure_and_the_results_are_plain_json(sim):
    for name in (FADING, GONE, RISING):
        x = sim["xref"][name]
        assert Path(x["figures"][0]).is_file() and x["services"]["rubin_alerts"] == "simulated"
        json.dumps(x)  # no numpy types leak into what the graph will store


def test_a_catalogued_star_at_the_alert_position_is_flagged(sim):
    plugin, name = sim["plugin"], FADING
    target = {**sim["targets"][name]}
    target["sim"] = {**target["sim"], "transient": {**target["sim"]["transient"], "in_gaia": True}}
    x = plugin.cross_reference(sim["findings"][name], CatalogClient(sim["settings"]), target)
    assert x["gaia_counterpart"]["g_mag"] == pytest.approx(16.8, abs=0.01)
    assert any("Gaia DR3 star" in n and "difference flux" in n for n in x["notes"])
    assert not sim["xref"][name]["gaia_counterpart"]  # absent without the catalogue entry


def test_an_old_rubin_detection_is_called_out(sim):
    plugin, name = sim["plugin"], GONE
    target = {**sim["targets"][name]}
    old = [{**p, "utc_iso": (datetime.fromisoformat(p["utc_iso"]) - timedelta(days=20)).isoformat(), "mjd": p["mjd"] - 20.0}
           for p in target["rubin"]["history"]]
    target["rubin"] = {**target["rubin"], "history": old}
    x = plugin.cross_reference(sim["findings"][name], CatalogClient(sim["settings"]), target)
    assert any("may have changed a lot" in n for n in x["notes"])


def test_too_few_frames_degrade_gracefully_and_cross_reference_says_so(sim):
    findings = photometry.analyze(sim["paths"][FADING][:3])
    assert "stack" not in findings and any("frame" in w for w in findings["warnings"])
    render_payload(findings["agent_summary"])
    x = sim["plugin"].cross_reference(findings, CatalogClient(sim["settings"]), sim["targets"][FADING])
    assert x["entries"][0]["classification"] == "not_measured" and x["summary"].startswith("Not measured")


def test_calibration_fails_loudly_when_the_catalogue_is_empty_or_the_field_is_rotated(sim):
    f = sim["findings"][FADING]
    assert photometry.calibrate(f["astrometry"], f["sources"], [])["status"] == "failed"
    rng = np.random.default_rng(0)
    fake = [{"ra_deg": 52.3 + rng.uniform(-0.2, 0.2), "dec_deg": -27.8 + rng.uniform(-0.2, 0.2), "g_mag": 14.0, "source_id": i}
            for i in range(60)]  # random positions: no consistent pattern
    cal = photometry.calibrate(f["astrometry"], f["sources"], fake)
    assert cal["status"] == "failed" and "pattern" in cal["reason"]


# --------------------------------------------------------------------------- #
# Photometry building blocks
# --------------------------------------------------------------------------- #
def test_hot_pixel_repair_removes_spikes_but_leaves_stars_alone():
    rng = np.random.default_rng(1)
    img = rng.normal(0.0, 10.0, (200, 200)).astype(np.float32)
    yy, xx = np.mgrid[0:200, 0:200]
    img += (3000.0 * np.exp(-((xx - 100) ** 2 + (yy - 100) ** 2) / (2 * 1.27**2))).astype(np.float32)  # a star, peak 3000
    star_peak = float(img[100, 100])
    img[40, 40] += 3000.0
    img[150, 20] += 3000.0
    n = photometry.repair_hot_pixels(img, 10.0, 8.0)
    assert n == 2 and abs(img[40, 40]) < 60 and abs(img[150, 20]) < 60
    assert img[100, 100] == pytest.approx(star_peak)


def test_paste_applies_integer_shifts_and_counts_coverage():
    acc, w = np.zeros((6, 8)), np.zeros((6, 8))
    data = np.arange(48, dtype=float).reshape(6, 8)
    photometry._paste(acc, w, data, 2.0, -1.0)  # content moves right 2, up 1
    assert acc[1, 2] == data[2, 0] and acc[3, 7] == data[4, 5] and w.sum() == 5 * 6
    assert w[5, :].sum() == 0 and w[:, :2].sum() == 0  # the uncovered strips stay at weight 0
    photometry._paste(acc, w, data, 100.0, 0.0)  # entirely off the frame: ignored
    assert w.sum() == 30


def test_measure_target_reports_unusable_positions(sim):
    f = sim["findings"][FADING]
    from tools.imaging import load_image

    stack, _ = load_image(f["stack"]["path"])
    args = (stack, f["stack"]["rms_adu"], f["stack"]["n_frames"])
    assert photometry.measure_target(*args, (-5.0, 100.0), f["sources"], 25.0)["status"] == "outside_frame"
    assert photometry.measure_target(*args, (4.0, 100.0), f["sources"], 25.0)["status"] == "edge"
    assert photometry.measure_target(*args, (float("nan"), 3.0), f["sources"], 25.0)["status"] == "outside_frame"
    blank = photometry.measure_target(*args, (300.0, 300.0), [], None)  # no zero point: signal-to-noise only
    assert blank["status"] == "ok" and "mag" not in blank and "limit_mag_5sigma" not in blank


def test_empirical_noise_tracks_the_real_scatter_on_empty_sky():
    rng = np.random.default_rng(3)
    img = rng.normal(0.0, 5.0, (400, 400)).astype(np.float32)
    p = photometry.PhotometryParams()
    sigma, n = photometry.empirical_noise(img, (200.0, 200.0), np.empty((0, 2)), 5.0, p, 1, seed=1)
    analytic = np.sqrt(78.5 * 25.0 * (1 + 78.5 / (np.pi * (14**2 - 9**2))))
    assert n >= 60 and sigma == pytest.approx(analytic, rel=0.25)
    assert photometry.empirical_noise(img[:60, :60], (30.0, 30.0), np.empty((0, 2)), 5.0, p, 1)[0] is None  # no room for apertures


# --------------------------------------------------------------------------- #
# Language-model firewall
# --------------------------------------------------------------------------- #
def test_summary_passes_the_data_firewall_and_leaves_the_heavy_data_out(sim):
    plugin = sim["plugin"]
    for name in (FADING, GONE, RISING):
        findings, xref, target = sim["findings"][name], sim["xref"][name], sim["targets"][name]
        digest = plugin.summarize_for_llm(findings, xref, target)
        text = render_payload(digest)  # raises if it breaks any data-reduction rule
        assert len(text) < 1800 and digest["result"]["classification"] == xref["entries"][0]["classification"]
        for key in HEAVY_KEYS:
            assert f'"{key}"' not in text
    heavy = {k: v for k, v in sim["findings"][FADING].items() if k != "agent_summary"}
    assert len(json.dumps(heavy, default=str)) > 10 * len(text)
    with pytest.raises(LLMPayloadError):
        render_payload(heavy)
    assert "difference" in plugin.llm_guidance() and "never a discovery" in plugin.llm_guidance()


# --------------------------------------------------------------------------- #
# Live target selection (fake Fink client)
# --------------------------------------------------------------------------- #
def _alert(oid, mag, age_d=1.0, band="r", rel=0.99, neg=False, tns=None, ra=100.0, dec=-20.0):
    when = datetime.now(timezone.utc) - timedelta(days=age_d)
    flux = 10 ** ((31.4 - mag) / 2.5)
    return {"dia_object_id": oid, "dia_source_id": "1", "ra_deg": ra, "dec_deg": dec, "band": band, "mjd": float(Time(when).tai.mjd),
            "utc_iso": when.isoformat(), "flux_njy": flux, "flux_err_njy": flux * 0.01, "mag": mag, "mag_err": 0.011,
            "reliability": rel, "is_negative": neg, "tns_name": tns, "simbad_otype": None}


class _FakeFink:
    def __init__(self, alerts=(), history=None, newest=None, fail=None):
        self.alerts, self.history, self.newest, self.fail, self.calls = list(alerts), history or {}, newest, fail, []

    def recent_alerts(self, tag, n=500, since=None):
        self.calls.append(("recent", tag, n, since))
        if self.fail:
            raise FinkError(self.fail)
        return list(self.alerts)

    def objects_history(self, ids):
        self.calls.append(("history", list(ids)))
        if self.fail:
            raise FinkError(self.fail)
        return {i: self.history.get(i, []) for i in ids}

    def object_history(self, oid):
        return self.objects_history([oid])[oid]

    def newest_alert_utc(self, tag):
        return self.newest


def _live(settings, monkeypatch, client, **env):
    monkeypatch.setenv("ASTRONOMO_RUBIN_MIN_ALT_DEG", "-90")  # sky position is irrelevant to these tests
    for k, v in env.items():
        monkeypatch.setenv(f"ASTRONOMO_RUBIN_{k}", v)
    plugin = load_plugin("rubin_followup", replace(settings, offline_services=False))
    monkeypatch.setattr(plugin, "_fink_client", lambda: client)
    return plugin


def test_live_targets_are_filtered_deduplicated_and_ranked(settings, monkeypatch):
    alerts = [
        _alert("1", 15.0, age_d=3.0, tns="AT 1"), _alert("1", 14.8, age_d=0.5, tns="AT 1"),  # same object: the newer alert wins
        _alert("2", 17.5), _alert("3", 15.5, rel=0.3), _alert("4", 15.2, neg=True), _alert("5", 15.3, age_d=20.0),
        _alert("6", 15.1, age_d=2.0),
    ]
    client = _FakeFink(alerts)
    plugin = _live(settings, monkeypatch, client)
    targets = plugin.generate_targets()
    assert [t["name"] for t in targets] == ["AT 1", "LSST 6"]
    assert targets[0]["priority"] > targets[1]["priority"]  # the graph observes by priority: brighter and fresher first
    r = targets[0]["rubin"]
    assert r["last_mag"] == pytest.approx(14.8, abs=0.001) and r["origin"] == "filter" and r["source"] == "fink-lsst" and "history" not in r
    kind, tag, n, since = client.calls[0]
    assert tag == "extragalactic_lt20mag_candidate" and n == 500 and datetime.now(timezone.utc) - since > timedelta(days=13)
    assert {c[1] for c in client.calls if c[0] == "recent"} == {"extragalactic_lt20mag_candidate", "in_tns"}


def test_live_watchlist_objects_skip_the_brightness_and_age_cuts(settings, monkeypatch):
    client = _FakeFink([_alert("1", 15.0)], history={"77": [_alert("77", 17.0, age_d=40.0), _alert("77", 19.0, age_d=30.0)]})
    plugin = _live(settings, monkeypatch, client, OBJECT_IDS="77")
    targets = {t["name"]: t for t in plugin.generate_targets()}
    assert set(targets) == {"LSST 1", "LSST 77"}
    w = targets["LSST 77"]["rubin"]
    assert w["origin"] == "watchlist" and w["last_mag"] == pytest.approx(19.0, abs=0.001)  # the newest detection
    assert ("history", ["77"]) in client.calls


def test_live_targets_say_so_when_the_alert_stream_is_dark(settings, monkeypatch):
    plugin = _live(settings, monkeypatch, _FakeFink([], newest=datetime(2026, 7, 14, tzinfo=timezone.utc)))
    with pytest.raises(CatalogError, match=r"no Rubin alerts from the last 14 days.*2026-07-14.*alert-stream-status"):
        plugin.generate_targets()


def test_live_targets_fail_loudly_when_fink_is_down(settings, monkeypatch):
    plugin = _live(settings, monkeypatch, _FakeFink(fail="HTTP 503"))
    with pytest.raises(CatalogError, match="every Fink query failed.*503"):
        plugin.generate_targets()


def test_live_targets_explain_why_nothing_qualified(settings, monkeypatch):
    plugin = _live(settings, monkeypatch, _FakeFink([_alert("1", 19.5), _alert("2", 18.0), _alert("3", 15.0, rel=0.2)]))
    with pytest.raises(CatalogError) as exc:
        plugin.generate_targets()
    msg = str(exc.value)
    assert "3 Rubin alert(s)" in msg and "2 fainter than limit" in msg and "1 low reliability" in msg and "mag 15-16" in msg


def test_live_targets_respect_visibility(settings, monkeypatch):
    monkeypatch.setenv("ASTRONOMO_RUBIN_MIN_ALT_DEG", "89.9")  # essentially nothing is that high
    plugin = load_plugin("rubin_followup", replace(settings, offline_services=False))
    monkeypatch.setattr(plugin, "_fink_client", lambda: _FakeFink([_alert("1", 15.0)]))
    with pytest.raises(CatalogError, match="1 not observable"):
        plugin.generate_targets()


def test_live_history_falls_back_to_the_selecting_alert_when_fink_fails(settings, monkeypatch):
    plugin = _live(settings, monkeypatch, _FakeFink(fail="HTTP 500"))
    target = {"name": "x", "rubin": {"dia_object_id": "1", "last_mag": 16.1, "last_mag_err": 0.02, "last_band": "g",
                                      "last_utc_iso": datetime.now(timezone.utc).isoformat()}}
    notes: list[str] = []
    pts = plugin._history(target, notes)
    assert len(pts) == 1 and pts[0]["mag"] == 16.1 and pts[0]["band"] == "g" and any("unavailable" in n for n in notes)
    fetched = _live(settings, monkeypatch, _FakeFink(history={"1": [_alert("1", 16.0), _alert("1", 16.2, age_d=0.5)]}))
    pts = fetched._history(target, [])  # the live light curve replaces the single selecting alert
    assert [round(p["mag"], 2) for p in pts] == [16.0, 16.2] and {"utc_iso", "mjd", "band", "mag"} <= set(pts[0])
