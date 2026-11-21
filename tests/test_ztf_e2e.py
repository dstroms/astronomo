"""A whole asteroid-finder mission on replayed ZTF-shaped cutouts, through the real graph.

IRSA (MOST + IBE), JPL Horizons and Gaia are faked; the rest is production code: the ZTF adapter builds
the visits and cutouts, the harness waits between them on the replay clock, the reduction registers the
frames from their WCS and finds the mover blind, and the cross-reference checks it against an ephemeris
computed for Palomar (MPC code I41) with the survey WCS kept and Gaia used only as a check.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest
from astropy.time import Time
from astropy.wcs import WCS

from core.clock import parse_time
from core.graph import run_mission
from core.state import initial_state
from plugins.asteroid_finder import ztf
from plugins.asteroid_finder.plugin import AsteroidFinderPlugin
from tests.conftest import Harness, make_settings, offline_world
from tests.test_archive_adapters import FakeIRSA, _burst, _render, _stars, _tan_header
from tools.catalogs import CatalogClient

pytestmark = pytest.mark.slow  # every test here runs a whole mission

MID = "m_ztf_e2e"
SHAPE = (200, 200)
FRAMES = 8
VX, VY = 5.0, 2.0  # mover speed, px per frame (5.4 arcsec per 34.6 s frame: fast, so 8 frames make a long track)
START = (30.0, 40.0)


class SkyIRSA(FakeIRSA):
    """Cutouts of a static star field with one mover; frame ``i`` of a visit has the mover at START + i*(VX, VY)."""

    def __init__(self, rows):
        super().__init__(rows)
        self.stars = _stars(np.random.default_rng(8), SHAPE, n=30, margin=15, min_sep=12.0)
        self.field = _render(SHAPE, self.stars)

    def mover_xy(self, i):
        return START[0] + VX * i, START[1] + VY * i

    def _image(self, row, index):
        x, y = self.mover_xy(index % FRAMES)
        return 100.0 + self.field + _render(SHAPE, [(x, y, 4000.0)]) + self.rng.normal(0, 5, SHAPE)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One two-visit mission, read by every test below (they only look at what it left behind)."""
    with offline_world(ASTRONOMO_ZTF_MAX_VISITS="2") as monkeypatch:
        h, final, ephemeris_calls = _run_ztf_mission(make_settings(tmp_path_factory.mktemp("ztf_e2e") / "data"), monkeypatch)
    try:
        yield h, final, ephemeris_calls
    finally:
        h.close()


def _run_ztf_mission(settings, monkeypatch):
    s = replace(settings, data_source="archive", offline_services=False, replay_speed=0.0, llm_provider="none")
    rows = _burst(FRAMES) + _burst(FRAMES, jd=2458503.8 + 20, base="2019020930")
    fake = SkyIRSA(rows)
    plugin = AsteroidFinderPlugin(s)
    plugin._archive = ztf.ZTFArchive(s.archive_cache_dir, http_get_fn=fake, cutout_arcmin=2.0)
    wcs = WCS(_tan_header(SHAPE, crval=(130.5, 12.25), scale_arcsec=1.01))

    ephemeris_calls: list[str | None] = []
    t_first: dict[str, datetime] = {}

    def horizons(self, designation, epochs, location=None):
        ephemeris_calls.append(location)
        out = []
        for ep in epochs:
            # frame index inside the visit the epoch belongs to (visits are days apart, frames 0.0004 d apart)
            visit_start = max((r for r in rows if Time(r["obsjd"], format="jd").to_datetime(timezone=timezone.utc) <= ep), key=lambda r: r["obsjd"])
            k = round((Time(ep).jd - float(visit_start["obsjd"])) / 0.0004) + rows.index(visit_start) % FRAMES
            x, y = fake.mover_xy(k)
            ra, dec = wcs.all_pix2world([x], [y], 0)
            rate = float(np.hypot(VX, VY)) * 1.01 / (0.0004 * 1440.0)  # arcsec per minute
            out.append({"designation": designation, "epoch_iso": ep.isoformat(), "ra_deg": float(ra[0]), "dec_deg": float(dec[0]),
                        "rate_arcsec_per_min": rate, "pa_deg": 0.0, "v_mag": 16.3, "delta_au": 1.5, "r_au": 2.4, "elevation_deg": 60.0,
                        "source": "jpl-horizons"})
        return out

    sras, sdecs = wcs.all_pix2world([st[0] for st in fake.stars], [st[1] for st in fake.stars], 0)
    gaia = [{"source_id": i, "ra_deg": float(a), "dec_deg": float(d), "g_mag": 13.0 + 0.1 * i, "pmra": 0.0, "pmdec": 0.0, "source": "gaia-dr3"}
            for i, (a, d) in enumerate(zip(sras, sdecs))]
    monkeypatch.setattr(CatalogClient, "horizons_ephemeris", horizons)
    monkeypatch.setattr(CatalogClient, "gaia_cone", lambda self, *a, **k: gaia)

    h = Harness(s, plugin)
    try:
        h.db.create_mission(MID, "asteroid_finder", False, {}, "archive")
        final = run_mission(h.app, MID, initial_state(MID, "asteroid_finder", False, "archive"))
    except BaseException:
        h.close()
        raise
    return h, final, ephemeris_calls


def test_two_ztf_visits_are_replayed_found_blind_and_matched_to_the_palomar_ephemeris(run):
    h, final, ephemeris_calls = run
    assert final["status"] == "COMPLETE" and len(final["observations"]) == 2
    for obs in final["observations"]:
        assert obs["findings"]["data_source"] == "archive" and len(obs["findings"]["detections"]) >= 1
        entry = obs["crossref"]["entries"][0]
        assert entry["classification"] == "known_object_match" and entry["match"]["rate_consistent"] is True
        assert entry["separation_arcsec"] < 3.0
        assert entry["match"]["ephemeris_delta_au"] == 1.5 and entry["match"]["ephemeris_r_au"] == 2.4
        cal = obs["crossref"]["calibration"]
        assert cal["status"] == "survey_wcs" and cal["gaia_check"] == "calibrated" and cal["offset_arcsec"] < 1.0
        assert obs["archive"]["observatory_code"] == "I41" and obs["archive"]["n_used"] == FRAMES
    assert ephemeris_calls and set(ephemeris_calls) == {"I41"}  # Palomar, not the user's own site


def test_the_harness_waits_between_visits_and_never_checks_weather(run):
    h, final, _ = run
    nodes = [e["node"] for e in h.db.list_logs(MID, limit=3000, kinds=["node_start"])]
    assert nodes.count("wait_for_visit") == 2 and "check_weather" not in nodes
    assert h.captures == 0 and h.db.list_weather(MID) == []
    targets = h.db.list_targets(MID)
    assert [t["status"] for t in targets] == ["OBSERVED", "OBSERVED"]
    gap = datetime.fromisoformat(targets[1]["payload"]["not_before"]) - datetime.fromisoformat(targets[0]["payload"]["not_before"])
    assert 19 <= gap.days <= 20
    assert parse_time(h.db.get_mission(MID)["sim_time"]) >= parse_time(targets[1]["payload"]["not_before"])  # the clock reached visit 2


def test_the_mission_log_credits_ztf_and_is_not_labelled_a_simulation(run):
    _, final, _ = run
    text = open(final["mission_log"]["path"], encoding="utf-8").read()
    assert final["mission_log"]["path"].endswith(".md") and "paper" not in final
    assert "## Acknowledgements" in text and "Zwicky Transient Facility" in text
    assert "Simulation." not in text  # real-format archive data is not labelled a simulation
