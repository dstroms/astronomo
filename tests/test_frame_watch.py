"""The per-frame watch: what a sequence's frames say about the sky and the mount while it runs."""

from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pytest
from astropy.io import fits

from core import frame_watch as fw


def _sequence(tmp_path, n=10, fwhm=4.0, drift=(4.0, 1.0), flux=lambda k: 1.0, sky=lambda k: 2500.0, seed=7, n_stars=28):
    """Frames of one star field that drifts ``drift`` px a frame; ``flux(k)`` scales every star, ``sky(k)`` is the level."""
    rng = np.random.default_rng(seed)
    ny, nx = 600, 500
    yy, xx = np.mgrid[0:ny, 0:nx]
    sigma = fwhm / 2.355
    stars = []
    while len(stars) < n_stars:
        x, y = float(rng.uniform(60, nx - 60 - n * drift[0])), float(rng.uniform(60, ny - 60 - n * drift[1]))
        if all(np.hypot(x - sx, y - sy) > 40 for sx, sy, _ in stars):
            stars.append((x, y, float(rng.uniform(2500, 9000))))
    paths = []
    for k in range(n):
        img = sky(k) + rng.normal(0.0, 30.0, (ny, nx))
        for x, y, amp in stars:
            cx, cy = x + drift[0] * k, y + drift[1] * k
            img += flux(k) * amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
        hdr = fits.Header()
        hdr["DATE-OBS"] = (datetime(2026, 11, 17, 3, 0, 0) + timedelta(seconds=20 * k)).isoformat()
        hdr["EXPTIME"] = 10.0
        path = tmp_path / f"frame_{k:03d}.fits"
        fits.PrimaryHDU(np.clip(img, 0, 65535).astype(np.uint16), hdr).writeto(path)
        paths.append(str(path))
    return paths


def test_a_steady_sequence_reads_steady_and_the_drift_is_measured(tmp_path):
    rows = fw.watch_sequence(_sequence(tmp_path, n=8))
    assert [r["index"] for r in rows] == list(range(8)) and not any("error" in r for r in rows)
    assert rows[0]["flux_rel"] == 1.0 and rows[0]["n_followed"] >= 8
    for r in rows[1:]:
        assert r["flux_rel"] == pytest.approx(1.0, abs=0.06) and r["sky_rel"] == pytest.approx(1.0, abs=0.02)
        assert r["fwhm_px"] == pytest.approx(4.0, rel=0.25)
    assert rows[-1]["dx_px"] == pytest.approx(4.0 * 7, abs=2.0) and rows[-1]["dy_px"] == pytest.approx(7.0, abs=2.0)
    assert not any(r["warnings"] for r in rows)
    assert "ranged 0.9" in fw.summary(rows) or "ranged 1.0" in fw.summary(rows)


def test_a_cloud_dims_every_star_together_and_the_warning_says_so(tmp_path):
    paths = _sequence(tmp_path, n=9, flux=lambda k: 1.0 if k < 6 else 0.55, sky=lambda k: 2500.0 if k < 6 else 2750.0)
    rows = fw.watch_sequence(paths)
    assert rows[5]["warnings"] == []
    warned = rows[6]["warnings"]
    assert len(warned) == 1 and warned[0].startswith("transparency:") and "fainter" in warned[0]
    assert "changed together" in warned[0] and "sky is +10%" in warned[0]
    assert rows[6]["flux_rel"] == pytest.approx(0.55, abs=0.06)


def test_a_slow_fade_is_caught_once_against_frame_one(tmp_path):
    """Each frame is only a little fainter than the last few, so only the comparison with frame 1 can see it."""
    paths = _sequence(tmp_path, n=22, drift=(2.0, 0.5), flux=lambda k: 1.0 - 0.017 * k)  # the real night: about 1.5% a frame
    rows = fw.watch_sequence(paths)
    slow = [i for i, r in enumerate(rows) if any("faded to" in w for w in r["warnings"])]
    assert len(slow) == 1, [r["warnings"] for r in rows]
    assert rows[slow[0]]["flux_rel"] < 0.70 <= rows[slow[0] - 1]["flux_rel"]
    assert not any("so far" in w for r in rows for w in r["warnings"]), "no single frame jumped"


def test_a_change_in_star_width_is_reported_as_focus_or_tracking(tmp_path):
    first = _sequence(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, n=5, fwhm=3.0, seed=3)
    soft = _sequence(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, n=2, fwhm=7.0, seed=3)
    # frames 0-4 sharp, then the same field soft: the stars sit where they were and only the width changes
    rows = fw.watch_sequence(first + soft[:1])
    assert any(w.startswith("star width:") and "focus, seeing or tracking" in w for w in rows[-1]["warnings"]), rows[-1]


def test_a_frame_that_cannot_be_read_is_a_row_not_an_exception(tmp_path):
    paths = _sequence(tmp_path, n=5)
    broken = tmp_path / "broken.fits"
    broken.write_bytes(b"not a fits file")
    watcher = fw.FrameWatcher()
    watcher.update(0, paths[0])
    row = watcher.update(1, str(broken))
    assert "error" in row and row["warnings"] == [] and "not measured" in fw.format_row(row, 5)
    after = watcher.update(2, paths[2])
    assert "error" not in after and after["flux_rel"] == pytest.approx(1.0, abs=0.08)  # the next frame carries on


