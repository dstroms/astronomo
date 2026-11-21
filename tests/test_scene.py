"""The simulation boundary: shared tools are generic, science-case simulation lives in plugins.

A brand-new science case (here: a blinking variable star, defined in this file) must be able to
simulate its own sky -- camera, offline Gaia and all -- without touching ``tools/``.
"""

import ast
import re
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from core.base_plugin import BaseExperimentPlugin
from core.graph import run_mission
from core.state import initial_state
from plugins.asteroid_finder.scene import AsteroidScene
from tests.conftest import Harness
from tools.catalogs import CatalogClient, CatalogError
from tools.mock_sky import FrameContext, MockSky, SkyScene, counts_for_mag

ROOT = Path(__file__).resolve().parent.parent
EPOCH0 = "2026-10-21T04:00:00+00:00"
TARGET = {"name": "Blinker", "ra_deg": 40.0, "dec_deg": 10.0,
          "sim": {"seed": 5, "epoch0_iso": EPOCH0, "pointing_error_arcsec": [20.0, -10.0],
                  "blinker": {"dx_px": 30.0, "dy_px": -20.0, "mag": 12.0, "period_s": 20.0}}}


class BlinkerScene(SkyScene):
    """A catalogued star that is bright on even frames, dark on odd ones, in a drifting, dimming field."""

    def __init__(self, cfg: dict, drift=(0.0, 0.0), dim_per_hr=0.0):
        self.cfg, self.drift, self.dim_per_hr = cfg, drift, dim_per_hr

    def _pixel(self, sky):
        ny, nx = sky.shape
        return (nx - 1) / 2.0 + self.cfg["dx_px"], (ny - 1) / 2.0 + self.cfg["dy_px"]

    def pointing_offset_px(self, sky, when):
        hours = (when - sky.epoch0).total_seconds() / 3600.0
        return self.drift[0] * hours, self.drift[1] * hours

    def transparency(self, sky, when, frame_index):
        return 1.0 - self.dim_per_hr * (when - sky.epoch0).total_seconds() / 3600.0

    def catalog_stars(self, sky):
        return [(*self._pixel(sky), self.cfg["mag"])]

    def draw(self, sky, signal, ctx: FrameContext):
        if ctx.frame_index % 2 == 0:
            x, y = self._pixel(sky)
            sky.stamp(signal, x + ctx.offset_px[0], y + ctx.offset_px[1],
                      counts_for_mag(self.cfg["mag"], ctx.exposure_s) * ctx.transparency)


def _counts(img, xy):
    x, y = (int(round(v)) for v in xy)
    box = img[y - 8:y + 9, x - 8:x + 9].astype(float)
    return float(box.sum() - np.median(img) * box.size)


# --------------------------------------------------------------------------- #
# MockSky + SkyScene
# --------------------------------------------------------------------------- #
def test_a_plain_sky_has_no_science_case_content():
    sky = MockSky.from_target(TARGET)  # no scene: just stars
    assert len(sky.star_catalog()["ra"]) == len(sky.stars()["x"])
    explicit = MockSky.from_target(TARGET, scene=SkyScene())
    assert np.array_equal(sky.render(sky.epoch0, 10.0, 0), explicit.render(sky.epoch0, 10.0, 0))
    # sim keys that only a plugin understands are inert to the generic sky
    assert np.array_equal(sky.render(sky.epoch0, 10.0, 0), MockSky.from_target({**TARGET, "sim": {**TARGET["sim"], "blinker": None}}).render(sky.epoch0, 10.0, 0))


def test_a_scene_draws_sources_and_reports_its_stars_to_the_catalogue():
    scene = BlinkerScene(TARGET["sim"]["blinker"])
    sky = MockSky.from_target(TARGET, scene=scene)
    cat = sky.star_catalog()
    assert len(cat["ra"]) == len(sky.stars()["x"]) + 1 and cat["mag"][-1] == 12.0
    xy = scene._pixel(sky)
    on, off = sky.render(sky.epoch0, 10.0, 0), sky.render(sky.epoch0, 10.0, 1)
    assert _counts(on, xy) > 20 * abs(_counts(off, xy)) + 1000  # present on even frames, absent on odd


def test_a_scene_can_move_and_dim_the_whole_field():
    scene = BlinkerScene(TARGET["sim"]["blinker"], drift=(6.0, -4.0), dim_per_hr=0.25)  # 100 % -> 50 % over 2 h
    sky = MockSky.from_target(TARGET, scene=scene)
    x, y = scene._pixel(sky)
    now = sky.render(sky.epoch0, 10.0, 0)
    later = sky.render(sky.epoch0 + timedelta(hours=2), 10.0, 2)
    assert _counts(later, (x + 12.0, y - 8.0)) / _counts(now, (x, y)) == pytest.approx(0.5, abs=0.06)
    # a bright catalogue star far from the target moved with the field and dimmed with it
    s = sky.stars()
    i = int(np.argmin(np.where((np.abs(s["x"] - 960) < 700) & (np.abs(s["y"] - 540) < 400), s["mag"], 99.0)))
    px, py = s["x"][i], s["y"][i]
    assert _counts(later, (px + 12.0, py - 8.0)) / _counts(now, (px, py)) == pytest.approx(0.5, abs=0.1)


