"""The observing site: where it comes from, what blocks its view, and what changes when it changes."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

import config
import main
from core import interlocks, site
from core.interlocks import SiteLimits, evaluate, minimum_altitude
from core.site import SiteError
from tests.test_interlocks import NOW, SITE, _target
from tests.test_opportunities import _by_name, live_settings, served  # noqa: F401 - the two fixtures


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("args, phrase", [
    (("x", 91, 0, 0), "latitude 91 is outside"),
    (("x", 0, 181, 0), "longitude 181 is outside"),
    (("x", 0, 0, 20000), "elevation 20000 m is outside"),
    (("x", "north", 0, 0), "must be numbers"),
    (("x", float("nan"), 0, 0), "finite"),
])
def test_a_site_out_of_range_is_refused_with_a_reason(args, phrase):
    with pytest.raises(SiteError, match=phrase):
        site.validated_site(*args)


def test_a_valid_site_is_tidied():
    s = site.validated_site("  ", 39.739236, -104.990251, 1609.04, [[180, 40], [90, 20]])
    assert s == {"name": "My site", "lat": 39.73924, "lon": -104.99025, "elev_m": 1609.0, "horizon": [[90.0, 20.0], [180.0, 40.0]]}


def test_a_horizon_is_parsed_from_what_a_person_types():
    assert site.parse_horizon("# house\n180 40\n90, 20\n\n  270;25 ") == ((90.0, 20.0), (180.0, 40.0), (270.0, 25.0))
    assert site.parse_horizon("") == ()
    for bad, phrase in (("180", "line 1"), ("a b", "line 1"), ("90 20\n400 10", "azimuth must be 0 to 360"), ("90 95", "altitude 0 to 89")):
        with pytest.raises(SiteError, match=phrase):
            site.parse_horizon(bad)
    assert site.horizon_text(((90.0, 20.0), (180.0, 40.5))) == "90 20\n180 40.5"


def test_the_horizon_is_a_straight_line_between_points_that_wraps_at_north():
    h = ((90.0, 20.0), (270.0, 40.0))
    assert site.horizon_altitude(h, 90.0) == pytest.approx(20.0) and site.horizon_altitude(h, 270.0) == pytest.approx(40.0)
    assert site.horizon_altitude(h, 180.0) == pytest.approx(30.0)  # half way along the straight line
    assert site.horizon_altitude(h, 0.0) == pytest.approx(30.0)  # across north the line goes from 270 (40) round to 90 (20)
    assert site.horizon_altitude(h, 360.0) == pytest.approx(30.0)
    assert list(site.horizon_altitude(h, np.array([90.0, 270.0]))) == pytest.approx([20.0, 40.0])
    assert site.horizon_altitude((), 123.0) == 0.0


# --------------------------------------------------------------------------- #
# Where the site comes from
# --------------------------------------------------------------------------- #
@pytest.fixture()
def clean_env(monkeypatch):
    for key in ("ASTRONOMO_SITE_LAT", "ASTRONOMO_SITE_LON", "ASTRONOMO_SITE_ELEV_M"):
        monkeypatch.delenv(key, raising=False)


def test_with_nothing_set_the_site_is_the_placeholder_and_says_so(tmp_path, clean_env):
    s = config.get_settings(data_dir=tmp_path)
    assert s.site_source == "default" and "placeholder" in site.describe(s)
    assert s.public_dict()["site"]["source"] == "default"


def test_a_saved_site_is_used_and_survives_a_restart(tmp_path, clean_env):
    site.save_site_file(tmp_path, "Back garden", 51.5, -0.12, 20.0, [[180, 35]])
    s = config.get_settings(data_dir=tmp_path)
    assert (s.site_name, s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m) == ("Back garden", 51.5, -0.12, 20.0)
    assert s.site_source == "file" and s.site_horizon == ((180.0, 35.0),)
    assert json.loads((tmp_path / "site.json").read_text())["name"] == "Back garden"  # plain JSON a person can read
    assert not list(tmp_path.glob("*.tmp"))
    assert s.public_dict()["site"]["horizon"] == [[180.0, 35.0]]


def test_the_environment_wins_over_the_saved_position_but_the_horizon_is_still_used(tmp_path, monkeypatch, clean_env):
    site.save_site_file(tmp_path, "Back garden", 51.5, -0.12, 20.0, [[180, 35]])
    monkeypatch.setenv("ASTRONOMO_SITE_LAT", "-33.9")  # Settings reads the variables when it is built; here it is built by hand
    s = site.apply_site(config.Settings(data_dir=tmp_path, site_latitude_deg=-33.9, site_longitude_deg=151.2))
    assert s.site_source == "environment" and s.site_latitude_deg == -33.9 and s.site_name != "Back garden"
    assert s.site_horizon == ((180.0, 35.0),)


def test_an_explicit_site_in_the_arguments_beats_the_saved_one(tmp_path, clean_env):
    site.save_site_file(tmp_path, "Back garden", 51.5, -0.12, 20.0)
    s = config.get_settings(data_dir=tmp_path, site_latitude_deg=10.0)
    assert s.site_latitude_deg == 10.0 and s.site_source == "default"


def test_a_damaged_site_file_is_ignored_not_trusted(tmp_path, clean_env):
    (tmp_path / "site.json").write_text('{"name": "x", "lat": 400, "lon": 0}')
    assert site.load_site_file(tmp_path) is None
    (tmp_path / "site.json").write_text("not json at all")
    assert site.load_site_file(tmp_path) is None
    assert config.get_settings(data_dir=tmp_path).site_source == "default"


def test_saving_a_bad_site_writes_nothing(tmp_path):
    with pytest.raises(SiteError):
        site.save_site_file(tmp_path, "x", 95, 0, 0)
    assert not (tmp_path / "site.json").exists()


# --------------------------------------------------------------------------- #
# Finding a place by name
# --------------------------------------------------------------------------- #
GEOCODED = {"results": [
    {"name": "Boulder", "latitude": 40.01499, "longitude": -105.27055, "elevation": 1655.0, "admin1": "Colorado", "country": "United States"},
    {"name": "Bad", "latitude": 400.0, "longitude": 0.0},
    {"name": "Boulder City", "latitude": 35.97, "longitude": -114.83, "admin1": "Nevada", "country": "United States"},
]}


def test_a_place_search_returns_places_and_drops_nonsense():
    places = site.geocode("Boulder", get_json=lambda url, params: GEOCODED)
    assert [p.label for p in places] == ["Boulder, Colorado, United States", "Boulder City, Nevada, United States"]
    assert places[0].elev_m == 1655.0 and places[1].elev_m is None
    assert site.parse_geocoding({}) == [] and site.parse_geocoding(None) == []


def test_a_place_search_sends_only_what_was_typed_and_says_so_when_it_fails():
    seen = {}
    site.geocode("80302", get_json=lambda url, params: seen.update(url=url, params=params) or {})
    assert seen["url"] == site.GEOCODING_URL and seen["params"]["name"] == "80302" and set(seen["params"]) == {"name", "count", "language", "format"}

    def down(url, params):
        raise ConnectionError("no route")

    with pytest.raises(SiteError, match="enter the coordinates by hand"):
        site.geocode("Boulder", get_json=down)
    with pytest.raises(SiteError, match="at least two characters"):
        site.geocode(" a ")


# --------------------------------------------------------------------------- #
# A horizon in the interlocks
# --------------------------------------------------------------------------- #
def _sky_at(alt, az):
    def fake(ra, dec, lat, lon, elev, times):
        n = len(times)
        full = lambda v: np.full(n, float(v))  # noqa: E731
        return {"target_alt": full(alt), "target_az": full(az), "sun_alt": full(-40), "sun_sep": full(120),
                "moon_alt": full(-20), "moon_sep": full(100), "moon_illum": full(0.5)}
    return fake


def test_the_horizon_only_raises_the_minimum_where_it_is_higher():
    limits = SiteLimits(min_altitude_deg=30.0, horizon=((180.0, 50.0),))
    assert minimum_altitude(180.0, limits) == 50.0
    assert minimum_altitude(0.0, limits) == 50.0  # a single point is a flat line all round
    assert minimum_altitude(180.0, SiteLimits(min_altitude_deg=60.0, horizon=((180.0, 50.0),))) == 60.0  # the stricter wins
    assert minimum_altitude(180.0, SiteLimits()) == 30.0 and minimum_altitude(None, limits) == 30.0
    assert list(minimum_altitude(np.array([0.0, 180.0]), SiteLimits(min_altitude_deg=30.0, horizon=((90.0, 20.0), (270.0, 60.0))))) == [40.0, 40.0]


def test_a_target_behind_the_house_waits_even_though_it_is_above_the_minimum(monkeypatch):
    limits = SiteLimits(min_altitude_deg=30.0, horizon=((150.0, 45.0), (210.0, 45.0)))
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky_at(alt=40.0, az=180.0))
    behind = evaluate(_target(), NOW, SITE, limits)
    assert not behind.ok and not behind.skip and "below the minimum of 45 deg in the direction 180 deg" in behind.reason
    assert behind.facts["required_altitude_deg"] == 45.0
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky_at(alt=40.0, az=300.0))
    clear = evaluate(_target(), NOW, SITE, SiteLimits(min_altitude_deg=30.0, horizon=((150.0, 45.0), (210.0, 45.0), (300.0, 5.0))))
    assert clear.ok  # the same height in a clear direction is fine


def test_the_window_warning_names_the_horizon(monkeypatch):
    from datetime import timedelta

    limits = SiteLimits(min_altitude_deg=30.0, horizon=((0.0, 55.0),))
    monkeypatch.setattr(interlocks, "sky_conditions_many", _sky_at(alt=40.0, az=90.0))
    target = _target(window_start=NOW.isoformat(), window_end=(NOW + timedelta(hours=2)).isoformat())
    notes = interlocks.window_notes(target, NOW, SITE, limits, max_sun_alt_deg=-12.0)
    assert len(notes) == 3 and "only 40 deg high (minimum 55 deg)" in notes[0]


def test_the_sky_reports_which_way_the_target_is():
    from tools.astrometry import sky_conditions_many, sky_from_grid, sun_moon_grid
    from datetime import timedelta

    times = [NOW, NOW + timedelta(hours=2)]
    exact = sky_conditions_many([150.0, 20.0], [10.0, -30.0], *SITE, times)
    assert np.all((exact["target_az"] >= 0) & (exact["target_az"] <= 360))
    grid = sun_moon_grid(*SITE, [NOW + timedelta(minutes=10 * i) for i in range(40)])
    assert np.allclose(sky_from_grid(grid, [150.0, 20.0], [10.0, -30.0], *SITE, times)["target_az"], exact["target_az"], atol=0.01)
    # Polaris is within a degree of the celestial pole, so it is almost due north (azimuth 0 or 360) from anywhere
    polaris = sky_conditions_many([37.95], [89.26], *SITE, [NOW])
    assert min(float(polaris["target_az"][0]), 360.0 - float(polaris["target_az"][0])) < 3.0


# --------------------------------------------------------------------------- #
# What the site changes in the Opportunities report
# --------------------------------------------------------------------------- #
def _report_for(settings, **site_fields):
    from plugins.transit_finder import opportunities as opp
    from tests.test_opportunities import NOW as T

    moonless = dataclasses.replace(settings, moon_avoid_deg=0.0, moon_floor_deg=0.0, **site_fields)
    return opp.build_report(moonless, now=T, days=30, refresh=True)


def test_the_same_lists_give_different_answers_at_different_places(live_settings, served):
    denver = _by_name(_report_for(live_settings))
    canberra = _by_name(_report_for(live_settings, site_name="Canberra", site_latitude_deg=-35.3, site_longitude_deg=149.1, site_elevation_m=580.0))
    assert any("never climbs high enough" in b for b in denver["South-4 b"]["blockers"])  # dec -60 from latitude 40
    assert not any("never climbs high enough" in b for b in canberra["South-4 b"]["blockers"])  # but it climbs to 65 deg from latitude -35
    assert any("never climbs high enough" in b for b in canberra["Fresh-1 b"]["blockers"])  # and dec +40 does not


def test_a_high_horizon_removes_windows_and_the_report_says_why(live_settings, served):
    clear = _by_name(_report_for(live_settings))
    walled = _report_for(live_settings, site_horizon=((0.0, 85.0), (180.0, 85.0)), site_source="file")
    ops = _by_name(walled)
    assert clear["Stale-2 b"]["windows"] and not ops["Stale-2 b"]["windows"]
    assert "behind your horizon" in " ".join(ops["Stale-2 b"]["blockers"])
    assert any("horizon is the 2 points" in c for c in walled["caveats"])
    assert any("No horizon is saved" in c for c in _report_for(live_settings)["caveats"])
    assert walled["site"]["source"] == "file" and _report_for(live_settings)["site"]["source"] == "default"


def test_a_low_horizon_changes_nothing(live_settings, served):
    clear = _by_name(_report_for(live_settings))
    low = _by_name(_report_for(live_settings, site_horizon=((0.0, 5.0), (180.0, 10.0))))
    assert [w["mid"] for w in low["Stale-2 b"]["windows"]] == [w["mid"] for w in clear["Stale-2 b"]["windows"]]


def test_the_report_is_recomputed_when_the_site_changes(tmp_path):
    from dashboard import opportunities as view
    from tests.test_dashboard_opportunities import NOW as T

    calls = []
    original = view.collect_reports
    view.collect_reports = lambda settings, **k: calls.append(1) or ([], [])  # noqa: E731
    try:
        s = config.get_settings(data_dir=tmp_path)
        state: dict = {}
        view.get_reports(state, s, now=T)
        view.get_reports(state, s, now=T)
        assert len(calls) == 1
        view.get_reports(state, dataclasses.replace(s, site_latitude_deg=10.0), now=T)
        assert len(calls) == 2
        view.get_reports(state, dataclasses.replace(s, site_horizon=((90.0, 30.0),)), now=T)
        assert len(calls) == 3
    finally:
        view.collect_reports = original


# --------------------------------------------------------------------------- #
# A real telescope needs a real site
# --------------------------------------------------------------------------- #
def test_a_real_telescope_run_refuses_the_placeholder_site(tmp_path, monkeypatch, capsys, clean_env):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "d")
    real = dataclasses.replace(config.get_settings(), mock_mode=False)
    monkeypatch.setattr(main, "build_settings", lambda args: real)
    assert real.site_source == "default"
    assert main.main(["--plugin", "asteroid_finder"]) == main.EXIT_FAILED
    err = capsys.readouterr().err
    assert "no observing site is set" in err and "Observing site" in err and "ASTRONOMO_SITE_LAT" in err


def test_a_simulated_run_does_not_need_a_site(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "d")
    s = config.get_settings()
    assert s.mock_mode and s.site_source == "default"  # the guard is for real hardware only; the rest of the suite runs on this


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.api.app import create_app

    return TestClient(create_app(data_dir=tmp_path, serve_frontend=False))


def test_the_page_warns_that_the_site_is_a_placeholder_until_one_is_saved(tmp_path, clean_env):
    client = _client(tmp_path)
    shell = client.get("/api/v1/shell").json()
    assert shell["site"]["source"] == "default"
    assert "No observing site is set" in (shell["site"].get("banner") or "")

    saved = client.put("/api/v1/settings/site", json={
        "name": "Back garden", "lat": 51.5, "lon": -0.12, "elev_m": 20.0, "horizon": [[180, 40], [90, 20]],
    })
    assert saved.status_code == 200 and saved.json()["ok"] is True
    written = json.loads((tmp_path / "site.json").read_text())
    assert written["name"] == "Back garden" and written["lat"] == 51.5
    assert written["horizon"] == [[90.0, 20.0], [180.0, 40.0]]
    after = client.get("/api/v1/shell").json()
    assert after["site"]["source"] == "file"
    assert after["site"].get("banner") in (None, "")


def test_a_bad_site_is_not_saved_and_the_page_says_why(tmp_path, clean_env):
    client = _client(tmp_path)
    bad = client.put("/api/v1/settings/site", json={"name": "x", "lat": 95.0, "lon": 0.0, "elev_m": 0.0})
    assert bad.status_code == 400 and "latitude 95 is outside" in bad.json()["detail"]
    assert not (tmp_path / "site.json").exists()
    assert "No observing site is set" in (client.get("/api/v1/shell").json()["site"].get("banner") or "")


def test_searching_for_a_place_fills_in_the_coordinates_to_check_and_save(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(site, "geocode", lambda query, **k: site.parse_geocoding(GEOCODED) if query == "Boulder" else [])
    client = _client(tmp_path)
    found = client.post("/api/v1/settings/site/search", json={"query": "Boulder"})
    assert found.status_code == 200
    labels = [p["label"] for p in found.json()["places"]]
    assert labels == ["Boulder, Colorado, United States", "Boulder City, Nevada, United States"]
    pick = found.json()["places"][0]
    assert pick["lat"] == pytest.approx(40.01499) and pick["elev_m"] == 1655.0 and pick["name"] == "Boulder"
    assert not (tmp_path / "site.json").exists()
    saved = client.put("/api/v1/settings/site", json={
        "name": pick["name"], "lat": pick["lat"], "lon": pick["lon"], "elev_m": pick["elev_m"] or 0.0,
    })
    assert saved.status_code == 200
    assert json.loads((tmp_path / "site.json").read_text())["name"] == "Boulder"


def test_a_failed_or_empty_search_says_so_and_changes_nothing(tmp_path, monkeypatch, clean_env):
    def down(query, **k):
        raise SiteError("the place search could not be reached (offline); enter the coordinates by hand")

    monkeypatch.setattr(site, "geocode", down)
    client = _client(tmp_path)
    failed = client.post("/api/v1/settings/site/search", json={"query": "Boulder"})
    assert failed.status_code == 400 and "enter the coordinates by hand" in failed.json()["detail"]
    assert not (tmp_path / "site.json").exists()
    monkeypatch.setattr(site, "geocode", lambda query, **k: [])
    empty = client.post("/api/v1/settings/site/search", json={"query": "Boulder"})
    assert empty.status_code == 200 and empty.json()["places"] == []
