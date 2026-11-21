"""The variable-star plugin: the AAVSO Target Tool reader, nightly windows, plans, and the exposure a mission uses.

The page the tool serves is imitated here (same table, same quirks: unquoted attributes, one row per star and filter,
'No Observation'), so none of this touches the network.  A full simulated mission, with a known eclipse and magnitude,
is in ``test_variable_star_mission.py``.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from core import networks as core_networks
from core.networks import NetworkError
from core.opportunities import OpportunityError, check_report
from plugins import available_plugins, load_plugin
from plugins.variable_stars import networks as vn
from plugins.variable_stars import opportunities as vo

NOW = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)  # noon in Denver; the night of the 28th is ahead

HEADER = [
    "Star Name", "RA<br/>(J2000.0)", "Dec<br/>(J2000.0)", "Const-<br/>ellation", "Var.<br/>Type", "Max<br/>Mag", "Min<br/>Mag",
    "Period (d)", "Observing<br/>Cadence&nbsp;(d)", "Observing<br/>Section", "Filter/<br/>Mode", "Last<br/>Observed", "High<br/>Priority", "Notes",
]


def _row(
    name, *, ra="02h 00m 00s", dec="+40° 00' 00\"", kind="EA", max_mag="10.0 V", min_mag="12.0 V", period="2.5", cadence="3.0",
    section="EB", mode="V", last="2026-10-20 04:00:00", label="danger", priority="B", notes="", oid=1,
) -> str:
    hh, mm, ss = ra.replace("h", " ").replace("m", " ").replace("s", " ").split()
    ra_html = f"{hh}<sup>h</sup> {mm}<sup>m</sup> {ss}<sup>s</sup>"  # as the real page writes it
    seen = (
        f'<span class="label label-{label} utcInTitle" title="{last} UTC">&nbsp;ago</span>' if last
        else '<span class="label label-danger">No Observation</span>'
    )
    sort_title = f"data-sort-title={last}" if last else "data-sort-title=1800"  # unquoted, with a space in it, as the real page does
    note_html = f"<div class=\"morexx\"><p>{notes}</p></div>" if notes else '<div class="morexx"></div>'
    return (
        f'<tr><td><a href="https://www.aavso.org/vsx/index.php?view=detail.top&amp;oid={oid}" target="_blank">{name}</a></td>'
        f"<td>{ra_html}</td><td>{dec.replace(chr(39), '&#x27;').replace(chr(34), '&quot;')}</td><td>Cep</td><td>{kind}</td>"
        f'<td><p style="white-space: nowrap;">{max_mag}</p></td><td><p style="white-space: nowrap;">{min_mag}</p></td>'
        f"<td>{period}</td><td>{cadence}</td><td>{section}</td><td>{mode}</td>"
        f"<td {sort_title}>{seen}<br/></td><td data-sort-ptitle={priority}></td>"
        f'<td class="comment">{note_html}</td></tr>'
    )


def _page(rows: list[str], *, fillers: int = vn.MIN_ROWS) -> str:
    """A page of ``rows`` plus faint filler stars (a real page has thousands; the parser refuses a short one)."""
    filler = [_row(f"Filler {i}", max_mag="16.0 V", min_mag="17.0 V", oid=1000 + i) for i in range(fillers)]
    head = "".join(f"<th><label>{h}</label></th>" for h in HEADER)
    return f"<html><body><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows + filler)}</tbody></table></body></html>"


GOOD = _row("RR Good", max_mag="10.0 V", min_mag="11.5 V", mode="V", priority="A", section="Alert/Campaign", last="2026-10-27 22:00:00", label="success", oid=7)


@pytest.fixture()
def vs_settings(settings):
    return settings  # offline_services stays True: the report reads the cache and never needs the network


def _cache(settings, rows: list[str]) -> None:
    path = Path(settings.archive_cache_dir) / vn.CACHE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page(rows), encoding="utf-8")


def _by_name(report):
    return {o["name"]: o for o in report["opportunities"]}


# --------------------------------------------------------------------------- #
# Reading the Target Tool page
# --------------------------------------------------------------------------- #
def test_a_star_is_read_with_its_position_range_cadence_and_priority():
    rows = {r["name"]: r for r in vn.parse_targets(_page([GOOD]))}
    r = rows["RR Good"]
    assert r["ra_deg"] == pytest.approx(30.0) and r["dec_deg"] == pytest.approx(40.0)
    assert (r["max_mag"], r["max_band"], r["min_mag"], r["min_band"]) == (10.0, "V", 11.5, "V")
    assert (r["cadence_d"], r["sections"], r["modes"], r["high_priority"], r["vsx_oid"]) == (3.0, ["Alert/Campaign"], ["V"], True, "7")
    assert r["last_observed_utc"] == "2026-10-27T22:00:00+00:00" and r["status"] == "on_cadence"  # the time survives its unquoted attribute


def test_a_star_that_was_never_observed_has_no_time_and_says_so():
    r = {x["name"]: x for x in vn.parse_targets(_page([_row("Never", last=None)]))}["Never"]
    assert r["last_observed_utc"] is None and r["status"] == "never"


def test_a_south_declination_and_a_dash_for_a_missing_magnitude_are_read():
    r = {x["name"]: x for x in vn.parse_targets(_page([_row("South", dec="-62° 40' 46\"", min_mag="&mdash;")]))}["South"]
    assert r["dec_deg"] == pytest.approx(-(62 + 40 / 60 + 46 / 3600)) and r["min_mag"] is None and r["max_mag"] == 10.0


def test_a_star_asked_for_in_several_filters_is_one_row_with_every_request_and_the_shortest_cadence():
    rows = vn.parse_targets(_page([
        _row("Many", mode="V", cadence="5.0", last="2026-10-20 00:00:00", label="danger", oid=9),
        _row("Many", mode="B", cadence="1.0", last="2026-10-27 00:00:00", label="success", priority="A", oid=9),
        _row("Many", mode="Spectroscopy", cadence="9.0", last=None, notes="see Alert Notice", oid=9),
    ]))
    many = [r for r in rows if r["name"] == "Many"]
    assert len(many) == 1
    m = many[0]
    assert m["modes"] == ["V", "B", "Spectroscopy"] and m["cadence_d"] == 1.0 and m["high_priority"] is True
    assert m["last_observed_utc"] == "2026-10-27T00:00:00+00:00" and m["status"] == "never"  # the most urgent request wins
    assert "Alert Notice" in m["notes"]


def test_only_the_requests_a_colour_camera_can_serve_decide_how_overdue_a_star_is():
    rows = vn.parse_targets(_page([
        _row("Many", mode="V", cadence="5.0", last="2026-10-20 00:00:00", label="danger", oid=9),
        _row("Many", mode="B", cadence="2.0", last="2026-10-27 00:00:00", label="success", priority="A", oid=9),
        _row("Many", mode="Spectroscopy", cadence="1.0", last=None, oid=9),  # never answered, and not ours to answer
    ]))
    star = vo.for_this_telescope(next(r for r in rows if r["name"] == "Many"))
    assert star["modes"] == ["V", "B"] and star["cadence_d"] == 2.0 and star["high_priority"] is True
    assert star["last_observed_utc"] == "2026-10-27T00:00:00+00:00" and star["status"] == "overdue"  # not "never"
    only_spectra = vo.for_this_telescope(_star(mode="Spectroscopy"))
    assert only_spectra["modes"] == ["Spectroscopy"]  # nothing servable: left as it was, and blocked elsewhere
    reason = vo.network_block_for(star, {"retrieved_utc": None}, NOW, LIMITS)["reason"]
    assert "spectroscopy" not in reason and "visual" not in reason


@pytest.mark.parametrize("body", ["<html>Sign in to continue</html>", "<html><table><thead><tr><th>a</th></tr></thead></table></html>"])
def test_a_page_that_is_not_the_table_is_refused(body):
    with pytest.raises(ValueError):
        vn.parse_targets(body)


def test_a_short_table_is_refused_and_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(core_networks, "_http_get_text", lambda url, params, timeout: _page([GOOD], fillers=3))
    with pytest.raises(NetworkError):
        vn.fetch_targets(tmp_path, 0.0)
    assert not (tmp_path / vn.CACHE_NAME).exists()


def test_the_list_is_cached_and_the_cache_is_used_without_the_network(tmp_path, monkeypatch):
    calls = []

    def get(url, params, timeout):
        calls.append((url, params))
        return _page([GOOD])

    monkeypatch.setattr(core_networks, "_http_get_text", get)
    rows, info = vn.fetch_targets(tmp_path, 0.0)
    assert any(r["name"] == "RR Good" for r in rows) and info["from_cache"] is False
    url, params = calls[0]
    assert url == vn.URL and params["ac"] == "on" and "exo" not in params  # exoplanet hosts are the transit plugin's
    monkeypatch.setattr(core_networks, "_http_get_text", lambda *a: pytest.fail("the network was used"))
    rows2, info2 = vn.fetch_targets(tmp_path, float("inf"), cached_only=True)
    assert len(rows2) == len(rows) and info2["from_cache"] is True
    with pytest.raises(NetworkError):
        vn.fetch_targets(tmp_path / "empty", float("inf"), cached_only=True)


# --------------------------------------------------------------------------- #
# What a 50 mm telescope can reach
# --------------------------------------------------------------------------- #
LIMITS = vo.Limits()


def _star(**kw):
    base = {r["name"]: r for r in vn.parse_targets(_page([_row("T", **kw)]))}["T"]
    return base


def test_a_bright_enough_star_in_a_servable_filter_has_no_objection():
    assert vo.static_blockers(_star(), LIMITS, 40.0) == []


def test_too_faint_too_bright_and_too_far_south_are_each_said_plainly():
    faint = vo.static_blockers(_star(max_mag="13.6 V", min_mag="16.0 V"), LIMITS, 40.0)
    assert len(faint) == 1 and "Too faint" in faint[0] and "13.6" in faint[0]
    bright = vo.static_blockers(_star(max_mag="3.0 V", min_mag="5.0 V"), LIMITS, 40.0)
    assert len(bright) == 1 and "Too bright" in bright[0] and "saturates" in bright[0]
    south = vo.static_blockers(_star(dec="-62° 40' 46\""), LIMITS, 40.0)
    assert len(south) == 1 and "never climbs high enough" in south[0]


def test_a_star_that_only_brightens_above_the_limit_is_reachable_but_says_so():
    s = _star(max_mag="10.0 V", min_mag="17.0 V", section="CV")
    assert vo.static_blockers(s, LIMITS, 40.0) == [] and vo.only_when_bright(s, LIMITS)
    assert "Only within reach" in vo.detail_text(s, LIMITS)
    assert vo.impact_estimate(s, NOW, LIMITS)["level"] == "low"  # a night now would most likely give an upper limit


def test_a_star_that_saturates_near_maximum_but_not_at_minimum_is_reachable_with_a_warning():
    s = _star(max_mag="5.2 V", min_mag="12.4 V", section="LPV")
    assert vo.static_blockers(s, LIMITS, 40.0) == [] and vo.saturates_when_bright(s, LIMITS)
    assert "saturates" in vo.detail_text(s, LIMITS)


def test_a_colour_camera_cannot_serve_every_request():
    assert vo.static_blockers(_star(mode="Spectroscopy"), LIMITS, 40.0)[0].startswith("The AAVSO asks for spectroscopy only")
    assert vo.static_blockers(_star(mode="I"), LIMITS, 40.0)  # no infrared filter
    s = _star(mode="V")
    assert vo.served_modes(s) == ["green channel (TG)"]
    assert vo.served_modes({"modes": ["B", "All"]}) == ["blue channel (TB)", "green channel (TG)"]


def test_a_star_with_no_listed_brightness_cannot_be_judged_reachable():
    assert any("not listed" in b for b in vo.static_blockers(_star(max_mag="&mdash;", min_mag="&mdash;"), LIMITS, 40.0))


# --------------------------------------------------------------------------- #
# Nightly windows
# --------------------------------------------------------------------------- #
SITE = (39.7392, -104.9903, 1609.0)


def test_the_altitude_arithmetic_agrees_with_astropy():
    from tools.astrometry import target_altaz_many

    rng = np.random.default_rng(3)
    ra, dec = rng.uniform(0, 360, 12), rng.uniform(-60, 80, 12)
    times = [NOW + timedelta(minutes=int(m)) for m in rng.integers(0, 60 * 24 * 20, 12)]
    mine_alt, mine_az = [], []
    for r, d, t in zip(ra, dec, times):
        a, z = vo.altaz_deg(np.array([r]), np.array([d]), SITE[0], SITE[1], np.array([t.timestamp()]))
        mine_alt.append(a[0, 0]), mine_az.append(z[0, 0])
    alt, az = target_altaz_many(ra, dec, SITE[0], SITE[1], SITE[2], times)
    assert np.max(np.abs(np.array(mine_alt) - alt)) < 0.6  # refraction, nutation and the sidereal-time shortcut: planning accuracy
    d_az = np.abs((np.array(mine_az) - az + 180.0) % 360.0 - 180.0)
    assert np.max(d_az[alt > 20]) < 1.0


def _rows(*kw):
    return [{**{r["name"]: r for r in vn.parse_targets(_page([_row("T", **k)]))}["T"], "name": f"S{i}"} for i, k in enumerate(kw)]


def test_a_high_star_gets_a_window_on_most_nights_and_each_is_dark_and_high():
    from tools.astrometry import sun_altitude_deg_many, target_altitude_deg_many

    star = _rows({"ra": "02h 00m 00s", "dec": "+40° 00' 00\""})
    slot = vo.night_windows(star, NOW, 14, LIMITS, SITE)[0]
    assert len(slot["windows"]) >= 12
    starts = [w["start"] for w in slot["windows"]]
    assert starts == sorted(starts)
    for w in slot["windows"][:5]:
        assert w["start"] < w["mid"] < w["end"] and (w["end"] - w["start"]) >= timedelta(minutes=LIMITS.min_window_min)
        inside = [w["start"] + timedelta(minutes=m) for m in (0, 30)]
        assert np.all(sun_altitude_deg_many(SITE[0], SITE[1], SITE[2], inside) <= LIMITS.max_sun_alt_deg + 0.5)
        assert np.all(target_altitude_deg_many([30.0, 30.0], [40.0, 40.0], SITE[0], SITE[1], SITE[2], inside) >= LIMITS.min_alt_deg - 0.6)


def test_no_window_is_offered_near_the_zenith():
    """V0381 And was 81 deg up on the first real night; the field turned 1.76 deg in nine minutes."""
    import dataclasses

    star = _rows({"ra": "02h 00m 00s", "dec": "+40° 00' 00\""})  # passes within 0.3 deg of the zenith of this site
    free = vo.night_windows(star, NOW, 14, LIMITS, SITE)[0]
    capped_limits = dataclasses.replace(LIMITS, max_alt_deg=70.0)
    capped = vo.night_windows(star, NOW, 14, capped_limits, SITE)[0]

    def highest(window):
        times = np.array([(window["start"] + timedelta(minutes=m)).timestamp() for m in range(0, int((window["end"] - window["start"]).total_seconds() // 60), 5)])
        alt, _ = vo.altaz_deg(np.array([star[0]["ra_deg"]]), np.array([star[0]["dec_deg"]]), SITE[0], SITE[1], times)
        return float(alt.max())

    assert max(highest(w) for w in free["windows"]) > 80.0, "without a cap the star is offered at the zenith"
    assert capped["windows"] and max(highest(w) for w in capped["windows"]) <= 72.0  # sampled between the planner's grid points
    assert "between 30 and 70 deg" in vo._no_window_reason({"nights": 3, "alt": 3, "moon": 0}, 7, capped_limits)


def test_the_planner_takes_the_lower_of_its_own_maximum_and_the_missions(settings, monkeypatch):
    assert vo.Limits.from_env(settings).max_alt_deg == settings.max_altitude_deg == 70.0
    monkeypatch.setenv("ASTRONOMO_VARSTAR_MAX_ALT_DEG", "60")
    assert vo.Limits.from_env(settings).max_alt_deg == 60.0
    monkeypatch.setenv("ASTRONOMO_VARSTAR_MAX_ALT_DEG", "85")
    assert vo.Limits.from_env(settings).max_alt_deg == 70.0
    assert vo.Limits.from_env(settings).site_limits().max_altitude_deg == 70.0


def test_a_star_that_is_below_the_limit_all_night_has_no_window_and_the_report_says_why():
    star = _rows({"dec": "+0° 00' 00\""})  # from Denver the celestial equator peaks at 50 deg; RA 14h is below the Sun's side of the sky at night
    star[0]["ra_deg"] = 14.0 * 15.0
    slot = vo.night_windows(star, NOW, 7, LIMITS, SITE)[0]
    assert slot["windows"] == [] and slot["nights"] >= 6
    assert "None of the" in vo._no_window_reason(slot, 7, LIMITS)


def test_the_moon_costs_a_star_that_is_next_to_it():
    # The Moon is up on the nights after the full Moon near this date; a star at the Moon's position is never usable while it is up.
    from tools.astrometry import sun_moon_grid

    grid = sun_moon_grid(*SITE, [NOW + timedelta(hours=h) for h in range(0, 30, 3)])
    moon_xyz = grid["moon_xyz"][4]
    ra = float(np.degrees(np.arctan2(moon_xyz[1], moon_xyz[0])) % 360.0)
    dec = float(np.degrees(np.arcsin(moon_xyz[2])))
    near = _rows({"dec": "+40° 00' 00\""})
    near[0]["ra_deg"], near[0]["dec_deg"] = ra, dec
    free = dataclasses.replace(LIMITS, moon_avoid_deg=0.0, moon_floor_deg=0.0)
    with_moon = vo.night_windows(near, NOW, 3, LIMITS, SITE)[0]
    without = vo.night_windows(near, NOW, 3, free, SITE)[0]
    assert len(with_moon["windows"]) <= len(without["windows"])


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def _report(settings, rows, **kw):
    _cache(settings, rows)
    return vo.build_report(settings, now=NOW, days=14, **kw)


def test_the_report_obeys_the_contract_and_ranks_reachable_stars_first(vs_settings):
    report = _report(vs_settings, [
        GOOD,
        _row("Faint EB", max_mag="14.5 V", min_mag="15.5 V", oid=2),
        _row("South EB", dec="-62° 40' 46\"", oid=3),
        _row("Dull EB", section="EB", last="2026-10-28 12:00:00", label="success", oid=4),
    ])
    check_report(report)
    ops = report["opportunities"]
    by = _by_name(report)
    assert by["RR Good"]["reachable"] and by["Dull EB"]["reachable"]
    assert not by["Faint EB"]["reachable"] and "Too faint" in by["Faint EB"]["blockers"][0]
    assert not by["South EB"]["reachable"]
    flags = [o["reachable"] for o in ops]
    assert flags == sorted(flags, reverse=True)  # reachable first
    assert ops[0]["name"] == "RR Good"  # high priority and a recent gap beats a star observed this morning
    assert by["RR Good"]["network"]["task"] == core_networks.TASK_MONITORING and by["RR Good"]["network"]["sources"] == [vn.SOURCE]
    assert report["plugin"] == "variable_stars" and report["planning"] is True and report["words"]["item"] == "Star"
    assert report["data"]["from_cache"] is True and report["n_candidates"] == len(ops)


def test_an_overdue_high_priority_star_outranks_one_that_is_on_cadence(vs_settings):
    report = _report(vs_settings, [
        _row("Late", priority="A", last="2026-09-01 00:00:00", oid=1), _row("Fresh", priority="A", last="2026-10-28 06:00:00", label="success", oid=2),
        _row("Late B", priority="B", last="2026-09-01 00:00:00", oid=3),
    ])
    by = _by_name(report)
    assert by["Late"]["impact"]["level"] == "high" and by["Late B"]["impact"]["level"] == "medium" and by["Fresh"]["impact"]["level"] == "low"
    scores = {n: by[n]["network"]["score"] for n in by if n in ("Late", "Fresh", "Late B")}
    assert scores["Late"] > scores["Late B"] > scores["Fresh"]
    assert set(by["Late"]["network"]["terms"]) == {"high_priority", "overdue", "measurable"}


def test_every_estimate_states_its_assumptions_and_the_noise_is_called_a_placeholder(vs_settings):
    report = _report(vs_settings, [GOOD, _row("EB Star", section="EB", oid=5)])
    for o in report["opportunities"]:
        if o["reachable"]:
            text = " ".join(o["impact"]["assumptions"])
            assert "placeholder" in text and "S50 Pro is not measured" in text
    eb = _by_name(report)["EB Star"]["impact"]["assumptions"]
    assert any("eclipse" in a and "VSX" in a for a in eb)  # the list gives a period, not the time of an eclipse


def test_the_noise_model_gets_worse_for_fainter_stars_and_is_not_extrapolated_brighter():
    assert vo.frame_sigma(10.0, 0.032) == pytest.approx(0.032)
    assert vo.frame_sigma(13.5, 0.032) == pytest.approx(0.032 * 10 ** 0.7, rel=0.01)  # photon noise in magnitudes: 10^0.2 per magnitude
    assert vo.frame_sigma(4.0, 0.032) == vo.frame_sigma(vo.NOISE_V_FLOOR, 0.032)
    assert vo.stack_sigma(10.0, LIMITS) < vo.frame_sigma(10.0, 0.032) / 5.0


def test_exoplanet_only_hosts_are_left_to_the_transit_plugin(vs_settings):
    report = _report(vs_settings, [GOOD, _row("Host b", section="EP", oid=6)])
    assert "Host b" not in _by_name(report) and "RR Good" in _by_name(report)


def test_nothing_downloaded_gives_an_empty_report_that_the_page_can_offer_to_fill(vs_settings):
    report = vo.build_report(vs_settings, now=NOW, days=7)
    check_report(report)
    assert report["opportunities"] == [] and report["data"]["retrieved_utc"] is None and report["n_candidates"] == 0


def test_a_refresh_downloads_and_the_offline_setting_stops_it(vs_settings, monkeypatch):
    live = dataclasses.replace(vs_settings, offline_services=False)
    calls = []
    monkeypatch.setattr(core_networks, "_http_get_text", lambda url, params, timeout: calls.append(url) or _page([GOOD]))
    report = vo.build_report(live, now=NOW, days=7, refresh=True)
    assert calls == [vn.URL] and "RR Good" in _by_name(report) and report["data"]["from_cache"] is False
    calls.clear()
    offline = vo.build_report(vs_settings, now=NOW, days=7, refresh=True)  # offline_services=True
    assert calls == [] and any("offline" in p for p in offline["data"]["problems"])


def test_a_failed_download_with_no_copy_is_reported_not_raised(vs_settings, monkeypatch):
    live = dataclasses.replace(vs_settings, offline_services=False)

    def down(url, params, timeout):
        raise NetworkError("no route")

    monkeypatch.setattr(core_networks, "_http_get_text", down)
    report = vo.build_report(live, now=NOW, days=7, refresh=True)
    check_report(report)
    assert report["opportunities"] == [] and any("no route" in p for p in report["data"]["problems"])


def test_limits_come_from_the_environment(vs_settings, monkeypatch):
    monkeypatch.setenv("ASTRONOMO_VARSTAR_MAX_VMAG", "9.0")
    monkeypatch.setenv("ASTRONOMO_VARSTAR_SIGMA_V10", "0.05")
    limits = vo.Limits.from_env(vs_settings)
    assert limits.max_vmag == 9.0 and limits.sigma_v10 == 0.05
    report = _report(vs_settings, [GOOD, _row("Mid", max_mag="9.8 V", min_mag="11.0 V", oid=8)])
    assert all(not o["reachable"] for o in report["opportunities"] if o["name"] in ("RR Good", "Mid"))


# --------------------------------------------------------------------------- #
# The plugin and the contract
# --------------------------------------------------------------------------- #
def test_the_plugin_is_found_and_a_simulated_mission_has_an_eclipse_and_a_control(vs_settings):
    assert "variable_stars" in available_plugins()
    plugin = load_plugin("variable_stars", vs_settings)
    assert plugin.opportunities(NOW, days=7)["plugin"] == "variable_stars"
    plan = plugin.validated_exposure_plan()
    assert plan["count"] >= 12 and plan["exposure_s"] > 0
    targets = plugin.validated_targets()
    by = {t["name"]: t for t in targets}
    assert set(by) == {"V1234 Lac (sim)", "Control star (sim)"}
    eclipse = by["V1234 Lac (sim)"]
    assert eclipse["sim"]["eclipse"]["depth"] > 0.2 and eclipse["predicted_min_iso"]
    assert eclipse["window_end"] > eclipse["window_start"]
    assert by["Control star (sim)"]["sim"]["eclipse"]["depth"] == 0.0
    assert plugin.validate_targets([eclipse])[0]["name"] == eclipse["name"]


def test_a_planned_eclipsing_binary_says_the_window_is_a_night_not_an_eclipse(vs_settings):
    plugin = load_plugin("variable_stars", vs_settings)
    _cache(vs_settings, [GOOD, _row("EB Star", section="EB", oid=5), _row("Mira Star", section="LPV", kind="M", oid=8)])
    report = plugin.opportunities(NOW, days=14)
    by = _by_name(report)
    eb = plugin.planned_target(by["EB Star"], by["EB Star"]["windows"][0])
    other = plugin.planned_target(by["RR Good"], by["RR Good"]["windows"][0])
    assert eb["window_kind"] == "night" and "not an eclipse" in eb["window_note"] and "time of minimum" in eb["window_note"]
    assert other["window_kind"] == "night" and "usable night" in other["window_note"] and "not an eclipse" not in other["window_note"]
    plugin.validate_targets([eb])
    notes = plugin.recheck_planned_target(eb)["notes"]
    assert any("not an eclipse" in n for n in notes) and any("still on the cached AAVSO list" in n for n in notes)


def test_live_targets_follow_the_cached_list_and_a_missing_list_is_labelled_built_in(vs_settings, monkeypatch):
    from dataclasses import replace

    from plugins.variable_stars import plugin as vp

    def windows(rows, now, days, limits, site):
        start, end = now, now + timedelta(hours=5)
        return [{"windows": [{"start": start, "mid": start + timedelta(hours=2), "end": end}], "nights": 1, "alt": 0, "moon": 0} for _ in rows]

    monkeypatch.setattr(vp.opp, "night_windows", windows)
    _cache(vs_settings, [GOOD, _row("EB Star", section="EB", oid=5)])
    live = replace(vs_settings, offline_services=False)
    targets = {t["name"]: t for t in load_plugin("variable_stars", live).generate_targets()}
    assert "RR Good" in targets and targets["RR Good"]["network"]["sources"] == [vn.SOURCE]
    assert targets["RR Good"]["window_kind"] == "night" and "usable night" in targets["RR Good"]["window_note"]
    assert "not an eclipse" in targets["EB Star"]["window_note"]

    monkeypatch.setenv("ASTRONOMO_VARSTAR_TARGETS", "EB Star")
    (only,) = load_plugin("variable_stars", live).generate_targets()
    assert only["name"] == "EB Star" and only["network"]["sources"] == ["operator"]

    monkeypatch.delenv("ASTRONOMO_VARSTAR_TARGETS")

    def down(*_a, **_k):
        raise NetworkError("down")

    monkeypatch.setattr(vp.networks, "fetch_targets", down)
    fallback = load_plugin("variable_stars", live).generate_targets()
    assert fallback and all(t["network"]["sources"] == ["built_in"] and t["network"]["task"] == "manual" for t in fallback)
    assert "RR Lyr" in {t["name"] for t in fallback}


def test_the_report_contract_checks_the_new_optional_fields(vs_settings):
    report = _report(vs_settings, [GOOD])
    check_report(report)
    with pytest.raises(OpportunityError):
        check_report({**report, "words": {"item": ""}})
    with pytest.raises(OpportunityError):
        check_report({**report, "words": {"banana": "x"}})
    with pytest.raises(OpportunityError):
        check_report({**report, "planning": "no"})
    with pytest.raises(OpportunityError):
        check_report({**report, "summary": "  "})
    bad = {**report, "opportunities": [{**report["opportunities"][0], "detail": "  "}]}
    with pytest.raises(OpportunityError):
        check_report(bad)


def test_star_rows_use_star_wording_not_planet(vs_settings):
    from dashboard import opportunities as view

    report = _report(vs_settings, [GOOD, _row("EB Star", oid=5), _row("Faint", max_mag="15.0 V", min_mag="16.0 V", oid=6)])
    ops = [o for o in report["opportunities"] if o["reachable"]]
    rows = view.opportunity_rows(ops, now=NOW, tz=timezone.utc, simple=True, item="Star")
    assert "Star" in rows[0] and "Planet" not in rows[0] and "Dip (%)" not in rows[0] and "Schedule" not in rows[0]
    assert rows[0]["What the network wants"] == "Keep its light curve current"
    bad = view.unreachable_rows(report["opportunities"], simple=True, item="Star")
    assert bad and "Star" in bad[0] and "Why not" in bad[0]
    assert view.has_transit_facts(report["opportunities"]) is False
