"""NASA Exoplanet Archive client (plain HTTP, TAP ``pscomppars`` table).

Only the transit plugin uses this, so it lives here rather than in the shared ``tools/``.
There is no offline stand-in: simulated transit targets carry their own catalogue values
(see :mod:`plugins.transit_finder.plugin`), so offline runs never call it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from tools.catalogs import CatalogError

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
_PLANET_NAME = re.compile(r"^[A-Za-z0-9 .+\-_]{1,40}$")


TTV_QUERY = "select pl_name from pscomppars where ttv_flag=1"


def parse_ttv_names(csv_text: str) -> list[str]:
    """Planet names from the one-column CSV the TAP service returns for :data:`TTV_QUERY`."""
    lines = [ln.strip().strip('"') for ln in csv_text.splitlines() if ln.strip()]
    if not lines or lines[0].lower() != "pl_name":
        raise CatalogError("NASA Exoplanet Archive returned an unexpected reply to the TTV query")
    return lines[1:]


def fetch_ttv_names(cache_path, ttl_s: float) -> tuple[list[str], dict[str, Any]]:
    """Names of planets the archive flags as showing transit-timing variations (``ttv_flag``), plus fetch metadata.

    Cached like the network target lists (see :func:`core.networks.fetch_text`), so an archive outage
    does not take the flag away.  Raises :class:`~tools.catalogs.CatalogError`.
    """
    from core import networks

    try:
        text, meta = networks.fetch_text(
            ARCHIVE_URL, cache_path, ttl_s=ttl_s, params={"query": TTV_QUERY, "format": "csv"}, validate=parse_ttv_names
        )
        return parse_ttv_names(text), meta
    except networks.NetworkError as exc:
        raise CatalogError(str(exc)) from exc


def fetch_planets(planet_names: Sequence[str]) -> list[dict[str, Any]]:
    """Transit ephemerides, one row per planet in ``pscomppars``.

    Returns one dict per *found* planet: ``planet``, ``host``, ``ra_deg``, ``dec_deg``,
    ``v_mag``, ``period_d``, ``period_err_d``, ``t0_bjd_tdb`` (a mid-transit time, BJD_TDB),
    ``t0_err_d``, ``duration_h`` (T14), ``depth_pct``, ``distance_pc`` (the system distance, or
    ``None``) and ``source``.  Planets missing a
    period, mid-transit time or duration are skipped (they cannot be scheduled).  Unknown names
    are simply absent from the result.  Raises :class:`~tools.catalogs.CatalogError` on a bad
    name or a network/parse failure.
    """
    names = [n.strip() for n in planet_names if n and n.strip()]
    bad = [n for n in names if not _PLANET_NAME.match(n)]
    if bad:
        raise CatalogError(f"refusing to query the archive with odd planet name(s): {bad}")
    if not names:
        return []

    import requests

    quoted = ",".join(f"'{n}'" for n in names)
    query = (
        "select pl_name,hostname,ra,dec,sy_vmag,pl_orbper,pl_orbpererr1,pl_tranmid,pl_tranmiderr1,"
        f"pl_trandur,pl_trandep,sy_dist from pscomppars where pl_name in ({quoted})"
    )
    try:
        resp = requests.get(ARCHIVE_URL, params={"query": query, "format": "json"}, timeout=30)
        resp.raise_for_status()
        table = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise CatalogError(f"NASA Exoplanet Archive query failed: {exc}") from exc

    def num(row: dict, key: str) -> float | None:
        v = row.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    out: list[dict[str, Any]] = []
    for row in table:
        period, t0, dur = num(row, "pl_orbper"), num(row, "pl_tranmid"), num(row, "pl_trandur")
        ra, dec = num(row, "ra"), num(row, "dec")
        if None in (period, t0, dur, ra, dec) or period <= 0 or dur <= 0:  # type: ignore[operator]
            log.info("Exoplanet Archive: skipping %s (incomplete transit ephemeris)", row.get("pl_name"))
            continue
        err1 = num(row, "pl_orbpererr1")
        out.append(
            {
                "planet": str(row.get("pl_name")),
                "host": str(row.get("hostname") or ""),
                "ra_deg": ra,
                "dec_deg": dec,
                "v_mag": num(row, "sy_vmag"),
                "period_d": period,
                "period_err_d": abs(err1) if err1 is not None else None,
                "t0_bjd_tdb": t0,
                "t0_err_d": abs(num(row, "pl_tranmiderr1") or 0.0),
                "duration_h": dur,
                "depth_pct": num(row, "pl_trandep"),
                "distance_pc": (d if (d := num(row, "sy_dist")) is not None and d > 0 else None),
                "source": "nasa-exoplanet-archive",
            }
        )
    return out