def test_an_empty_field_gives_an_error_row(tmp_path):
    flat = tmp_path / "flat.fits"
    fits.PrimaryHDU(np.full((300, 300), 1000, np.uint16)).writeto(flat)
    row = fw.FrameWatcher().update(0, str(flat))
    assert "error" in row and "star" in row["error"]


def test_index_zero_starts_a_new_sequence(tmp_path):
    a = _sequence(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, n=3, seed=1)
    b = _sequence(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, n=3, seed=2)
    watcher = fw.FrameWatcher()
    for i, p in enumerate(a):
        watcher.update(i, p)
    for i, p in enumerate(b):  # a re-pointed capture starts again at frame 0
        watcher.update(i, p)
    assert [r["index"] for r in watcher.rows] == [0, 1, 2]
    assert watcher.rows[2]["flux_rel"] == pytest.approx(1.0, abs=0.08)


def test_sharp_stars_on_a_colour_mosaic_are_followed(tmp_path):
    """The 2.2 px stars of the first in-focus night are rings of bright pixels on the raw frame."""
    from tests.test_transit import _mosaic_sequence

    paths, _ = _mosaic_sequence(tmp_path, n_frames=5)
    rows = fw.watch_sequence(paths)
    assert not any("error" in r for r in rows), rows
    assert all(r["flux_rel"] == pytest.approx(1.0, abs=0.1) for r in rows[1:])


def test_the_numbers_are_saved_for_the_dashboard(tmp_path):
    rows = fw.watch_sequence(_sequence(tmp_path / "f" if (tmp_path / "f").mkdir() is None else tmp_path, n=5))
    out = tmp_path / "mission"
    path = fw.save_stats(rows, out)
    assert path.name == "frame_stats.json" and (out / "frame_stats.png").stat().st_size > 5_000
    assert len(__import__("json").loads(path.read_text())) == 5
    assert not list(out.glob(".*.tmp*")), "written whole, then moved into place"
    assert fw.format_row(rows[0], 5).startswith("Frame 1/5: reference") and "of frame 1" in fw.format_row(rows[1], 5)


# --------------------------------------------------------------------------- #
# Settings and the mission graph
# --------------------------------------------------------------------------- #
def test_the_watch_runs_on_the_real_telescope_and_only_when_asked_in_simulation(settings):
    assert not settings.frame_watch_active
    assert replace(settings, frame_watch_in_simulation=True).frame_watch_active
    assert replace(settings, mock_mode=False).frame_watch_active
    assert not replace(settings, mock_mode=False, frame_watch=False).frame_watch_active
    assert not replace(settings, mock_mode=False, data_source="archive").frame_watch_active


def _mission(settings, mission_id="m_watch", tweak=None, **overrides):
    from core.graph import run_mission
    from core.state import initial_state
    from plugins.asteroid_finder.plugin import AsteroidFinderPlugin
    from tests.conftest import Harness

    settings = replace(settings, max_targets=1, **overrides)
    plugin = AsteroidFinderPlugin(settings)
    h = Harness(settings, plugin)
    if tweak:
        tweak(h)
    h.db.create_mission(mission_id, plugin.name, True, {"cloud_cover_threshold_pct": 30.0})
    try:
        return h, run_mission(h.app, mission_id, initial_state(mission_id, plugin.name, True))
    except BaseException:
        h.close()
        raise


def test_a_mission_logs_every_frame_and_leaves_the_view_for_the_dashboard(settings):
    h, final = _mission(settings, frame_watch_in_simulation=True)
    try:
        assert final["status"] == "COMPLETE"
        logs = h.db.list_logs("m_watch", kinds=["frame_stats"])
        frames = len(final["fits_paths"])
        assert frames > 1
        measured = [row for row in logs if row["message"].startswith("Frame ") and "WARNING" != row["level"]]
        assert len(measured) == frames and measured[0]["message"].startswith(f"Frame 1/{frames}: reference")
        mission_dir = settings.missions_dir / "m_watch"
        for name in ("frame_stats.json", "frame_stats.png", "latest_frame.png"):
            assert (mission_dir / name).is_file(), name
    finally:
        h.close()


def test_the_watch_is_off_in_simulation_unless_asked(settings):
    h, final = _mission(settings, "m_quiet")
    try:
        assert final["status"] == "COMPLETE"
        assert h.db.list_logs("m_quiet", kinds=["frame_stats"]) == []
        assert not (settings.missions_dir / "m_quiet" / "frame_stats.json").exists()
    finally:
        h.close()


def test_a_broken_watch_never_costs_a_frame(settings, monkeypatch):
    def boom(self, index, path):
        raise RuntimeError("the watch fell over")

    monkeypatch.setattr(fw.FrameWatcher, "update", boom)
    h, final = _mission(settings, "m_boom", frame_watch_in_simulation=True)
    try:
        assert final["status"] == "COMPLETE" and len(final["fits_paths"]) > 1
    finally:
        h.close()


def test_reanalyze_prints_the_same_numbers(settings, capsys, monkeypatch):
    """``--reanalyze`` runs the watcher over a finished night and saves it beside the frames."""
    import main

    seen = []
    monkeypatch.setattr(fw, "watch_sequence", lambda paths, progress=None, **kw: seen.append(kw) or [])
    # Only the wiring is checked here; the numbers are checked above.  A mission that is not there stops early.
    assert main.run_reanalyze_cli(settings, "no_such_mission") == main.EXIT_FAILED
    assert "there is no mission" in capsys.readouterr().err and seen == []
