"""Archive data sources: the same pipeline, fed by a public survey instead of the telescope.

A plugin that supports ``ASTRONOMO_DATA_SOURCE=archive`` returns an :class:`ArchiveSource` from
:meth:`~core.base_plugin.BaseExperimentPlugin.archive_source`.  The graph calls
:meth:`ArchiveSource.acquire` where it would otherwise call ``telescope.capture_sequence``; what
comes back is ordinary FITS on disk, so ``analyze_frames`` / ``cross_reference`` /
``build_submissions`` do not know (and must not care) where the pixels came from.

The one thing an archive adds is *time*: every frame has the instant it was really taken.
``acquire`` announces each frame through ``on_frame(index, total, obs_time)`` **before** writing
it and the graph blocks in that callback until the mission clock reaches ``obs_time``.  With a
real-time replay clock frames therefore "arrive" at the survey's own cadence; with the default
jump clock they all arrive at once.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: ``on_frame(index, total, obs_time)``; ``obs_time`` is the frame's real (survey) UTC time or ``None``.
FrameCallback = Callable[[int, int, "datetime | None"], None]


class ArchiveError(RuntimeError):
    """An archive could not supply the requested data (network, parsing, corrupt products)."""


@dataclass
class FrameBatch:
    """What one visit produced: the FITS paths plus provenance for the mission log and the dashboard."""

    paths: list[str]
    #: Exposure plan equivalent for the observation record (gain/exposure_s/filter/count + extras).
    plan: dict[str, Any]
    #: Provenance: ``source``, ``credit``, ``urls``, ``skipped`` (frames that could not be used)...
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ArchiveSource(Protocol):
    #: Short machine name ("ztf") and the credit line the mission log and upload notes must carry.
    name: str
    credit: str

    def acquire(self, target: dict, out_dir: Path, on_frame: FrameCallback | None = None) -> FrameBatch:
        """Fetch/prepare the frames of ``target`` into ``out_dir`` and return them."""


def http_get(
    url: str,
    *,
    params: dict | None = None,
    timeout: float = 120.0,
    retries: int = 3,
    stream_to: Path | None = None,
    session: Any = None,
) -> bytes | None:
    """GET with retries and backoff.  Returns the body, or ``None`` when streamed to ``stream_to``.

    Raises :class:`ArchiveError` after the last retry.  A partial download never leaves a file at
    ``stream_to`` (it is written to ``<name>.part`` and renamed once complete).
    """
    import requests

    getter = session or requests
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if stream_to is None:
                resp = getter.get(url, params=params, timeout=timeout)
                resp.raise_for_status()
                return resp.content
            part = stream_to.with_name(stream_to.name + ".part")
            stream_to.parent.mkdir(parents=True, exist_ok=True)
            with getter.get(url, params=params, timeout=timeout, stream=True) as resp:
                resp.raise_for_status()
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        fh.write(chunk)
            part.replace(stream_to)
            return None
        except (requests.RequestException, OSError) as exc:
            last = exc
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(min(2.0 ** attempt, 10.0))
    raise ArchiveError(f"GET {url} failed after {retries} attempts: {last}")
