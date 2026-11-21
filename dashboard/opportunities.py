"""Opportunity helpers: what could I take part in, can my telescope reach it, when, and what would it be worth?

They read the *opportunity reports* plugins offer (``BaseExperimentPlugin.opportunities``, shape in
:mod:`core.opportunities`).  They know no science case; all they know is a network's task, a window and an
impact estimate.  They start nothing and send nothing.  The one thing they write is a *planned observation*
(``Database.plan_observation``): a note a person made.  Turning a plan into a mission is
``python main.py --from-plan ID``.  A window whose plan is done or missed can be planned again: that is a new
plan and a new mission, and the earlier mission stays on the earlier plan.

The network lists are read from the cache when the UI opens, so it opens at once and never hits a network
by surprise; **Refresh from the networks** is the only thing that downloads.
"""

from __future__ import annotations

import math
from collections.abc import MutableMapping
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from core.clock import parse_time
from dashboard.fleet import fmt_span
from dashboard.vocab import PLAN_STATUS_COLOURS, PLAN_STATUS_PLAIN, chip, plural

HORIZON_DAYS = 30
CACHE_SECONDS = 600  # a held report is reused this long (it is cheap to recompute from the cached lists, but not free)

TASK_PLAIN = {
    "ephemeris_refinement": "Keep its schedule fresh",
    "ttv_monitoring": "Watch for timing changes",
    "astrometry_followup": "Measure its position",
    "transient_followup": "Follow up an alert",
    "photometric_monitoring": "Keep its light curve current",
    "manual": "Chosen by hand",
}
TASK_TECHNICAL = {
    "ephemeris_refinement": "ephemeris refinement", "ttv_monitoring": "TTV monitoring",
    "astrometry_followup": "astrometry follow-up", "transient_followup": "transient follow-up",
    "photometric_monitoring": "photometric monitoring", "manual": "manual",
}
SOURCE_PLAIN = {
    "exoclock": "ExoClock", "exoplanet_watch": "NASA Exoplanet Watch", "nasa_exoplanet_archive": "NASA Exoplanet Archive",
    "mpc_neocp": "Minor Planet Center", "aavso_target_tool": "AAVSO Target Tool", "manual": "Chosen by hand", "built_in": "Built-in list", "operator": "Your own list",
}
#: Impact level -> (colour, plain label, technical label)
IMPACT_STYLE = {
    "high": ("#198754", "High value", "high"),
    "medium": ("#fd7e14", "Some value", "medium"),
    "low": ("#6c757d", "Little gain", "low"),
    "unknown": ("#0dcaf0", "Not estimable", "unknown"),
}
#: Schedule verdict -> label (the plugin's sentence follows it)
SCHEDULE_LABEL = {
    "up_to_date": "Up to date", "getting_stale": "Getting stale", "out_of_date": "Out of date", "unknown": "Can't tell",
}
#: Sorting by gain: a schedule with no stated error is the least trustworthy of all, so it follows the measured high-value ones.
FORECAST_PLAIN = {"clear": "clear", "partly": "partly cloudy", "cloudy": "cloudy"}
FIRST_LIGHT_MAX_V = 11.5  # bright enough that a small scope sees it well
FIRST_LIGHT_MIN_DEPTH_PCT = 1.0  # deep enough to be obvious in the very first light curve
FIRST_LIGHT_MIN_ALT_DEG = 40.0  # high at mid-transit: least air, least field-edge trouble
FIRST_LIGHT_MAX_ALT_DEG = 70.0  # but not at the zenith, where an alt-az mount turns the field fastest (MAX_ALT_DEG)
FIRST_LIGHT_DURATION_H = (1.0, 4.5)  # long enough to bracket, short enough for a first supervised night
FIRST_LIGHT_SCHEDULES = ("up_to_date", "getting_stale")  # a well-known schedule: it will be where the page says

IMPACT_ORDER = {"high": 0, "unknown": 1, "medium": 2, "low": 3}
SORTS = {
    "priority": "Best (what the networks rank highest)",
    "easiest": "Easiest to observe (bright and high)",
    "soonest": "Soonest (the next window)",
    "gain": "Biggest gain (one measurement helps most)",
}
#: The card's "stays above" line. Also a small bonus in the ease ranking.
HIGH_ALT_DEG = 70.0
#: Degrees of peak altitude that count the same as one magnitude in "Easiest to observe".
EASE_ALT_PER_MAG = 25.0
#: One hour above ``HIGH_ALT_DEG``, counted in magnitudes.
EASE_HOUR_WORTH_MAG = 0.08
#: Below this the air gets thick quickly, so each degree costs extra.
EASE_LOW_ALT_DEG = 40.0
EASE_LOW_PER_DEG = 0.05
#: A frame that saturates near maximum is treated as one magnitude harder.
EASE_SATURATION_PENALTY = 1.0
#: Greenwich mean sidereal time, degrees per SI second. Matches the night grid in the variable-star planner.
_SIDEREAL_DEG_PER_S = 360.98564736629 / 86400.0
#: The altitude curve is sampled on this step, and "now" joins the curve when the window opens within this long.
CURVE_STEP = timedelta(minutes=10)
CURVE_LEAD = timedelta(hours=6)
CURVE_TAIL = timedelta(hours=2)
#: What a plugin's targets and windows are called.  A report may override them (``report["words"]``, see core/opportunities.py).
DEFAULT_WORDS = {"item": "Planet", "items": "planets", "chance": "transit", "chances": "transits"}


def words_for(report: dict[str, Any]) -> dict[str, str]:
    return {**DEFAULT_WORDS, **(report.get("words") or {})}


def category_label(report: dict[str, Any]) -> str:
    """The science-case switch: the plugin's name, and how many of its targets this telescope can reach.

    An empty list (nothing downloaded yet) is named on its own, with no count of zero.
    """
    if not report.get("opportunities"):
        return str(report["label"])
    words = words_for(report)
    n = sum(1 for o in report["opportunities"] if o["reachable"])
    noun = words["item"].lower() if n == 1 else words["items"].lower()
    return f"{report['label']} ({n} {noun})"


