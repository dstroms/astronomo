"""Shared plumbing for taking part in collaborative (Pro-Am) science networks.

Astronomo's goal is to contribute to databases that professional and amateur observers already
maintain together -- ephemeris refinement for known planets, astrometry for known asteroids,
follow-up of public alerts -- rather than to "discover" things in isolation.  A *network* is
therefore two things for a plugin:

* a **target source**: an authoritative list of what is worth observing and why (ExoClock,
  NASA Exoplanet Watch, the MPC's NEO Confirmation Page ...), and
* a **destination**: the database the result is formatted for (see :mod:`core.submission`).

This module holds only what every source needs and nothing science-specific: a cached download
that survives an outage, a name normaliser, the vocabulary of task labels, and the ``network``
block that records on each target where it came from and why it was chosen.  The concrete
clients live in the plugin that understands the data (``plugins/transit_finder/networks.py`` ...),
so adding a source never touches the orchestrator.

The ``network`` block on a target (optional; validated by ``BaseExperimentPlugin``)::

    {
      "sources": ["exoclock", "exoplanet_watch"],  # every list that named this target
      "task": "ephemeris_refinement",              # one of TASKS (or "manual")
      "reason": str,                               # one plain sentence a person can check
      "score": float | None,                       # the plugin's ranking score, if it has one
      "terms": {str: float},                       # what the score is made of (audit trail)
      "retrieved_utc": str | None,                 # when the list was fetched
      "stale_cache": bool,                         # True if the fetch failed and an old copy was used
    }
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: What a target is for.  ``manual`` means a person (or a built-in fallback list) chose it, not a network.
TASK_EPHEMERIS = "ephemeris_refinement"  # a published schedule is going stale: re-measure it
TASK_TTV = "ttv_monitoring"  # the planet is flagged for transit-timing variations: keep sampling it
TASK_ASTROMETRY = "astrometry_followup"  # a small body needs more positions
TASK_FOLLOWUP = "transient_followup"  # a public alert wants a second look
TASK_MONITORING = "photometric_monitoring"  # a variable star needs its brightness measured on a regular cadence
TASK_MANUAL = "manual"
TASKS = (TASK_EPHEMERIS, TASK_TTV, TASK_ASTROMETRY, TASK_FOLLOWUP, TASK_MONITORING, TASK_MANUAL)


class NetworkError(RuntimeError):
    """A target list could not be fetched (and no usable cached copy exists)."""


def _http_get_text(url: str, params: dict | None, timeout: float) -> str:
    """One GET, returning the body.  A module-level function so tests can replace the network."""
    import requests

    try:
        resp = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "astronomo (pro-am target lists)"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise NetworkError(f"GET {url} failed: {exc}") from exc
    return resp.text


def fetch_text(
    url: str,
    cache_path: Path,
    *,
    ttl_s: float,
    params: dict | None = None,
    timeout: float = 30.0,
    validate: Callable[[str], Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Download ``url`` (or reuse a fresh copy) and return ``(text, meta)``.

    ``meta`` has ``retrieved_utc`` (when the *content* was fetched), ``from_cache`` and ``stale``.
    A cached copy younger than ``ttl_s`` is used without touching the network.  If the download
    fails, an older copy is used (``stale=True``, with a warning) so an outage does not stop a night
    that could still run on yesterday's list; with no copy at all this raises :class:`NetworkError`.

    ``validate`` is called with a freshly downloaded body *before* it is cached, and may raise
    anything if the body is not what was asked for.  This matters: a captive portal, a "site blocked"
    interstitial or an error page comes back as HTTP 200 HTML, and caching it would replace a good
    copy with one that cannot be read -- and the stale-copy fallback above would then serve that.  A body
    that fails validation is treated exactly like a failed download.
    """
    cache_path = Path(cache_path)
    cached_at = cache_path.stat().st_mtime if cache_path.is_file() else None
    if cached_at is not None and (time.time() - cached_at) <= ttl_s:
        return cache_path.read_text(encoding="utf-8"), _meta(cached_at, from_cache=True, stale=False)
    try:
        text = _http_get_text(url, params, timeout)
        if validate is not None:
            try:
                validate(text)
            except Exception as exc:  # noqa: BLE001 - any parse failure means "this is not the data"
                raise NetworkError(f"GET {url} returned something that is not the expected data ({exc})") from exc
    except NetworkError as exc:
        if cached_at is None:
            raise
        log.warning("%s; using the copy fetched %s", exc, _meta(cached_at, True, True)["retrieved_utc"])
        return cache_path.read_text(encoding="utf-8"), _meta(cached_at, from_cache=True, stale=True)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cache_path)  # never leave a half-written list for the next run
    return text, _meta(time.time(), from_cache=False, stale=False)


def _meta(epoch_s: float, from_cache: bool, stale: bool) -> dict[str, Any]:
    when = datetime.fromtimestamp(epoch_s, tz=timezone.utc).replace(microsecond=0)
    return {"retrieved_utc": when.isoformat(), "from_cache": from_cache, "stale": stale}


def normalize_name(name: str) -> str:
    """A comparison key that survives the naming differences between catalogues.

    ``"WASP-12 b"``, ``"WASP-12b"`` and ``"wasp12 B"`` all become ``"wasp12b"``.
    """
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def network_block(
    sources: list[str],
    task: str,
    reason: str,
    *,
    score: float | None = None,
    terms: dict[str, float] | None = None,
    retrieved_utc: str | None = None,
    stale_cache: bool = False,
) -> dict[str, Any]:
    """Build the ``network`` block a target carries (see the module docstring)."""
    if task not in TASKS:
        raise ValueError(f"unknown network task {task!r}; expected one of {', '.join(TASKS)}")
    return {
        "sources": list(sources),
        "task": task,
        "reason": reason,
        "score": None if score is None else round(float(score), 3),
        "terms": {k: round(float(v), 3) for k, v in (terms or {}).items()},
        "retrieved_utc": retrieved_utc,
        "stale_cache": bool(stale_cache),
    }


def check_network_block(target_name: str, block: Any) -> dict[str, Any]:
    """Validate a target's ``network`` block; returns it unchanged or raises ``ValueError``."""
    if not isinstance(block, dict):
        raise ValueError(f"target '{target_name}': 'network' must be a dict")
    sources = block.get("sources")
    if not isinstance(sources, list) or not sources or not all(isinstance(s, str) and s for s in sources):
        raise ValueError(f"target '{target_name}': network.sources must be a non-empty list of names")
    if block.get("task") not in TASKS:
        raise ValueError(f"target '{target_name}': network.task must be one of {', '.join(TASKS)}, not {block.get('task')!r}")
    if not isinstance(block.get("reason"), str) or not block["reason"].strip():
        raise ValueError(f"target '{target_name}': network.reason must say, in a sentence, why this target was chosen")
    return block
