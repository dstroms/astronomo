"""Transit-finder plugin: model, simulated scene, photometry/fit recovery, cross-reference, archive and graph."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.time import Time

from config import get_settings
from core.digest import LLMPayloadError, render_payload
from plugins import load_plugin
from plugins.transit_finder import archive, photometry
from plugins.transit_finder.scene import TransitScene
from plugins.transit_finder.transit_model import transit_flux, transit_shape
from tools.astrometry import bjd_tdb_to_utc, utc_to_bjd_tdb
from tools.catalogs import CatalogClient, CatalogError
from tools.mock_sky import MockSky
from tools.telescope import SeestarController

NO_SKY_LIMITS = {"min_altitude_deg": -90.0, "min_sun_separation_deg": 0.0, "moon_avoid_deg": 0.0, "moon_floor_deg": 0.0}  # where the sky is on the day the suite runs must not decide a test

HEAVY_KEYS = ("light_curve", "reference_stars", "wcs", "frame_paths", "dx_px", "baseline_coeffs", "figures")


# --------------------------------------------------------------------------- #
# Transit model
# --------------------------------------------------------------------------- #
def test_trapezoid_shape_depth_and_symmetry():
    t = np.linspace(-5000, 5000, 2001)
    f = transit_flux(t, 0.0, 0.01, 4000.0, 500.0)
    assert f.min() == pytest.approx(0.99) and f.max() == 1.0
    assert np.allclose(f, f[::-1])  # symmetric about t_mid
    assert transit_flux(2100.0, 0.0, 0.01, 4000.0, 500.0) == 1.0  # just outside T14/2
    assert transit_flux(1000.0, 0.0, 0.01, 4000.0, 500.0) == pytest.approx(0.99)  # on the floor
    assert 0.99 < transit_flux(1750.0, 0.0, 0.01, 4000.0, 500.0) < 1.0  # on the ramp
    assert transit_shape(np.array([0.0]), 0.0, 10.0, 1e9)[0] == 1.0  # absurd ingress is clamped


# --------------------------------------------------------------------------- #
# Transit scene: host star and systematics (plugin-owned simulation)
# --------------------------------------------------------------------------- #
EPOCH0 = "2026-10-21T04:00:00+00:00"


def _sim_target(depth=0.02, **extra):
    sim = {"seed": 12, "epoch0_iso": EPOCH0, "pointing_error_arcsec": [10.0, -5.0],
           "transit": {"dx_px": 5.0, "dy_px": -3.0, "mag": 11.4, "depth": depth, "t_mid_s": 3600.0,
                       "duration_s": 3600.0, "ingress_s": 400.0}}
    sim.update(extra)
    return {"name": "T", "ra_deg": 120.0, "dec_deg": 5.0, "sim": sim}


def _sky(target):
    """The scene and the generic sky it extends, built the way the graph builds them."""
    scene = TransitScene.from_target(target)
    return scene, MockSky.from_target(target, scene=scene)


def _host_counts(img, host):
    x, y = (int(round(v)) for v in host)
    box = img[y - 8:y + 9, x - 8:x + 9].astype(float)
    return float(box.sum() - np.median(img) * box.size)


def test_mock_host_flux_follows_the_transit():
    scene, sky = _sky(_sim_target())
    t0 = sky.epoch0
    assert scene.host_relative_flux(sky, t0 + timedelta(seconds=3600)) == pytest.approx(0.98)
    assert scene.host_relative_flux(sky, t0) == 1.0 and scene.host_relative_flux(sky, t0 + timedelta(hours=3)) == 1.0
    out_of, in_to = sky.render(t0, 10.0, 0), sky.render(t0 + timedelta(seconds=3600), 10.0, 1)
    ratio = _host_counts(in_to, scene.host_pixel(sky)) / _host_counts(out_of, scene.host_pixel(sky))
    assert 0.965 < ratio < 0.995  # 2 % dip, ~0.3 % photon noise per frame


def test_mock_host_is_in_the_star_catalogue_and_other_skies_are_unchanged():
    _, with_host = _sky(_sim_target())
    n_stars = len(with_host.stars()["x"])
    cat = with_host.star_catalog()
    assert len(cat["ra"]) == n_stars + 1 and cat["mag"][-1] == pytest.approx(11.4)
    no_transit = {"name": "T", "ra_deg": 120.0, "dec_deg": 5.0, "sim": {"seed": 12, "epoch0_iso": EPOCH0}}
    scene, plain = _sky(no_transit)
    assert scene.host_pixel(plain) is None and len(plain.star_catalog()["ra"]) == len(plain.stars()["x"])
    assert scene.host_relative_flux(plain, plain.epoch0) == 1.0


def test_mock_drift_and_transparency_are_common_to_all_stars():
    drift = _sim_target(depth=0.0, drift_px_per_hr=[6.0, -4.0], transparency_trend_per_hr=-0.1)
    scene, sky = _sky(drift)
    later = sky.epoch0 + timedelta(hours=2)
    img0, img2 = sky.render(sky.epoch0, 10.0, 0), sky.render(later, 10.0, 1)
    x, y = scene.host_pixel(sky)
    c0 = _host_counts(img0, (x, y))
    c2 = _host_counts(img2, (x + 12.0, y - 8.0))  # 2 h of drift at (6, -4) px/h
    assert c2 / c0 == pytest.approx(0.8, abs=0.03)  # -10 %/h transparency trend over 2 h


# --------------------------------------------------------------------------- #
# Light-curve fit (no images: fast, statistical)
# --------------------------------------------------------------------------- #
T = np.arange(30) * 480.0 + 5.0
SIGMA = 0.0024


def _lc(rng, depth, t_mid=7200.0, duration=7200.0, slope=-0.002):
    f = transit_flux(T, t_mid, depth, duration, 900.0) * (1 + slope * T / 3600.0) + rng.normal(0, SIGMA, len(T))
    return f, np.full(len(T), SIGMA)


def test_fit_recovers_an_injected_transit_without_bias():
    rng, p = np.random.default_rng(1), photometry.PhotometryParams()
    fits = [photometry.fit_transit(T, *_lc(rng, 0.014), p) for _ in range(40)]
    depth = np.array([f["depth"] for f in fits])
    err = np.mean([f["depth_err"] for f in fits])
    assert depth.mean() == pytest.approx(0.014, abs=0.0006)
    assert depth.std() == pytest.approx(err, rel=0.35)  # the quoted error is about right
    assert np.mean([f["t_mid_s"] for f in fits]) == pytest.approx(7200.0, abs=100.0)
    assert all(f["snr"] > 5 and f["delta_bic"] > 10 for f in fits)


@pytest.mark.slow
def test_fit_makes_no_false_detections_in_pure_noise():
    rng, p = np.random.default_rng(2), photometry.PhotometryParams()
    flagged = 0
    for _ in range(150):
        fit = photometry.fit_transit(T, *_lc(rng, 0.0), p)
        flagged += bool(fit.get("ok") and fit["snr"] >= p.min_snr and fit["delta_bic"] >= p.min_delta_bic and fit["n_in"] >= p.min_in_transit_points)
    assert flagged <= 2


def test_fit_reports_coverage_for_a_transit_cut_off_by_the_end_of_the_data():
    fit = photometry.fit_transit(T, *_lc(np.random.default_rng(3), 0.014, t_mid=12500.0), photometry.PhotometryParams())
    assert fit["ok"] and fit["n_post"] < 3 and fit["n_pre"] > 10


def test_fixed_ephemeris_depth_gives_an_upper_limit_for_a_flat_light_curve():
    rng = np.random.default_rng(4)
    f, e = _lc(rng, 0.0)
    res = photometry.fit_fixed_transit(T, f, e, 7200.0, 7200.0, 720.0)
    assert abs(res["depth"]) < 3 * res["depth_err"] and res["n_in"] >= 10
    f2, e2 = _lc(rng, 0.014)
    assert photometry.fit_fixed_transit(T, f2, e2, 7200.0, 7200.0, 720.0)["depth"] == pytest.approx(0.014, abs=0.004)


def test_red_noise_factor_is_one_for_white_noise_and_larger_for_correlated_noise():
    rng = np.random.default_rng(5)
    assert photometry.red_noise_beta(rng.normal(size=200)) < 1.3
    slow = np.convolve(rng.normal(size=260), np.ones(8) / 8.0, mode="valid")[:200]
    assert photometry.red_noise_beta(slow / slow.std()) > 1.5


def test_a_series_shorter_than_the_shortest_dip_returns_no_fit_instead_of_raising():
    # A live variable-star visit: 40 frames about 37 s apart is 24 min, under the 30 min minimum duration.
    t = np.arange(40) * 37.0
    f = 1.0 + np.random.default_rng(6).normal(0, 0.002, 40)
    fit = photometry.fit_transit(t, f, np.full(40, 0.002), photometry.PhotometryParams())
    assert fit["ok"] is False and "too short" in fit["reason"]


def _soft_star_frame(tmp_path, fwhm_px, name="soft.fits", n_stars=40, seed=11):
    """Gaussian stars on a sky with the per-site offsets of the Seestar's raw colour mosaic (first real night)."""
    from astropy.io import fits

    rng = np.random.default_rng(seed)
    ny, nx = 600, 500
    yy, xx = np.mgrid[0:ny, 0:nx]
    img = rng.normal(2500.0, 60.0, (ny, nx))
    sigma = fwhm_px / 2.355
    for _ in range(n_stars):
        x, y, amp = rng.uniform(40, nx - 40), rng.uniform(40, ny - 40), rng.uniform(300, 1500)
        img += amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))
    for (row, col), offset in {(0, 0): 100, (0, 1): -60, (1, 0): -170, (1, 1): 95}.items():
        img[row::2, col::2] += offset  # the four mosaic sites sit at different levels on a real frame
    path = tmp_path / name
    fits.PrimaryHDU(np.clip(img, 0, 65535).astype(np.uint16)).writeto(path, overwrite=True)
    return str(path)