def has_transit_facts(ops: list[dict[str, Any]]) -> bool:
    """True when the targets are transiting planets (the first-light picker is built on depth and duration)."""
    return any((o.get("facts") or {}).get("depth_pct") is not None for o in ops)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def local_tz() -> tzinfo:
    """This computer's time zone (the dashboard normally runs at the telescope)."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def night_of(when: datetime, tz: tzinfo) -> date:
    """The calendar date of the *evening* a moment belongs to: 01:00 on the 29th is part of the night of the 28th."""
    return (when.astimezone(tz) - timedelta(hours=12)).date()


def tonight(now: datetime, tz: tzinfo) -> date:
    """The night a person means by "tonight": the one in progress before about 07:00, otherwise the coming one.

    (``night_of`` files a *window* under its evening and flips at noon, which is right for a window that is always
    in the dark.  "Tonight" has to flip earlier: at 08:00 the night that just ended is not tonight.)
    """
    return (now.astimezone(tz) - timedelta(hours=7)).date()


def zone_name(when: datetime) -> str:
    """The name of ``when``'s own zone, such as ``MST``. A fixed offset with no name is shown as ``UTC``."""
    return when.tzname() or "UTC"


def fmt_clock(when: datetime, *, hour12: bool = True) -> str:
    """``21:30`` or ``9:30 PM``. Midnight is 12:00 AM and noon is 12:00 PM."""
    if not hour12:
        return f"{when:%H:%M}"
    hour = when.hour % 12 or 12
    return f"{hour}:{when:%M} {'AM' if when.hour < 12 else 'PM'}"


#: Shared with the Opportunities page, so one choice applies on both.


def fmt_when(iso: str, tz: tzinfo, *, simple: bool = True, hour12: bool = True) -> str:
    """``Thu 29 Oct, 11:42 PM MST`` (and ``(5:42 AM UTC)`` as well in the technical view)."""
    when = parse_time(iso)
    local = when.astimezone(tz)
    text = f"{local:%a %d %b}, {fmt_clock(local, hour12=hour12)} {zone_name(local)}"
    return text if simple else f"{text} ({fmt_clock(when, hour12=hour12)} UTC)"


def fmt_range(start_iso: str, end_iso: str, tz: tzinfo, *, simple: bool = True, hour12: bool = True) -> str:
    """A start and end in local time, each with its zone. The end keeps its date when the span crosses midnight."""
    start, end = parse_time(start_iso), parse_time(end_iso)
    left = fmt_when(start.isoformat(), tz, simple=simple, hour12=hour12)
    end_local = end.astimezone(tz)
    same_night = simple and start.astimezone(tz).date() == end_local.date()
    right = (
        f"{fmt_clock(end_local, hour12=hour12)} {zone_name(end_local)}"
        if same_night else fmt_when(end.isoformat(), tz, simple=simple, hour12=hour12)
    )
    return f"{left} to {right}"


def window_timing(
    start: str, end: str, *, mid: str | None = None, duration_h: float | None = None, night: bool = False,
) -> dict[str, Any]:
    """The times a person needs before committing a window.

    ``start`` and ``end`` are the proposed mission: the span a mission would wait for and observe.
    When ``mid`` is the middle of a published event and ``duration_h`` is that event's length, the
    event runs from half that length before the middle to half after. A night has no such event,
    so none is invented.
    """
    opened, closed = parse_time(start), parse_time(end)
    event_start = event_end = None
    if not night and mid and duration_h is not None and float(duration_h) > 0:
        centre = parse_time(mid)
        half = timedelta(hours=float(duration_h) / 2.0)
        event_start, event_end = (centre - half).isoformat(), (centre + half).isoformat()
    return {
        "mission_start": opened.isoformat(), "mission_end": closed.isoformat(),
        "mission_hours": (closed - opened).total_seconds() / 3600.0,
        "event_start": event_start, "event_end": event_end, "night": night,
    }


def timing_for_window(window: dict[str, Any], *, duration_h: float | None = None, night: bool = False) -> dict[str, Any]:
    """:func:`window_timing` for an opportunity window (``start``, ``end``, and ``mid`` when it has one)."""
    return window_timing(window["start"], window["end"], mid=window.get("mid"), duration_h=duration_h, night=night)


def timing_for_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """:func:`window_timing` for a saved plan. A night plan keeps its window and is not given an event."""
    target = plan.get("target") or {}
    night = target.get("window_kind") == "night"
    return window_timing(
        plan["window_start"], plan["window_end"],
        mid=target.get("predicted_t_mid_iso"), duration_h=None if night else target.get("duration_h"), night=night,
    )


def timing_lines(
    timing: dict[str, Any], tz: tzinfo, *, simple: bool = True, noted: bool = False, hour12: bool = True,
) -> list[str]:
    """The sentences for one window: the event when there is one, then the proposed mission and its length.

    ``noted`` means a longer note already says what the window is (a usable night, for example), so this
    does not repeat it.
    """
    lines = []
    if timing["event_start"]:
        lines.append(f"Event: {fmt_range(timing['event_start'], timing['event_end'], tz, simple=simple, hour12=hour12)}")
    lines.append(
        f"Proposed mission: {fmt_range(timing['mission_start'], timing['mission_end'], tz, simple=simple, hour12=hour12)} "
        f"({timing['mission_hours']:.1f} h)"
    )
    if not timing["event_start"] and not noted:
        what = "a usable night, not a timed event" if timing["night"] else "the time the mission would run"
        lines.append(f"No separate event: this window is {what}.")
    return lines


def when_phrase(window: dict[str, Any], now: datetime) -> str:
    """``under way now`` / ``in 3 h 05 m`` / ``in 2 d 4 h``."""
    start, end = parse_time(window["start"]), parse_time(window["end"])
    if start <= now <= end:
        return "under way now"
    return f"in {fmt_span((start - now).total_seconds())}"


