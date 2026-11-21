import numpy as np
import pytest

from tools.astrometry import estimate_translation, make_tan_wcs, shift_wcs


def _points(n=120, seed=0):
    return np.random.default_rng(seed).uniform(0, 1900, size=(n, 2))


def test_translation_recovered_with_outliers_and_missing_points():
    rng = np.random.default_rng(1)
    src = _points()
    shift = np.array([11.717, 7.112])  # sits inside a single histogram bin (regression: tie-break bug)
    dst = src + shift + rng.normal(0, 0.1, src.shape)
    dst = dst[rng.random(len(dst)) > 0.3]  # 30% of points missing
    dst = np.vstack([dst, rng.uniform(0, 1900, (30, 2))])  # plus junk
    est = estimate_translation(src, dst, max_shift=150, tol=2.0, min_matches=8)
    assert est is not None
    assert abs(est["dx"] - shift[0]) < 0.2 and abs(est["dy"] - shift[1]) < 0.2
    assert est["n_matches"] > 60


def test_translation_many_shifts_across_bin_boundaries():
    src = _points(seed=3)
    for dx in np.arange(-40, 40, 1.37):
        est = estimate_translation(src, src + [dx, -dx / 2], max_shift=100, tol=2.0)
        assert est is not None and abs(est["dx"] - dx) < 0.05


def test_translation_returns_none_for_unrelated_sets():
    assert estimate_translation(_points(seed=1), _points(seed=2), max_shift=100, tol=2.0, min_matches=10) is None


def test_shift_wcs_moves_sky_to_pixel_mapping():
    wcs = make_tan_wcs(150.0, 10.0, (1080, 1920), 2.39)
    px = np.array(wcs.all_world2pix([150.01], [10.01], 0))
    moved = shift_wcs(wcs, 5.0, -3.0)
    px2 = np.array(moved.all_world2pix([150.01], [10.01], 0))
    assert np.allclose(px2 - px, [[5.0], [-3.0]], atol=1e-6)


# --------------------------------------------------------------------------- #
# Field rotation
# --------------------------------------------------------------------------- #
def _rotated_stars(deg, shift=(12.0, -7.0), noise=0.1, seed=1):
    import math

    rng = np.random.default_rng(seed)
    src = rng.uniform([0, 0], [1920, 1080], (80, 2))
    c = np.array([960.0, 540.0])
    th = math.radians(deg)
    m = np.array([[math.cos(th), math.sin(th)], [-math.sin(th), math.cos(th)]])
    return src, (src - c) @ m + c + np.array(shift) + rng.normal(0, noise, src.shape), (960.0, 540.0)


@pytest.mark.parametrize("deg", [0.0, 0.05, 0.3, -0.3])
def test_estimate_rotation_recovers_the_angle(deg):
    from tools.astrometry import estimate_rotation, estimate_translation

    src, dst, c = _rotated_stars(deg)
    est = estimate_translation(src, dst, max_shift=100, tol=2.0, min_matches=8)
    guess = (est["dx"], est["dy"]) if est else (12.0, -7.0)
    out = estimate_rotation(src, dst, guess, c)
    assert out["angle_deg"] == pytest.approx(deg, abs=0.01) and out["rms_px"] < 0.3


@pytest.mark.parametrize("deg", [1.0, -2.0, 3.5])
def test_find_rotation_explains_a_failed_translation_match(deg):
    from tools.astrometry import estimate_translation, find_rotation

    src, dst, c = _rotated_stars(deg)
    assert estimate_translation(src, dst, max_shift=250, tol=2.0, min_matches=8) is None
    assert find_rotation(src, dst, c)["angle_deg"] == pytest.approx(deg, abs=0.05)


def test_nominal_pixel_scale_matches_the_solved_pro_frames():
    from tools.astrometry import SEESTAR_S50_PIXEL_SCALE_ARCSEC, nominal_pixel_scale_arcsec

    assert nominal_pixel_scale_arcsec((1080, 1920)) == SEESTAR_S50_PIXEL_SCALE_ARCSEC
    assert nominal_pixel_scale_arcsec((3840, 2160)) == SEESTAR_S50_PIXEL_SCALE_ARCSEC
    assert nominal_pixel_scale_arcsec((2160, 3840)) == SEESTAR_S50_PIXEL_SCALE_ARCSEC


def test_match_rigid_recovers_a_rotation_and_a_mirror():
    import math

    from tools.astrometry import match_rigid

    rng = np.random.default_rng(2)
    src = rng.uniform([80, 80], [900, 700], (24, 2))
    center = np.array([480.0, 360.0])
    angle = math.radians(37.0)
    cos, sin = math.cos(angle), math.sin(angle)
    rotation = np.array([[cos, -sin], [sin, cos]])
    shift = np.array([11.0, -6.0])
    dst = (src - center) @ rotation.T + center + shift
    out = match_rigid(src, dst, tuple(center), tol=2.0, min_matches=8)
    assert out["n_matches"] >= 20
    assert out["dx"] == pytest.approx(11.0, abs=0.5) and out["dy"] == pytest.approx(-6.0, abs=0.5)
    assert out["angle_deg"] == pytest.approx(37.0, abs=1.0) and out["mirrored"] is False

    mirrored = src.copy()
    mirrored[:, 0] = 2 * center[0] - mirrored[:, 0]
    flipped = (mirrored - center) @ rotation.T + center + shift
    out = match_rigid(src, flipped, tuple(center), tol=2.0, min_matches=8)
    assert out["mirrored"] is True and out["n_matches"] >= 20
    assert math.hypot(out["dx"] - 11.0, out["dy"] + 6.0) < 1.0


def test_find_rotation_gives_up_on_unrelated_fields():
    from tools.astrometry import find_rotation

    rng = np.random.default_rng(5)
    a, b = rng.uniform(0, 1000, (60, 2)), rng.uniform(0, 1000, (60, 2))
    assert find_rotation(a, b, (500.0, 500.0)) is None