def test_a_soft_field_is_re_detected_with_a_wider_kernel_and_the_aperture_grows(tmp_path):
    from tools.imaging import load_image

    path = _soft_star_frame(tmp_path, 8.5)
    p = photometry.PhotometryParams()
    ref = photometry._load_frame(path, 0, p, load_image)
    assert len(ref.xy) < 8  # the 3 px finder misses broad stars (the first real night found 4)
    wide, ref2, note = photometry._widen_if_soft(ref, p, load_image)
    assert len(ref2.xy) >= 8 and wide.fwhm_px > 3.0 and note and "wider" in note
    assert wide.aperture_radius_px > p.aperture_radius_px and wide.annulus_px[0] > p.annulus_px[0]


def test_a_finder_that_misses_the_target_is_widened_even_when_other_stars_are_found(tmp_path):
    """Hat-P-23 on 2026-11-18: 159 stars at 3 px, and the target was not one of them.

    The target is a colour-mosaic star, bright on one diagonal of each 2x2 cell.  The 3 px finder
    rejects that shape.  A 6 px finder accepts it.  The other stars are ordinary and keep the count
    above the "too few stars" threshold, which used to be the only reason to widen.
    """
    import math

    from astropy.io import fits
    from tools.imaging import load_image

    rng = np.random.default_rng(4)
    ny, nx = 360, 360
    yy, xx = np.mgrid[0:ny, 0:nx]
    img = rng.normal(2500.0, 25.0, (ny, nx))
    target = (180.0, 180.0)
    sharp = 2.2 / 2.355
    for _ in range(35):
        x, y = rng.uniform(25, nx - 25), rng.uniform(25, ny - 25)
        if math.hypot(x - target[0], y - target[1]) < 70:
            continue
        img += 1400 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sharp**2))
    sigma = 5.0 / 2.355
    star = np.exp(-((xx - target[0]) ** 2 + (yy - target[1]) ** 2) / (2 * sigma**2))
    green = (yy.astype(int) % 2) == (xx.astype(int) % 2)
    img += 800 * star * np.where(green, 1.0, 0.25)
    path = tmp_path / "missed_target.fits"
    fits.PrimaryHDU(np.clip(img, 0, 65535).astype(np.uint16)).writeto(path, overwrite=True)

    p = photometry.PhotometryParams(target_xy=target)
    ref = photometry._load_frame(str(path), 0, p, load_image)
    assert len(ref.xy) >= 8
    assert photometry._target_unseen(ref.xy, p)
    wide, ref2, note = photometry._widen_if_soft(ref, p, load_image)
    assert note and "none within" in note and wide.fwhm_px > p.fwhm_px
    assert not photometry._target_unseen(ref2.xy, wide)


