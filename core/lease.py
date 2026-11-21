"""Mission ownership: one process works on a mission at a time, many missions run side by side.

Several ``main.py`` processes (and, say, a dashboard) share one database.  A mission is *owned* by
the process that keeps refreshing ``missions.heartbeat_at``.  A background thread does that every
few seconds -- also while the main thread is stuck in a 10-minute reduction or a multi-day wait --
so "heartbeat older than the lease timeout" reliably means the owner is gone.  Another process
may then resume the mission from its LangGraph checkpoint (see ``main.choose_mission``).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from db.database import Database

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 10.0


class LeaseError(RuntimeError):
    """The mission is owned by another live process."""


def worker_identity() -> tuple[int, str]:
    return os.getpid(), socket.gethostname()


def liveness(mission: dict[str, Any], lease_s: float = 45.0, now: datetime | None = None) -> str:
    """``'finished'``, ``'running'`` (live owner), ``'orphaned'`` (unfinished, owner gone) or ``'idle'``.

    ``idle`` = never owned (created but not started, or cleanly released between cycles).
    """
    if mission.get("status") in ("COMPLETE", "FAILED", "ABORTED", "STOPPED"):
        return "finished"
    beat = mission.get("heartbeat_at")
    if not beat:
        return "idle"
    now = now or datetime.now(timezone.utc)
    when = datetime.fromisoformat(beat) if isinstance(beat, str) else beat
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return "running" if now - when <= timedelta(seconds=lease_s) else "orphaned"


class Lease:
    """Context manager: claim a mission, heartbeat it in a thread, release on exit.

    ``with Lease(db, mission_id, lease_s=45): run_mission(...)``
    """

    def __init__(self, db: Database, mission_id: str, lease_s: float = 45.0, interval_s: float = HEARTBEAT_INTERVAL_S):
        self.db, self.mission_id, self.lease_s = db, mission_id, lease_s
        self.interval_s = max(0.05, min(interval_s, lease_s / 3.0))
        self.pid, self.host = worker_identity()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def __enter__(self) -> "Lease":
        if not self.db.claim_mission(self.mission_id, self.pid, self.host, self.lease_s):
            raise LeaseError(f"mission {self.mission_id} is owned by another live process")
        self._thread = threading.Thread(target=self._beat, name=f"lease-{self.mission_id}", daemon=True)
        self._thread.start()
        return self

    def _beat(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                if not self.db.heartbeat(self.mission_id, self.pid, self.host):
                    self.lost = True
                    log.error("Lost the lease on mission %s (another process took it over)", self.mission_id)
                    return
            except Exception:  # noqa: BLE001 - a locked DB must not kill the mission thread
                log.debug("heartbeat failed", exc_info=True)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        try:
            self.db.release_mission(self.mission_id, self.pid, self.host)
        except Exception:  # noqa: BLE001
            log.debug("release failed", exc_info=True)
