"""First-frame focus / pointing / calibration checks, on simulated frames (shifted, defocused)."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from matplotlib import image as mpimg

from core import setup_checks as sc
from core.graph import run_mission
from core.mission_log import build_log, with_preview
from core.state import initial_state
from db.database import Database
from plugins.asteroid_finder.plugin import AsteroidFinderPlugin
from tests.conftest import Harness
from tools.catalogs import CatalogClient
from tools.mock_sky import MockSky

EPOCH = "2026-10-21T04:00:00+00:00"


def _target(**sim):
    return {"name": "Check Field", "ra_deg": 150.0, "dec_deg": 11.0,
            "sim": {"seed": 3, "epoch0_iso": EPOCH, "pointing_error_arcsec": [5.0, -4.0], **sim}}


def _frame(tmp_path, target, name="f.fits"):
    sky = MockSky.from_target(target)
    start = datetime.fromisoformat(EPOCH)
    path = tmp_path / name
    fits.PrimaryHDU(sky.render(start, 10.0, 0), sky.header(start, 10.0, 80, "IRCUT", 0, target["name"])).writeto(path)
    return str(path)


def _run(settings, tmp_path, target):
    return sc.run_setup_checks(settings, _frame(tmp_path, target), target, CatalogClient(settings))


def _by_name(report):
    return {c["name"]: c for c in report["checks"]}


def test_focus_measures_the_simulated_star_width(tmp_path):
    from tools.imaging import load_image

    for fwhm in (3.0, 6.0, 9.0):
        data, _ = load_image(_frame(tmp_path, _target(fwhm_px=fwhm), f"{fwhm}.fits"))
        out = sc.measure_focus(data)
        assert out["fwhm_px"] == pytest.approx(fwhm, rel=0.2) and out["n_stars"] >= 8
        assert out["fwhm_arcsec"] == pytest.approx(out["fwhm_px"] * 2.39, rel=0.01)


def test_a_well_focused_well_pointed_frame_passes(settings, tmp_path):
    report = _run(settings, tmp_path, _target())
    checks = _by_name(report)
    assert report["ok"]
    assert checks["focus"]["status"] == "PASS" and checks["pointing (star pattern)"]["status"] == "PASS"
    assert checks["pointing (star pattern)"]["offset_arcsec"] == pytest.approx(6.4, abs=4.0)  # the simulated error
    assert checks["pointing (scope's solver)"]["status"] == "INFO"
    assert "No darks or flats" in checks["calibration"]["detail"]


def test_a_defocused_frame_fails_the_focus_check(settings, tmp_path):
    report = _run(settings, tmp_path, _target(fwhm_px=12.0))
    assert not report["ok"]
    focus = _by_name(report)["focus"]
    assert focus["status"] == "FAIL" and "refocus" in focus["detail"]
    assert "focus" in sc.failure_message(report)


def test_a_softish_frame_warns_but_passes(settings, tmp_path):
    report = _run(settings, tmp_path, _target(fwhm_px=7.0))
    assert report["ok"] and _by_name(report)["focus"]["status"] == "WARN"


def test_a_large_on_chip_error_is_kept_and_stamped(settings, tmp_path):
    """18 arcmin is past the old failure limit and still on this chip, so the frame is usable."""
    target = _target(pointing_error_arcsec=[0.0, 1080.0])
    path = _frame(tmp_path, target)
    report = sc.run_setup_checks(settings, path, target, CatalogClient(settings))
    point = _by_name(report)["pointing (star pattern)"]
    assert report["ok"] and point["status"] == "WARN" and point["action"] == "accept"
    assert point["on_chip"] and point["offset_arcsec"] == pytest.approx(1080.0, rel=0.05)
    with fits.open(path) as hdul:
        assert hdul[0].header["TARGRA"] == pytest.approx(target["ra_deg"])
        assert hdul[0].header["TARGDEC"] == pytest.approx(target["dec_deg"])
        from astropy.wcs import WCS
        x, y = WCS(hdul[0].header).all_world2pix([target["ra_deg"]], [target["dec_deg"]], 0)
    assert point["target_x"] == pytest.approx(float(x[0]), abs=2.0)
    assert point["target_y"] == pytest.approx(float(y[0]), abs=2.0)


def test_the_miss_is_measured_from_the_target_not_from_a_correction_command():
    """After a correction slew the header command is the offset that was sent; the star is what matters."""
    from tools.astrometry import make_tan_wcs

    shape, scale = (3840, 2160), 2.39
    target_ra, target_dec = 347.2375, 38.9150
    # The camera is centred on the target, but the command was nudged a degree south (the correction).
    solved = make_tan_wcs(target_ra, target_dec, shape, scale)
    point = sc._describe_solution(solved, shape, target_ra, target_dec - 1.0, target_ra, target_dec, 64.0)
    assert point["offset_arcsec"] == pytest.approx(3600.0, rel=0.01)  # from the command
    assert point["target_offset_arcsec"] < 30.0  # from the star
    assert point["on_chip"]


def test_a_target_on_the_chip_but_far_from_its_centre_asks_to_be_re_centred(settings, tmp_path):
    """The first real night landed 34 arcmin off, on the chip, and drifted off it the sooner for that."""
    settings = replace(settings, pointing_recentre_arcmin=10.0)  # off by default
    target = _target(pointing_error_arcsec=[0.0, 720.0])  # 12 arcmin: on this chip, past the 10 arcmin re-centre level
    report = _run(settings, tmp_path, target)
    point = _by_name(report)["pointing (star pattern)"]
    assert report["ok"] and point["status"] == "WARN" and point["action"] == "recentre" and point["on_chip"]
    assert "re-centre level" in point["detail"]
    assert point["next_dec_deg"] == pytest.approx(target["dec_deg"] - 720.0 / 3600.0, abs=0.02)
    for name in ("near", "off"):
        (tmp_path / name).mkdir()
    near = _run(settings, tmp_path / "near", _target(pointing_error_arcsec=[0.0, 240.0]))
    assert _by_name(near)["pointing (star pattern)"]["action"] == "accept"  # 4 arcmin: left where it is
    off = _run(replace(settings, pointing_recentre_arcmin=0.0), tmp_path / "off", target)
    assert _by_name(off)["pointing (star pattern)"]["action"] == "accept"  # 0 turns it off


def test_a_target_off_the_chip_asks_for_a_corrected_slew(settings, tmp_path):
    target = _target(pointing_error_arcsec=[0.0, 1320.0])  # 22 arcmin, past the short axis of this chip
    report = _run(settings, tmp_path, target)
    point = _by_name(report)["pointing (star pattern)"]
    assert point["status"] == "FAIL" and point["action"] == "nudge" and not point["on_chip"]
    assert not report["ok"]
    # The mount error is +22 arcmin, so the corrected command aims 22 arcmin south of the target.
    assert point["next_dec_deg"] == pytest.approx(target["dec_deg"] - 1320.0 / 3600.0, abs=0.02)
    assert point["next_ra_deg"] == pytest.approx(target["ra_deg"], abs=0.02)


def test_an_unreachable_catalogue_refuses_the_frame(settings, tmp_path, monkeypatch):
    def down(*_a, **_k):
        raise sc.CatalogError("no route to the catalogue")

    monkeypatch.setattr(CatalogClient, "gaia_cone", down)
    report = _run(settings, tmp_path, _target())
    point = _by_name(report)["pointing (star pattern)"]
    assert point["status"] == "FAIL" and point["action"] == "stop" and not report["ok"]


def test_a_rotated_frame_is_still_measured(settings, tmp_path):
    from scipy.ndimage import rotate

    target = _target()
    sky = MockSky.from_target(target)
    start = datetime.fromisoformat(EPOCH)
    turned = rotate(sky.render(start, 10.0, 0).astype(float), 37.0, reshape=False, order=1, cval=float(np.median(sky.render(start, 10.0, 0))))
    path = tmp_path / "rotated.fits"
    fits.PrimaryHDU(turned.astype(np.float32), sky.header(start, 10.0, 80, "IRCUT", 0, target["name"])).writeto(path)
    point = _by_name(sc.run_setup_checks(settings, str(path), target, CatalogClient(settings)))["pointing (star pattern)"]
    assert point["status"] == "PASS"
    assert point["offset_arcsec"] == pytest.approx(6.4, abs=20)
    assert abs(point["angle_deg"]) == pytest.approx(37.0, abs=5)


def test_a_colour_mosaic_at_the_pro_scale_is_still_measured(settings, tmp_path):
    target = _target()
    sky = MockSky.from_target(target)
    start = datetime.fromisoformat(EPOCH)
    native = sky.render(start, 10.0, 0).astype(np.float32)
    mosaic = np.zeros((native.shape[0] * 2, native.shape[1] * 2), dtype=np.float32)
    mosaic[0::2, 0::2] = native  # one pixel of each 2x2 is lit, so the stars are not round
    header = sky.header(start, 10.0, 80, "IRCUT", 0, target["name"])
    header["PIXSCALE"] = header["PIXSCALE"] / 2.0
    path = tmp_path / "mosaic.fits"
    fits.PrimaryHDU(mosaic, header).writeto(path)
    point = _by_name(sc.run_setup_checks(settings, str(path), target, CatalogClient(settings)))["pointing (star pattern)"]
    assert point["status"] == "PASS"
    assert point["offset_arcsec"] == pytest.approx(6.4, abs=8)


def test_a_moderate_pointing_error_only_warns(settings, tmp_path):
    report = _run(settings, tmp_path, _target(pointing_error_arcsec=[0.0, 420.0]))  # 7 arcmin
    assert report["ok"] and _by_name(report)["pointing (star pattern)"]["status"] == "WARN"


def test_the_scopes_own_solution_is_checked_too(settings, tmp_path):
    target = _target()
    path = _frame(tmp_path, target)
    far = {"ra_deg": 150.0, "dec_deg": 11.0 + 0.5}  # 30 arcmin off
    report = sc.run_setup_checks(settings, path, target, CatalogClient(settings), solved=far)
    assert _by_name(report)["pointing (scope's solver)"]["status"] == "FAIL" and not report["ok"]
    near = {"ra_deg": 150.0, "dec_deg": 11.0 + 0.01}
    report = sc.run_setup_checks(settings, path, target, CatalogClient(settings), solved=near)
    assert _by_name(report)["pointing (scope's solver)"]["status"] == "PASS"


def test_an_empty_frame_is_not_verified_rather_than_passed(settings, tmp_path):
    rng = np.random.default_rng(1)
    path = tmp_path / "blank.fits"
    fits.PrimaryHDU(rng.normal(500, 5, (1080, 1920)).astype(np.uint16),
                    fits.Header({"RA": 150.0, "DEC": 11.0, "PIXSCALE": 2.39})).writeto(path)
    report = sc.run_setup_checks(settings, str(path), _target(), CatalogClient(settings))
    checks = _by_name(report)
    assert checks["focus"]["status"] == "WARN" and "not verified" in checks["focus"]["detail"]
    assert checks["pointing (star pattern)"]["status"] == "WARN"
    assert report["ok"]  # unverifiable is a warning, not a refusal


def test_a_dark_obstruction_stays_dark_in_the_saved_view(tmp_path):
    """A roof or a tree is a dark block. Star counts miss it; the picture must keep it dark."""
    rng = np.random.default_rng(0)
    sky = rng.normal(4000, 30, (80, 120)).astype(np.float32)
    sky[:30, :40] = 50
    path = tmp_path / "frame.fits"
    fits.PrimaryHDU(sky).writeto(path)
    dest = sc.save_first_frame_view(str(path), tmp_path / "mission")
    assert dest.name == "first_frame.png" and dest.is_file()
    img = mpimg.imread(dest)
    if img.ndim == 3:
        img = img[..., 0]
    assert img[:30, :40].mean() < img[40:, 60:].mean()


def test_a_flat_frame_still_writes_a_view(tmp_path):
    path = tmp_path / "flat.fits"
    fits.PrimaryHDU(np.full((20, 20), 1000, np.float32)).writeto(path)
    dest = sc.save_first_frame_view(str(path), tmp_path)
    assert dest.is_file() and dest.stat().st_size > 0


def test_a_wide_frame_is_shrunk_for_the_operator(tmp_path):
    view = sc.display_view(np.zeros((100, 3000), np.float32))
    assert max(view.shape) <= sc._DISPLAY_MAX_SIDE
    assert view.shape[1] > view.shape[0]


def test_report_is_saved_and_printable(settings, tmp_path):
    report = _run(settings, tmp_path, _target())
    path = sc.save_report(report, tmp_path / "mission")
    assert path.name == "setup_check.json" and path.is_file()
    assert any(line.startswith("[PASS] focus") for line in sc.format_report(report))


def _runtime(settings):
    db = Database(settings.db_path)
    db.create_mission("m_pic", "asteroid_finder", True, {})
    return type("RT", (), {"settings": settings, "catalogs": CatalogClient(settings), "db": db})(), db


def test_the_view_is_on_disk_before_the_check_returns(settings, tmp_path):
    """The capture loop waits on this function, so the picture exists before the next exposure."""
    from core.graph import _first_frame_checks

    rt, db = _runtime(settings)
    try:
        frame = _frame(tmp_path, _target())
        frame_written = Path(frame).stat().st_mtime
        report = _first_frame_checks(rt, {"mission_id": "m_pic"}, _target(), frame, None, None)
        preview = settings.missions_dir / "m_pic" / "first_frame.png"
        assert report["preview"] == "first_frame.png" and preview.is_file()
        saved = json.loads((settings.missions_dir / "m_pic" / "setup_check.json").read_text())
        assert saved["preview"] == "first_frame.png" and saved["ok"]
        assert preview.stat().st_mtime >= frame_written
    finally:
        db.dispose()


def test_a_picture_that_cannot_be_written_leaves_the_check_in_charge(settings, tmp_path, monkeypatch):
    from core.graph import _first_frame_checks

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("core.graph.setup_checks.save_first_frame_view", boom)
    rt, db = _runtime(settings)
    try:
        report = _first_frame_checks(rt, {"mission_id": "m_pic"}, _target(), _frame(tmp_path, _target()), None, None)
        assert report["ok"] and "preview" not in report
        saved = json.loads((settings.missions_dir / "m_pic" / "setup_check.json").read_text())
        assert "preview" not in saved
        assert not (settings.missions_dir / "m_pic" / "first_frame.png").exists()
    finally:
        db.dispose()


def test_the_mission_log_carries_the_view_when_the_file_is_there(tmp_path):
    preview = tmp_path / "first_frame.png"
    preview.write_bytes(b"png")
    mission = {
        "mission_id": "m1", "plugin_name": "asteroid_finder", "status": "COMPLETE",
        "targets": [], "observations": [],
        "setup": {"ok": True, "target": "Check Field", "preview": "first_frame.png", "checks": [
            {"name": "focus", "status": "PASS", "detail": "median FWHM 3.0 px"},
        ]},
    }
    linked = with_preview(mission, tmp_path)
    section = next(s for s in build_log(linked)["sections"] if s["heading"] == "Telescope setup")
    assert section["figures"][0]["path"] == str(preview)
    assert "stretched view" in section["paragraphs"][1]
    assert with_preview(mission, tmp_path / "nowhere")["setup"].get("preview_path") is None
    bare = build_log(mission)  # the filename alone is not a path the log can attach
    assert next(s for s in bare["sections"] if s["heading"] == "Telescope setup")["figures"] == []


def test_checks_run_on_the_real_telescope_and_only_when_asked_in_simulation(settings):
    assert not settings.setup_checks_active
    assert replace(settings, setup_checks_in_simulation=True).setup_checks_active
    assert replace(settings, mock_mode=False).setup_checks_active


# --------------------------------------------------------------------------- #
# In the mission graph
# --------------------------------------------------------------------------- #
class _Field(AsteroidFinderPlugin):
    """The asteroid plugin with every simulated target given extra ``sim`` settings."""

    sim_extra: dict = {}

    def generate_targets(self):
        targets = super().generate_targets()
        for t in targets:
            t["sim"] = {**(t.get("sim") or {}), **self.sim_extra}
        return targets


def _mission(settings, **sim):
    settings = replace(settings, setup_checks_in_simulation=True, max_targets=2)
    plugin = _Field(settings)
    plugin.sim_extra = sim
    h = Harness(settings, plugin)
    h.db.create_mission("m_setup", plugin.name, True, {"cloud_cover_threshold_pct": 30.0})
    try:
        return h, run_mission(h.app, "m_setup", initial_state("m_setup", plugin.name, True))
    except BaseException:
        h.close()
        raise


def test_a_good_first_frame_is_checked_once_and_logged(settings):
    h, final = _mission(settings)
    try:
        assert final["status"] == "COMPLETE" and final["setup"]["ok"]
        logs = h.db.list_logs("m_setup", kinds=["setup_check"])
        # focus, pointing x2, target star, calibration -- once, not once per target
        assert len(logs) == 5
        assert any("target star" in row["message"] and "a star is detected" in row["message"] for row in logs)
        mission_dir = settings.missions_dir / "m_setup"
        assert (mission_dir / "setup_check.json").is_file()
        preview = mission_dir / "first_frame.png"
        assert final["setup"]["preview"] == "first_frame.png" and preview.is_file()
        frames = sorted((mission_dir / "frames").rglob("frame_*.fits"), key=lambda p: (p.stat().st_mtime, str(p)))
        assert len(frames) > 1
        assert preview.stat().st_mtime <= frames[1].stat().st_mtime  # the view exists before the next exposure
        log = next((mission_dir / "results").glob("mission_log_*.md"))
        assert "stretched" in log.read_text()
        assert len(final["observations"]) == 2
    finally:
        h.close()


def test_a_defocused_first_frame_refuses_the_mission_with_a_clear_reason(settings):
    h, final = _mission(settings, fwhm_px=13.0)
    try:
        assert final["status"] == "FAILED"
        error = h.db.get_mission("m_setup")["error"]
        assert "SetupCheckError" in error and "focus" in error and "refocus" in error
        assert final.get("observations") == []
        assert h.db.list_logs("m_setup", kinds=["setup_check"])  # the evidence is in the log
        saved = list((settings.missions_dir / "m_setup" / "frames").rglob("*.fits"))
        assert len(saved) == 1  # the first frame failed the check, so the rest were not taken
        assert (settings.missions_dir / "m_setup" / "first_frame.png").is_file()
        saved_report = json.loads((settings.missions_dir / "m_setup" / "setup_check.json").read_text())
        assert saved_report["preview"] == "first_frame.png" and not saved_report["ok"]
    finally:
        h.close()


def test_an_off_chip_pointing_is_corrected_before_the_rest_of_the_sequence(settings):
    h, final = _mission(settings, pointing_error_arcsec=[0.0, 1320.0])
    try:
        assert final["status"] == "COMPLETE", (h.db.get_mission("m_setup") or {}).get("error")
        messages = [row["message"] for row in h.db.list_logs("m_setup", kinds=["setup_check"])]
        assert any("Pointing correction 1/" in message for message in messages)
        assert final["setup"]["ok"] and final["setup"]["checks"]
    finally:
        h.close()


def test_an_off_centre_target_is_re_centred_once_and_then_the_sequence_goes_ahead(settings):
    h, final = _mission(replace(settings, pointing_recentre_arcmin=10.0), pointing_error_arcsec=[0.0, 720.0])
    try:
        assert final["status"] == "COMPLETE", (h.db.get_mission("m_setup") or {}).get("error")
        messages = [row["message"] for row in h.db.list_logs("m_setup", kinds=["setup_check"])]
        assert any(message.startswith("Re-centring 1/") for message in messages), messages
        assert not any("Re-centring 2/" in message for message in messages), "one slew put it in the middle"
        assert final["setup"]["ok"] and final["setup"]["checks"]
    finally:
        h.close()


def test_when_the_corrections_are_used_up_an_on_chip_target_is_accepted_not_refused(settings):
    """An off-centre target that is still on the chip is no reason to lose the night."""
    h, final = _mission(replace(settings, pointing_max_nudges=0, pointing_recentre_arcmin=10.0), pointing_error_arcsec=[0.0, 720.0])
    try:
        assert final["status"] == "COMPLETE", (h.db.get_mission("m_setup") or {}).get("error")
        logs = h.db.list_logs("m_setup", kinds=["setup_check"])
        assert any("Re-centring stopped after 0 correction(s)" in row["message"] and row["level"] == "WARNING" for row in logs)
        assert final["setup"]["ok"]
        assert len(final["observations"]) == 2 and len(final["fits_paths"]) > 1
    finally:
        h.close()


def test_an_off_chip_target_is_still_refused_when_the_corrections_are_used_up(settings):
    h, final = _mission(replace(settings, pointing_max_nudges=0), pointing_error_arcsec=[0.0, 1320.0])
    try:
        assert final["status"] == "FAILED"
        assert "still off the chip after 0 pointing correction(s)" in h.db.get_mission("m_setup")["error"]
    finally:
        h.close()


def test_simulation_skips_the_checks_by_default(settings):
    plugin = AsteroidFinderPlugin(replace(settings, max_targets=1))
    h = Harness(replace(settings, max_targets=1), plugin)
    h.db.create_mission("m_plain", plugin.name, True, {"cloud_cover_threshold_pct": 30.0})
    try:
        final = run_mission(h.app, "m_plain", initial_state("m_plain", plugin.name, True))
        assert final["status"] == "COMPLETE" and not final.get("setup")
    finally:
        h.close()
