"""Transit-timing opportunities: which planets the networks want timed, which of them this telescope can reach, when, and what a timing would be worth.

This answers the dashboard's "what could I take part in?" question without starting anything.  It reads the
same merged and ranked Pro-Am lists a mission uses (:mod:`plugins.transit_finder.networks`), so what is
shown here is what a mission would choose from, and it uses the same limits (``Limits.from_env``, shared
with ``TransitFinderPlugin._schedule``).  The report shape is :mod:`core.opportunities`.

Reachable
    A planet is reachable when its host is bright enough (V), the transit deep enough, it climbs high enough
    from the site at all, and at least one transit in the horizon is **dark and high enough for the whole
    window** (the transit plus a baseline either side).  A planet that fails is still listed, with the
    reasons, never hidden.

Impact (an estimate, with its assumptions attached)
    A published ephemeris predicts a mid-transit time ``n`` epochs out with an uncertainty that grows with
    ``n`` (the ranking's own formula, :func:`plugins.transit_finder.networks.propagated_uncertainty_d`).  A
    new timing with error ``s`` combines with that prediction at its epoch (inverse-variance), which sets
    the mid-time far better than the old schedule did.  What the person sees is the effect a year later:
    the uncertainty of a transit one year after the timing, before and after.  The telescope's own timing
    error is **not measured yet** (a clean night is roadmap Phase 2), so ``s`` is an assumed placeholder
    (``ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN``) that the estimate states every time.  A schedule that is already
    known to a fraction of a minute is reported as "little gain", which is the honest answer for it.

Checked like a mission checks them (core/interlocks.py): the Sun, a minimum altitude, distance from the Sun and from the Moon.
Not checked: weather, a real horizon or obstructions (only a minimum altitude).  The report says so.

Environment overrides (all optional, shared with the plugin)
    ASTRONOMO_TRANSIT_MIN_ALT_DEG          minimum altitude across the window (default 30)
    ASTRONOMO_TRANSIT_BASELINE_MIN         out-of-transit baseline either side (default 60)
    ASTRONOMO_TRANSIT_MAX_VMAG             faintest host V to attempt (default 13.0)
    ASTRONOMO_TRANSIT_MIN_DEPTH_PCT        shallowest depth to attempt (default 0.5)
    ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN      assumed timing error of one night's data, in minutes (default 3.0)
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from config import Settings
from core import interlocks
from core.networks import NetworkError, normalize_name
from core.opportunities import check_report
from plugins.transit_finder import networks
from tools.astrometry import bjd_tdb_to_utc_many, sky_from_grid, sun_moon_grid

LABEL = "Exoplanet transit timing"
SUMMARY = "Planets the networks want timed: which this telescope can reach from this site, when, and what one timing would be worth."
HORIZON_DAYS = 30
SUN_GRID_MIN = 10  # the Sun and Moon are computed on this grid and interpolated (they move ~0.25 deg a minute or less)
MIN_PERIOD_D = 0.2  # a shorter listed period is a data error, not a planet
MAX_EPOCHS_PER_PLANET = 400
LEVEL_HIGH_PCT = 50.0  # one timing tightens the year-ahead uncertainty by at least this much: high
LEVEL_MEDIUM_PCT = 15.0
YEAR_D = 365.25


def _env(name: str, default: str) -> str:
    return os.environ.get(f"ASTRONOMO_TRANSIT_{name}", default)


@dataclass(frozen=True)
class Limits:
    """What "reachable" means.  The first four are the plugin's own mission limits; the rest come from the site
    (the same limits a mission enforces on real hardware, ``core/interlocks.py``)."""

    min_alt_deg: float = 30.0
    max_alt_deg: float = 90.0  # the field turns fastest near the zenith; a mission refuses above its own maximum
    baseline_min: float = 60.0
    max_vmag: float = 13.0
    min_depth_pct: float = 0.5
    max_sun_alt_deg: float = -12.0
    assumed_timing_err_min: float = 3.0
    min_sun_sep_deg: float = 30.0
    moon_avoid_deg: float = 60.0
    moon_floor_deg: float = 15.0
    horizon: tuple = ()  # the site's own horizon, ((azimuth, altitude), ...): see core/site.py

    @classmethod
    def from_env(cls, settings: Settings) -> "Limits":
        return cls(
            # the stricter of the plugin's own minimum and the site-wide one a mission enforces
            min_alt_deg=max(float(_env("MIN_ALT_DEG", "30")), float(settings.min_altitude_deg)),
            max_alt_deg=min(float(_env("MAX_ALT_DEG", "90")), float(settings.max_altitude_deg)),
            baseline_min=float(_env("BASELINE_MIN", "60")),
            max_vmag=float(_env("MAX_VMAG", "13.0")),
            min_depth_pct=float(_env("MIN_DEPTH_PCT", "0.5")),
            max_sun_alt_deg=float(settings.max_sun_altitude_deg),
            assumed_timing_err_min=float(_env("ASSUMED_ERR_MIN", "3.0")),
            min_sun_sep_deg=float(settings.min_sun_separation_deg),
            moon_avoid_deg=float(settings.moon_avoid_deg),
            moon_floor_deg=float(settings.moon_floor_deg),
            horizon=tuple(settings.site_horizon),
        )

    def site_limits(self) -> interlocks.SiteLimits:
        return interlocks.SiteLimits(
            self.min_alt_deg, self.min_sun_sep_deg, self.moon_avoid_deg, self.moon_floor_deg, tuple(self.horizon), self.max_alt_deg
        )


# --------------------------------------------------------------------------- #
# Impact
# --------------------------------------------------------------------------- #
def _finite(value: float | None) -> float | None:
    """``None`` for a missing or infinite number (an unbounded uncertainty is not a figure that can be stored or shown)."""
    return None if value is None or not math.isfinite(value) else float(value)


def fmt_minutes(minutes: float) -> str:
    """``0.3 min`` / ``12 min`` / ``3.4 h``: the unit a person can read at a glance."""
    if minutes < 10:
        return f"{minutes:.1f} min"
    if minutes < 180:
        return f"{minutes:.0f} min"
    return f"{minutes / 60.0:.1f} h"


#: The schedule verdict.  *Out of date*: the predicted transit time is uncertain by half the transit or more (the ratio the
#: network ranking uses as its staleness term), so the transit could fall outside a normal window.  *Up to date*: known to
#: within what one night's timing could achieve (the assumed timing error), so it would barely change: the same footing as the
#: "Little gain" impact level, so the two never contradict each other.  *Getting stale*: in between.
OUT_OF_DATE_FROM = 1.0


def _jd_to_datetime(jd: float) -> datetime:
    return datetime(1858, 11, 17, tzinfo=timezone.utc) + timedelta(days=jd - 2400000.5)  # MJD epoch; a date, not a timestamp


def _ago(then: datetime, now: datetime) -> str:
    days = (now - then).total_seconds() / 86400.0
    if days < 45:
        return "less than two months ago"
    if days < 700:
        return f"about {round(days / 30.44)} months ago"
    return f"about {round(days / 365.25)} years ago"


def schedule_status(
    row: dict[str, Any], now: datetime, limits: Limits, *, provisional: bool = False
) -> dict[str, Any]:
    """A plain verdict on whether this planet's published schedule is out of date, with the date it is counted from.

    ``staleness`` is the predicted transit time's uncertainty over half the transit's length (what the ranking
    already uses): 1 or more is *out of date*; an uncertainty no larger than ``limits.assumed_timing_err_min`` is
    *up to date*; in between is *getting stale*.  The date is the transit the published schedule is anchored on
    (``t0``).  The networks do not publish when a planet was last observed, so it is not that.  ``provisional``
    marks a verdict that may change once a missing list (ExoClock, usually the fresher one) loads.
    """
    ref = _jd_to_datetime(row["t0_bjd_tdb"])
    counted = f"its schedule is counted from a transit on {ref.day} {ref:%b %Y} ({_ago(ref, now)})"
    ratio, sigma = _finite(row.get("staleness")), _finite(row.get("sigma_min"))
    if ratio is None or sigma is None:
        verdict = "unknown"
        headline = f"its published period has no stated error, so its schedule cannot be checked ({counted})."
    elif ratio >= OUT_OF_DATE_FROM:
        verdict = "out_of_date"
        headline = (
            f"{counted}, and its predicted transit time is now uncertain by about +/-{fmt_minutes(sigma)}, "
            f"which is more than half the {row['duration_h']:.1f} h transit."
        )
    elif sigma <= limits.assumed_timing_err_min:
        verdict = "up_to_date"
        headline = (
            f"{counted}, and its predicted transit time is known to about +/-{fmt_minutes(sigma)}, "
            "about as well as one night's timing could."
        )
    else:
        verdict = "getting_stale"
        headline = (
            f"{counted}, and its predicted transit time is now uncertain by about +/-{fmt_minutes(sigma)} "
            f"against a {row['duration_h']:.1f} h transit: it can still be found, but a timing would tighten it."
        )
    provisional = provisional and verdict in ("out_of_date", "getting_stale")
    if provisional:
        headline += " ExoClock's list was not available, so this may overstate how out of date it is."
    return {
        "verdict": verdict, "headline": headline[0].upper() + headline[1:],
        "reference_utc": _iso(ref), "uncertainty_min": sigma, "provisional": provisional,
    }


def source_notice(info: dict[str, Any]) -> str | None:
    """A warning a person must see before trusting the figures, when a list that matters could not be read."""
    sources = info.get("sources", {})
    if networks.SRC_EXOCLOCK not in sources and networks.SRC_EXOPLANET_WATCH in sources:
        return (
            "ExoClock's list is not available, so every schedule here comes from NASA Exoplanet Watch and the NASA archive "
            "only. ExoClock re-fits schedules from new observations and is usually the fresher one, so planets may look "
            "more out of date, and one timing more valuable, than they really are. Treat 'Out of date' and 'High value' as "
            "provisional until it loads (Refresh from networks tries again)."
        )
    if networks.SRC_EXOPLANET_WATCH not in sources and networks.SRC_EXOCLOCK in sources:
        return (
            "NASA Exoplanet Watch's list is not available, so its ranking is missing and planets only on that list are "
            "absent. Refresh from networks tries again."
        )
    return None


def impact_estimate(row: dict[str, Any], epoch: int, limits: Limits) -> dict[str, Any]:
    """What one timing of this planet at ``epoch`` would be worth, as an estimate with its assumptions (see the module docstring)."""
    period, dp, dt0 = row["period_d"], row.get("period_err_d"), row.get("t0_err_d") or 0.0
    s_meas = limits.assumed_timing_err_min
    assumptions = [
        f"Assumes one night's data times the transit to +/-{s_meas:g} min. This telescope's real timing error is not measured yet, so "
        "this is a placeholder (ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN), not a result.",
        "Takes the published period and mid-time errors as fully correlated (the pessimistic case, as the ranking does).",
        "Assumes a single timing fixes the mid-time but leaves the period error as it was.",
        "Counts one timing only: a series of timings across a season is worth more than the sum of its points.",
        "Says nothing about discovery: one timing from a 50 mm telescope is unlikely to reveal a timing variation by itself.",
    ]
    sigma_now_d = networks.propagated_uncertainty_d(epoch, dp, dt0)
    figures: dict[str, float | None] = {
        "epoch": float(epoch),
        "assumed_timing_err_min": s_meas,
        "staleness": _finite(row.get("staleness")),
        "sigma_now_min": None, "sigma_after_min": None,
        "sigma_year_before_min": None, "sigma_year_after_min": None, "gain_pct": None,
    }
    if math.isinf(sigma_now_d):
        return {
            "level": "unknown",
            "headline": (
                "Its published period has no stated error, so its schedule cannot be trusted and any careful timing "
                "helps. How much cannot be estimated."
            ),
            "figures": figures, "assumptions": assumptions,
        }
    sigma_now = sigma_now_d * 1440.0
    sigma_after = 1.0 / math.sqrt(1.0 / sigma_now**2 + 1.0 / s_meas**2) if sigma_now > 0 else 0.0
    k = YEAR_D / period  # epochs in a year
    before = networks.propagated_uncertainty_d(epoch + k, dp, dt0) * 1440.0
    after = math.sqrt(sigma_after**2 + (k * abs(dp or 0.0) * 1440.0) ** 2)
    gain = 100.0 * (1.0 - after / before) if before > 0 else 0.0
    figures.update(
        sigma_now_min=sigma_now, sigma_after_min=sigma_after,
        sigma_year_before_min=before, sigma_year_after_min=after, gain_pct=gain,
    )
    # A percentage means nothing once the schedule is already finer than one night's data can improve: a year on,
    # +/-0.5 min "23% tighter" is not value, whatever the ratio.
    if before <= s_meas:
        level = "low"
    elif gain >= LEVEL_HIGH_PCT:
        level = "high"
    elif gain >= LEVEL_MEDIUM_PCT:
        level = "medium"
    else:
        level = "low"
    if level == "low":
        headline = (
            f"Little gain: its schedule is already known to about +/-{fmt_minutes(sigma_now)} at this transit "
            f"(+/-{fmt_minutes(before)} a year on), so one timing would barely change it."
        )
    else:
        headline = (
            f"One timing here would cut the uncertainty of its transit time a year later from about "
            f"+/-{fmt_minutes(before)} to +/-{fmt_minutes(after)} ({gain:.0f}% tighter)."
        )
    return {"level": level, "headline": headline, "figures": figures, "assumptions": assumptions}


# --------------------------------------------------------------------------- #
# Reachability and windows
# --------------------------------------------------------------------------- #
def static_blockers(row: dict[str, Any], limits: Limits, lat_deg: float) -> list[str]:
    """Reasons, from the planet alone, that this telescope cannot reach it from this site (empty: no objection yet)."""
    out: list[str] = []
    v = row.get("v_mag")
    if v is None:
        out.append("Its brightness is not listed, so it cannot be judged reachable.")
    elif v > limits.max_vmag:
        out.append(f"Too faint for a 50 mm telescope: V = {v:.1f}, and the limit is {limits.max_vmag:g}.")
    depth = row.get("depth_pct")
    if depth is None:
        out.append("Its transit depth is not listed.")
    elif depth < limits.min_depth_pct:
        out.append(f"The transit is too shallow to time well: {depth:.2f}% deep, and the limit is {limits.min_depth_pct:g}%.")
    peak = 90.0 - abs(lat_deg - row["dec_deg"])
    if peak < limits.min_alt_deg:
        out.append(
            f"It never climbs high enough from this site: its highest point is about {peak:.0f} deg, "
            f"and the limit is {limits.min_alt_deg:.0f} deg."
        )
    if row["period_d"] < MIN_PERIOD_D:
        out.append("Its listed period is implausibly short, so the schedule cannot be trusted.")
    return out


def _epochs(row: dict[str, Any], jd_lo: float, jd_hi: float) -> range:
    n_lo = math.ceil((jd_lo - row["t0_bjd_tdb"]) / row["period_d"])
    n_hi = math.floor((jd_hi - row["t0_bjd_tdb"]) / row["period_d"])
    return range(n_lo, min(n_hi, n_lo + MAX_EPOCHS_PER_PLANET) + 1)


def _jd(when: datetime) -> float:
    return when.timestamp() / 86400.0 + 2440587.5


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _base_target(row: dict[str, Any], net: dict[str, Any]) -> dict[str, Any]:
    """The target dict a mission would carry for this planet, without a window."""
    return {
        "name": row["planet"], "designation": row["planet"], "planet": row["planet"], "host": row.get("host") or "",
        "ra_deg": row["ra_deg"], "dec_deg": row["dec_deg"],
        "v_mag": row["v_mag"], "period_d": row["period_d"], "period_err_d": row.get("period_err_d"),
        "duration_h": row["duration_h"], "depth_pct": row["depth_pct"], "distance_pc": row.get("distance_pc"),
        "ephemeris_ref": row.get("ephemeris_source"), "network": net,
    }


def _window_events(
    rows: list[dict[str, Any]], now: datetime, days: int, limits: Limits, site: tuple[float, float, float]
) -> dict[int, dict[str, Any]]:
    """Every transit of every row in ``rows`` inside the horizon, with whether each is observable in full.

    One vectorised pass for all planets together: ``{row index: {"events": [...], "n": total, "sun": n, "alt": n}}``
    where an event is ``{"start", "mid", "end", "epoch", "ok"}``; ``ok`` means that at the start, middle and end of the
    window the Sun is low enough, the target high enough, and the target clear of the Sun and the Moon (the checks a
    mission makes, ``core/interlocks.py``).
    """
    lat, lon, elev = site
    jd_lo, jd_hi = _jd(now) - 1.0, _jd(now) + days + 1.0
    idx: list[int] = []
    epochs: list[int] = []
    bjds: list[float] = []
    for i, row in enumerate(rows):
        for n in _epochs(row, jd_lo, jd_hi):
            idx.append(i)
            epochs.append(n)
            bjds.append(row["t0_bjd_tdb"] + n * row["period_d"])
    out: dict[int, dict[str, Any]] = {i: {"events": [], "n": 0, "sun": 0, "alt": 0, "moon": 0} for i in range(len(rows))}
    if not idx:
        return out
    ra = np.array([rows[i]["ra_deg"] for i in idx])
    dec = np.array([rows[i]["dec_deg"] for i in idx])
    mids = bjd_tdb_to_utc_many(bjds, ra, dec, lat, lon, elev)
    half = [timedelta(hours=rows[i]["duration_h"] / 2.0) for i in idx]
    base = timedelta(minutes=limits.baseline_min)
    starts = [m - h - base for m, h in zip(mids, half)]
    ends = [m + h + base for m, h in zip(mids, half)]
    horizon_end = now + timedelta(days=days)
    keep = [j for j in range(len(idx)) if ends[j] >= now and starts[j] <= horizon_end]
    if not keep:
        return out

    ra_k, dec_k = ra[keep], dec[keep]
    times = [starts[j] for j in keep] + [mids[j] for j in keep] + [ends[j] for j in keep]
    grid_start = now - timedelta(days=1.5)
    grid_times = [grid_start + timedelta(minutes=SUN_GRID_MIN * g) for g in range(int((days + 3) * 1440 / SUN_GRID_MIN) + 1)]
    sky = sky_from_grid(sun_moon_grid(lat, lon, elev, grid_times), np.tile(ra_k, 3), np.tile(dec_k, 3), lat, lon, elev, times)
    sky = {k: v.reshape(3, len(keep)) for k, v in sky.items()}
    site_limits = limits.site_limits()
    moon_need = interlocks.required_moon_separation(sky["moon_alt"], sky["moon_illum"], site_limits)

    need_alt = interlocks.minimum_altitude(sky["target_az"], site_limits)  # the site's horizon, where it is higher than the minimum
    alt_ok = np.all((sky["target_alt"] >= need_alt) & (sky["target_alt"] <= limits.max_alt_deg), axis=0)
    sun_ok = (sky["sun_alt"].max(axis=0) <= limits.max_sun_alt_deg) & (sky["sun_sep"].min(axis=0) >= limits.min_sun_sep_deg)
    moon_ok = ~np.any((sky["moon_alt"] > 0.0) & (sky["moon_sep"] < moon_need), axis=0)
    for pos, j in enumerate(keep):
        slot = out[idx[j]]
        slot["n"] += 1
        slot["sun"] += int(not sun_ok[pos])
        slot["alt"] += int(not alt_ok[pos])
        slot["moon"] += int(not moon_ok[pos])
        slot["events"].append({
            "start": starts[j], "mid": mids[j], "end": ends[j], "epoch": epochs[j],
            "ok": bool(alt_ok[pos] and sun_ok[pos] and moon_ok[pos]),
        })
    return out


def _no_window_reason(slot: dict[str, Any], days: int, limits: Limits) -> str:
    n = slot["n"]
    if n == 0:
        return f"It has no transit in the next {days} days."
    return (
        f"None of its {n} transit{'' if n == 1 else 's'} in the next {days} days is both dark and high enough for the "
        f"whole window ({slot['sun']} in twilight or daylight, {slot['alt']} too low: below {limits.min_alt_deg:.0f} deg"
        f"{' or behind your horizon' if limits.horizon else ''}, "
        f"{slot.get('moon', 0)} too close to the Moon)."
    )


def caveats(limits: Limits) -> list[str]:
    return [
        "Weather is not checked: a clear window is only a chance.",
        f"The Moon is checked the way a mission checks it: while it is up, the target must be at least {limits.moon_floor_deg:.0f} deg "
        f"away, rising to {limits.moon_avoid_deg:.0f} deg at full Moon. Windows that fail are not offered.",
        (
            f"Your horizon is the {len(limits.horizon)} points saved with the site (in addition to a minimum altitude of {limits.min_alt_deg:.0f} deg): "
            "a straight line between them, checked at the start, middle and end of each window."
            if limits.horizon else
            f"No horizon is saved for this site, so obstructions (trees, buildings) are not checked: only a minimum altitude of {limits.min_alt_deg:.0f} deg."
        ),
        "Times are predictions from published schedules. Where the uncertainty is larger than the baseline taken either side "
        f"({limits.baseline_min:g} min), the transit may fall partly or wholly outside its window.",
        "Impact figures are estimates built on an assumed timing error and the assumptions listed with each one.",
        "The networks do not publish when a planet was last observed. The date shown is the transit its published schedule is "
        "counted from, which can be older or newer than the last observation.",
    ]


def impact_guide(limits: Limits) -> dict[str, Any]:
    """What one timing is, and is not, worth: the text behind the "What this estimate assumes" box.

    Plain-language by default; sections marked technical add how the figures are worked out.  Keep it honest rather
    than encouraging: the value is real only for an out-of-date schedule, and it is never a discovery claim.
    """
    s = limits.assumed_timing_err_min
    return {
        "title": "What is one timing worth?",
        "summary": (
            "One timing is worth the most when a planet's schedule is out of date, and almost nothing when the schedule is "
            "already well known. On its own it is rarely enough to show that a planet's timing is changing."
        ),
        "sections": [
            {"heading": "When one timing helps",
             "body": (
                 "A predicted transit time gets less certain the longer it has been since the planet was last measured well. "
                 "If the schedule is off by an hour or more, one good timing pulls it back to within minutes, so other "
                 "observers and larger telescopes do not waste a night waiting at the wrong time. That is what the "
                 "'High value' label means. Even a timing right on schedule is useful: it shows the planet has not moved far "
                 "from its predicted time."
             ), "technical": False},
            {"heading": "What 'out of date' means",
             "body": (
                 "'Out of date' means the predicted transit time is uncertain by half the transit's length or more, so the "
                 "transit could fall outside a normal observing window. 'Up to date' means it is known about as well as one "
                 f"night's timing could do ({s:g} minutes), so a new timing would barely change it. 'Getting stale' is in "
                 "between: the transit can still be found, but a timing would tighten the schedule. The date shown is the "
                 "transit the published schedule is counted from. The networks do not say when a planet was last observed, so "
                 "that date can be older or newer than the last observation."
             ), "technical": False},
            {"heading": "When it barely helps",
             "body": (
                 "If the schedule is already known to a minute or two, one timing from a small telescope cannot improve it. "
                 "That is the 'Little gain' label. We say so instead of counting it as value."
             ), "technical": False},
            {"heading": "What one timing cannot show",
             "body": (
                 "A planet's transit times can really change (another planet pulling on it, or its orbit slowly shrinking). "
                 "But one timing that comes out early or late cannot be told apart from a bad clock, noise or thin cloud. "
                 "Shifts of a few minutes need several timings on different dates, and slow effects need years of timings "
                 "from many observers. One timing is a contribution to that record, not a discovery."
             ), "technical": False},
            {"heading": "How many timings are enough?",
             "body": (
                 "As a rough guide, spotting a repeating shift about as large as your timing error takes dozens of timings. "
                 "A shift twice that size takes about a dozen, and one five times as large takes two or three. They also have "
                 "to be spread across the cycle, which can mean months or years."
             ), "technical": False},
            {"heading": "It only counts if it is accepted",
             "body": (
                 "The networks combine many observers' timings and screen out poor ones. A timing they reject adds nothing, "
                 "so the value here assumes yours would be accepted."
             ), "technical": False},
            {"heading": "How accurate is this telescope's timing?",
             "body": (
                 f"We assume {s:g} minutes for one night's data. That is a placeholder: a 50 mm telescope may do worse, "
                 "especially on fainter stars, until real nights have been measured. Every figure on this page is an estimate."
             ), "technical": False},
            {"heading": "How the figures are worked out",
             "body": (
                 "'Before' is the uncertainty of the predicted transit time one year later, propagated from the published "
                 "period and mid-time errors, treated as fully correlated (deliberately pessimistic). 'After' combines it with "
                 f"one new timing of {s:g} min by inverse variance, then adds the period error's growth over the year. "
                 "The level is 'little gain' when the schedule is already known better than one timing, otherwise 'high' for "
                 "a gain of 50% or more and 'medium' for 15% or more. Detection rule of thumb: for a repeating shift of "
                 "amplitude A and timing error sigma, N is about 50 (sigma/A)^2 timings for a 5-sigma detection."
             ), "technical": True},
        ],
    }


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def build_report(
    settings: Settings, *, now: datetime | None = None, days: int = HORIZON_DAYS, refresh: bool = False
) -> dict[str, Any]:
    """The opportunity report for transit timing (shape: :mod:`core.opportunities`).

    ``refresh=False`` reads the cached network lists and never touches the network.  ``refresh=True`` asks the
    networks again (unless the settings say services are offline), falling back to the cached copy, flagged
    stale, if a download fails.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    limits = Limits.from_env(settings)
    site = (settings.site_latitude_deg, settings.site_longitude_deg, settings.site_elevation_m)
    extra_problems: list[str] = []
    fetch_fresh = refresh
    if refresh and settings.offline_services:
        fetch_fresh = False
        extra_problems.append("Services are set to offline (simulation), so nothing was fetched; the cached lists were used.")

    report: dict[str, Any] = {
        "plugin": "transit_finder", "label": LABEL, "summary": SUMMARY, "generated_utc": _iso(now), "horizon_days": int(days),
        "site": {"name": settings.site_name, "lat": site[0], "lon": site[1], "elev_m": site[2], "source": settings.site_source},
        "limits": asdict(limits),
        "data": {"retrieved_utc": None, "stale_cache": False, "from_cache": True, "problems": extra_problems},
        "n_candidates": 0, "caveats": caveats(limits), "impact_guide": impact_guide(limits), "opportunities": [],
    }
    try:
        rows, info = networks.fetch_candidates(
            settings.archive_cache_dir, 0.0 if fetch_fresh else math.inf, _jd(now), cached_only=not fetch_fresh
        )
    except NetworkError as exc:
        report["data"]["problems"].append(str(exc))
        return check_report(report)
    report["data"].update(
        retrieved_utc=info["retrieved_utc"], stale_cache=info["stale_cache"], from_cache=info["from_cache"],
        problems=extra_problems + list(info["problems"]),
    )
    report["n_candidates"] = len(rows)
    notice = source_notice(info)
    exoclock_missing = networks.SRC_EXOCLOCK not in info.get("sources", {})
    if notice:
        report["data"]["notice"] = notice

    blockers = {i: static_blockers(r, limits, site[0]) for i, r in enumerate(rows)}
    open_idx = [i for i in range(len(rows)) if not blockers[i]]
    slots = _window_events([rows[i] for i in open_idx], now, days, limits, site)

    slot_of: dict[int, dict[str, Any]] = {}
    for pos, i in enumerate(open_idx):
        slot = slots[pos]
        slot["good"] = sorted((e for e in slot["events"] if e["ok"]), key=lambda e: e["start"])
        if not slot["good"]:
            blockers[i] = [_no_window_reason(slot, days, limits)]
        slot_of[i] = slot

    opportunities: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        net = networks.network_block_for(row, info)
        reachable = not blockers[i]
        windows: list[dict[str, Any]] = []
        impact = None
        if reachable:
            for e in slot_of[i]["good"]:
                err_d = networks.propagated_uncertainty_d(e["epoch"], row.get("period_err_d"), row.get("t0_err_d"))
                windows.append({
                    "start": _iso(e["start"]), "mid": _iso(e["mid"]), "end": _iso(e["end"]), "epoch": int(e["epoch"]),
                    "mid_err_min": None if math.isinf(err_d) else round(err_d * 1440.0, 2),
                })
            impact = impact_estimate(row, windows[0]["epoch"], limits)
            if exoclock_missing and notice:
                impact["assumptions"].append(
                    "ExoClock's list was not available, so the schedule comes from the other lists only; ExoClock's is "
                    "usually fresher, so this may overstate how out of date the schedule is and how much a timing would help."
                )
        opportunities.append({
            "id": normalize_name(row["planet"]), "name": row["planet"], "host": row.get("host") or None,
            "network": net, "reachable": reachable, "blockers": blockers[i], "windows": windows,
            "impact": impact, "schedule": schedule_status(row, now, limits, provisional=bool(notice) and exoclock_missing),
            "target": _base_target(row, net),
            "facts": {
                "v_mag": row.get("v_mag"), "depth_pct": row.get("depth_pct"), "duration_h": row["duration_h"],
                "period_d": row["period_d"], "ra_deg": row["ra_deg"], "dec_deg": row["dec_deg"],
                "staleness": _finite(row.get("staleness")), "sigma_min": _finite(row.get("sigma_min")),
                "exoclock_priority": row.get("exoclock_priority"), "ew_rank": row.get("ew_rank"),
                "ew_total": row.get("ew_total"), "ttv_flag": bool(row.get("ttv_flag")),
                "ephemeris_source": row.get("ephemeris_source"),
            },
        })
    opportunities.sort(key=lambda o: (
        not o["reachable"], -(o["network"]["score"] or 0.0), o["windows"][0]["start"] if o["windows"] else "",
    ))
    report["opportunities"] = opportunities
    return check_report(report)