def test_a_focused_field_is_left_alone(tmp_path):
    from tools.imaging import load_image

    path = _soft_star_frame(tmp_path, 3.0, name="sharp.fits")
    p = photometry.PhotometryParams()
    ref = photometry._load_frame(path, 0, p, load_image)
    assert len(ref.xy) >= 8
    same, ref2, note = photometry._widen_if_soft(ref, p, load_image)
    assert same is p and ref2 is ref and note is None


def test_a_failed_star_selection_says_why_in_the_summary(tmp_path):
    from astropy.io import fits

    # Blank frames: nothing to detect, so the reason is "only N stars", not a generic message.
    paths = []
    for k in range(14):
        path = tmp_path / f"blank_{k:02d}.fits"
        hdr = fits.Header()
        hdr["DATE-OBS"] = (datetime(2026, 11, 17, 1, 0, 0) + timedelta(seconds=40 * k)).isoformat()
        hdr["EXPTIME"] = 10.0
        fits.PrimaryHDU(np.full((300, 300), 2500, dtype=np.uint16), hdr).writeto(path)
        paths.append(str(path))
    findings = photometry.analyze(paths)
    assert "star" in findings["summary"].lower() and "light_curve" not in findings


def test_fit_handles_a_light_curve_with_no_dip_at_all():
    t = np.arange(20) * 100.0
    fit = photometry.fit_transit(t, np.linspace(1.0, 1.01, 20), np.full(20, 0.001), photometry.PhotometryParams())
    assert fit["ok"] is False or fit["delta_bic"] < 10  # a pure ramp is absorbed by the baseline