def parse_report_time(report: dict[str, Any]) -> datetime:
    return parse_time(report["generated_utc"])


# --------------------------------------------------------------------------- #
# Filtering and views
# --------------------------------------------------------------------------- #
def windows_within(op: dict[str, Any], now: datetime, days: int) -> list[dict[str, Any]]:
    """The windows of ``op`` that have not ended and begin inside the next ``days`` days."""
    horizon = now + timedelta(days=days)
    return [w for w in op["windows"] if parse_time(w["end"]) >= now and parse_time(w["start"]) <= horizon]


def filter_opportunities(
    ops: list[dict[str, Any]], *, now: datetime, tz: tzinfo, days: int = HORIZON_DAYS,
    tasks: set[str] | None = None, tonight_only: bool = False,
) -> list[dict[str, Any]]:
    """Reachable opportunities with their windows cut to the horizon (and to tonight), best first, none with no window left."""
    out: list[dict[str, Any]] = []
    for op in ops:
        if not op["reachable"] or (tasks is not None and op["network"]["task"] not in tasks):
            continue
        wins = windows_within(op, now, days)
        if tonight_only:
            wins = [w for w in wins if night_of(parse_time(w["mid"]), tz) == tonight(now, tz)]
        if wins:
            out.append({**op, "windows": wins})
    return out  # the report already ordered them by the network's own ranking


def is_uncertain(window: dict[str, Any], baseline_min: float | None) -> bool:
    """True when the predicted mid-time is less certain than the baseline taken either side (or has no stated error)."""
    if baseline_min is None:
        return False
    err = window.get("mid_err_min")
    return err is None or err > baseline_min


def _gmst_deg(unix_s: float) -> float:
    """Greenwich mean sidereal time in degrees. Good to about 0.1 deg: the same approximation the night grid uses."""
    jd = unix_s / 86400.0 + 2440587.5
    return (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0


def _lst_deg(unix_s: float, lon_deg: float) -> float:
    return (_gmst_deg(unix_s) + lon_deg) % 360.0


def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _altitude_deg(ra_deg: float, dec_deg: float, lat_deg: float, ha_deg: float) -> float:
    """Altitude from latitude, declination and hour angle, by plain spherical astronomy."""
    lat, dec, ha = math.radians(lat_deg), math.radians(dec_deg), math.radians(ha_deg)
    sin_alt = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))


def _altitude_at(ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, when: datetime) -> float:
    ha = _wrap180(_lst_deg(when.timestamp(), lon_deg) - ra_deg)
    return _altitude_deg(ra_deg, dec_deg, lat_deg, ha)


def _hours_above(ha0_deg: float, dur_s: float, dec_deg: float, lat_deg: float, alt_min: float) -> float:
    """Hours in a window of ``dur_s`` seconds, starting at hour angle ``ha0_deg``, spent at or above ``alt_min``."""
    if dur_s <= 0:
        return 0.0
    lat, dec = math.radians(lat_deg), math.radians(dec_deg)
    denom = math.cos(lat) * math.cos(dec)
    if abs(denom) < 1e-9:
        return dur_s / 3600.0 if _altitude_deg(0.0, dec_deg, lat_deg, 0.0) >= alt_min else 0.0
    cos_h = (math.sin(math.radians(alt_min)) - math.sin(lat) * math.sin(dec)) / denom
    if cos_h >= 1.0:
        return 0.0  # even culmination is below the line
    if cos_h <= -1.0:
        return dur_s / 3600.0  # still above the line at its lowest
    half_s = math.degrees(math.acos(cos_h)) / _SIDEREAL_DEG_PER_S
    # The star is high for ``half_s`` either side of each transit. A window under a day meets at most two.
    total = 0.0
    for centre_s in (-ha0_deg / _SIDEREAL_DEG_PER_S, (360.0 - ha0_deg) / _SIDEREAL_DEG_PER_S):
        lo, hi = max(0.0, centre_s - half_s), min(dur_s, centre_s + half_s)
        if hi > lo:
            total += hi - lo
    return total / 3600.0


def _counting_magnitude(facts: dict[str, Any]) -> float | None:
    """The magnitude the ease ranking can count on: the faint end of a range, not an outburst maximum.

    A catalogue maximum of a nova or a Mira is how bright the star once got, not how bright it is tonight.
    ``faintest_mag`` is that faint end when a plugin computed one; otherwise the fainter of ``v_mag`` and ``v_min_mag``.
    """
    bright = facts.get("v_mag")
    faint = facts.get("faintest_mag")
    if faint is None and facts.get("v_min_mag") is not None:
        faint = facts["v_min_mag"] if bright is None else max(float(facts["v_min_mag"]), float(bright))
    if facts.get("only_when_bright"):
        if faint is not None:
            return float(faint)
        return None if bright is None else float(bright) + 5.0
    if faint is not None:
        return float(faint)
    return None if bright is None else float(bright)


def _ease(peak_alt_deg: float, hours_above: float, facts: dict[str, Any]) -> float:
    """Higher is easier to observe. Missing coordinates never get here; a missing magnitude sorts last.

    One magnitude brighter is worth ``EASE_ALT_PER_MAG`` degrees of peak altitude. An hour above
    ``HIGH_ALT_DEG`` is a small extra. Below ``EASE_LOW_ALT_DEG`` each degree costs more, because the air
    thickens. A target that saturates near its brightest loses a magnitude: a fixed exposure may not be usable then.
    """
    mag = _counting_magnitude(facts)
    altitude = peak_alt_deg / EASE_ALT_PER_MAG + hours_above * EASE_HOUR_WORTH_MAG
    if peak_alt_deg < EASE_LOW_ALT_DEG:
        altitude -= (EASE_LOW_ALT_DEG - peak_alt_deg) * EASE_LOW_PER_DEG
    if mag is None:
        return altitude - 100.0
    penalty = EASE_SATURATION_PENALTY if facts.get("saturates_when_bright") else 0.0
    return -mag + altitude - penalty


