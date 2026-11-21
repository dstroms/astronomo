"""The Pro-Am clients against trimmed copies of the *real* published files.

``tests/data/exoplanet_watch_real.csv`` and ``tests/data/nasa_ttv_flag_real.csv`` are rows copied
unchanged from the live Exoplanet Watch list (astro.swarthmore.edu/transits) and the NASA Exoplanet
Archive TTV query as downloaded on 2026-10-28.  ``tests/data/exoclock_planets_real.json`` is seven
planets copied unchanged from the live ExoClock ``planets_json`` file on 2026-11-14 (776 planets;
every one parsed).  The tests check the parsers against things that are independently known
(WASP-12 b's period, HD 189733 b's depth, ...) and the failure modes a real network produces
(a "site blocked" page that comes back as HTTP 200 HTML).
"""

import math
import shutil
from pathlib import Path

import pytest

from core import networks
from core.networks import NetworkError
from plugins.transit_finder import archive
from plugins.transit_finder import networks as tn

DATA = Path(__file__).parent / "data"
WATCH = (DATA / "exoplanet_watch_real.csv").read_text()
TTV = (DATA / "nasa_ttv_flag_real.csv").read_text()
JD_REF = 2461000.5
BLOCK_PAGE = "<html><head><title>Website blocked</title></head><body>This site has been blocked.</body></html>"


@pytest.fixture(scope="module")
def planets():
    return {r["planet"]: r for r in tn.parse_exoplanet_watch(WATCH)}


def test_every_real_row_parses_and_none_are_lost():
    rows = tn.parse_exoplanet_watch(WATCH)
    assert len(rows) == len(WATCH.strip().splitlines()) - 1 > 30


def test_parsed_values_agree_with_what_is_known_about_these_planets(planets):
    wasp12 = planets["WASP-12 b"]
    assert wasp12["period_d"] == pytest.approx(1.0914, abs=1e-3)  # literature: 1.09142 d
    assert wasp12["duration_h"] == pytest.approx(3.0, abs=0.3)
    assert wasp12["ra_deg"] == pytest.approx(97.64, abs=0.05) and wasp12["dec_deg"] == pytest.approx(29.67, abs=0.05)
    hd = planets["HD 189733 b"]
    assert hd["depth_pct"] == pytest.approx(2.4, abs=0.3) and hd["v_mag"] == pytest.approx(7.67, abs=0.05)
    assert planets["HD 209458 b"]["period_d"] == pytest.approx(3.5247, abs=1e-3)
    assert planets["WASP-43 b"]["dec_deg"] < 0  # a southern target keeps its sign


def test_the_whole_real_file_is_internally_sane(planets):
    for r in planets.values():
        assert 0 <= r["ra_deg"] < 360 and -90 <= r["dec_deg"] <= 90, r["planet"]
        assert r["period_d"] > 0 and r["duration_h"] > 0 and r["t0_bjd_tdb"] > 2.4e6, r["planet"]
        assert r["depth_pct"] is None or 0 < r["depth_pct"] < 30, r["planet"]
        assert r["source"] == tn.SRC_EXOPLANET_WATCH and isinstance(r["ew_rank"], int)


def test_blank_uncertainties_and_a_huge_period_error_are_handled(planets):
    # Real quirk: a few rows leave the epoch or period uncertainty blank.
    no_t0_err = [r for r in planets.values() if r["planet"] in ("Kepler-105 b", "GJ 3470 b", "Kepler-447 b")]
    assert no_t0_err and all(r["t0_err_d"] == 0.0 for r in no_t0_err)  # blank is read as 0, a small underestimate
    for r in no_t0_err:
        assert math.isfinite(tn.staleness(r, JD_REF)[1])
    no_p_err = [r for r in planets.values() if r["planet"].startswith("Kepler-129")]
    assert no_p_err and all(r["period_err_d"] is None for r in no_p_err)
    for r in no_p_err:  # no stated period error: never "fresh", never a crash, and the reason says why
        assert math.isinf(tn.staleness(r, JD_REF)[0])
        assert "no stated error" in tn.score(r, JD_REF)["reason"]
    worst = max(planets.values(), key=lambda r: r["period_err_d"] or 0)
    assert worst["period_err_d"] > 1000  # e.g. a planet seen once: its schedule is meaningless
    ratio, _ = tn.staleness(worst, JD_REF)
    assert ratio > 100 and tn.score(worst, JD_REF)["score"] < 100  # capped, not infinite or crashing