# --------------------------------------------------------------------------- #
# Images -> light curve -> cross-reference (shared, built once)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    root = tmp_path_factory.mktemp("transit")
    settings = get_settings(data_dir=root / "data", mock_mode=True, offline_services=True, mock_capture_seconds=0.0)
    settings.ensure_dirs()
    plugin = load_plugin("transit_finder", settings)
    targets = {t["name"]: t for t in plugin.validated_targets()}
    plan = plugin.validated_exposure_plan()
    out = {"settings": settings, "plugin": plugin, "targets": targets, "plan": plan, "findings": {}, "paths": {}}
    with SeestarController(settings, sleep_fn=lambda s: None) as tel:
        for name, target in targets.items():
            paths = tel.capture_sequence(target, plan, root / name.split()[0], scene=plugin.mock_scene(target))
            out["paths"][name] = paths
            out["findings"][name] = plugin.analyze_frames(paths)
    return out


PLANET, CONTROL = "WASP-12 b (sim)", "Control star (sim)"


def test_plugin_contract_and_simulated_targets(sim):
    plugin, targets, plan = sim["plugin"], sim["targets"], sim["plan"]
    assert plugin.name == "transit_finder" and set(targets) == {PLANET, CONTROL}
    assert plan["count"] >= 12 and plan["interval_s"] > 0
    host = targets[PLANET]["sim"]["transit"]
    assert 0 < host["t_mid_s"] < plan["count"] * (plan["exposure_s"] + plan["interval_s"])
    assert targets[CONTROL]["predicted_t_mid_iso"] is None


def test_injected_transit_is_recovered(sim):
    findings, truth = sim["findings"][PLANET], sim["targets"][PLANET]["sim"]["transit"]
    assert len(findings["detections"]) == 1
    det = findings["detections"][0]
    assert det["quality"] == "good" and det["coverage"] == "full"
    assert det["depth_pct"] == pytest.approx(100 * truth["depth"], abs=max(4 * det["depth_err_pct"], 0.2))
    t_mid = datetime.fromisoformat(sim["targets"][PLANET]["sim"]["epoch0_iso"]) + timedelta(seconds=truth["t_mid_s"])
    assert abs((datetime.fromisoformat(det["epoch_mid_iso"]) - t_mid).total_seconds()) < 10 * 60
    assert det["duration_h"] == pytest.approx(truth["duration_s"] / 3600.0, rel=0.15)
    assert findings["n_comps"] >= 5 and findings["n_frames_used"] == sim["plan"]["count"]
    assert findings["rms_ppt"] < 6.0
    assert Path(findings["figures"][0]).is_file()
    finder = [p for p in findings["figures"] if Path(p).name.startswith("finder")]
    assert finder and Path(finder[0]).is_file() and Path(finder[0]).read_bytes().startswith(b"\x89PNG")


def test_the_injected_host_is_the_measured_target_and_drift_is_tracked(sim):
    findings = sim["findings"][PLANET]
    scene, sky = _sky(sim["targets"][PLANET])
    hx, hy = scene.host_pixel(sky)
    tx, ty = findings["photometry"]["target_xy"]
    assert np.hypot(tx - hx, ty - hy) < 1.5
    dx, dy = findings["photometry"]["drift_px"]
    hours = findings["span_h"]
    # registration shift is "reference = frame + shift", so it is minus the sky drift
    assert (dx, dy) == pytest.approx((-6.0 * hours, 4.0 * hours), abs=2.0)


def test_control_star_gives_no_transit(sim):
    findings = sim["findings"][CONTROL]
    assert findings["detections"] == [] and not findings["fit"].get("significant")
    assert findings["summary"].startswith("No ")


def test_finder_chart_marks_the_measured_stars_and_is_a_png(tmp_path):
    from astropy.io import fits

    image = np.full((40, 60), 100.0, dtype=np.float32)
    image[20, 30] = 8000.0
    image[8, 10] = 4000.0
    path = tmp_path / "frame.fits"
    fits.PrimaryHDU(image).writeto(path)
    out = photometry.render_finder(str(path), (30.0, 20.0), [(10.0, 8.0)], tmp_path / "finder.png", name="KELT-7")
    raw = Path(out).read_bytes()
    assert raw.startswith(b"\x89PNG") and len(raw) > 1000


def test_too_few_frames_degrades_gracefully(sim):
    findings = photometry.analyze(sim["paths"][PLANET][:5])
    assert findings["detections"] == [] and "light_curve" not in findings
    assert any("frame" in w for w in findings["warnings"])
    render_payload(findings["agent_summary"])