def _airmass(alt_deg: float) -> float:
    s = math.sin(math.radians(alt_deg))
    return 1.0 / s if s > 0.1 else 10.0


def _sky_coords(op: dict[str, Any], site: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """``(ra, dec, lat, lon)`` in degrees, or ``None`` when either the target or the site is incomplete."""
    target, facts = op.get("target") or {}, op.get("facts") or {}
    ra = target.get("ra_deg")
    if ra is None:
        ra = facts.get("ra_deg")
    dec = target.get("dec_deg")
    if dec is None:
        dec = facts.get("dec_deg")
    lat, lon = site.get("lat"), site.get("lon")
    if ra is None or dec is None or lat is None or lon is None:
        return None
    return float(ra), float(dec), float(lat), float(lon)


def window_altitude(op: dict[str, Any], window: dict[str, Any], site: dict[str, Any]) -> dict[str, Any] | None:
    """How high ``op`` gets during ``window``, when, and how long it stays above ``HIGH_ALT_DEG``.

    ``None`` when the target has no coordinates or the site has no latitude. Altitude is plain spherical
    astronomy (a fraction of a degree): the same footing as the windows themselves, not a pointing model.
    ``ease`` is higher for a brighter target that climbs higher (see :func:`_ease`).
    """
    coords = _sky_coords(op, site)
    if coords is None:
        return None
    ra, dec, lat, lon = coords
    facts = op.get("facts") or {}
    start, end = parse_time(window["start"]), parse_time(window["end"])
    dur_s = (end - start).total_seconds()
    if dur_s <= 0:
        return None
    ha0 = _wrap180(_lst_deg(start.timestamp(), lon) - ra)
    # Hour angle increases. ha0 <= 0 means the transit is still ahead; ha0 > 0 means it has passed and the next one is a day away.
    if ha0 <= 0.0:
        transit_in_s = -ha0 / _SIDEREAL_DEG_PER_S
    else:
        transit_in_s = (360.0 - ha0) / _SIDEREAL_DEG_PER_S
    if 0.0 <= transit_in_s <= dur_s:
        peak_in_s = transit_in_s
        peak = _altitude_deg(ra, dec, lat, 0.0)
        at_start = at_end = False
    else:
        alt_start = _altitude_deg(ra, dec, lat, ha0)
        alt_end = _altitude_at(ra, dec, lat, lon, end)
        at_start = alt_start >= alt_end
        at_end = not at_start
        peak_in_s = 0.0 if at_start else dur_s
        peak = alt_start if at_start else alt_end
    hours = _hours_above(ha0, dur_s, dec, lat, HIGH_ALT_DEG)
    return {
        "peak_alt_deg": peak, "peak_at": start + timedelta(seconds=peak_in_s),
        "hours_above": hours, "high_deg": HIGH_ALT_DEG,
        "peaks_at_start": at_start, "peaks_at_end": at_end, "airmass": _airmass(peak),
        "ease": _ease(peak, hours, facts),
    }


def altitude_line(summary: dict[str, Any], tz: tzinfo, *, simple: bool = True, hour12: bool = True) -> str:
    """One sentence: the highest point of this window, and whether the target stays above 70 deg."""
    peak = summary["peak_alt_deg"]
    alt = f"{peak:.0f}" if simple else f"{peak:.1f}"
    local = summary["peak_at"].astimezone(tz)
    clock = f"{fmt_clock(local, hour12=hour12)} {zone_name(local)}"
    if summary["peaks_at_start"]:
        head = f"Highest at the start of the window, {alt} deg"
    elif summary["peaks_at_end"]:
        head = f"Highest at the end of the window, {alt} deg"
    else:
        head = f"Climbs to {alt} deg at {clock}"
    if not simple:
        head += f" (airmass {summary['airmass']:.2f})"
    high = summary["high_deg"]
    hours = summary["hours_above"]
    if hours >= 0.05:
        stay = f"stays above {high:.0f} deg for {hours:.1f} h"
        return f"{head}, and it {stay}." if summary["peaks_at_start"] or summary["peaks_at_end"] else f"{head} and {stay}."
    return f"{head}. It does not reach {high:.0f} deg."


def altitude_track(
    op: dict[str, Any], window: dict[str, Any], site: dict[str, Any], now: datetime,
) -> dict[str, Any] | None:
    """:func:`window_altitude` plus the curve to draw and where the target is at ``now``.

    The curve covers the window. ``now`` is included when it falls in the window, within
    :data:`CURVE_LEAD` before it opens, or within :data:`CURVE_TAIL` after it closes, so a window
    opening this evening shows the climb into it. ``samples`` are ``{"at", "alt_deg"}``, earliest first.
    """
    sky = window_altitude(op, window, site)
    coords = _sky_coords(op, site)
    if sky is None or coords is None:
        return None
    ra, dec, lat, lon = coords
    start, end = parse_time(window["start"]), parse_time(window["end"])
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    plot_start, plot_end = start, end
    if start - CURVE_LEAD <= now < start:
        plot_start = now
    elif end < now <= end + CURVE_TAIL:
        plot_end = now
    times = [plot_start]
    cursor = plot_start + CURVE_STEP
    while cursor < plot_end - timedelta(seconds=30):
        times.append(cursor)
        cursor += CURVE_STEP
    if plot_end != plot_start:
        times.append(plot_end)
    peak_at = sky["peak_at"]
    if plot_start <= peak_at <= plot_end:
        times.append(peak_at)
    if plot_start <= now <= plot_end:
        times.append(now)
    samples = [{"at": t, "alt_deg": _altitude_at(ra, dec, lat, lon, t)} for t in sorted(set(times))]
    now_alt = _altitude_at(ra, dec, lat, lon, now)
    later = _altitude_at(ra, dec, lat, lon, now + timedelta(minutes=20))
    return {
        **sky, "samples": samples, "plot_start": plot_start, "plot_end": plot_end,
        "now_alt_deg": now_alt, "now_at": now, "now_on_curve": plot_start <= now <= plot_end,
        "rising": later > now_alt + 0.05,
        "now_airmass": _airmass(now_alt) if now_alt > 5.0 else None,
    }


def altitude_now_line(track: dict[str, Any], *, simple: bool = True) -> str:
    """Where the target is at the moment the page was drawn."""
    alt = track["now_alt_deg"]
    shown = f"{alt:.0f}" if simple else f"{alt:.1f}"
    near_peak = (
        track["now_on_curve"]
        and abs((track["now_at"] - track["peak_at"]).total_seconds()) <= 15 * 60
        and alt >= track["peak_alt_deg"] - 1.0
    )
    if alt < 0:
        phrase = f"Now {shown} deg, below the horizon"
    elif near_peak:
        phrase = f"Now {shown} deg, at its highest"
    else:
        phrase = f"Now {shown} deg and {'climbing' if track['rising'] else 'descending'}"
    if not simple and track.get("now_airmass"):
        phrase += f" (airmass {track['now_airmass']:.2f})"
    if not track["now_on_curve"]:
        if track["now_at"] < track["plot_start"]:
            phrase += ", before this window"
        else:
            return phrase + ". This window has ended."
    return phrase + "."


def altitude_figure(track: dict[str, Any], tz: tzinfo, *, hour12: bool = True):
    """Altitude through the window. The highest point is marked, and so is now when it lies on the curve.

    Times are the site's clock with the zone stripped, so the axis shows the same numbers as the rest of
    the page rather than shifting them into the browser's zone.
    """
    import plotly.graph_objects as go

    def local(when: datetime) -> datetime:
        return when.astimezone(tz).replace(tzinfo=None)

    samples = track["samples"]
    hover = [
        f"{fmt_clock(s['at'].astimezone(tz), hour12=hour12)} {zone_name(s['at'].astimezone(tz))}<br>{s['alt_deg']:.0f} deg"
        for s in samples
    ]

    def label_pos(when: datetime) -> str:
        # A label centred on a point at either end of the axis is clipped by the plot edge.
        span = (track["plot_end"] - track["plot_start"]).total_seconds() or 1.0
        frac = (when - track["plot_start"]).total_seconds() / span
        if frac < 0.2:
            return "top right"
        if frac > 0.8:
            return "top left"
        return "top center"

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[local(s["at"]) for s in samples], y=[s["alt_deg"] for s in samples], mode="lines", name="Altitude",
        line=dict(width=2, color="#4dabf7"), text=hover, hovertemplate="%{text}<extra></extra>",
    ))
    fig.add_hline(
        y=HIGH_ALT_DEG, line=dict(width=1, dash="dot", color="#868e96"),
        annotation_text="70 deg", annotation_position="bottom right", annotation_font_color="#868e96",
    )
    peak_is_now = track["now_on_curve"] and abs((track["now_at"] - track["peak_at"]).total_seconds()) < 10 * 60
    if not peak_is_now:
        fig.add_trace(go.Scatter(
            x=[local(track["peak_at"])], y=[track["peak_alt_deg"]], mode="markers+text", name="Highest",
            marker=dict(size=9, symbol="circle", color="#69db7c"),
            text=[f"{track['peak_alt_deg']:.0f} deg"], textposition=label_pos(track["peak_at"]),
            textfont=dict(color="#69db7c"),
            hovertemplate=f"Highest<br>{fmt_clock(track['peak_at'].astimezone(tz), hour12=hour12)}<br>{track['peak_alt_deg']:.0f} deg<extra></extra>",
        ))
    if track["now_on_curve"]:
        label = f"now, {track['now_alt_deg']:.0f} deg" if peak_is_now else "now"
        fig.add_trace(go.Scatter(
            x=[local(track["now_at"])], y=[track["now_alt_deg"]], mode="markers+text", name="Now",
            marker=dict(size=11, symbol="diamond", color="#ff922b"),
            text=[label], textposition=label_pos(track["now_at"]), textfont=dict(color="#ff922b"),
            hovertemplate=f"Now<br>{track['now_alt_deg']:.0f} deg<extra></extra>",
        ))
    lows = [s["alt_deg"] for s in samples]
    floor = min(0.0, min(lows) - 2.0)
    fig.update_layout(
        height=210, margin=dict(l=8, r=8, t=22, b=8), showlegend=False,
        yaxis=dict(title="Altitude (deg)", range=[floor, 96], dtick=15),
        xaxis=dict(title=zone_name(samples[0]["at"].astimezone(tz)), tickformat="%I:%M %p" if hour12 else "%H:%M"),
    )
    return fig


