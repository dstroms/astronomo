"""Pro-Am target lists: the cached fetch, ExoClock / Exoplanet Watch / NEOCP parsing, and the staleness ranking.

The network is never used.  ``core.networks._http_get_text`` is replaced (the conftest already makes it an outage);
payloads are small hand-made samples in the shape of the real files, including their quirks (the Exoplanet Watch
header ``"priority "`` has a trailing space, ExoClock reports an O-C of 0.0 for planets nobody has timed, the NEOCP
uses 99.9 for an unknown magnitude and decimal *hours* for RA).
"""

import json
import os
import time

import pytest

from core import networks
from core.networks import NetworkError, check_network_block, fetch_text, network_block, normalize_name
from plugins.asteroid_finder import mpc
from plugins.transit_finder import archive
from plugins.transit_finder import networks as tn

JD_REF = 2461000.5  # 2026-10-20 12:00 UTC, near enough; only differences matter


# --------------------------------------------------------------------------- #
# Samples
# --------------------------------------------------------------------------- #
def _ec(name, *, period=1.0, period_unc=1e-7, t0_age_epochs=1000, t0_unc=1e-4, dur_h=3.0, priority="medium",
        oc=0.0, recent=0, depth_mmag=12.0, v=11.5, ra="06:30:32.79", dec="+29:40:20.2"):
    return {
        "name": name, "star": name[:-1], "period_days": period, "period_unc": period_unc,
        "t0_bjd_tdb": JD_REF - t0_age_epochs * period, "t0_unc": t0_unc, "duration_hours": dur_h,
        "ra_j2000": ra, "dec_j2000": dec, "v_mag": v, "depth_r_mmag": depth_mmag, "priority": priority,
        "current_oc_min": oc, "total_observations_recent": recent,
    }


EXOCLOCK = json.dumps({
    "Stale-1b": _ec("Stale-1b", period_unc=1e-4, priority="high", recent=3, oc=7.5),  # +/-144 min after 1000 epochs
    "Fresh-2b": _ec("Fresh-2b", period_unc=1e-8, priority="low"),
    "Ttv-3b": _ec("Ttv-3b", period_unc=1e-7, priority="low"),
    "Broken-4b": {"name": "Broken-4b", "period_days": None},  # no usable ephemeris: skipped
})

WATCH_HEADER = "name,RA,Dec,epoch,epoch_uncertainty,period,period_uncertainty,duration,depth,vmag,priority \n"
EXOPLANET_WATCH = WATCH_HEADER + "\n".join([
    f"Stale-1 b,06:30:32.79,+29:40:20.2,{JD_REF - 1000.0},0.0002,1.0,0.0005,3.0,14.0,11.5,2",  # worse than ExoClock
    f"Ttv-3 b,06:30:32.79,+29:40:20.2,{JD_REF - 1000.0},0.0001,1.0,0.00001,3.0,13.0,11.4,1",  # worse than ExoClock too
    f"Only-5 b,10:00:00,-10:00:00,{JD_REF - 500.0},0.0001,2.0,0.00001,2.5,8.0,12.0,3",
    "Junk-6 b,not-a-ra,+10:00:00,1,1,1,1,1,1,1,4",
]) + "\n"

NASA_TTV = "pl_name\nTtv-3 b\nSomething-else b\n"