def test_summary_passes_the_data_firewall_and_leaves_the_heavy_data_out(sim):
    findings = sim["findings"][PLANET]
    plugin, target = sim["plugin"], sim["targets"][PLANET]
    xref = plugin.cross_reference(findings, CatalogClient(sim["settings"]), target)
    digest = plugin.summarize_for_llm(findings, xref, target)
    text = render_payload(digest)  # raises if it breaks any data-reduction rule
    assert len(text) < 1800
    for key in HEAVY_KEYS:
        assert f'"{key}"' not in text
    heavy = {k: v for k, v in findings.items() if k != "agent_summary"}
    assert len(json.dumps(heavy, default=str)) > 10 * len(text)
    with pytest.raises(LLMPayloadError):
        render_payload(heavy)
    # the control star's digest says plainly that nothing was detected
    ctrl = plugin.summarize_for_llm(sim["findings"][CONTROL], None, sim["targets"][CONTROL])
    assert ctrl["n_transits_detected"] == 0 and "transit" not in ctrl
    render_payload(ctrl)


def _xref(sim, name, **target_overrides):
    target = {**sim["targets"][name], **target_overrides}
    return sim["plugin"].cross_reference(sim["findings"][name], CatalogClient(sim["settings"]), target)


def test_known_transit_is_confirmed_against_the_catalogue(sim):
    xref = _xref(sim, PLANET)
    entry = xref["entries"][0]
    assert entry["classification"] == "known_transit_confirmed" and entry["match"]["designation"] == "WASP-12 b"
    assert -10.0 < entry["t_mid_residual_min"] < 0.0  # the "archive" is 4 min late
    assert 0.8 < entry["depth_ratio"] < 1.3
    assert xref["identity"]["status"] == "confirmed" and xref["identity"]["separation_arcsec"] < 3.0
    assert xref["calibration"]["status"] == "calibrated" and xref["calibration"]["n_matches"] > 50
    assert xref["calibration"]["offset_arcsec"] == pytest.approx(33.0, abs=1.5)  # the injected pointing error


def test_stale_ephemeris_and_wrong_depth_are_reported_not_hidden(sim):
    predicted = sim["targets"][PLANET]["predicted_t_mid_iso"]
    late = (datetime.fromisoformat(predicted) + timedelta(minutes=90)).isoformat()
    assert _xref(sim, PLANET, predicted_t_mid_iso=late)["entries"][0]["classification"] == "known_transit_timing_offset"
    assert _xref(sim, PLANET, depth_pct=4.0)["entries"][0]["classification"] == "known_transit_shape_mismatch"
    assert _xref(sim, PLANET, predicted_t_mid_iso=None)["entries"][0]["classification"] == "unexpected_dip"


def test_identity_mismatch_overrides_everything(sim):
    plugin = sim["plugin"]
    det = sim["findings"][PLANET]["detections"][0]
    expected = plugin._expected(sim["targets"][PLANET])
    entry = plugin._classify(det, expected, {"status": "mismatch", "separation_arcsec": 300.0})
    assert entry["classification"] == "target_identity_mismatch"


def test_non_detection_gives_an_upper_limit_at_the_expected_time(sim):
    ctrl = sim["targets"][CONTROL]
    epoch0 = datetime.fromisoformat(ctrl["sim"]["epoch0_iso"])
    mid = epoch0 + timedelta(seconds=ctrl["sim"]["transit"]["t_mid_s"])
    xref = _xref(sim, CONTROL, predicted_t_mid_iso=mid.isoformat(), duration_h=2.0, depth_pct=1.4)
    nd = xref["non_detection"]
    assert xref["entries"] == [] and nd["classification"] == "expected_transit_not_detected"
    assert nd["depth_upper_limit_3sigma_pct"] < 1.0 < 1.4
    assert "No significant transit" in xref["summary"]

    far = _xref(sim, CONTROL, predicted_t_mid_iso=(mid + timedelta(hours=30)).isoformat(), duration_h=2.0, depth_pct=1.4)
    assert far["non_detection"]["classification"] == "expected_window_not_covered"

    clean = _xref(sim, CONTROL)
    assert clean["non_detection"]["expected"] is False and "none was expected" in clean["summary"]


def _identity(**over):
    return {"status": "confirmed", "nearest_gaia_parallax_mas": 7.0, "nearest_gaia_parallax_err_mas": 0.02, **over}


