"""Pro-Am target lists for exoplanet transit timing: ExoClock, NASA Exoplanet Watch, NASA's TTV flag.

These networks exist because published transit ephemerides go stale: a period known to a few parts
in 10^7 still drifts by tens of minutes over a decade, and a handful of planets (transit-timing
variations, orbital decay) wander by more.  Each list says which planets most need a fresh timing:

``ExoClock``              the ARIEL Ephemerides Working Group's maintained ephemerides, with a
                          priority (ALERT: observed O-C beyond 10 min in the last two years; HIGH:
                          the prediction is already too uncertain or too few recent epochs; MEDIUM;
                          LOW) and the latest observed-minus-calculated offset.
``NASA Exoplanet Watch``  a ranked list (Swarthmore Transit Finder CSV), ranked by the figure of
                          merit of Zellem et al. 2020, built from the NASA Exoplanet Archive.
``NASA Exoplanet Archive`` the ``ttv_flag`` column marks planets reported to show TTVs.

This module only fetches, parses and ranks.  Whether a planet can be observed from this site tonight
is the plugin's call (altitude, darkness, how bright a star a 50 mm telescope can measure), and the
upload file's destination is :mod:`tools.upload_formats`.  Parsers take text so they can be tested
against recorded payloads; the real HTTP lives in :func:`core.networks.fetch_text`.

Ephemeris staleness
    The uncertainty of a predicted mid-transit time, ``n`` epochs from the reference time, is::

        dT = sqrt((n dP)^2 + 2 n dP dT0 + dT0^2)

    (period and mid-time errors assumed fully correlated, the pessimistic case).  A planet counts
    as *stale* when ``dT`` reaches half the transit duration: at that point a predicted transit can
    no longer be reliably framed by a short observing window.  ``staleness = dT / (0.5 * duration)``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from core import networks as core_networks
from core.networks import (
    TASK_EPHEMERIS,
    TASK_TTV,
    NetworkError,
    fetch_text,
    normalize_name,
)
from plugins.transit_finder import archive

log = logging.getLogger(__name__)

EXOCLOCK_URL = "https://www.exoclock.space/database/planets_json"
EXOPLANET_WATCH_URL = "https://astro.swarthmore.edu/transits/exoplanet_watch_targets.csv"

SRC_EXOCLOCK = "exoclock"
SRC_EXOPLANET_WATCH = "exoplanet_watch"
SRC_NASA_TTV = "nasa_exoplanet_archive"

#: ExoClock's own priority ladder as a number (none = 0).
PRIORITY_VALUE = {"low": 1.0, "medium": 2.0, "high": 3.0, "alert": 4.0}
#: How the ranking score is assembled; reported per target in ``network.terms`` so it can be audited.
WEIGHTS = {"staleness": 1.0, "exoclock_priority": 0.75, "ew_rank": 1.0, "ttv": 1.5}
STALENESS_CAP = 4.0  # a planet whose timing is hopeless is not 100x more worth a night than one at 4x


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _sexagesimal(text: str, *, hours: bool) -> float | None:
    """``"06:30:32.79"`` (hours) or ``"+29:40:20.2"`` (degrees) -> decimal degrees."""
    m = re.fullmatch(r"\s*([+-]?)(\d+):(\d+):(\d+(?:\.\d*)?)\s*", str(text or ""))
    if not m:
        return None
    sign = -1.0 if m.group(1) == "-" else 1.0
    value = float(m.group(2)) + float(m.group(3)) / 60.0 + float(m.group(4)) / 3600.0
    return sign * value * (15.0 if hours else 1.0)


def _num(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def pretty_planet_name(name: str) -> str:
    """``"WASP-12b"`` -> ``"WASP-12 b"`` (the NASA style); names that already have a space are left alone."""
    name = str(name).strip()
    m = re.fullmatch(r"(.*\d)([b-j])", name)
    return f"{m.group(1)} {m.group(2)}" if m else name


def mmag_to_depth_pct(mmag: float) -> float:
    """A transit depth in milli-magnitudes as a percentage of the stellar flux."""
    return (1.0 - 10.0 ** (-0.4 * mmag / 1000.0)) * 100.0


def parse_exoclock(text: str) -> list[dict[str, Any]]:
    """Planets from the ExoClock ``planets_json`` payload, in the shape the plugin schedules from.

    Rows without a usable period, mid-time, duration or position are skipped: they cannot be
    scheduled.  Raises :class:`NetworkError` if the payload is not the expected JSON object.
    """
    try:
        # The mirror we fetch through may escape underscores; plain JSON has none of these.
        data = json.loads(text.replace("\\_", "_"))
    except ValueError as exc:
        raise NetworkError(f"ExoClock reply is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise NetworkError("ExoClock reply is not the expected object of planets")
    out: list[dict[str, Any]] = []
    for key, p in data.items():
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or key)
        period = _num(p.get("period_days", p.get("ephem_period")))
        t0 = _num(p.get("t0_bjd_tdb", p.get("ephem_mid_time")))
        dur = _num(p.get("duration_hours"))
        ra = _sexagesimal(p.get("ra_j2000"), hours=True)
        dec = _sexagesimal(p.get("dec_j2000"), hours=False)
        if None in (period, t0, dur, ra, dec) or period <= 0 or dur <= 0:  # type: ignore[operator]
            continue
        mmag = _num(p.get("depth_r_mmag"))
        out.append({
            "planet": pretty_planet_name(name),
            "host": str(p.get("star") or ""),
            "ra_deg": ra,
            "dec_deg": dec,
            "v_mag": _num(p.get("v_mag")),
            "period_d": period,
            "period_err_d": abs(_num(p.get("period_unc", p.get("ephem_period_e1"))) or 0.0) or None,
            "t0_bjd_tdb": t0,
            "t0_err_d": abs(_num(p.get("t0_unc", p.get("ephem_mid_time_e1"))) or 0.0),
            "duration_h": dur,
            "depth_pct": mmag_to_depth_pct(mmag) if mmag and mmag > 0 else None,
            "distance_pc": None,
            "source": SRC_EXOCLOCK,
            "exoclock_priority": str(p.get("priority") or "").lower() or None,
            # ExoClock reports an O-C of 0.0 for planets nobody has timed lately; that is "no data", not a measurement.
            "exoclock_oc_min": _num(p.get("current_oc_min")) if (_num(p.get("total_observations_recent")) or 0) > 0 else None,
        })
    return out


def parse_exoplanet_watch(text: str) -> list[dict[str, Any]]:
    """Planets from the Exoplanet Watch ranked CSV (``priority`` is the rank, 1 = most wanted).

    Column names carry stray spaces in the published file (``"priority "``), so they are stripped.
    The depth column is in parts per thousand.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise NetworkError("Exoplanet Watch reply is empty")
    reader.fieldnames = [f.strip() for f in reader.fieldnames]
    needed = {"name", "RA", "Dec", "epoch", "period", "duration", "priority"}
    if not needed.issubset(reader.fieldnames):
        raise NetworkError(f"Exoplanet Watch reply lacks columns {sorted(needed - set(reader.fieldnames))}")
    out: list[dict[str, Any]] = []
    for row in reader:
        row = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        period, t0, dur = _num(row.get("period")), _num(row.get("epoch")), _num(row.get("duration"))
        ra, dec = _sexagesimal(row.get("RA"), hours=True), _sexagesimal(row.get("Dec"), hours=False)
        rank = _num(row.get("priority"))
        if None in (period, t0, dur, ra, dec, rank) or period <= 0 or dur <= 0:  # type: ignore[operator]
            continue
        depth_ppt = _num(row.get("depth"))
        out.append({
            "planet": str(row["name"]),
            "host": "",
            "ra_deg": ra,
            "dec_deg": dec,
            "v_mag": _num(row.get("vmag")),
            "period_d": period,
            "period_err_d": abs(_num(row.get("period_uncertainty")) or 0.0) or None,
            "t0_bjd_tdb": t0,
            "t0_err_d": abs(_num(row.get("epoch_uncertainty")) or 0.0),
            "duration_h": dur,
            "depth_pct": depth_ppt / 10.0 if depth_ppt is not None and depth_ppt > 0 else None,
            "distance_pc": None,
            "source": SRC_EXOPLANET_WATCH,
            "ew_rank": int(rank),
        })
    return out