NEOCP = json.dumps([
    {"Temp_Desig": "P22abcd", "R.A.": 6.5, "Decl.": 12.0, "V": 14.2, "Score": 80, "NObs": 12, "Arc": 1.2,
     "Not_Seen_dys": 0.4, "H": 22.0, "Updated": "2026 Oct 21.1"},
    {"Temp_Desig": "P22faint", "R.A.": 7.0, "Decl.": 10.0, "V": 21.5, "Score": 90, "NObs": 5, "Arc": 0.3,
     "Not_Seen_dys": 0.2, "H": 25.0},
    {"Temp_Desig": "P22old", "R.A.": 8.0, "Decl.": 5.0, "V": 15.0, "Score": 70, "NObs": 9, "Arc": 2.0,
     "Not_Seen_dys": 6.0, "H": 21.0},
    {"Temp_Desig": "P22nomag", "R.A.": 9.0, "Decl.": 5.0, "V": 99.9, "Score": 70, "NObs": 9, "Arc": 2.0,
     "Not_Seen_dys": 0.1, "H": 21.0},
    {"Temp_Desig": "P22lowscore", "R.A.": 10.0, "Decl.": 5.0, "V": 14.0, "Score": 10, "NObs": 9, "Arc": 2.0,
     "Not_Seen_dys": 0.1, "H": 21.0},
    {"Temp_Desig": "bad", "R.A.": 99.0, "Decl.": 5.0},  # RA out of range: skipped
])


@pytest.fixture()
def served(monkeypatch):
    """Serve the samples by URL; ``served.calls`` records what was requested."""
    calls = []

    def get(url, params, timeout):
        calls.append(url)
        if url == tn.EXOCLOCK_URL:
            return EXOCLOCK
        if url == tn.EXOPLANET_WATCH_URL:
            return EXOPLANET_WATCH
        if url == archive.ARCHIVE_URL and params and "ttv_flag" in params.get("query", ""):
            return NASA_TTV
        if url == mpc.NEOCP_URL:
            return NEOCP
        raise NetworkError(f"unexpected URL {url}")

    monkeypatch.setattr(networks, "_http_get_text", get)
    return calls


# --------------------------------------------------------------------------- #
# core.networks
# --------------------------------------------------------------------------- #
def test_fetch_text_caches_and_reuses_a_fresh_copy(tmp_path, served):
    path = tmp_path / "c" / "list.json"
    text, meta = fetch_text(tn.EXOCLOCK_URL, path, ttl_s=3600)
    assert text == EXOCLOCK and meta["from_cache"] is False and meta["stale"] is False and path.is_file()
    text2, meta2 = fetch_text(tn.EXOCLOCK_URL, path, ttl_s=3600)
    assert text2 == EXOCLOCK and meta2["from_cache"] is True and meta2["stale"] is False
    assert served == [tn.EXOCLOCK_URL]  # the second call never touched the network


def test_fetch_text_falls_back_to_a_stale_copy_and_says_so(tmp_path, monkeypatch):
    path = tmp_path / "list.json"
    path.write_text("yesterday's list")
    old = time.time() - 3 * 86400
    os.utime(path, (old, old))  # expired for a 1 h ttl
    text, meta = fetch_text(tn.EXOCLOCK_URL, path, ttl_s=3600)  # the conftest default is an outage
    assert text == "yesterday's list" and meta["stale"] is True and meta["from_cache"] is True
    assert meta["retrieved_utc"].startswith(time.strftime("%Y", time.gmtime(old)))


def test_fetch_text_with_no_cache_and_an_outage_raises(tmp_path):
    with pytest.raises(NetworkError, match="blocked"):
        fetch_text(tn.EXOCLOCK_URL, tmp_path / "none.json", ttl_s=3600)
    assert not (tmp_path / "none.json").exists()


def test_fetch_text_never_leaves_a_half_written_file(tmp_path, served):
    path = tmp_path / "list.json"
    fetch_text(tn.EXOCLOCK_URL, path, ttl_s=0)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["list.json"]


def test_normalize_name_makes_the_lists_agree():
    assert normalize_name("WASP-12b") == normalize_name("WASP-12 b") == normalize_name("wasp_12 B")