def test_host_distance_uses_the_gaia_parallax_only_when_the_star_is_confirmed_and_the_parallax_significant(sim):
    dist = sim["plugin"]._distance
    ok = dist(143.7, _identity())
    assert ok["gaia_pc"] == pytest.approx(142.857, abs=0.01) and ok["archive_pc"] == 143.7
    assert ok["consistent"] is True and ok["note"] is None

    off = dist(250.0, _identity())  # the two sources disagree by far more than 10 %
    assert off["consistent"] is False and "check that the right star was measured" in off["note"]

    assert dist(143.7, _identity(status="mismatch"))["gaia_pc"] is None  # a parallax of the wrong star is not the host's
    assert dist(143.7, _identity(nearest_gaia_parallax_err_mas=2.0))["gaia_pc"] is None  # S/N 3.5: do not invert
    assert dist(143.7, _identity(nearest_gaia_parallax_mas=-0.5))["gaia_pc"] is None
    assert dist(143.7, {"status": "confirmed"})["gaia_pc"] is None  # offline Gaia has no parallax
    assert dist(None, _identity())["consistent"] is None  # nothing to compare with


def test_simulated_planet_carries_a_catalogue_distance_but_no_gaia_parallax_and_the_control_none(sim):
    d = _xref(sim, PLANET)["distance"]
    assert d["archive_pc"] == 427.2 and d["gaia_pc"] is None and d["note"] is None
    c = _xref(sim, CONTROL)["distance"]
    assert c["archive_pc"] is None and c["gaia_pc"] is None


# --------------------------------------------------------------------------- #
# Time scales
# --------------------------------------------------------------------------- #
def test_bjd_conversion_round_trips_and_is_a_few_minutes():
    when = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
    args = (97.6366, 29.6723, 39.7392, -104.9903, 1609.0)
    bjd = utc_to_bjd_tdb(when, *args)
    assert abs((bjd_tdb_to_utc(bjd, *args) - when).total_seconds()) < 1.0
    shift_min = (bjd - float(Time(when).tdb.jd)) * 1440.0
    assert abs(shift_min) < 8.6  # light-travel time across 1 AU is 8.3 min


# --------------------------------------------------------------------------- #
# NASA Exoplanet Archive (no network: faked responses)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _row(name, **over):
    row = {"pl_name": name, "hostname": name[:-2], "ra": 97.6, "dec": 29.7, "sy_vmag": 11.5, "pl_orbper": 1.09,
           "pl_orbpererr1": 2e-8, "pl_tranmid": 2457607.5, "pl_tranmiderr1": 3e-5, "pl_trandur": 3.0, "pl_trandep": 1.5}
    row.update(over)
    return row


def test_exoplanet_archive_parses_rows_and_skips_incomplete_ones(monkeypatch, settings):
    import requests

    seen = {}

    def fake_get(url, params, timeout):
        seen.update(url=url, params=params)
        return _Resp([_row("WASP-12 b"), _row("Bad b", pl_tranmid=None), _row("Nodepth b", pl_trandep=None)])

    monkeypatch.setattr(requests, "get", fake_get)
    rows = archive.fetch_planets(["WASP-12 b", "Bad b", "Nodepth b"])
    assert [r["planet"] for r in rows] == ["WASP-12 b", "Nodepth b"]
    assert rows[0]["t0_bjd_tdb"] == 2457607.5 and rows[0]["duration_h"] == 3.0 and rows[1]["depth_pct"] is None
    assert "sy_dist" in seen["params"]["query"] and rows[0]["distance_pc"] is None  # not in this fake row
    assert "pl_name in ('WASP-12 b','Bad b','Nodepth b')" in seen["params"]["query"]


def test_exoplanet_archive_reports_the_system_distance(monkeypatch, settings):
    import requests

    monkeypatch.setattr(requests, "get", lambda url, params, timeout: _Resp([_row("WASP-2 b", sy_dist=143.7), _row("Odd b", sy_dist=-1.0)]))
    rows = archive.fetch_planets(["WASP-2 b", "Odd b"])
    assert rows[0]["distance_pc"] == 143.7 and rows[1]["distance_pc"] is None  # a non-positive distance is not a distance


def test_exoplanet_archive_refuses_odd_names_and_reports_failures(monkeypatch, settings):
    import requests

    with pytest.raises(CatalogError, match="odd planet name"):
        archive.fetch_planets(["x'); drop table pscomppars;--"])

    def boom(url, params, timeout):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(CatalogError, match="query failed"):
        archive.fetch_planets(["WASP-12 b"])
    assert archive.fetch_planets([]) == []