# --------------------------------------------------------------------------- #
# Ephemeris staleness and ranking
# --------------------------------------------------------------------------- #
def propagated_uncertainty_d(n_epochs: float, period_err_d: float | None, t0_err_d: float | None) -> float:
    """Mid-transit time uncertainty (days) ``n_epochs`` after the reference epoch (see the module docstring).

    An unknown period error is treated as unbounded (``inf``): a schedule with no stated error
    cannot be called fresh.
    """
    if period_err_d is None:
        return math.inf
    n = abs(n_epochs)
    dp, dt0 = abs(period_err_d), abs(t0_err_d or 0.0)
    return math.sqrt((n * dp) ** 2 + 2.0 * n * dp * dt0 + dt0**2)


def staleness(row: dict[str, Any], jd_ref: float) -> tuple[float, float]:
    """``(ratio, sigma_min)``: predicted-time uncertainty over half the duration, and that uncertainty in minutes."""
    n = (jd_ref - row["t0_bjd_tdb"]) / row["period_d"]
    sigma_d = propagated_uncertainty_d(n, row.get("period_err_d"), row.get("t0_err_d"))
    if math.isinf(sigma_d):
        return math.inf, math.inf
    return sigma_d / (0.5 * row["duration_h"] / 24.0), sigma_d * 1440.0


