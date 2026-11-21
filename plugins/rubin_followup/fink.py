"""Client for the public Fink/LSST REST API: Rubin Observatory alerts, no account needed.

Rubin alert packets are world public (no proprietary period) and reach the community through
brokers.  `Fink <https://fink-broker.org>`_ exposes processed alerts over a REST API that needs no
login (https://api.lsst.fink-portal.org).  This plugin uses three read-only endpoints:

* ``/api/v1/tags``     the most recent alerts selected by a named Fink filter,
* ``/api/v1/sources``  every alert detection ("diaSource") of one object, i.e. its light curve,
* ``/api/v1/statistics`` how many alerts arrived per night (to tell "quiet sky" from "stream down").

What an alert's photometry *is*: ``psfFlux`` is a **difference-image** PSF flux in nanojansky.  It is
the change relative to a template, so for a new transient it is the transient's total flux, but for a
variable star or an AGN on a bright template it is only the *change* and says nothing about the total
brightness.  Magnitudes here are ``m_AB = 31.4 - 2.5 log10(flux_nJy)``.

The service is shared and the maintainers ask clients not to bulk-download: this client only ever
asks for a few hundred rows and a handful of columns.

Everything here is plain HTTP + JSON; failures raise :class:`FinkError` (a
:class:`~tools.catalogs.CatalogError`) and never return partial data silently.
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from tools.catalogs import CatalogError

log = logging.getLogger(__name__)

DEFAULT_URL = "https://api.lsst.fink-portal.org"
USER_AGENT = "astronomo-rubin-followup (small read-only queries)"

#: Columns requested for an alert.  ``r:`` columns are the Rubin alert packet, ``f:`` are Fink additions.
ALERT_COLUMNS = (
    "r:diaObjectId", "r:diaSourceId", "r:ra", "r:dec", "r:psfFlux", "r:psfFluxErr", "r:band",
    "r:midpointMjdTai", "r:reliability", "r:isNegative", "f:xm_simbad_otype", "f:xm_tns_fullname",
)
HISTORY_COLUMNS = ("r:diaObjectId", "r:diaSourceId", "r:ra", "r:dec", "r:psfFlux", "r:psfFluxErr", "r:band", "r:midpointMjdTai")

_TAG = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_OBJECT_ID = re.compile(r"^[0-9]{1,20}$")
# Fink fills xm_* columns with this when a cross-match service failed or found nothing.
_NO_VALUE = {"", "Fail", "Unknown", "nan", "None", "null"}


# --------------------------------------------------------------------------- #
# Photometry and time conversions
# --------------------------------------------------------------------------- #
def ab_mag(flux_njy: float | None) -> float | None:
    """AB magnitude of a flux in nJy (``None`` for a non-positive or missing flux)."""
    if flux_njy is None or not math.isfinite(flux_njy) or flux_njy <= 0:
        return None
    return 31.4 - 2.5 * math.log10(flux_njy)


def ab_mag_err(flux_njy: float | None, err_njy: float | None) -> float | None:
    if flux_njy is None or err_njy is None or not (math.isfinite(flux_njy) and math.isfinite(err_njy)) or flux_njy <= 0:
        return None
    return 2.5 / math.log(10.0) * abs(err_njy) / flux_njy


def mjd_tai_to_utc(mjd: float) -> datetime:
    """Rubin stamps visits in MJD (TAI); the observatory's UTC is a few tens of seconds behind it."""
    from astropy.time import Time

    return Time(float(mjd), format="mjd", scale="tai").utc.to_datetime(timezone=timezone.utc)