def test_network_block_validation():
    block = network_block(["exoclock"], networks.TASK_EPHEMERIS, "why", score=1.5, terms={"staleness": 1.5})
    assert check_network_block("T", block)["task"] == "ephemeris_refinement"
    with pytest.raises(ValueError):
        network_block(["exoclock"], "not a task", "why")
    for bad in (None, {}, {**block, "sources": []}, {**block, "task": "x"}, {**block, "reason": ""}):
        with pytest.raises(Exception):
            check_network_block("T", bad)


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def test_parse_exoclock_skips_unusable_rows_and_reads_units():
    rows = {r["planet"]: r for r in tn.parse_exoclock(EXOCLOCK)}
    assert set(rows) == {"Stale-1 b", "Fresh-2 b", "Ttv-3 b"}
    r = rows["Stale-1 b"]
    assert r["ra_deg"] == pytest.approx(97.636625, abs=1e-5) and r["dec_deg"] == pytest.approx(29.672278, abs=1e-5)
    assert r["period_err_d"] == pytest.approx(1e-4) and r["exoclock_priority"] == "high"
    assert r["depth_pct"] == pytest.approx(1.1, abs=0.05)  # 12 mmag
    assert r["exoclock_oc_min"] == 7.5  # it has recent observations, so the O-C means something
    assert rows["Fresh-2 b"]["exoclock_oc_min"] is None  # an O-C of 0.0 with no recent observations is "no data"


def test_parse_exoclock_rejects_a_non_json_reply():
    with pytest.raises(NetworkError, match="not JSON"):
        tn.parse_exoclock("<html>Service unavailable</html>")
    with pytest.raises(NetworkError):
        tn.parse_exoclock("[1, 2, 3]")


def test_parse_exoplanet_watch_handles_the_stray_space_and_units():
    rows = {r["planet"]: r for r in tn.parse_exoplanet_watch(EXOPLANET_WATCH)}
    assert set(rows) == {"Stale-1 b", "Ttv-3 b", "Only-5 b"}  # the junk row has no usable RA
    assert rows["Ttv-3 b"]["ew_rank"] == 1 and rows["Only-5 b"]["ew_rank"] == 3
    assert rows["Only-5 b"]["depth_pct"] == pytest.approx(0.8)  # 8 ppt
    assert rows["Only-5 b"]["dec_deg"] == pytest.approx(-10.0)


def test_parse_exoplanet_watch_rejects_a_reply_without_the_expected_columns():
    with pytest.raises(NetworkError, match="lacks columns"):
        tn.parse_exoplanet_watch("a,b\n1,2\n")
    with pytest.raises(NetworkError, match="empty"):
        tn.parse_exoplanet_watch("")


def test_pretty_planet_name_and_depth_conversion():
    assert tn.pretty_planet_name("WASP-12b") == "WASP-12 b"
    assert tn.pretty_planet_name("WASP-12 b") == "WASP-12 b"
    assert tn.mmag_to_depth_pct(10.9) == pytest.approx(1.0, abs=0.01)


# --------------------------------------------------------------------------- #
# Staleness, merge, ranking
# --------------------------------------------------------------------------- #
def test_propagated_uncertainty_matches_the_formula_and_treats_a_missing_error_as_unbounded():
    n, dp, dt0 = 1000.0, 1e-4, 2e-4
    expected = (((n * dp) ** 2) + 2 * n * dp * dt0 + dt0**2) ** 0.5
    assert tn.propagated_uncertainty_d(n, dp, dt0) == pytest.approx(expected)
    assert tn.propagated_uncertainty_d(-n, dp, dt0) == pytest.approx(expected)  # before the reference epoch
    assert tn.propagated_uncertainty_d(n, None, dt0) == float("inf")


def test_staleness_is_uncertainty_over_half_the_transit_duration():
    row = {"t0_bjd_tdb": JD_REF - 1000.0, "period_d": 1.0, "period_err_d": 1e-4, "t0_err_d": 0.0, "duration_h": 3.0}
    ratio, sigma_min = tn.staleness(row, JD_REF)
    assert sigma_min == pytest.approx(0.1 * 1440.0)  # 1000 epochs x 1e-4 d = 144 min
    assert ratio == pytest.approx(0.1 / (1.5 / 24.0))
    assert tn.staleness({**row, "period_err_d": None}, JD_REF) == (float("inf"), float("inf"))