def test_real_rows_rank_and_every_reason_is_a_sentence(planets):
    ranked = tn.rank(tn.merge([], list(planets.values()), [], JD_REF), JD_REF)
    assert len(ranked) == len(planets)
    assert [r["score"] for r in ranked] == sorted((r["score"] for r in ranked), reverse=True)
    assert all(r["reason"].endswith(".") and len(r["reason"]) > 20 for r in ranked)


def test_the_real_ttv_list_parses_and_flags_the_planets_in_it():
    names = archive.parse_ttv_names(TTV)
    assert "Kepler-82 c" in names and "TOI-216.01" in names
    rows = tn.merge([], tn.parse_exoplanet_watch(WATCH), names, JD_REF)
    flagged = {r["planet"] for r in rows if r["ttv_flag"]}
    assert {"Kepler-82 c", "Kepler-82 b", "TOI-216.01"} <= flagged and "WASP-12 b" not in flagged
    top = tn.rank(rows, JD_REF)[0]
    assert top["task"] == tn.TASK_TTV


def _seed_cache(tmp_path, **files):
    cache = tmp_path / "networks"
    cache.mkdir(parents=True)
    for name, text in files.items():
        (cache / name).write_text(text)
    return tmp_path


def test_fetch_candidates_runs_end_to_end_on_the_real_files_with_exoclock_down(tmp_path):
    root = _seed_cache(tmp_path, **{"exoplanet_watch_targets.csv": WATCH, "nasa_ttv_flag.csv": TTV})
    rows, info = tn.fetch_candidates(root, ttl_s=math.inf, jd_ref=JD_REF)  # the conftest makes the network an outage
    assert len(rows) > 30 and info["from_cache"] is True
    assert any(p.startswith("exoclock:") for p in info["problems"])  # reported, not hidden
    assert set(info["sources"]) == {tn.SRC_EXOPLANET_WATCH, tn.SRC_NASA_TTV}


def test_a_site_blocked_page_never_replaces_a_good_cached_list(tmp_path, monkeypatch):
    """A filter/captive portal answers HTTP 200 with HTML.  Caching that would lose the good copy."""
    cache = _seed_cache(tmp_path, **{"exoplanet_watch_targets.csv": WATCH, "nasa_ttv_flag.csv": TTV}) / "networks"
    old = Path(cache / "exoplanet_watch_targets.csv")
    old_time = 1_700_000_000
    import os
    os.utime(old, (old_time, old_time))  # expired, so a download is attempted
    monkeypatch.setattr(networks, "_http_get_text", lambda url, params, timeout: BLOCK_PAGE)

    rows, info = tn.fetch_candidates(tmp_path, ttl_s=60, jd_ref=JD_REF)

    assert len(rows) > 30 and info["stale_cache"] is True  # served the old copy, and said so
    assert old.read_text() == WATCH  # the blocked page was not cached
    assert not (cache / "exoclock_planets.json").exists()


def test_a_blocked_page_with_no_cached_copy_is_a_clean_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(networks, "_http_get_text", lambda url, params, timeout: BLOCK_PAGE)
    with pytest.raises(NetworkError, match="neither ExoClock nor Exoplanet Watch"):
        tn.fetch_candidates(tmp_path, ttl_s=60, jd_ref=JD_REF)
    assert not list((tmp_path / "networks").glob("*")) if (tmp_path / "networks").exists() else True


def test_an_empty_but_well_formed_list_is_not_accepted(tmp_path, monkeypatch):
    header_only = WATCH.splitlines()[0] + "\n"
    monkeypatch.setattr(networks, "_http_get_text", lambda url, params, timeout: header_only)
    with pytest.raises(NetworkError):
        tn.fetch_candidates(tmp_path, ttl_s=60, jd_ref=JD_REF)