def planned_target(opportunity: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    """The target a mission would carry for this window: the base target plus the window and the predicted mid-time."""
    mid_err = window.get("mid_err_min")
    return {
        **opportunity["target"],
        "window_start": window["start"], "window_end": window["end"],
        "predicted_t_mid_iso": window["mid"],
        "predicted_t_mid_err_min": mid_err if mid_err is not None else 60.0,
        "epoch_number": window.get("epoch"),
    }


def recheck_planned(settings: Settings, target: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Look again at a planned transit before a mission is built from it (``BaseExperimentPlugin.recheck_planned_target``).

    A plan stores the window and predicted mid-time from the lists as they were that day.  The lists are re-read
    (the cached copy only: never the network), the same epoch's mid-time is recomputed, and if it moved by a minute
    or more the window is moved with it.  Returns ``{"target": refreshed target or None, "notes": [sentences]}``.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    name, epoch = target.get("name"), target.get("epoch_number")
    if epoch is None or not target.get("predicted_t_mid_iso"):
        return {"target": None, "notes": ["This plan has no predicted mid-time to re-check."]}
    try:
        rows, _info = networks.fetch_candidates(settings.archive_cache_dir, math.inf, _jd(now), cached_only=True)
    except NetworkError:
        return {"target": None, "notes": ["The cached network lists are unavailable, so the schedule was not re-checked."]}
    row = next((r for r in rows if normalize_name(r["planet"]) == normalize_name(str(name))), None)
    if row is None:
        return {"target": None, "notes": [f"{name} is no longer on any cached network list; the stored schedule is used."]}

    lat, lon, elev = settings.site_latitude_deg, settings.site_longitude_deg, settings.site_elevation_m
    bjd = row["t0_bjd_tdb"] + int(epoch) * row["period_d"]
    mid = bjd_tdb_to_utc_many([bjd], np.array([row["ra_deg"]]), np.array([row["dec_deg"]]), lat, lon, elev)[0]
    old_mid = datetime.fromisoformat(str(target["predicted_t_mid_iso"]))
    shift_min = (mid - old_mid).total_seconds() / 60.0
    if abs(shift_min) < 1.0:
        return {"target": None, "notes": [f"The predicted transit time is unchanged (within {abs(shift_min):.1f} min) on the cached lists."]}

    limits = Limits.from_env(settings)
    half = timedelta(hours=row["duration_h"] / 2.0)
    base = timedelta(minutes=limits.baseline_min)
    err_d = networks.propagated_uncertainty_d(int(epoch), row.get("period_err_d"), row.get("t0_err_d"))
    refreshed = {
        **target,
        "window_start": _iso(mid - half - base), "window_end": _iso(mid + half + base),
        "predicted_t_mid_iso": _iso(mid),
        "predicted_t_mid_err_min": None if math.isinf(err_d) else round(err_d * 1440.0, 2),
    }
    return {"target": refreshed, "notes": [
        f"The predicted transit time moved {shift_min:+.1f} min since this plan was made, so the window moved with it "
        f"(now {_iso(mid - half - base)} to {_iso(mid + half + base)})."
    ]}