def sort_opportunities(
    ops: list[dict[str, Any]], how: str, *, site: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """``ops`` in the chosen order.

    ``priority`` keeps the networks' own ranking (as given). ``soonest`` follows the next window.
    ``gain`` follows the estimated timing gain. ``easiest`` follows :func:`window_altitude` on the next
    window: bright and high first, counting a brightness range at its faint end. Without ``site``,
    ``easiest`` has nothing to rank on and the given order is kept.
    """
    if how == "soonest":
        return sorted(ops, key=lambda o: parse_time(o["windows"][0]["start"]))
    if how == "gain":
        def key(o: dict[str, Any]) -> tuple[int, float, float]:
            impact = o.get("impact") or {}
            gain = (impact.get("figures") or {}).get("gain_pct")
            return IMPACT_ORDER.get(impact.get("level", "unknown"), 1), -(gain or 0.0), -(o["network"].get("score") or 0.0)
        return sorted(ops, key=key)
    if how == "easiest" and site:
        def ease_key(o: dict[str, Any]) -> tuple[float, datetime, str]:
            sky = window_altitude(o, o["windows"][0], site) if o.get("windows") else None
            ease = sky["ease"] if sky else -1e9
            start = parse_time(o["windows"][0]["start"]) if o.get("windows") else parse_time("9999-01-01T00:00:00+00:00")
            return (-ease, start, o["name"])
        return sorted(ops, key=ease_key)
    return list(ops)


def window_forecast(forecast: dict[str, Any] | None, window: dict[str, Any]) -> dict[str, Any] | None:
    """The forecast summary for one window (see :func:`tools.weather.summarise_window`), or None when not covered."""
    if not forecast or not forecast.get("hours"):
        return None
    from tools.weather import summarise_window

    return summarise_window(forecast["hours"], parse_time(window["start"]), parse_time(window["end"]))


def forecast_text(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    return f"forecast {FORECAST_PLAIN[summary['verdict']]} ({summary['cloud_mean_pct']:.0f}% cloud on average, up to {summary['cloud_max_pct']:.0f}%)"


def drop_cloudy(ops: list[dict[str, Any]], forecast: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``ops`` without the windows forecast to be cloudy (windows beyond the forecast are kept: nothing is known about them)."""
    if not forecast:
        return ops
    out = []
    for op in ops:
        wins = [w for w in op["windows"] if (window_forecast(forecast, w) or {}).get("verdict") != "cloudy"]
        if wins:
            out.append({**op, "windows": wins})
    return out


def first_light_candidates(
    ops: list[dict[str, Any]], *, site: dict[str, Any], now: datetime, days: int = 7, baseline_min: float | None = None,
) -> list[dict[str, Any]]:
    """Targets for the very first nights with a new telescope: to *verify* the pipeline, not to do science.

    Bright, deep, high at mid-transit, well-known schedule, and a transit that fits comfortably in a night:
    exactly the kind of event whose light curve should come out right if the telescope, the driver and the
    reduction all work.  Each result keeps only its windows that qualify, carries ``first_light`` (a score, the
    altitude at mid-transit and a list of reasons a person can check) and the best score comes first.
    Where the telescope is not an input: altitude is worked out for ``site`` at the middle of each window.
    """
    from tools.astrometry import target_altitude_deg

    out: list[dict[str, Any]] = []
    for op in ops:
        facts = op.get("facts") or {}
        sched = (op.get("schedule") or {}).get("verdict")
        v, depth, dur = facts.get("v_mag"), facts.get("depth_pct"), facts.get("duration_h")
        if v is None or depth is None or dur is None or sched not in FIRST_LIGHT_SCHEDULES:
            continue
        if v > FIRST_LIGHT_MAX_V or depth < FIRST_LIGHT_MIN_DEPTH_PCT or not (FIRST_LIGHT_DURATION_H[0] <= dur <= FIRST_LIGHT_DURATION_H[1]):
            continue
        target = op.get("target") or {}
        ra, dec = target.get("ra_deg"), target.get("dec_deg")
        if ra is None or dec is None:
            continue
        kept: list[tuple[float, dict[str, Any]]] = []
        for w in windows_within(op, now, days):
            if is_uncertain(w, baseline_min) or w.get("mid_err_min") is None:
                continue
            alt = target_altitude_deg(float(ra), float(dec), site["lat"], site["lon"], site.get("elev_m", 0.0), parse_time(w["mid"]))
            if FIRST_LIGHT_MIN_ALT_DEG <= alt <= FIRST_LIGHT_MAX_ALT_DEG:
                kept.append((alt, w))
        if not kept:
            continue
        kept.sort(key=lambda aw: parse_time(aw[1]["start"]))
        alt0 = max(a for a, _ in kept)
        score = (12.0 - v) + min(depth, 3.0) + (alt0 - FIRST_LIGHT_MIN_ALT_DEG) / 15.0 + (1.5 if sched == "up_to_date" else 0.5)
        err = kept[0][1].get("mid_err_min")
        why = [
            f"bright (host V {v:.1f})", f"deep (dips {depth:.1f}%)",
            f"high ({alt0:.0f} deg above the horizon at mid-transit)",
            f"well-known schedule (mid-time good to about +/-{err:.0f} min)",
            f"fits a night ({dur:.1f} h transit)",
        ]
        out.append({**op, "windows": [w for _, w in kept], "first_light": {"score": score, "altitude_deg": alt0, "why": why}})
    out.sort(key=lambda o: (-o["first_light"]["score"], parse_time(o["windows"][0]["start"])))
    return out


def window_label(
    window: dict[str, Any], tz: tzinfo, now: datetime, *, simple: bool = True, baseline_min: float | None = None,
    forecast: dict[str, Any] | None = None, hour12: bool = True,
) -> str:
    err = window.get("mid_err_min")
    text = f"{fmt_when(window['mid'], tz, simple=simple, hour12=hour12)}, {when_phrase(window, now)}"
    if not simple and err is not None:
        text += f", +/-{err:.0f} min"
    if is_uncertain(window, baseline_min):
        text += " (time uncertain)"
    summary = window_forecast(forecast, window)
    if summary:
        text += f" [{FORECAST_PLAIN[summary['verdict']]}]"
    return text


def calendar_entry_line(entry: dict[str, Any]) -> str:
    """One night's line: the local time, the target, ``(!)`` when the time is uncertain, and the sky when known."""
    sky = {"clear": " - clear", "partly": " - partly cloudy", "cloudy": " - cloudy"}.get(entry.get("sky") or "", "")
    return f"{entry['time']} {entry['name']}" + (" (!)" if entry["uncertain"] else "") + sky


def night_target_list(entries: list[dict[str, Any]]) -> str:
    """Every target that night, one per line, earliest mid-time first.

    A night runs evening to morning, so 9:55 PM comes before 12:51 AM. The calendar cell keeps network
    rank; this order is only for the copyable list.
    """
    return "\n".join(calendar_entry_line(e) for e in sorted(entries, key=lambda e: e["mid"]))


def calendar_weeks(
    ops: list[dict[str, Any]], *, now: datetime, days: int, tz: tzinfo, per_day: int = 4, baseline_min: float | None = None,
    forecast: dict[str, Any] | None = None, hour12: bool = True,
) -> list[list[dict[str, Any]]]:
    """A Monday-first calendar of nights from tonight for ``days`` nights.

    Each day is ``{"date", "in_range", "is_tonight", "entries", "more", "all_entries"}``. An entry is one
    transit (``name``, ``time``, ``level``, ``uncertain``, ``sky``). ``entries`` is the first ``per_day`` and
    ``all_entries`` is every one that night, both best-ranked first (``ops`` order). ``more`` is how many
    are past the cap.
    """
    first = tonight(now, tz)
    last = first + timedelta(days=days - 1)
    by_day: dict[date, list[dict[str, Any]]] = {}
    for op in ops:
        for w in op["windows"]:
            mid = parse_time(w["mid"])
            night = night_of(mid, tz)
            if first <= night <= last:
                local = mid.astimezone(tz)
                by_day.setdefault(night, []).append({
                    "name": op["name"], "time": f"{fmt_clock(local, hour12=hour12)} {zone_name(local)}", "mid": mid,
                    "level": (op.get("impact") or {}).get("level", "unknown"),
                    "uncertain": is_uncertain(w, baseline_min),
                    "sky": (window_forecast(forecast, w) or {}).get("verdict"),
                })
    start = first - timedelta(days=first.weekday())
    weeks: list[list[dict[str, Any]]] = []
    day = start
    while day <= last:
        week = []
        for _ in range(7):
            entries = by_day.get(day, [])
            week.append({
                "date": day, "in_range": first <= day <= last, "is_tonight": day == first,
                "entries": entries[:per_day], "all_entries": entries, "more": max(0, len(entries) - per_day),
            })
            day += timedelta(days=1)
        weeks.append(week)
    return weeks


def schedule_label(op: dict[str, Any]) -> str:
    """The plain schedule verdict for a table cell ('' when the plugin gave none; '(provisional)' when it may change)."""
    sched = op.get("schedule")
    if not sched:
        return ""
    return SCHEDULE_LABEL.get(sched["verdict"], sched["verdict"]) + (" (provisional)" if sched.get("provisional") else "")


def opportunity_rows(
    ops: list[dict[str, Any]], *, now: datetime, tz: tzinfo, simple: bool, baseline_min: float | None = None,
    forecast: dict[str, Any] | None = None, item: str = "Planet", hour12: bool = True,
    site: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One table row per reachable opportunity (``ops`` already filtered)."""
    rows = []
    for op in ops:
        net, facts, impact = op["network"], op["facts"], op.get("impact") or {}
        nxt = op["windows"][0]
        sky = window_altitude(op, nxt, site) if site else None
        row = {
            item: op["name"],
            "What the network wants": TASK_PLAIN.get(net["task"], net["task"]) if simple else TASK_TECHNICAL.get(net["task"], net["task"]),
            "Value": IMPACT_STYLE.get(impact.get("level", "unknown"), IMPACT_STYLE["unknown"])[1 if simple else 2],
        }
        if any(o.get("schedule") for o in ops):  # a column only when some plugin gave a verdict
            row["Schedule"] = schedule_label(op)
        row.update({
            "Next chance": window_label(nxt, tz, now, simple=simple, baseline_min=baseline_min, forecast=forecast, hour12=hour12),
            "Chances": len(op["windows"]),
            "Brightness (V)": facts.get("v_mag"),
        })
        if site:
            row["Highest (deg)"] = None if sky is None else round(sky["peak_alt_deg"])
            row["Above 70 deg (h)"] = None if sky is None else round(sky["hours_above"], 1)
        if facts.get("depth_pct") is not None:
            row["Dip (%)"] = facts.get("depth_pct")
        if facts.get("v_min_mag") is not None:
            row["Faintest (mag)"] = facts.get("v_min_mag")
        if forecast:
            row["Sky forecast"] = forecast_text(window_forecast(forecast, nxt)) or "beyond the forecast"
        if op.get("first_light"):
            row["First-light score"] = round(op["first_light"]["score"], 1)
        if not simple:
            row.update({
                "Rank score": net.get("score"),
                "Lists": ", ".join(net["sources"]),
                "Timing +/- (min)": nxt.get("mid_err_min"),
                "Gain (%)": ((impact.get("figures") or {}).get("gain_pct")),
            })
            if facts.get("depth_pct") is not None:  # a transit measure: not for a plugin whose targets do not transit
                row["Staleness (x half transit)"] = facts.get("staleness")
        rows.append(row)
    return rows


def unreachable_rows(ops: list[dict[str, Any]], *, simple: bool, item: str = "Planet") -> list[dict[str, Any]]:
    rows = []
    for op in ops:
        if op["reachable"]:
            continue
        facts, net = op["facts"], op["network"]
        row = {
            item: op["name"], "Why not": " ".join(op["blockers"]),
            "What the network wants": TASK_PLAIN.get(net["task"], net["task"]) if simple else TASK_TECHNICAL.get(net["task"], net["task"]),
            "Brightness (V)": facts.get("v_mag"),
        }
        if facts.get("depth_pct") is not None:
            row["Dip (%)"] = facts.get("depth_pct")
        if not simple:
            row["Rank score"] = net.get("score")
        rows.append(row)
    return rows


def source_names(sources: list[str]) -> str:
    return ", ".join(SOURCE_PLAIN.get(s, s.replace("_", " ").title()) for s in sources)


def planned_keys(planned: list[dict[str, Any]]) -> dict[tuple[str, datetime], int]:
    """``{(target name, window start): record id}`` for the open plan on that window.

    An open plan is still a note (``PLANNED``, also the default when a row has no status) or a mission in
    progress (``STARTED``).  A finished attempt does not occupy the window, so it can be planned again.
    An unstarted note wins over a mission in progress; otherwise the newest id wins.
    """
    rank = {"PLANNED": 0, "STARTED": 1}
    best: dict[tuple[str, datetime], tuple[tuple[int, int], int]] = {}
    for p in planned:
        status = p.get("status") or "PLANNED"
        if status not in rank:
            continue
        try:
            start = parse_time(p["window_start"]).replace(microsecond=0)
        except (KeyError, TypeError, ValueError):
            continue
        key = (p["target_name"], start)
        score = (rank[status], -int(p["id"]))
        current = best.get(key)
        if current is None or score < current[0]:
            best[key] = (score, int(p["id"]))
    return {key: chosen[1] for key, chosen in best.items()}


def finished_attempts(planned: list[dict[str, Any]], name: str, window: dict[str, Any]) -> list[dict[str, Any]]:
    """Plans for this target and window that already finished (``DONE`` or ``MISSED``), oldest first."""
    key = (name, parse_time(window["start"]).replace(microsecond=0))
    found = []
    for p in planned:
        if p.get("status") not in ("DONE", "MISSED"):
            continue
        try:
            start = parse_time(p["window_start"]).replace(microsecond=0)
        except (KeyError, TypeError, ValueError):
            continue
        if (p.get("target_name"), start) == key:
            found.append(p)
    return sorted(found, key=lambda p: int(p.get("id") or 0))


def earlier_attempt_line(attempts: list[dict[str, Any]]) -> str:
    """One sentence naming the latest finished attempt at this window."""
    latest = max(attempts, key=lambda p: int(p.get("id") or 0))
    word = PLAN_STATUS_PLAIN.get(latest.get("status", ""), str(latest.get("status") or "finished")).lower()
    mission = f", mission {latest['mission_id']}" if latest.get("mission_id") else ""
    count = f" ({len(attempts)} attempts)" if len(attempts) > 1 else ""
    return f"Earlier: {word}{mission}{count}."


def closed_window_sentence(plan: dict[str, Any], now: datetime | None = None) -> str | None:
    """Why a finished plan cannot be planned again, or ``None`` when its window is still open."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        end = parse_time(plan["window_end"])
    except (KeyError, TypeError, ValueError):
        return "This plan has no window, so it cannot be planned again."
    if end <= now:
        return f"This window closed at {end.strftime('%Y-%m-%d %H:%M UTC')}. A later window can be planned from its card."
    return None


def plan_commands(plan: dict[str, Any]) -> dict[str, str]:
    """The two commands that build a mission from ``plan``: a rehearsal on the simulated sky, and the real run."""
    return {
        "rehearse": f"python main.py --from-plan {plan['id']} --simulate --fast",
        "run": f"ASTRONOMO_MOCK_MODE=false python main.py --from-plan {plan['id']}",
    }


def plan_chip(status: str) -> str:
    bg, fg = PLAN_STATUS_COLOURS.get(status, ("#6c757d", "#ffffff"))
    return chip(PLAN_STATUS_PLAIN.get(status, status.title()), bg, fg)


def planned_id(keys: dict[tuple[str, datetime], int], name: str, window: dict[str, Any]) -> int | None:
    return keys.get((name, parse_time(window["start"]).replace(microsecond=0)))


# --------------------------------------------------------------------------- #
# Collecting the reports
# --------------------------------------------------------------------------- #
def collect_reports(settings: Any, *, now: datetime, days: int, refresh: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Every plugin's opportunity report, and a sentence for each plugin that failed (one plugin's bug must not hide the rest)."""
    from plugins import available_plugins, load_plugin

    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    for name in available_plugins():
        try:
            report = load_plugin(name, settings).opportunities(now, days=days, refresh=refresh)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            continue
        if report is not None:
            reports.append(report)
    return reports, errors


def get_reports(
    state: MutableMapping[str, Any], settings: Any, *, force: bool = False, now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The reports for this session: held for ``CACHE_SECONDS`` and per data directory, recomputed on ``force`` (a refresh)."""
    now = now or datetime.now(timezone.utc)
    site = (settings.site_latitude_deg, settings.site_longitude_deg, settings.site_elevation_m, tuple(settings.site_horizon))
    key = (str(settings.data_dir), site, int(now.timestamp() // CACHE_SECONDS))  # a changed site must not show the old windows
    held = state.get("opp_reports")
    if not force and held is not None and held[0] == key:
        return held[1], held[2]
    reports, errors = collect_reports(settings, now=now, days=HORIZON_DAYS, refresh=force)
    tried = state.setdefault("opp_first_download", set())
    if not force and str(settings.data_dir) not in tried and _nothing_downloaded(reports):
        # First time on this computer: an empty page would only say "press Refresh", so download the lists once.
        # One attempt per session and data directory, so an outage is not retried on every rerun.
        tried.add(str(settings.data_dir))
        reports, errors = collect_reports(settings, now=now, days=HORIZON_DAYS, refresh=True)
    state["opp_reports"] = (key, reports, errors)
    return reports, errors


def _nothing_downloaded(reports: list[dict[str, Any]]) -> bool:
    """True when a report found no list on this computer at all (not merely an old one)."""
    return any(r["data"].get("retrieved_utc") is None and r.get("n_candidates", 0) == 0 for r in reports)


# --------------------------------------------------------------------------- #
# The observing site
# --------------------------------------------------------------------------- #
def site_banner(source: str) -> str | None:
    """The sentence shown above the page when the site is not one a person set (``None`` when it is)."""
    if source == "default":
        return (
            "No observing site is set, so every window below is worked out for a placeholder (Denver), not for your telescope. "
            "Set yours on Settings; the list will recompute."
        )
    return None


def guide_sections(guide: dict[str, Any], *, simple: bool) -> list[dict[str, Any]]:
    """The guide's sections for this view: the technical ones appear only in the Technical view."""
    return [s for s in guide["sections"] if not s.get("technical") or not simple]

