import pytest

from plugins.asteroid_finder.reduction import analyze
from plugins.asteroid_finder.scene import AsteroidScene
from tools.telescope import SeestarController

EPOCH0 = "2026-10-21T04:00:00+00:00"
PLAN = {"gain": 80, "exposure_s": 10.0, "filter": "IRCUT", "count": 4}


def _capture(settings, tmp_path, name, seed, mover):
    target = {"name": name, "ra_deg": 120.0, "dec_deg": 5.0, "sim": {"seed": seed, "epoch0_iso": EPOCH0, "mover": mover}}
    with SeestarController(settings, sleep_fn=lambda s: None) as tel:
        return tel.capture_sequence(target, PLAN, tmp_path / name, scene=AsteroidScene.from_target(target))


@pytest.mark.slow
def test_injected_mover_is_recovered_with_correct_velocity(settings, tmp_path):
    mover = {"dx_px": -60, "dy_px": 20, "vx_px_s": 0.25, "vy_px_s": -0.10, "mag": 14.5}
    findings = analyze(_capture(settings, tmp_path, "mv", 7, mover), figure_dir=tmp_path / "fig")
    assert len(findings["detections"]) == 1
    det = findings["detections"][0]
    assert det["n_points"] == 4
    assert det["vx_px_s"] == pytest.approx(0.25, abs=0.01)
    assert det["vy_px_s"] == pytest.approx(-0.10, abs=0.01)
    assert det["quality"] == "good"
    assert findings["figures"] and (tmp_path / "fig" / "reduction_overview.png").is_file()


@pytest.mark.slow
def test_hot_pixels_and_stars_are_not_reported(settings, tmp_path):
    for seed in range(5):
        findings = analyze(_capture(settings, tmp_path, f"null{seed}", 100 + seed, None))
        assert findings["detections"] == [], f"false positive in null field seed {seed}"


def test_too_few_frames_degrades_gracefully(settings, tmp_path):
    paths = _capture(settings, tmp_path, "few", 5, None)[:2]
    findings = analyze(paths)
    assert findings["detections"] == []
    assert any("usable frame" in w for w in findings["warnings"])
