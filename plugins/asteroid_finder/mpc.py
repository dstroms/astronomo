"""Minor Planet Center: the NEO Confirmation Page (NEOCP) as a target list.

The NEOCP lists newly reported objects that still need follow-up astrometry before they have an
orbit.  That is the clearest "someone is waiting for this measurement" task in solar-system
astronomy, and a collaborative network in the plainest sense: surveys post candidates, observers
anywhere on Earth send positions back to the MPC.

Limits, stated up front because they decide how useful this source is to a 50 mm telescope:

* Almost everything on the list is faint (V 19-24).  The Seestar S50 reaches roughly V 14-15 in a short
  sequence, so on most nights **no entry qualifies**; the plugin then falls back to its built-in bright
  list and the mission log says the network was consulted and had nothing usable.
* The feed gives the position at the object's *last observation*, not now.  An object last seen
  ``Not_Seen_dys`` days ago may have moved by a field of view or more, so entries unseen for longer
  than ``max_not_seen_d`` are rejected, and an accepted entry is flagged as a position that is only good
  to the size of the field.  The MPC's ephemeris service would give a proper position; it is not used here.
* An NEOCP object has no designation Horizons knows, so the cross-reference cannot identify it and
  the astrometry is reported under its temporary designation (``trkSub``), as the MPC expects.

Feed: ``https://minorplanetcenter.net/Extended_Files/neocp.json`` (a JSON list; ``R.A.`` is in decimal
hours, ``Decl.`` in degrees, ``V`` is 99.9 when unknown, ``Not_Seen_dys`` is days since the last report).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.networks import NetworkError, fetch_text

log = logging.getLogger(__name__)

NEOCP_URL = "https://minorplanetcenter.net/Extended_Files/neocp.json"
SOURCE = "mpc_neocp"
UNKNOWN_V = 99.0  # the feed's 99.9 means "no magnitude"
CACHE_TTL_S = 3600.0  # the page changes through the day; never trust a copy older than an hour


def parse_neocp(text: str) -> list[dict[str, Any]]:
    """Entries from the NEOCP JSON as plain rows (``temp_desig``, ``ra_deg``, ``dec_deg``, ``v_mag``, ...).

    Entries without a usable position are skipped.  Raises :class:`NetworkError` when the payload is not
    the expected list.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise NetworkError(f"NEOCP reply is not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise NetworkError("NEOCP reply is not the expected list")
    rows: list[dict[str, Any]] = []
    for e in data:
        try:
            ra_h, dec = float(e["R.A."]), float(e["Decl."])
            desig = str(e["Temp_Desig"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not desig or not (0.0 <= ra_h < 24.0) or not (-90.0 <= dec <= 90.0):
            continue
        v = e.get("V")
        rows.append({
            "temp_desig": desig,
            "ra_deg": ra_h * 15.0,
            "dec_deg": dec,
            "v_mag": float(v) if isinstance(v, (int, float)) and v < UNKNOWN_V else None,
            "score": e.get("Score"),
            "n_obs": e.get("NObs"),
            "arc_d": e.get("Arc"),
            "not_seen_d": float(e["Not_Seen_dys"]) if isinstance(e.get("Not_Seen_dys"), (int, float)) else None,
            "h_mag": e.get("H"),
            "updated": str(e.get("Updated") or ""),
        })
    return rows


def fetch_neocp(cache_dir: str | Path, ttl_s: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The current NEOCP and the fetch metadata (``retrieved_utc``, ``stale``).  Raises :class:`NetworkError`."""
    text, meta = fetch_text(NEOCP_URL, Path(cache_dir) / "networks" / "neocp.json", ttl_s=min(ttl_s, CACHE_TTL_S), validate=parse_neocp)
    return parse_neocp(text), meta


def usable(rows: list[dict[str, Any]], *, max_v: float, max_not_seen_d: float, min_score: float = 0.0) -> list[dict[str, Any]]:
    """Entries a small telescope can point at: bright enough, recently seen, a known magnitude.

    Brightest first.  ``score`` is the MPC's own NEO-likelihood score (0-100); ``min_score`` lets a user
    insist on likely NEOs.
    """
    keep = [
        r for r in rows
        if r["v_mag"] is not None and r["v_mag"] <= max_v
        and r["not_seen_d"] is not None and r["not_seen_d"] <= max_not_seen_d
        and (r["score"] is None or r["score"] >= min_score)
    ]
    keep.sort(key=lambda r: r["v_mag"])
    return keep


def reason(row: dict[str, Any], max_not_seen_d: float) -> str:
    """One sentence, checkable against the NEOCP, saying why this object was chosen."""
    return (
        f"The MPC NEO Confirmation Page lists {row['temp_desig']} (V~{row['v_mag']:.1f}, {row['n_obs']} observations over "
        f"{row['arc_d']} d, last seen {row['not_seen_d']:.1f} d ago) as awaiting follow-up astrometry; its position is "
        f"the one at its last observation, good only to about a field of view."
    )