def merge(
    exoclock: list[dict[str, Any]],
    watch: list[dict[str, Any]],
    ttv_names: list[str],
    jd_ref: float,
) -> list[dict[str, Any]]:
    """One row per planet named by any list, carrying what each list said.

    The ephemeris used is the one with the smaller propagated uncertainty at ``jd_ref`` (ExoClock
    re-fits its ephemerides from new observations, so it normally wins); ``ephemeris_source`` says
    which.  Position, duration and depth come from the same source as the ephemeris so the pieces
    stay consistent, falling back to the other list for what is missing.
    """
    ttv = {normalize_name(n) for n in ttv_names}
    by_key: dict[str, dict[str, Any]] = {}
    for rows in (exoclock, watch):
        for r in rows:
            by_key.setdefault(normalize_name(r["planet"]), {})[r["source"]] = r
    n_watch = max(len(watch), 1)

    merged: list[dict[str, Any]] = []
    for key, found in by_key.items():
        ec, ew = found.get(SRC_EXOCLOCK), found.get(SRC_EXOPLANET_WATCH)

        def sigma(r: dict) -> float:
            return staleness(r, jd_ref)[1]

        primary = min((r for r in (ec, ew) if r), key=sigma)
        other = ew if primary is ec else ec
        row = dict(primary)
        for field in ("v_mag", "depth_pct", "host"):
            if not row.get(field) and other and other.get(field):
                row[field] = other[field]
        if ew:  # the NASA-style name is the more familiar spelling
            row["planet"] = ew["planet"]
        row.update(
            sources=[s for s in (SRC_EXOCLOCK if ec else None, SRC_EXOPLANET_WATCH if ew else None) if s],
            ephemeris_source=primary["source"],
            exoclock_priority=ec.get("exoclock_priority") if ec else None,
            exoclock_oc_min=ec.get("exoclock_oc_min") if ec else None,
            ew_rank=ew["ew_rank"] if ew else None,
            ew_total=n_watch,
            ttv_flag=key in ttv,
        )
        if row["ttv_flag"]:
            row["sources"].append(SRC_NASA_TTV)
        merged.append(row)
    return merged


def score(row: dict[str, Any], jd_ref: float) -> dict[str, Any]:
    """The ranking score for one merged row, its terms, its task and a one-sentence reason a person can check."""
    ratio, sigma_min = staleness(row, jd_ref)
    capped = STALENESS_CAP if math.isinf(ratio) else min(ratio, STALENESS_CAP)
    prio_name = row.get("exoclock_priority")
    prio = PRIORITY_VALUE.get(prio_name or "", 0.0)
    ew_rank = row.get("ew_rank")
    ew_term = 0.0 if ew_rank is None else max(0.0, 1.0 - (ew_rank - 1) / max(row.get("ew_total", 1), 1))
    ttv = 1.0 if row.get("ttv_flag") else 0.0
    terms = {"staleness": capped, "exoclock_priority": prio, "ew_rank": ew_term, "ttv": ttv}
    total = sum(WEIGHTS[k] * v for k, v in terms.items())

    task = TASK_TTV if ttv else TASK_EPHEMERIS
    parts: list[str] = []
    if prio_name:
        oc = row.get("exoclock_oc_min")
        parts.append(
            f"ExoClock rates it {prio_name.upper()} priority"
            + (f" (latest O-C {oc:+.1f} min)" if oc is not None else "")
        )
    if ew_rank is not None:
        parts.append(f"it is rank {ew_rank} of {row.get('ew_total')} on the Exoplanet Watch list")
    if ttv:
        parts.append("the NASA Exoplanet Archive flags transit-timing variations")
    if math.isinf(sigma_min):
        parts.append("its published period has no stated error, so its schedule cannot be trusted")
    else:
        parts.append(
            f"its predicted transit time is uncertain by about +/-{sigma_min:.{1 if sigma_min < 10 else 0}f} min "
            f"({ratio:.1f}x half the {row['duration_h']:.1f} h transit)"
        )
    reason = parts[0][0].upper() + parts[0][1:] + ("; " + "; ".join(parts[1:]) if len(parts) > 1 else "") + "."
    return {"score": total, "terms": terms, "task": task, "reason": reason, "staleness": ratio, "sigma_min": sigma_min}


