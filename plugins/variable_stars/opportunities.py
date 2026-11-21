"""Variable-star opportunities: which stars the AAVSO says need data, which of them this telescope can measure, and when.

This answers the dashboard's "what could I take part in?" question without starting anything.  The target list is
the AAVSO Target Tool's (:mod:`plugins.variable_stars.networks`) and the report shape is :mod:`core.opportunities`.
A reachable window can be saved as a plan and started by a person (``python main.py --from-plan``).  The window is
a usable night.  For an eclipsing binary it is not an eclipse: the list has a period and no time of minimum, and
the plan says so.

Reachable
    A star is reachable when it gets bright enough for a 50 mm telescope (its *brightest* catalogue magnitude is
    within ``MAX_VMAG``), is not so bright that a 10 s frame saturates even at its faintest (``SATURATE_VMAG``),
    reaches a usable altitude from the site, and is asked for in a filter or mode a colour camera can serve (V, All
    and Visual through the green channel, B through the blue one).  A star that fails is still listed, with the
    reasons.  A star that is reachable only when bright (a cataclysmic variable in outburst, a Mira near maximum) is
    reachable, but says so.

Windows
    Unlike a transit, a monitoring target has no event to catch, so a window is a *night*: the longest stretch of that
    night in which the Sun is low enough, the star is high enough and clear of the Moon (the checks a mission makes,
    ``core/interlocks.py``), if it is at least ``MIN_WINDOW_MIN`` long.  An eclipsing binary is the exception that
    matters: the list gives a period but no time of minimum, so a night's window does not say whether an eclipse falls
    in it.  Say so wherever it is shown.

Impact (an estimate, with its assumptions attached)
    How overdue the AAVSO's own list says the star is (when it was last observed against the cadence asked for,
    and whether it is flagged high priority), and how precisely a 10 minute stack should measure it.  The precision
    is a **placeholder**, anchored to AAVSO observations of T CrB with an S50 and unmeasured on the S50 Pro.

Checked like a mission checks them (core/interlocks.py): the Sun, a minimum altitude (and the site's horizon), the Moon.
Not checked: weather, the star's brightness *now* (the list gives the catalogue's range only), crowding, comparison stars.

Environment overrides (all optional)
    ASTRONOMO_VARSTAR_MIN_ALT_DEG      minimum altitude across the window (default 30)
    ASTRONOMO_VARSTAR_MAX_ALT_DEG      maximum altitude across the window; the lower of this and ASTRONOMO_MAX_ALT_DEG (default 70) applies
    ASTRONOMO_VARSTAR_MAX_VMAG         faintest brightness a star must reach to count as reachable (default 13.0)
    ASTRONOMO_VARSTAR_SATURATE_VMAG    brighter than this even at its faintest and a 10 s frame saturates (default 7.0)
    ASTRONOMO_VARSTAR_MIN_WINDOW_MIN   the shortest stretch worth a window, minutes (default 60)
    ASTRONOMO_VARSTAR_SIGMA_V10        assumed noise of one 10 s frame at V = 10, magnitudes (default 0.032)
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
from core import networks as core_networks
from core.networks import NetworkError, normalize_name
from core.opportunities import check_report
from plugins.variable_stars import networks
from tools.astrometry import sun_moon_grid

LABEL = "Variable-star photometry"
SUMMARY = "Stars the AAVSO wants observed: which this telescope can measure, and on which nights."
HORIZON_DAYS = 30
GRID_MIN = 15  # the night is sampled on this grid
MAX_WINDOWS = 30
FRAME_CADENCE_S = 13.0  # the S50 writes a 10 s sub-frame about every 12 to 15 s (AAVSO's own S50 notes)
STACK_MIN = 10.0  # the stack the precision figure is for
NOISE_V_FLOOR = 8.0  # the noise anchor is only supported for V of about 8 to 12; do not extrapolate brighter
WORDS = {"item": "Star", "items": "stars", "chance": "night", "chances": "nights"}

#: What each requested mode needs, as this telescope would serve it.  ``None``: a colour camera cannot supply it.
SERVES = {"v": "green channel (TG)", "all": "green channel (TG)", "visual": "green channel (TG)", "vis": "green channel (TG)", "b": "blue channel (TB)"}
MODE_PLAIN = {
    "all": "any filter", "visual": "visual (a green-channel estimate stands in)", "vis": "visual (a green-channel estimate stands in)",
    "spectroscopy": "spectroscopy",
}
SECTION_PLAIN = {
    "Alert/Campaign": "an AAVSO alert or campaign", "CV": "cataclysmic variables", "EB": "eclipsing binaries",
    "SPP": "short-period pulsators", "LPV": "long-period variables", "YSO": "young stellar objects",
    "HET": "high-energy targets", "Misc": "miscellaneous", "EP": "exoplanet hosts",
}
STATUS_PLAIN = {
    "on_cadence": "observed within the cadence asked for", "due_soon": "nearly due", "overdue": "overdue",
    "never": "not observed on the AAVSO's record",
}


def _env(name: str, default: str) -> str:
    return os.environ.get(f"ASTRONOMO_VARSTAR_{name}", default)


@dataclass(frozen=True)
class Limits:
    """What "reachable" means (the site's limits are the ones a mission enforces on real hardware)."""

    min_alt_deg: float = 30.0
    max_alt_deg: float = 90.0  # the field turns fastest near the zenith; a mission refuses above its own maximum
    max_vmag: float = 13.0
    saturate_vmag: float = 7.0
    min_window_min: float = 60.0
    max_sun_alt_deg: float = -12.0
    sigma_v10: float = 0.032
    min_sun_sep_deg: float = 30.0
    moon_avoid_deg: float = 60.0
    moon_floor_deg: float = 15.0
    horizon: tuple = ()

    @classmethod
    def from_env(cls, settings: Settings) -> "Limits":
        return cls(
            min_alt_deg=max(float(_env("MIN_ALT_DEG", "30")), float(settings.min_altitude_deg)),
            max_alt_deg=min(float(_env("MAX_ALT_DEG", "90")), float(settings.max_altitude_deg)),
            max_vmag=float(_env("MAX_VMAG", "13.0")),
            saturate_vmag=float(_env("SATURATE_VMAG", "7.0")),
            min_window_min=float(_env("MIN_WINDOW_MIN", "60")),
            max_sun_alt_deg=float(settings.max_sun_altitude_deg),
            sigma_v10=float(_env("SIGMA_V10", "0.032")),
            min_sun_sep_deg=float(settings.min_sun_separation_deg),
            moon_avoid_deg=float(settings.moon_avoid_deg),
            moon_floor_deg=float(settings.moon_floor_deg),
            horizon=tuple(settings.site_horizon),
        )

    def site_limits(self) -> interlocks.SiteLimits:
        return interlocks.SiteLimits(
            self.min_alt_deg, self.min_sun_sep_deg, self.moon_avoid_deg, self.moon_floor_deg, tuple(self.horizon), self.max_alt_deg
        )


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _finite(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else float(value)


# --------------------------------------------------------------------------- #
# What a star asks for, and what this telescope can supply
# --------------------------------------------------------------------------- #
def served_modes(row: dict[str, Any]) -> list[str]:
    """The channels (``TG``, ``TB``) that would answer what the AAVSO asks for this star; empty when none would."""
    out: list[str] = []
    for mode in row["modes"]:
        how = SERVES.get(mode.strip().lower())
        if how and how not in out:
            out.append(how)
    return out


def served_requests(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in row.get("requests", []) if q["mode"].strip().lower() in SERVES]


def for_this_telescope(row: dict[str, Any]) -> dict[str, Any]:
    """``row`` with its cadence, last-observed time, status and priority taken from the requests this telescope can serve.

    A star is often asked for in several modes at once (V, B, spectroscopy ...) with different cadences and last-observed
    times; a colour camera can answer only some, and a spectroscopy request that was never answered must not make a star
    look unobserved in the filters that were.  With none servable the row is returned as it is (it is not reachable anyway).
    """
    reqs = served_requests(row)
    if not reqs:
        return row
    cadences = [q["cadence_d"] for q in reqs if q["cadence_d"]]
    lasts = [q["last_observed_utc"] for q in reqs if q["last_observed_utc"]]
    status = max((q["status"] for q in reqs), key=networks.order_of)
    return {
        **row, "cadence_d": min(cadences) if cadences else None, "last_observed_utc": max(lasts) if lasts else None,
        "status": "never" if not lasts else status, "high_priority": any(q["high_priority"] for q in reqs),
        "modes": [q["mode"] for q in reqs],
    }


def brightest(row: dict[str, Any]) -> float | None:
    return row["max_mag"] if row["max_mag"] is not None else row["min_mag"]


def faintest(row: dict[str, Any]) -> float | None:
    return row["min_mag"] if row["min_mag"] is not None else row["max_mag"]


def only_when_bright(row: dict[str, Any], limits: Limits) -> bool:
    """Reachable at its brightest but beyond reach at its faintest: a catalogue range that spans the limit."""
    f, b = faintest(row), brightest(row)
    return f is not None and b is not None and b <= limits.max_vmag < f


def frame_sigma(v_mag: float, sigma_v10: float) -> float:
    """Noise of one 10 s frame (magnitudes) at ``v_mag``: ``sigma_v10`` at V = 10, times 10^0.2 (1.58) per magnitude fainter (photon noise)."""
    return sigma_v10 * 10.0 ** (0.2 * (max(v_mag, NOISE_V_FLOOR) - 10.0))


def stack_sigma(v_mag: float, limits: Limits, minutes: float = STACK_MIN) -> float:
    """Expected scatter of a ``minutes`` stack of 10 s frames, white noise only (the placeholder noise model)."""
    n = max(1.0, minutes * 60.0 / FRAME_CADENCE_S)
    return frame_sigma(v_mag, limits.sigma_v10) / math.sqrt(n)


def static_blockers(row: dict[str, Any], limits: Limits, lat_deg: float) -> list[str]:
    """Reasons, from the star alone, that this telescope cannot measure it from this site (empty: no objection yet)."""
    out: list[str] = []
    if not served_modes(row):
        asked = ", ".join(MODE_PLAIN.get(m.lower(), m) for m in row["modes"]) or "nothing a camera can supply"
        out.append(f"The AAVSO asks for {asked} only, which a colour camera cannot supply.")
    b, f = brightest(row), faintest(row)
    if b is None:
        out.append("Its brightness range is not listed, so it cannot be judged reachable.")
    else:
        if b > limits.max_vmag:
            out.append(f"Too faint for a 50 mm telescope: it is never brighter than V = {b:.1f}, and the limit is {limits.max_vmag:g}.")
        if f is not None and f < limits.saturate_vmag:
            out.append(
                f"Too bright for a fixed 10 s frame: even at its faintest it is V = {f:.1f}, which saturates "
                f"(brighter than {limits.saturate_vmag:g}). Defocusing is the usual fix; it is not supported yet."
            )
    peak = 90.0 - abs(lat_deg - row["dec_deg"])
    if peak < limits.min_alt_deg:
        out.append(f"It never climbs high enough from this site: its highest point is about {peak:.0f} deg, and the limit is {limits.min_alt_deg:.0f} deg.")
    return out


# --------------------------------------------------------------------------- #
# Windows: the longest usable stretch of each night
# --------------------------------------------------------------------------- #
def _gmst_deg(unix_s: np.ndarray) -> np.ndarray:
    """Greenwich mean sidereal time in degrees (good to about 0.1 deg: planning, not pointing)."""
    jd = unix_s / 86400.0 + 2440587.5
    return (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0


def altaz_deg(ra_deg: np.ndarray, dec_deg: np.ndarray, lat_deg: float, lon_deg: float, unix_s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Altitude and azimuth (from north through east), shape ``(stars, times)``, by plain spherical astronomy."""
    lst = np.radians((_gmst_deg(unix_s) + lon_deg) % 360.0)[None, :]
    ra, dec = np.radians(ra_deg)[:, None], np.radians(dec_deg)[:, None]
    lat = math.radians(lat_deg)
    h = lst - ra
    sin_alt = math.sin(lat) * np.sin(dec) + math.cos(lat) * np.cos(dec) * np.cos(h)
    alt = np.degrees(np.arcsin(np.clip(sin_alt, -1.0, 1.0)))
    az = np.degrees(np.arctan2(-np.cos(dec) * np.sin(h), np.sin(dec) * math.cos(lat) - np.cos(dec) * math.sin(lat) * np.cos(h))) % 360.0
    return alt, az


def night_windows(
    rows: list[dict[str, Any]], now: datetime, days: int, limits: Limits, site: tuple[float, float, float]
) -> list[dict[str, Any]]:
    """For each row, ``{"windows": [...], "nights": n, "sun": n, "alt": n, "moon": n}``: the longest usable stretch of each night.

    A window is ``{"start", "mid", "end"}`` (datetimes).  ``nights`` counts the nights in the horizon that
    reached darkness at all; ``alt`` and ``moon`` count nights lost to the star being too low or too near the Moon.
    """
    lat, lon, elev = site
    out: list[dict[str, Any]] = [{"windows": [], "nights": 0, "alt": 0, "moon": 0} for _ in rows]
    if not rows:
        return out
    step = timedelta(minutes=GRID_MIN)
    n_grid = int(days * 1440 / GRID_MIN) + 1
    grid = [now + step * k for k in range(n_grid)]
    sky = sun_moon_grid(lat, lon, elev, grid)
    dark = np.flatnonzero(sky["sun_alt"] <= limits.max_sun_alt_deg)
    if dark.size == 0:
        return out
    t = sky["t"][dark]
    local_h = t / 3600.0 + lon / 15.0  # local mean solar time, hours since the epoch
    night = np.floor((local_h - 12.0) / 24.0).astype(int)  # an evening and the following morning share a night

    ra = np.array([r["ra_deg"] for r in rows])
    dec = np.array([r["dec_deg"] for r in rows])
    alt, az = altaz_deg(ra, dec, lat, lon, t)
    site_limits = limits.site_limits()
    need = np.asarray(interlocks.minimum_altitude(az.ravel(), site_limits)).reshape(az.shape)
    alt_ok = (alt >= need) & (alt <= limits.max_alt_deg)

    star = np.stack([np.cos(np.radians(dec)) * np.cos(np.radians(ra)), np.cos(np.radians(dec)) * np.sin(np.radians(ra)), np.sin(np.radians(dec))], axis=1)
    moon_vec = sky["moon_xyz"][dark]
    moon_sep = np.degrees(np.arccos(np.clip(np.einsum("ik,jk->ij", star, moon_vec), -1.0, 1.0)))  # einsum: macOS BLAS matmul is unreliable here
    moon_need = interlocks.required_moon_separation(sky["moon_alt"][dark], sky["moon_illum"][dark], site_limits)[None, :]
    moon_ok = ~((sky["moon_alt"][dark][None, :] > 0.0) & (moon_sep < moon_need))
    ok = alt_ok & moon_ok

    min_pts = max(1, int(math.ceil(limits.min_window_min / GRID_MIN)))
    for nid in np.unique(night):
        cols = np.flatnonzero(night == nid)
        if cols.size < min_pts:
            continue
        for i in range(len(rows)):
            out[i]["nights"] += 1
        block = ok[:, cols]
        run = np.zeros(len(rows), dtype=int)
        best = np.zeros(len(rows), dtype=int)
        best_end = np.zeros(len(rows), dtype=int)
        for c in range(cols.size):
            run = np.where(block[:, c], run + 1, 0)
            better = run > best
            best = np.where(better, run, best)
            best_end = np.where(better, c, best_end)
        for i in range(len(rows)):
            if best[i] >= min_pts:
                first = cols[best_end[i] - best[i] + 1]
                last = cols[best_end[i]]
                start = datetime.fromtimestamp(float(t[first]), tz=timezone.utc)
                end = datetime.fromtimestamp(float(t[last]), tz=timezone.utc) + step
                out[i]["windows"].append({"start": start, "mid": start + (end - start) / 2, "end": end})
            else:  # no stretch long enough: was it the altitude that cost the night, or the Moon?
                out[i]["alt" if alt_ok[i, cols].sum() < min_pts else "moon"] += 1
    for slot in out:
        slot["windows"].sort(key=lambda w: w["start"])
        del slot["windows"][MAX_WINDOWS:]
    return out


def _no_window_reason(slot: dict[str, Any], days: int, limits: Limits) -> str:
    if slot["nights"] == 0:
        return f"There is no dark night in the next {days} days."
    return (
        f"None of the {slot['nights']} nights in the next {days} days has a stretch of at least {limits.min_window_min:g} minutes "
        f"when it is dark, between {limits.min_alt_deg:.0f} and {limits.max_alt_deg:.0f} deg{' and clear of your horizon' if limits.horizon else ''} and clear of the Moon "
        f"({slot['alt']} nights too low, {slot['moon']} too close to the Moon)."
    )


# --------------------------------------------------------------------------- #
# Words, impact and the report
# --------------------------------------------------------------------------- #
def _ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "never (on the AAVSO's record)"
    days = (now - datetime.fromisoformat(iso)).total_seconds() / 86400.0
    if days < 1.0:
        return "less than a day ago"
    if days < 45:
        return f"{days:.0f} day{'' if round(days) == 1 else 's'} ago"
    return f"about {round(days / 30.44)} months ago"


def days_since(row: dict[str, Any], now: datetime) -> float | None:
    iso = row.get("last_observed_utc")
    return None if not iso else max(0.0, (now - datetime.fromisoformat(iso)).total_seconds() / 86400.0)


def _range_text(row: dict[str, Any]) -> str:
    b, f = row["max_mag"], row["min_mag"]
    bb, fb = row["max_band"] or "", row["min_band"] or ""
    if b is not None and f is not None:
        return f"{b:g}{bb and ' ' + bb} to {f:g}{fb and ' ' + fb}"
    if b is not None or f is not None:
        v = b if b is not None else f
        return f"about {v:g}"
    return "range not listed"


def saturates_when_bright(row: dict[str, Any], limits: Limits) -> bool:
    """Brighter than the saturation limit at its brightest (so a 10 s frame saturates near maximum) but not at its faintest."""
    b, f = brightest(row), faintest(row)
    return b is not None and b < limits.saturate_vmag and (f is None or f >= limits.saturate_vmag)


def rank_terms(row: dict[str, Any], now: datetime, limits: Limits) -> dict[str, float]:
    """What the ranking is made of.

    ``high_priority``: the AAVSO's flag.  ``overdue``: how late the star is against the cadence asked for, on a log
    scale so a star three cadences late is not worth three times one a cadence late (a star never observed counts as
    6, the same as one 63 cadences late).  ``measurable``: this telescope can measure it at *any* point of its
    range, rather than only when it brightens (a cataclysmic variable in quiescence cannot be measured).
    """
    ds, cad = days_since(row, now), row.get("cadence_d")
    if row["status"] == "never" or ds is None:
        overdue = 6.0
    elif cad:
        overdue = min(6.0, math.log2(1.0 + ds / cad))
    else:
        overdue = 0.0
    measurable = 0.0 if only_when_bright(row, limits) else 1.5
    return {"high_priority": 2.0 if row["high_priority"] else 0.0, "overdue": overdue, "measurable": measurable}


def impact_estimate(row: dict[str, Any], now: datetime, limits: Limits) -> dict[str, Any]:
    ds, cad = days_since(row, now), row.get("cadence_d")
    b, f = brightest(row), faintest(row)
    v_bright = max(b if b is not None else limits.max_vmag, limits.saturate_vmag)
    v_faint = min(f if f is not None else limits.max_vmag, limits.max_vmag)
    figures: dict[str, float | None] = {
        "days_since_last": None if ds is None else round(ds, 1), "cadence_d": cad,
        "overdue_ratio": None if (ds is None or not cad) else round(ds / cad, 2),
        "err_10min_bright_mag": round(stack_sigma(v_bright, limits), 4), "err_10min_faint_mag": round(stack_sigma(v_faint, limits), 4),
        "gain_pct": None,
    }
    assumptions = [
        f"Assumes one 10 s frame is good to {limits.sigma_v10:g} mag at V = 10, rising as a star fades (photon noise), and a frame about every "
        f"{FRAME_CADENCE_S:g} s. That comes from AAVSO observations of T CrB with an S50; it is a placeholder (ASTRONOMO_VARSTAR_SIGMA_V10) "
        "until this telescope's own scatter is measured, and the S50 Pro is not measured at all.",
        "Counts white noise only. Real scatter is higher: no flat frames, a colour sensor measured through one channel, and differential "
        "photometry against comparison stars the AAVSO would have to accept.",
        "The AAVSO's 'last observed' time and cadence are taken as given. Whether it is overdue says what the AAVSO would like, not what it "
        "will do with a measurement from a 50 mm telescope.",
        "The star's brightness now is not on the list. Only its catalogue range is, so it may be at either end of it.",
        "One night's measurements are a point on a light curve: a contribution to the record, never a discovery.",
    ]
    if "EB" in row["sections"]:
        assumptions.append(
            "For an eclipsing binary the list gives a period but not when an eclipse falls, so a night's window may miss it. "
            "A time of minimum needs the eclipse ephemeris from elsewhere (the AAVSO's VSX holds it)."
        )
    due = row["status"] in ("overdue", "never")
    if only_when_bright(row, limits):
        level = "low"
        headline = (
            f"Usually too faint to measure: it is V {faintest(row):g} at its faintest and within reach (V {limits.max_vmag:g} or brighter) "
            f"only when it brightens. The list does not say whether it has. A night now would most likely give an upper limit."
        )
    elif due:
        level = "high" if row["high_priority"] else "medium"
        when = "has no recorded observation" if ds is None else f"was last observed {_ago(row['last_observed_utc'], now)}"
        asked = f" against a request for one every {cad:g} days" if cad else ""
        headline = (
            f"It {when}{asked}, so a measurement fills a gap. A 10 minute stack should reach about "
            f"+/-{figures['err_10min_bright_mag']:.3f} mag at V {v_bright:.0f} and +/-{figures['err_10min_faint_mag']:.3f} mag at V {v_faint:.0f} "
            "(white noise only; an estimate)."
        )
    else:
        level = "low"
        headline = (
            f"Little gain tonight: it was last observed {_ago(row['last_observed_utc'], now)}, "
            f"{STATUS_PLAIN[row['status']]}" + (f" (asked for every {cad:g} days)." if cad else ".")
        )
    return {"level": level, "headline": headline, "figures": figures, "assumptions": assumptions}


def network_block_for(row: dict[str, Any], info: dict[str, Any], now: datetime, limits: Limits) -> dict[str, Any]:
    terms = rank_terms(row, now, limits)
    ds, cad = days_since(row, now), row.get("cadence_d")
    sections = ", ".join(SECTION_PLAIN.get(s, s) for s in row["sections"] if s != "None") or "the AAVSO's target list"
    asked = ", ".join(MODE_PLAIN.get(m.lower(), m) for m in row["modes"] if m.lower() in SERVES)
    reason = (
        f"On the AAVSO Target Tool as {sections}" + (f", asked for in {asked}" if asked else "")
        + (f", one every {cad:g} days" if cad else "")
        + (", flagged high priority" if row["high_priority"] else "")
        + f"; last observed {_ago(row['last_observed_utc'], now)}."
    )
    return core_networks.network_block(
        [networks.SOURCE], core_networks.TASK_MONITORING, reason, score=sum(terms.values()), terms=terms,
        retrieved_utc=info.get("retrieved_utc"), stale_cache=bool(info.get("stale_cache")),
    )


def detail_text(row: dict[str, Any], limits: Limits) -> str:
    kind = row["var_type"] or "variable"
    parts = [f"{kind}, brightness {_range_text(row)}"]
    if row.get("period_d"):
        parts.append(f"period {row['period_d']:g} d")
    ch = served_modes(row)
    if ch:
        parts.append("measured through the " + " and the ".join(ch))
    if "EB" in row["sections"]:
        parts.append("a listed night is a usable night, not an eclipse (the list has no time of minimum)")
    if any(m.lower() in ("visual", "vis") for m in row["modes"]):
        parts.append("a visual request is answered with a green-channel estimate")
    text = "; ".join(parts) + "."
    if only_when_bright(row, limits):
        text += f" Only within reach when brighter than V {limits.max_vmag:g}."
    if saturates_when_bright(row, limits):
        text += f" Near its brightest (brighter than V {limits.saturate_vmag:g}) a 10 s frame saturates."
    if row["notes"]:
        text += f" AAVSO note: {row['notes'][:200]}"
    return text


def caveats(limits: Limits) -> list[str]:
    return [
        "Weather is not checked: a clear window is only a chance.",
        f"The Moon is checked the way a mission checks it: while it is up, the star must be at least {limits.moon_floor_deg:.0f} deg away, "
        f"rising to {limits.moon_avoid_deg:.0f} deg at full Moon.",
        (
            f"Your horizon is the {len(limits.horizon)} points saved with the site (in addition to a minimum altitude of {limits.min_alt_deg:.0f} deg)."
            if limits.horizon else
            f"No horizon is saved for this site, so obstructions are not checked: only a minimum altitude of {limits.min_alt_deg:.0f} deg."
        ),
        "A window is a night, not an event. The list gives a star's brightness range, not its brightness now, and for an eclipsing "
        "binary a period but not the time of an eclipse.",
        "A window can be saved as a plan and started by a person. It is a usable night for one brightness measurement. "
        "For an eclipsing binary it is not an eclipse: the list has a period and no time of minimum, and the plan says so.",
        "Altitudes are worked out by plain spherical astronomy (good to a fraction of a degree): enough to say a night is usable, "
        "not to point a telescope.",
        "Impact figures are estimates built on an assumed noise and the assumptions listed with each one.",
    ]


def impact_guide(limits: Limits) -> dict[str, Any]:
    return {
        "title": "What is a night of measurements worth?",
        "summary": (
            "A variable star's value is a light curve built up over years. One night fills a gap in it when the AAVSO says the "
            "star is overdue, and adds almost nothing when the star was observed recently."
        ),
        "sections": [
            {"heading": "What 'overdue' means", "body": (
                "The AAVSO's Target Tool records when each star was last observed and how often it should be. A star past that "
                "cadence is overdue. That is the AAVSO's wish, not a promise that it will use a measurement from this telescope."
            ), "technical": False},
            {"heading": "High priority", "body": (
                "The tool flags stars in AAVSO alerts and campaigns (a nova, an outburst, a star under study by a satellite). "
                "Those are where timely data is most wanted. The flag is the AAVSO's."
            ), "technical": False},
            {"heading": "How precise a measurement is", "body": (
                f"We assume one 10 s frame is good to {limits.sigma_v10:g} mag at V = 10, and worse for fainter stars, and the figure "
                "shown is for a 10 minute stack. It is a placeholder taken from other people's S50 data, not a measurement of this "
                "telescope, and real scatter will be larger."
            ), "technical": False},
            {"heading": "What is not on the list", "body": (
                "The tool does not say how bright a star is now, nor, for an eclipsing binary, when its eclipse falls. A window is "
                "therefore a usable night, not a promise that anything happens in it."
            ), "technical": False},
            {"heading": "How the figures are worked out", "body": (
                f"One frame: {limits.sigma_v10:g} x 10^(0.2 (V - 10)) mag. A stack of N frames: that divided by sqrt(N), with a frame about "
                f"every {FRAME_CADENCE_S:g} s. The level is 'high' for a high-priority star that is overdue or never observed, 'medium' for any "
                "other overdue star, and 'low' for one observed within its cadence or one that is usually too faint to measure."
            ), "technical": True},
        ],
    }


def limits_text(limits: Limits, site: dict[str, Any]) -> str:
    return (
        f"Reachable means: it gets brighter than V {limits.max_vmag:g}, is not brighter than V {limits.saturate_vmag:g} even at its faintest, "
        f"is asked for in a filter a colour camera can serve, is above {limits.min_alt_deg:.0f} deg for at least {limits.min_window_min:g} minutes "
        f"of a night with the Sun below {abs(limits.max_sun_alt_deg):.0f} deg, and is clear of the Moon "
        f"(at least {limits.moon_floor_deg:.0f} deg while it is up, up to {limits.moon_avoid_deg:.0f} deg at full Moon). "
        f"Site: {site['name']} ({site['lat']:.2f}, {site['lon']:.2f})."
    )


def build_report(
    settings: Settings, *, now: datetime | None = None, days: int = HORIZON_DAYS, refresh: bool = False
) -> dict[str, Any]:
    """The opportunity report for variable-star photometry (shape: :mod:`core.opportunities`).

    ``refresh=False`` reads the copy of the AAVSO list on this computer and never touches the network;
    ``refresh=True`` asks again (unless the settings say services are offline), falling back to the old copy, flagged stale.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    limits = Limits.from_env(settings)
    site = (settings.site_latitude_deg, settings.site_longitude_deg, settings.site_elevation_m)
    site_info = {"name": settings.site_name, "lat": site[0], "lon": site[1], "elev_m": site[2], "source": settings.site_source}
    extra_problems: list[str] = []
    fetch_fresh = refresh
    if refresh and settings.offline_services:
        fetch_fresh = False
        extra_problems.append("Services are set to offline (simulation), so nothing was fetched; the copy on this computer was used.")
    report: dict[str, Any] = {
        "plugin": "variable_stars", "label": LABEL, "summary": SUMMARY, "generated_utc": _iso(now), "horizon_days": int(days),
        "site": site_info, "limits": asdict(limits), "limits_text": limits_text(limits, site_info),
        "data": {"retrieved_utc": None, "stale_cache": False, "from_cache": True, "problems": extra_problems},
        "n_candidates": 0, "caveats": caveats(limits), "impact_guide": impact_guide(limits), "words": WORDS,
        "planning": True, "opportunities": [],
    }
    try:
        rows, info = networks.fetch_targets(
            settings.archive_cache_dir, 0.0 if fetch_fresh else math.inf, cached_only=not fetch_fresh
        )
    except NetworkError as exc:
        if not fetch_fresh and "has not been downloaded" in str(exc):
            return check_report(report)  # nothing on this computer yet: an empty report, and the page offers Refresh
        report["data"]["problems"].append(str(exc))
        return check_report(report)
    rows = [for_this_telescope(r) for r in rows if set(r["sections"]) != {"EP"}]  # exoplanet hosts are the transit plugin's
    report["data"].update(
        retrieved_utc=info["retrieved_utc"], stale_cache=info["stale_cache"], from_cache=info["from_cache"],
        problems=extra_problems + list(info["problems"]),
    )
    report["n_candidates"] = len(rows)

    blockers = {i: static_blockers(r, limits, site[0]) for i, r in enumerate(rows)}
    open_idx = [i for i in range(len(rows)) if not blockers[i]]
    slots = night_windows([rows[i] for i in open_idx], now, days, limits, site)
    slot_of: dict[int, dict[str, Any]] = {}
    for pos, i in enumerate(open_idx):
        slot_of[i] = slots[pos]
        if not slots[pos]["windows"]:
            blockers[i] = [_no_window_reason(slots[pos], days, limits)]

    opportunities: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        net = network_block_for(row, info, now, limits)
        reachable = not blockers[i]
        windows = [
            {"start": _iso(w["start"]), "mid": _iso(w["mid"]), "end": _iso(w["end"]), "epoch": None, "mid_err_min": 0.0}
            for w in slot_of[i]["windows"]
        ] if reachable else []
        b, f = brightest(row), faintest(row)
        opportunities.append({
            "id": normalize_name(row["name"]), "name": row["name"], "host": None, "network": net, "reachable": reachable,
            "blockers": blockers[i], "windows": windows, "impact": impact_estimate(row, now, limits) if reachable else None,
            "detail": detail_text(row, limits),
            "target": {
                "name": row["name"], "designation": row["name"], "ra_deg": row["ra_deg"], "dec_deg": row["dec_deg"],
                "v_mag": b, "v_max_mag": row["max_mag"], "v_min_mag": row["min_mag"],
                "var_type": row["var_type"], "period_d": row["period_d"], "cadence_d": row["cadence_d"],
                "sections": list(row["sections"]), "modes": row["modes"], "network": net,
            },
            "facts": {
                "v_mag": b, "v_max_mag": row["max_mag"], "v_min_mag": row["min_mag"], "faintest_mag": f,
                "var_type": row["var_type"], "period_d": row["period_d"], "cadence_d": row["cadence_d"],
                "sections": row["sections"], "modes": row["modes"], "channels": served_modes(row),
                "status": row["status"], "last_observed_utc": row["last_observed_utc"], "high_priority": row["high_priority"],
                "only_when_bright": only_when_bright(row, limits), "saturates_when_bright": saturates_when_bright(row, limits), "ra_deg": row["ra_deg"], "dec_deg": row["dec_deg"],
                "constellation": row["constellation"], "vsx_oid": row["vsx_oid"], "notes": row["notes"],
                "notices": row["notices"],
            },
        })
    opportunities.sort(key=lambda o: (
        not o["reachable"], -(o["network"]["score"] or 0.0), o["windows"][0]["start"] if o["windows"] else "", o["name"],
    ))
    report["opportunities"] = opportunities
    return check_report(report)


def planned_target(opportunity: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    """The target a mission would carry for this night.

    The window is a usable stretch of darkness, not an eclipse.  Eclipsing binaries say so on the target, because
    the AAVSO list has a period and no time of minimum.
    """
    sections = list((opportunity.get("facts") or {}).get("sections") or [])
    if "EB" in sections:
        note = (
            "This window is a usable night, not an eclipse. The AAVSO list gives a period but not the time of "
            "minimum, so this plan is one brightness measurement, not a time of minimum."
        )
    else:
        note = "This window is a usable night for one brightness measurement, not a timed event."
    score = (opportunity.get("network") or {}).get("score")
    return {
        **opportunity["target"],
        "window_start": window["start"], "window_end": window["end"],
        "window_kind": "night", "window_note": note,
        "priority": float(score) if score is not None else 1.0,
    }


def recheck_planned(settings: Settings, target: dict[str, Any]) -> dict[str, Any]:
    """Look again at a planned star before a mission is built from it.  Reads the cached list only.

    A night window does not move when a schedule is refreshed (there is no eclipse time to move).  The notes say
    whether the star is still on the list, and repeat that the window is a night rather than an eclipse.
    """
    notes = [str(target["window_note"])] if target.get("window_note") else []
    try:
        rows, _info = networks.fetch_targets(settings.archive_cache_dir, math.inf, cached_only=True)
    except NetworkError:
        notes.append("The cached AAVSO list is unavailable, so the star was not re-checked.")
        return {"target": None, "notes": notes}
    row = next((r for r in rows if normalize_name(r["name"]) == normalize_name(str(target.get("name") or ""))), None)
    if row is None:
        notes.append(f"{target.get('name')} is no longer on the cached AAVSO list; the stored target is used.")
    else:
        notes.append(f"{row['name']} is still on the cached AAVSO list ({str(row['status']).replace('_', ' ')}).")
    return {"target": None, "notes": notes}