def test_fetch_text_validate_only_gates_fresh_downloads(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(networks, "_http_get_text", lambda url, params, timeout: "payload")

    def validate(text):
        calls.append(text)
        if text != "payload":
            raise ValueError("bad")

    path = tmp_path / "x.txt"
    text, meta = networks.fetch_text("http://x", path, ttl_s=3600, validate=validate)
    assert text == "payload" and calls == ["payload"] and not meta["from_cache"]
    networks.fetch_text("http://x", path, ttl_s=3600, validate=validate)  # fresh cache: no download, no re-validation
    assert calls == ["payload"]


def test_the_real_exoclock_file_parses_and_agrees_with_known_planets():
    """Seven planets copied unchanged from the live ExoClock file on 2026-11-14.

    The download held 776 planets and every one parsed.  These seven keep the quirks that showed up:
    an O-C is dropped when ``total_observations_recent`` is 0, even if the published number is not 0.0;
    a southern declination keeps its sign; ``55Cnce`` is left as published (it is not ``55 Cnc e``).
    """
    text = (DATA / "exoclock_planets_real.json").read_text()
    rows = {r["planet"]: r for r in tn.parse_exoclock(text)}
    assert len(rows) == 7

    wasp12 = rows["WASP-12 b"]
    assert wasp12["period_d"] == pytest.approx(1.09142, abs=1e-4)  # literature: 1.09142 d
    assert wasp12["duration_h"] == pytest.approx(3.0, abs=0.05)
    assert wasp12["ra_deg"] == pytest.approx(97.64, abs=0.05) and wasp12["dec_deg"] == pytest.approx(29.67, abs=0.05)
    assert wasp12["host"] == "WASP-12" and wasp12["exoclock_priority"] == "low"
    assert wasp12["exoclock_oc_min"] == pytest.approx(-5.43)  # 31 recent timings, so the O-C is a measurement

    wasp2 = rows["WASP-2 b"]
    assert wasp2["period_d"] == pytest.approx(2.1522, abs=1e-3)
    assert wasp2["duration_h"] == pytest.approx(1.79, abs=0.05)

    hd = rows["HD189733 b"]  # the file has no space in the catalogue name; the merger key still matches
    assert networks.normalize_name(hd["planet"]) == networks.normalize_name("HD 189733 b")
    assert hd["v_mag"] == pytest.approx(7.65, abs=0.05) and hd["depth_pct"] == pytest.approx(2.5, abs=0.3)
    assert hd["exoclock_oc_min"] == pytest.approx(0.59)

    assert rows["Qatar-2 b"]["dec_deg"] < 0
    assert rows["55Cnce"]["exoclock_priority"] == "high" and rows["55Cnce"]["exoclock_oc_min"] is None
    assert rows["WASP-123 b"]["exoclock_priority"] == "medium" and rows["WASP-123 b"]["exoclock_oc_min"] is None
    assert rows["HATS-50 b"]["exoclock_priority"] == "alert" and rows["HATS-50 b"]["exoclock_oc_min"] == pytest.approx(10.25)

    for r in rows.values():
        assert 0 <= r["ra_deg"] < 360 and -90 <= r["dec_deg"] <= 90, r["planet"]
        assert r["period_d"] > 0 and r["duration_h"] > 0 and r["t0_bjd_tdb"] > 2.4e6, r["planet"]
        assert r["source"] == tn.SRC_EXOCLOCK


def test_the_real_neocp_feed_parses_with_ra_in_hours():
    """Eight rows copied unchanged from https://minorplanetcenter.net/Extended_Files/neocp.json (2026-10-28)."""
    from plugins.asteroid_finder import mpc

    text = (DATA / "neocp_real.json").read_text()
    rows = mpc.parse_neocp(text)
    assert len(rows) == 8
    import json

    for raw, row in zip(json.loads(text), rows):
        assert row["temp_desig"] == raw["Temp_Desig"]
        assert row["ra_deg"] == pytest.approx(raw["R.A."] * 15.0)  # the feed's RA is decimal hours
        assert 0 <= row["ra_deg"] < 360 and -90 <= row["dec_deg"] <= 90
        assert row["v_mag"] is None or row["v_mag"] < 30