def _merged():
    return tn.merge(tn.parse_exoclock(EXOCLOCK), tn.parse_exoplanet_watch(EXOPLANET_WATCH), archive.parse_ttv_names(NASA_TTV), JD_REF)


def test_merge_joins_the_lists_on_normalised_names_and_keeps_the_better_ephemeris():
    rows = {r["planet"]: r for r in _merged()}
    assert set(rows) == {"Stale-1 b", "Fresh-2 b", "Ttv-3 b", "Only-5 b"}
    stale = rows["Stale-1 b"]
    assert stale["sources"] == ["exoclock", "exoplanet_watch"]
    assert stale["ephemeris_source"] == "exoclock"  # +/-144 min beats Exoplanet Watch's +/-720 min
    assert stale["period_err_d"] == pytest.approx(1e-4) and stale["exoclock_priority"] == "high" and stale["ew_rank"] == 2
    assert rows["Only-5 b"]["sources"] == ["exoplanet_watch"] and rows["Only-5 b"]["exoclock_priority"] is None
    ttv = rows["Ttv-3 b"]
    assert ttv["ttv_flag"] is True and "nasa_exoplanet_archive" in ttv["sources"]
    assert rows["Fresh-2 b"]["ttv_flag"] is False


def test_rank_puts_the_stale_prioritised_planet_first_and_explains_itself():
    ranked = tn.rank(_merged(), JD_REF)
    assert [r["score"] for r in ranked] == sorted((r["score"] for r in ranked), reverse=True)
    by_name = {r["planet"]: r for r in ranked}
    assert ranked[0]["planet"] == "Stale-1 b"
    top = by_name["Stale-1 b"]
    assert top["task"] == "ephemeris_refinement"
    assert "HIGH priority" in top["reason"] and "+7.5 min" in top["reason"] and "rank 2 of" in top["reason"]
    assert top["terms"]["staleness"] == pytest.approx(min(top["staleness"], tn.STALENESS_CAP))
    # Each score is exactly the audited weighted sum of its terms.
    for r in ranked:
        assert r["score"] == pytest.approx(sum(tn.WEIGHTS[k] * v for k, v in r["terms"].items()))
    assert by_name["Ttv-3 b"]["task"] == "ttv_monitoring" and "transit-timing variations" in by_name["Ttv-3 b"]["reason"]
    assert by_name["Fresh-2 b"]["score"] < by_name["Ttv-3 b"]["score"]


def test_a_planet_with_no_stated_period_error_is_capped_not_infinite():
    row = {**_merged()[0], "period_err_d": None}
    out = tn.score(row, JD_REF)
    assert out["terms"]["staleness"] == tn.STALENESS_CAP and "cannot be trusted" in out["reason"]


# --------------------------------------------------------------------------- #
# fetch_candidates
# --------------------------------------------------------------------------- #
def test_fetch_candidates_merges_every_list_and_records_provenance(tmp_path, served):
    rows, info = tn.fetch_candidates(tmp_path, 3600.0, JD_REF)
    assert {r["planet"] for r in rows} == {"Stale-1 b", "Fresh-2 b", "Ttv-3 b", "Only-5 b"}
    assert set(info["sources"]) == {"exoclock", "exoplanet_watch", "nasa_exoplanet_archive"}
    assert info["problems"] == [] and info["stale_cache"] is False and info["retrieved_utc"]
    block = tn.network_block_for(rows[0], info)
    assert block["sources"] == rows[0]["sources"] and block["task"] == rows[0]["task"] and block["score"] == pytest.approx(rows[0]["score"], abs=1e-3)
    assert check_network_block("x", block)