def rank(rows: list[dict[str, Any]], jd_ref: float) -> list[dict[str, Any]]:
    """Rows with ``score``, ``terms``, ``task``, ``reason`` added, best first."""
    ranked = [{**r, **score(r, jd_ref)} for r in rows]
    ranked.sort(key=lambda r: -r["score"])
    return ranked


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _nonempty(parse):
    """A validator for a downloaded list: it must parse and hold at least one usable planet."""

    def check(text: str) -> None:
        if not parse(text):
            raise NetworkError("no usable planets in the reply")

    return check


def fetch_candidates(
    cache_dir: str | Path, ttl_s: float, jd_ref: float, *, cached_only: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch every list, merge, and rank.  Returns ``(ranked_rows, info)``.

    ``info`` has ``retrieved_utc`` (the oldest list used), ``stale_cache`` (any list came from an
    expired cached copy because the fetch failed), ``from_cache`` (no list needed the network),
    ``sources`` (name -> fetch metadata) and ``problems`` (lists that could not be used).  A failing
    list does not stop the others; only when neither ExoClock nor Exoplanet Watch is usable does this
    raise :class:`NetworkError`.

    ``cached_only`` reads whatever copies are already on disk, however old, and never touches the
    network (a list with no copy is a problem, not a download).  It is what a page that must open
    instantly uses; ``ttl_s`` is ignored.
    """
    cache = Path(cache_dir) / "networks"
    info: dict[str, Any] = {"sources": {}, "problems": [], "stale_cache": False, "from_cache": True, "retrieved_utc": None}
    if cached_only:
        ttl_s = math.inf
    exoclock: list[dict] = []
    watch: list[dict] = []
    ttv: list[str] = []

    def run(name: str, fn, path: Path):
        try:
            if cached_only and not path.is_file():
                raise NetworkError("not downloaded yet (refresh from the networks to fetch it)")
            result, meta = fn()
        except (NetworkError, archive.CatalogError) as exc:
            info["problems"].append(f"{name}: {exc}")
            log.warning("Target list %s unavailable: %s", name, exc)
            return None
        info["sources"][name] = meta
        info["stale_cache"] = info["stale_cache"] or bool(meta.get("stale"))
        info["from_cache"] = info["from_cache"] and bool(meta.get("from_cache"))
        return result

    def get_exoclock():
        text, meta = fetch_text(EXOCLOCK_URL, cache / "exoclock_planets.json", ttl_s=ttl_s, validate=_nonempty(parse_exoclock))
        return parse_exoclock(text), meta

    def get_watch():
        text, meta = fetch_text(
            EXOPLANET_WATCH_URL, cache / "exoplanet_watch_targets.csv", ttl_s=ttl_s, validate=_nonempty(parse_exoplanet_watch)
        )
        return parse_exoplanet_watch(text), meta

    exoclock = run(SRC_EXOCLOCK, get_exoclock, cache / "exoclock_planets.json") or []
    watch = run(SRC_EXOPLANET_WATCH, get_watch, cache / "exoplanet_watch_targets.csv") or []
    ttv = run(
        SRC_NASA_TTV, lambda: archive.fetch_ttv_names(cache / "nasa_ttv_flag.csv", ttl_s), cache / "nasa_ttv_flag.csv"
    ) or []
    if not exoclock and not watch:
        raise NetworkError("neither ExoClock nor Exoplanet Watch could be read: " + "; ".join(info["problems"]))

    stamps = [m["retrieved_utc"] for m in info["sources"].values() if m.get("retrieved_utc")]
    info["retrieved_utc"] = min(stamps) if stamps else None
    rows = rank(merge(exoclock, watch, ttv, jd_ref), jd_ref)
    log.info(
        "Target lists: %d ExoClock, %d Exoplanet Watch, %d TTV-flagged -> %d distinct planets%s",
        len(exoclock), len(watch), len(ttv), len(rows), " (some from an expired cache)" if info["stale_cache"] else "",
    )
    return rows, info


def network_block_for(row: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """The ``network`` block a target carries (see :mod:`core.networks`)."""
    return core_networks.network_block(
        row["sources"], row["task"], row["reason"],
        score=row["score"], terms=row["terms"],
        retrieved_utc=info.get("retrieved_utc"), stale_cache=bool(info.get("stale_cache")),
    )