def test_live_targets_are_scheduled_without_overlap_and_in_time_order(monkeypatch, settings):
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MIN_ALT_DEG", "-90")  # sky position is irrelevant to this test
    now = datetime.now(timezone.utc)
    jd_now = float(Time(now).jd)

    def planet(name, hours_ahead, depth, **over):  # period 1 d, so exactly one transit in the next 24 h
        return {"planet": name, "host": name[:-2], "ra_deg": 100.0, "dec_deg": 30.0, "v_mag": 11.5, "period_d": 1.0,
                "period_err_d": 1e-7, "t0_bjd_tdb": jd_now - 10.0 + hours_ahead / 24.0, "t0_err_d": 1e-4,
                "duration_h": 3.0, "depth_pct": depth, "source": "test", **over}

    rows = [
        planet("Late b", 14.0, 0.8),
        planet("Deep b", 5.0, 2.0),
        planet("Clash b", 6.0, 1.0),  # overlaps Deep b and is shallower: dropped
        planet("Faint b", 8.0, 2.0, v_mag=15.0),
        planet("Shallow b", 10.0, 0.2),
    ]
    monkeypatch.setattr(archive, "fetch_planets", lambda names: rows)
    plugin = load_plugin("transit_finder", replace(settings, offline_services=False, **NO_SKY_LIMITS))
    targets = plugin.generate_targets()
    assert [t["name"] for t in targets] == ["Deep b", "Late b"]
    assert targets[0]["priority"] > targets[1]["priority"]  # the graph observes by priority: earliest first
    mid = datetime.fromisoformat(targets[0]["predicted_t_mid_iso"])
    assert abs((mid - (now + timedelta(hours=5))).total_seconds()) < 12 * 60  # within the barycentric correction
    assert targets[0]["window_end"] < targets[1]["window_start"]
    assert targets[0]["predicted_t_mid_err_min"] < 1.0


def test_live_targets_fail_loudly_when_nothing_is_observable(monkeypatch, settings):
    monkeypatch.setenv("ASTRONOMO_TRANSIT_MIN_ALT_DEG", "-90")
    monkeypatch.setattr(archive, "fetch_planets", lambda names: [])
    plugin = load_plugin("transit_finder", replace(settings, offline_services=False, **NO_SKY_LIMITS))
    with pytest.raises(CatalogError, match="no usable ephemeris"):
        plugin.generate_targets()


# --------------------------------------------------------------------------- #
# Field rotation is measured, never silent
# --------------------------------------------------------------------------- #
def _rotated_copies(paths, total_deg, out_dir):
    """Copy the frames, rotating frame k by ``total_deg * k / (n - 1)`` about the frame centre."""
    from astropy.io import fits
    from scipy.ndimage import rotate

    out_dir.mkdir(parents=True, exist_ok=True)
    n, copies = len(paths), []
    for k, path in enumerate(paths):
        with fits.open(path) as hdul:
            data, header = hdul[0].data.astype(float), hdul[0].header.copy()
        angle = total_deg * k / (n - 1)
        # order=1 keeps the stars compact; reshape=False and a median fill keep the frame size
        data = rotate(data, angle, reshape=False, order=1, mode="constant", cval=float(np.median(data)))
        dest = out_dir / f"frame_{k + 1:03d}.fits"
        fits.PrimaryHDU(np.clip(data, 0, 65535).astype(np.uint16), header).writeto(dest, overwrite=True)
        copies.append(str(dest))
    return copies


def test_large_field_rotation_is_corrected_by_default(sim, tmp_path):
    paths = _rotated_copies(sim["paths"][PLANET], 0.6, tmp_path / "corrected")
    findings = photometry.analyze(paths)
    rot = findings["rotation"]
    assert rot["corrected"] and 0.3 < rot["max_abs_deg"] < 0.9 and rot["corner_displacement_px"] > 4.0
    assert findings["n_frames_used"] == len(paths)
    truth = sim["findings"][PLANET]["detections"][0]["depth_pct"]
    found = findings["detections"][0]
    assert abs(found["depth_pct"] - truth) < max(0.25 * truth, 3 * found["depth_err_pct"])
    assert any("corrected" in w for w in findings["warnings"])


def test_large_field_rotation_is_refused_loudly_when_not_corrected(sim, tmp_path):
    paths = _rotated_copies(sim["paths"][PLANET], 0.6, tmp_path / "big")
    findings = photometry.analyze(paths, photometry.PhotometryParams(correct_rotation=False))
    assert findings["detections"] == [] and "light_curve" not in findings
    rot = findings["rotation"]
    assert 0.3 < rot["max_abs_deg"] < 0.9 and rot["corner_displacement_px"] > 4.0
    assert "rotation" in findings["summary"].lower() and "no light curve" in findings["summary"]
    assert any("translation-only" in w for w in findings["warnings"])


def test_small_field_rotation_is_measured_and_warned_but_not_refused(sim, tmp_path):
    paths = _rotated_copies(sim["paths"][PLANET], 0.12, tmp_path / "small")
    findings = photometry.analyze(paths)
    rot = findings["rotation"]
    assert 0.08 < rot["max_abs_deg"] < 0.18
    assert "light_curve" in findings
    assert any("rotation" in w.lower() for w in findings["warnings"]) == (rot["corner_displacement_px"] > 1.5)