def test_one_list_failing_does_not_stop_the_others(tmp_path, monkeypatch, served):
    real = networks._http_get_text

    def flaky(url, params, timeout):
        if url == tn.EXOCLOCK_URL:
            raise NetworkError("exoclock is down")
        return real(url, params, timeout)

    monkeypatch.setattr(networks, "_http_get_text", flaky)
    rows, info = tn.fetch_candidates(tmp_path, 3600.0, JD_REF)
    assert {r["planet"] for r in rows} == {"Stale-1 b", "Ttv-3 b", "Only-5 b"}
    assert any("exoclock" in p for p in info["problems"])


def test_the_ttv_flag_is_optional(tmp_path, monkeypatch, served):
    real = networks._http_get_text

    def no_tap(url, params, timeout):
        if url == archive.ARCHIVE_URL:
            raise NetworkError("TAP is down")
        return real(url, params, timeout)

    monkeypatch.setattr(networks, "_http_get_text", no_tap)
    rows, info = tn.fetch_candidates(tmp_path, 3600.0, JD_REF)
    assert not any(r["ttv_flag"] for r in rows) and any("nasa" in p for p in info["problems"])


def test_fetch_candidates_raises_when_neither_primary_list_is_readable(tmp_path):
    with pytest.raises(NetworkError, match="neither ExoClock nor Exoplanet Watch"):
        tn.fetch_candidates(tmp_path, 3600.0, JD_REF)


def test_an_expired_cache_is_used_during_an_outage_and_flagged(tmp_path, served, monkeypatch):
    tn.fetch_candidates(tmp_path, 3600.0, JD_REF)  # populate the cache
    old = time.time() - 5 * 86400
    for p in (tmp_path / "networks").iterdir():
        os.utime(p, (old, old))
    monkeypatch.setattr(networks, "_http_get_text", lambda *a: (_ for _ in ()).throw(NetworkError("offline")))
    rows, info = tn.fetch_candidates(tmp_path, 3600.0, JD_REF)
    assert rows and info["stale_cache"] is True
    assert tn.network_block_for(rows[0], info)["stale_cache"] is True


# --------------------------------------------------------------------------- #
# MPC NEOCP
# --------------------------------------------------------------------------- #
def test_parse_neocp_converts_units_and_skips_bad_entries():
    rows = {r["temp_desig"]: r for r in mpc.parse_neocp(NEOCP)}
    assert "bad" not in rows and len(rows) == 5
    assert rows["P22abcd"]["ra_deg"] == pytest.approx(97.5)  # hours -> degrees
    assert rows["P22nomag"]["v_mag"] is None  # 99.9 means "unknown"
    with pytest.raises(NetworkError):
        mpc.parse_neocp("{}")
    with pytest.raises(NetworkError, match="not JSON"):
        mpc.parse_neocp("nope")


def test_usable_neocp_entries_are_bright_recent_and_likely():
    rows = mpc.parse_neocp(NEOCP)
    assert [r["temp_desig"] for r in mpc.usable(rows, max_v=15.0, max_not_seen_d=1.5)] == ["P22lowscore", "P22abcd"]
    assert [r["temp_desig"] for r in mpc.usable(rows, max_v=15.0, max_not_seen_d=1.5, min_score=50)] == ["P22abcd"]
    assert mpc.usable(rows, max_v=10.0, max_not_seen_d=1.5) == []  # the usual night: nothing within reach
    text = mpc.reason(mpc.usable(rows, max_v=15.0, max_not_seen_d=1.5)[1], 1.5)
    assert "P22abcd" in text and "NEO Confirmation Page" in text


def test_fetch_neocp_caps_the_cache_lifetime_at_an_hour(tmp_path, served):
    path = tmp_path / "networks" / "neocp.json"
    mpc.fetch_neocp(tmp_path, ttl_s=30 * 3600.0)
    old = time.time() - 2 * 3600  # two hours old: expired for the NEOCP even though the global ttl is 30 h
    os.utime(path, (old, old))
    mpc.fetch_neocp(tmp_path, ttl_s=30 * 3600.0)
    assert served.count(mpc.NEOCP_URL) == 2