class FinkError(CatalogError):
    """The Fink service was unreachable, refused the query or returned something unparsable."""


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text in _NO_VALUE else text


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One Fink row (``r:``/``f:`` keys) -> a flat alert dict, or ``None`` if it has no usable position/time."""
    ra, dec, mjd = _num(row.get("r:ra")), _num(row.get("r:dec")), _num(row.get("r:midpointMjdTai"))
    oid = row.get("r:diaObjectId")
    if None in (ra, dec, mjd) or oid is None:
        return None
    flux, flux_err = _num(row.get("r:psfFlux")), _num(row.get("r:psfFluxErr"))
    return {
        "dia_object_id": str(int(oid)),
        "dia_source_id": str(int(row["r:diaSourceId"])) if row.get("r:diaSourceId") is not None else None,
        "ra_deg": ra % 360.0, "dec_deg": dec,
        "band": _clean(row.get("r:band")),
        "mjd": mjd, "utc_iso": mjd_tai_to_utc(mjd).isoformat(),
        "flux_njy": flux, "flux_err_njy": flux_err,
        "mag": ab_mag(flux), "mag_err": ab_mag_err(flux, flux_err),
        "reliability": _num(row.get("r:reliability")),
        "is_negative": bool(row.get("r:isNegative")) if row.get("r:isNegative") is not None else False,
        "tns_name": _clean(row.get("f:xm_tns_fullname")),
        "simbad_otype": _clean(row.get("f:xm_simbad_otype")),
    }


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
class FinkClient:
    def __init__(self, base_url: str | None = None, timeout_s: float = 60.0, session: Any = None):
        self.base_url = (base_url or os.environ.get("ASTRONOMO_RUBIN_FINK_URL") or DEFAULT_URL).rstrip("/")
        self.timeout_s = timeout_s
        self._session = session

    # -- transport ---------------------------------------------------------- #
    def _get(self, path: str, params: dict[str, Any]) -> Any:
        import requests

        http = self._session or requests
        url = f"{self.base_url}{path}"
        try:
            resp = http.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise FinkError(f"Fink request to {path} failed: {exc}") from exc
        if resp.status_code != 200:
            raise FinkError(f"Fink {path} answered HTTP {resp.status_code}: {str(resp.text)[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FinkError(f"Fink {path} returned something that is not JSON: {str(resp.text)[:120]!r}") from exc
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise FinkError(f"Fink {path} reported an error: {str(payload.get('text'))[:200]}")
        return payload

    # -- alerts ------------------------------------------------------------- #
    def recent_alerts(self, tag: str, n: int = 500, since: datetime | None = None) -> list[dict[str, Any]]:
        """The latest ``n`` alerts selected by Fink filter ``tag``, newest data first (parsed, unfiltered)."""
        if not _TAG.match(tag):
            raise FinkError(f"refusing to query Fink with an odd tag name: {tag!r}")
        params: dict[str, Any] = {"tag": tag, "n": int(max(1, min(n, 2000))), "columns": ",".join(ALERT_COLUMNS)}
        if since is not None:
            params["startdate"] = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        payload = self._get("/api/v1/tags", params)
        if not isinstance(payload, list):
            raise FinkError(f"Fink /tags returned {type(payload).__name__}, expected a list")
        rows = [r for r in (parse_row(x) for x in payload if isinstance(x, dict)) if r]
        rows.sort(key=lambda r: r["mjd"], reverse=True)
        return rows

    def newest_alert_utc(self, tag: str) -> datetime | None:
        """When the newest alert of ``tag`` was observed (no date filter), or ``None`` if there are none."""
        rows = self.recent_alerts(tag, n=1)
        return datetime.fromisoformat(rows[0]["utc_iso"]) if rows else None

    def available_tags(self) -> dict[str, bool]:
        """``{tag: usable through the REST API}``."""
        payload = self._get("/api/v1/tags", {})
        if not isinstance(payload, dict):
            raise FinkError("Fink /tags (listing) did not return a mapping")
        return {str(k): bool((v or {}).get("API support", False)) for k, v in payload.items()}

    def object_history(self, dia_object_id: str) -> list[dict[str, Any]]:
        """Every alert detection of one object (oldest first), as parsed alert dicts."""
        return self.objects_history([dia_object_id]).get(str(dia_object_id), [])

    def objects_history(self, dia_object_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        ids = [str(i).strip() for i in dia_object_ids if str(i).strip()]
        bad = [i for i in ids if not _OBJECT_ID.match(i)]
        if bad:
            raise FinkError(f"refusing to query Fink with odd diaObjectId value(s): {bad}")
        if not ids:
            return {}
        payload = self._get("/api/v1/sources", {"diaObjectId": ",".join(ids), "columns": ",".join(HISTORY_COLUMNS)})
        if not isinstance(payload, list):
            raise FinkError(f"Fink /sources returned {type(payload).__name__}, expected a list")
        out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        for row in (parse_row(x) for x in payload if isinstance(x, dict)):
            if row and row["dia_object_id"] in out:
                out[row["dia_object_id"]].append(row)
        for rows in out.values():
            rows.sort(key=lambda r: r["mjd"])
        return out


# --------------------------------------------------------------------------- #
# Selection helpers (pure functions: tested without a network)
# --------------------------------------------------------------------------- #
def latest_per_object(alerts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each object's most recent alert, newest first (a tag returns many alerts per object)."""
    best: dict[str, dict[str, Any]] = {}
    for a in alerts:
        cur = best.get(a["dia_object_id"])
        if cur is None or a["mjd"] > cur["mjd"]:
            best[a["dia_object_id"]] = a
    return sorted(best.values(), key=lambda a: a["mjd"], reverse=True)


def light_curve_points(history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The positive-flux detections of a history as compact ``{utc_iso, mjd, band, mag, mag_err}`` points."""
    pts = []
    for h in history:
        if h.get("mag") is None or h.get("is_negative"):
            continue
        pts.append({
            "utc_iso": h["utc_iso"], "mjd": h["mjd"], "band": h.get("band") or "?",
            "mag": round(float(h["mag"]), 3), "mag_err": None if h.get("mag_err") is None else round(float(h["mag_err"]), 3),
        })
    pts.sort(key=lambda p: p["mjd"])
    return pts


def days_between(earlier_iso: str, later: datetime) -> float:
    a = datetime.fromisoformat(earlier_iso)
    a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
    return (later - a).total_seconds() / 86400.0


def cutoff(now: datetime, max_age_d: float) -> datetime:
    return now - timedelta(days=max_age_d)