def test_an_unrotated_sequence_reports_no_rotation(sim):
    rot = sim["findings"][PLANET]["rotation"]
    assert rot["max_abs_deg"] < 0.02 and not any("rotation" in w.lower() for w in sim["findings"][PLANET]["warnings"])


def test_rotation_check_can_be_disabled(sim, tmp_path):
    paths = _rotated_copies(sim["paths"][PLANET], 0.6, tmp_path / "off")
    findings = photometry.analyze(paths, photometry.PhotometryParams(max_rotation_edge_px=None, correct_rotation=False))
    assert findings["rotation"]["corner_displacement_px"] > 4.0  # still measured
    assert "no light curve was produced" not in findings["summary"]


# --------------------------------------------------------------------------- #
# Sharp stars on a colour mosaic (the first in-focus night, 2026-11-16)
# --------------------------------------------------------------------------- #
def _mosaic_sequence(tmp_path, n_frames=14, fwhm_px=2.2, instrument="Seestar S50 Pro Telephoto Camera", seed=5):
    """Sharp stars centred on the dead sites of a mosaic: four bright green neighbours around a sky-level centre.

    The star at the image centre is the target.  Returns ``(paths, star_positions)``.
    """
    from astropy.io import fits

    rng = np.random.default_rng(seed)
    ny, nx = 600, 500
    yy, xx = np.mgrid[0:ny, 0:nx]
    sigma = fwhm_px / 2.355
    stars = [(250, 300, 9000.0)]
    while len(stars) < 40:
        x, y = 2 * int(rng.integers(20, nx // 2 - 20)), 2 * int(rng.integers(20, ny // 2 - 20))
        if all(np.hypot(x - sx, y - sy) > 40 for sx, sy, _ in stars):
            stars.append((x, y, float(rng.uniform(2500, 9000))))
    clean = np.zeros((ny, nx))
    for x, y, amp in stars:
        clean += amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))
    gain = np.ones((ny, nx))
    gain[0::2, 0::2] = 0.0  # red/blue sites: the real star's centre pixel read the sky level, so ~nothing
    gain[1::2, 1::2] = 0.0
    paths = []
    for k in range(n_frames):
        img = 2500.0 + rng.normal(0.0, 45.0, (ny, nx)) + clean * gain
        hdr = fits.Header()
        hdr["DATE-OBS"] = (datetime(2026, 11, 17, 3, 0, 0) + timedelta(seconds=40 * k)).isoformat()
        hdr["EXPTIME"] = 10.0
        hdr["INSTRUME"] = instrument
        path = tmp_path / f"mosaic_{k:02d}.fits"
        fits.PrimaryHDU(np.clip(img, 0, 65535).astype(np.uint16), hdr).writeto(path)
        paths.append(str(path))
    return paths, [(x, y) for x, y, _ in stars]


def test_a_sharp_star_on_a_mosaic_is_missed_raw_and_found_after_averaging(tmp_path):
    from astropy.io import fits
    from core.setup_checks import target_star_seen

    paths, stars = _mosaic_sequence(tmp_path, n_frames=1)
    data = fits.getdata(paths[0]).astype(np.float32)
    seen = [target_star_seen(data, x, y, radius_px=3.0) for x, y in stars[:20]]  # on the star, not on a neighbour
    raw = sum(s["raw"] is not None for s in seen)
    averaged = sum(s["averaged"] is not None for s in seen)
    assert averaged >= 18 and raw <= averaged // 2, (raw, averaged)


def test_narrow_stars_from_a_seestar_are_averaged_and_the_target_is_found(tmp_path):
    paths, _stars = _mosaic_sequence(tmp_path)
    findings = photometry.analyze(paths)
    assert findings["mosaic_averaged"] is True
    assert any("averaged over 2x2" in w for w in findings["warnings"])
    assert findings["n_comps"] >= 2 and "light_curve" in findings, findings["summary"]
    assert np.hypot(*(np.array(findings["photometry"]["target_xy"]) - [250.5, 300.5])) < 3.0


def test_averaging_is_only_for_narrow_seestar_frames_and_can_be_switched_off(tmp_path):
    from dataclasses import replace

    other, _ = _mosaic_sequence(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, instrument="Some Mono Camera")
    assert photometry.analyze(other)["mosaic_averaged"] is False  # not a Seestar: left alone
    mine, _ = _mosaic_sequence(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    off = photometry.analyze(mine, replace(photometry.PhotometryParams(), mosaic_averaging="off"))
    assert off["mosaic_averaged"] is False
    assert "light_curve" not in off, "without the averaging the finder cannot see these stars (the 2026-11-16 failure)"
    wide, _ = _mosaic_sequence(tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path, fwhm_px=6.0)
    assert photometry.analyze(wide)["mosaic_averaged"] is False  # not narrow: left alone