def test_offline_gaia_includes_the_scene_stars_only_when_given_the_scene(settings):
    cc = CatalogClient(settings)
    scene = BlinkerScene(TARGET["sim"]["blinker"])
    sky = MockSky.from_target(TARGET, scene=scene)
    ra, dec = sky.pixel_to_radec(*scene._pixel(sky))
    rows = cc.gaia_cone(ra, dec, 5.0, 17.0, target=TARGET, scene=scene)
    assert [round(r["g_mag"], 1) for r in rows] == [12.0] and rows[0]["source"] == "simulated"
    assert cc.gaia_cone(ra, dec, 5.0, 17.0, target=TARGET) == []  # the plain sky has nothing there


def test_horizons_has_no_generic_offline_stand_in(settings):
    with pytest.raises(CatalogError, match="no generic offline stand-in"):
        CatalogClient(settings).horizons_ephemeris("433", [])


# --------------------------------------------------------------------------- #
# The asteroid scene: camera and offline ephemeris agree (the point of one shared scene)
# --------------------------------------------------------------------------- #
def test_asteroid_ephemeris_matches_where_the_mover_is_rendered():
    mover = {"dx_px": -60.0, "dy_px": 20.0, "vx_px_s": 0.25, "vy_px_s": -0.10, "mag": 14.5}
    target = {"name": "A", "ra_deg": 120.0, "dec_deg": 5.0, "sim": {"seed": 7, "epoch0_iso": EPOCH0, "mover": mover}}
    scene = AsteroidScene.from_target(target)
    sky = MockSky.from_target(target, scene=scene)
    when = sky.epoch0 + timedelta(seconds=25)
    eph = scene.ephemeris(sky, "433", [when])[0]
    assert (eph["ra_deg"], eph["dec_deg"]) == scene.mover_radec(sky, when)
    assert eph["source"] == "simulated" and eph["v_mag"] == 14.5 and eph["rate_arcsec_per_min"] > 10
    assert AsteroidScene(None).ephemeris(sky, "x", [when]) == []


# --------------------------------------------------------------------------- #
# A brand-new plugin through the real graph, with no change to tools/
# --------------------------------------------------------------------------- #
class BlinkerPlugin(BaseExperimentPlugin):
    name = "blinker"
    description = "Test double: a variable star that exists only in this test file."

    def generate_targets(self):
        return [TARGET]

    def get_exposure_plan(self):
        return {"gain": 80, "exposure_s": 10.0, "filter": "IRCUT", "count": 4}

    def mock_scene(self, target):
        return BlinkerScene(target["sim"]["blinker"])

    def analyze_frames(self, fits_paths):
        sky = MockSky.from_target(TARGET, scene=self.mock_scene(TARGET))
        xy = BlinkerScene._pixel(self.mock_scene(TARGET), sky)
        flux = []
        for p in fits_paths:
            with fits.open(p) as hdul:
                flux.append(_counts(hdul[0].data, xy))
        on, off = np.mean(flux[0::2]), np.mean(flux[1::2])
        return {"summary": f"blinks: {on > 20 * abs(off) + 1000}", "blinks": bool(on > 20 * abs(off) + 1000)}


def test_a_new_science_case_simulates_its_own_sky_through_the_graph(settings):
    h = Harness(settings, BlinkerPlugin(settings))
    try:
        mid = "m_blinker"
        h.db.create_mission(mid, "blinker", True, {"cloud_cover_threshold_pct": 30.0})
        final = run_mission(h.app, mid, initial_state(mid, "blinker", True))
        assert final["status"] == "COMPLETE"
        assert final["observations"][0]["findings"]["blinks"] is True
    finally:
        h.close()


def test_the_default_scene_hook_is_none():
    assert BaseExperimentPlugin.mock_scene(None, TARGET) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Architecture guard
# --------------------------------------------------------------------------- #
SHARED_TOOLS = ("mock_sky.py", "telescope.py", "catalogs.py", "imaging.py", "astrometry.py")
SCIENCE_WORDS = re.compile(r"mover|asteroid|transit|exoplanet|comet|horizon_target", re.I)


def _identifiers(path: Path) -> set[str]:
    """Names, attributes, defs and non-docstring string constants of a module (comments and docstrings excluded)."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node):
            docstrings.add(id(node.body[0].value))  # type: ignore[attr-defined]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.add(node.value)
    return out


@pytest.mark.parametrize("filename", SHARED_TOOLS)
def test_shared_tools_know_no_science_case(filename):
    names = _identifiers(ROOT / "tools" / filename)
    offenders = sorted(n for n in names if SCIENCE_WORDS.search(n))
    assert not offenders, f"tools/{filename} mentions a science case: {offenders}"


def test_tools_and_core_never_import_plugins():
    for folder in ("tools", "core"):
        for path in (ROOT / folder).glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                assert not any(m == "plugins" or m.startswith("plugins.") for m in mods), f"{path.name} imports {mods}"
