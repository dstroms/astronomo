"""SQLite persistence engine.

A thin repository layer over SQLAlchemy.  All public methods return plain ``dict``s so
callers (graph nodes, the dashboard, the HTTP API) never touch detached ORM instances.

SQLite runs in WAL mode so the dashboard process can read while the mission process
writes.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from sqlalchemy import and_, create_engine, event, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    Artifact, Base, LLMCall, LogEvent, Mission, Observation, PlannedObservation, Target, WeatherReading, utcnow,
)

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {"COMPLETE", "FAILED", "ABORTED", "STOPPED"}
#: Every plan status a person still wants to see (a cancelled plan is hidden).
LIVE_PLAN_STATUSES = ("PLANNED", "STARTED", "DONE", "MISSED")


def _utc_naive(value: Any) -> datetime:
    """An ISO-8601 string or datetime as the naive-UTC datetime the tables store."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def summarise_llm_calls(rows: Iterable[dict]) -> dict:
    """Aggregate ledger rows.  Single implementation shared by the DB, the client and the CLI.

    A row *costs tokens* if it was not a cache hit and the server reported/estimated tokens
    (this includes calls that returned an unusable reply: they were still billed).  Those rows
    are what the per-mission budget counts.
    """
    rows = list(rows)
    paid = [r for r in rows if not r.get("cached") and (r.get("ok") or (r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)) > 0)]
    return {
        "calls": len(paid),
        "cached_calls": sum(1 for r in rows if r.get("cached") and r.get("ok")),
        "failed_calls": sum(1 for r in rows if not r.get("ok")),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in paid),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in paid),
        "total_tokens": sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in paid),
        "max_prompt_chars": max((r.get("prompt_chars", 0) for r in rows), default=0),
    }


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        event.listen(self.engine, "connect", self._set_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self._write_lock = threading.RLock()
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (``create_all`` never alters tables).

        Only additive, nullable/defaulted columns are supported, which is all this schema needs;
        anything else should be a real migration tool.
        """
        from sqlalchemy import inspect, text

        inspector = inspect(self.engine)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(self.engine.dialect)}'
                default = getattr(column.default, "arg", None) if column.default is not None else None
                if isinstance(default, (str, int, float)) and not isinstance(default, bool):
                    ddl += f" DEFAULT {default!r}" if isinstance(default, str) else f" DEFAULT {default}"
                log.info("DB migration: %s", ddl)
                with self.engine.begin() as conn:
                    conn.execute(text(ddl))

    @staticmethod
    def _set_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._write_lock:
            session = self._sessions()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def dispose(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------ #
    # Missions
    # ------------------------------------------------------------------ #
    def create_mission(
        self,
        mission_id: str,
        plugin_name: str,
        simulated: bool,
        config: dict | None = None,
        data_source: str = "telescope",
    ) -> dict:
        with self.session() as s:
            mission = Mission(
                id=mission_id,
                plugin_name=plugin_name,
                simulated=simulated,
                status="INIT",
                config=config,
                data_source=data_source,
            )
            s.add(mission)
            s.flush()
            return mission.to_dict()

    def update_mission(self, mission_id: str, **fields: Any) -> None:
        with self.session() as s:
            mission = s.get(Mission, mission_id)
            if mission is None:
                log.warning("update_mission: unknown mission %s", mission_id)
                return
            for key, value in fields.items():
                setattr(mission, key, value)

    def get_mission(self, mission_id: str) -> dict | None:
        with self.session() as s:
            mission = s.get(Mission, mission_id)
            return mission.to_dict() if mission else None

    def latest_mission(
        self,
        plugin_name: str | None = None,
        simulated: bool | None = None,
        unfinished_only: bool = False,
        data_source: str | None = None,
        unowned_only: bool = False,
        lease_s: float = 45.0,
    ) -> dict | None:
        """Newest matching mission.  ``unowned_only`` skips missions a live process is working on."""
        with self.session() as s:
            stmt = select(Mission).order_by(Mission.created_at.desc(), Mission.id.desc())
            if plugin_name is not None:
                stmt = stmt.where(Mission.plugin_name == plugin_name)
            if simulated is not None:
                stmt = stmt.where(Mission.simulated == simulated)
            if data_source is not None:
                stmt = stmt.where(Mission.data_source == data_source)
            if unfinished_only:
                stmt = stmt.where(Mission.status.notin_(TERMINAL_STATUSES))
            if unowned_only:
                stmt = stmt.where(or_(Mission.heartbeat_at.is_(None), Mission.heartbeat_at < utcnow() - timedelta(seconds=lease_s)))
            mission = s.scalars(stmt.limit(1)).first()
            return mission.to_dict() if mission else None

    def list_missions(self, limit: int = 50) -> list[dict]:
        with self.session() as s:
            stmt = select(Mission).order_by(Mission.created_at.desc()).limit(limit)
            return [m.to_dict() for m in s.scalars(stmt)]

    # ------------------------------------------------------------------ #
    # Ownership lease / live progress (several processes, one database)
    # ------------------------------------------------------------------ #
    def claim_mission(self, mission_id: str, pid: int, host: str, lease_s: float = 45.0) -> bool:
        """Atomically become the owner of ``mission_id``.

        Succeeds when nobody owns it, the owner's heartbeat is older than ``lease_s`` (it died), or
        we already own it.  Returns whether the claim was granted.
        """
        cutoff = utcnow() - timedelta(seconds=lease_s)
        with self.session() as s:
            result = s.execute(
                update(Mission)
                .where(
                    Mission.id == mission_id,
                    or_(
                        Mission.heartbeat_at.is_(None),
                        Mission.heartbeat_at < cutoff,
                        and_(Mission.worker_pid == pid, Mission.worker_host == host),
                    ),
                )
                .values(worker_pid=pid, worker_host=host, heartbeat_at=utcnow())
            )
            return result.rowcount == 1

    def heartbeat(self, mission_id: str, pid: int, host: str) -> bool:
        """Refresh the lease; ``False`` means somebody else owns the mission now."""
        with self.session() as s:
            result = s.execute(
                update(Mission)
                .where(Mission.id == mission_id, Mission.worker_pid == pid, Mission.worker_host == host)
                .values(heartbeat_at=utcnow())
            )
            return result.rowcount == 1

    def release_mission(self, mission_id: str, pid: int, host: str) -> None:
        with self.session() as s:
            s.execute(
                update(Mission)
                .where(Mission.id == mission_id, Mission.worker_pid == pid, Mission.worker_host == host)
                .values(heartbeat_at=None, worker_pid=None, worker_host=None)
            )

    def update_progress(self, mission_id: str, **fields: Any) -> None:
        """Merge ``fields`` into ``Mission.progress`` (a ``None`` value deletes the key)."""
        with self.session() as s:
            mission = s.get(Mission, mission_id)
            if mission is None:
                return
            merged = dict(mission.progress or {})
            for key, value in fields.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            mission.progress = merged

    # ------------------------------------------------------------------ #
    # Targets
    # ------------------------------------------------------------------ #
    def save_targets(self, mission_id: str, targets: Iterable[dict]) -> list[int]:
        """Persist targets (replacing any previous set for this mission)."""
        ids: list[int] = []
        with self.session() as s:
            for existing in s.scalars(select(Target).where(Target.mission_id == mission_id)):
                s.delete(existing)
            s.flush()
            for position, tgt in enumerate(targets):
                row = Target(
                    mission_id=mission_id,
                    position=position,
                    name=str(tgt["name"]),
                    ra_deg=float(tgt["ra_deg"]),
                    dec_deg=float(tgt["dec_deg"]),
                    priority=float(tgt.get("priority", 0.0)),
                    payload=tgt,
                )
                s.add(row)
                s.flush()
                ids.append(row.id)
        return ids

    def update_target(self, target_id: int, **fields: Any) -> None:
        with self.session() as s:
            row = s.get(Target, target_id)
            if row is not None:
                for key, value in fields.items():
                    setattr(row, key, value)

    def list_targets(self, mission_id: str) -> list[dict]:
        with self.session() as s:
            stmt = select(Target).where(Target.mission_id == mission_id).order_by(Target.position)
            return [t.to_dict() for t in s.scalars(stmt)]

    # ------------------------------------------------------------------ #
    # Planned observations (chosen on the Opportunities page; nothing here starts a mission)
    # ------------------------------------------------------------------ #
    def plan_observation(
        self,
        plugin_name: str,
        target: dict,
        *,
        network: dict | None = None,
        impact: dict | None = None,
        note: str | None = None,
    ) -> tuple[dict, bool]:
        """Record an intention to observe ``target`` in its window.  Returns ``(row, created)``.

        ``target`` needs ``name``, ``window_start`` and ``window_end``.  One window has at most one open plan
        (``PLANNED`` or ``STARTED``): planning it again returns that record (``created=False``).  A cancelled
        plan is revived.  A window whose plan is ``DONE`` or ``MISSED`` is planned again as a new row, so a
        failed or finished mission can be tried again and the earlier mission stays where it is.
        """
        start, end = _utc_naive(target["window_start"]), _utc_naive(target["window_end"])
        with self.session() as s:
            matches = list(s.scalars(
                select(PlannedObservation).where(
                    PlannedObservation.plugin_name == plugin_name,
                    PlannedObservation.target_name == str(target["name"]),
                    PlannedObservation.window_start == start,
                ).order_by(PlannedObservation.id)
            ))
            open_note = next((row for row in reversed(matches) if row.status == "PLANNED"), None)
            if open_note is not None:
                return open_note.to_dict(), False
            running = next((row for row in reversed(matches) if row.status == "STARTED"), None)
            if running is not None:
                return running.to_dict(), False
            cancelled = [row for row in matches if row.status == "CANCELLED"]
            if cancelled:
                row = cancelled[-1]
                row.status = "PLANNED"
                row.target = target
                row.window_end = end
                if network is not None:
                    row.network = network
                if impact is not None:
                    row.impact = impact
                if note is not None:
                    row.note = note
                row.outcome = None
                s.flush()
                return row.to_dict(), True
            row = PlannedObservation(
                plugin_name=plugin_name, target_name=str(target["name"]), window_start=start, window_end=end,
                target=target, network=network, impact=impact, note=note,
            )
            s.add(row)
            s.flush()
            return row.to_dict(), True

    def list_planned(self, *, status: str | Iterable[str] | None = "PLANNED", plugin_name: str | None = None) -> list[dict]:
        """Planned observations, soonest window first.

        ``status`` is one status, several (``LIVE_PLAN_STATUSES`` is every one except cancelled), or ``None`` for all.
        """
        with self.session() as s:
            stmt = select(PlannedObservation).order_by(PlannedObservation.window_start)
            if isinstance(status, str):
                stmt = stmt.where(PlannedObservation.status == status)
            elif status is not None:
                stmt = stmt.where(PlannedObservation.status.in_(list(status)))
            if plugin_name is not None:
                stmt = stmt.where(PlannedObservation.plugin_name == plugin_name)
            return [p.to_dict() for p in s.scalars(stmt)]

    def get_planned(self, planned_id: int) -> dict | None:
        with self.session() as s:
            row = s.get(PlannedObservation, planned_id)
            return row.to_dict() if row else None

    def planned_for_mission(self, mission_id: str) -> dict | None:
        """The plan a mission was built from, if any."""
        with self.session() as s:
            row = s.scalar(select(PlannedObservation).where(PlannedObservation.mission_id == mission_id))
            return row.to_dict() if row else None

    def update_planned_target(self, planned_id: int, target: dict) -> bool:
        """Replace a still-``PLANNED`` plan's target (and the window columns) with a refreshed copy."""
        with self.session() as s:
            row = s.get(PlannedObservation, planned_id)
            if row is None or row.status != "PLANNED":
                return False
            row.target = target
            row.window_start, row.window_end = _utc_naive(target["window_start"]), _utc_naive(target["window_end"])
            return True

    def start_planned(self, planned_id: int, mission_id: str) -> bool:
        """``PLANNED`` -> ``STARTED``, linking the mission.  Returns whether the plan was there to start."""
        with self.session() as s:
            row = s.get(PlannedObservation, planned_id)
            if row is None or row.status != "PLANNED":
                return False
            row.status, row.mission_id, row.outcome = "STARTED", mission_id, None
            return True

    def finish_planned(self, planned_id: int, status: str, outcome: str) -> bool:
        """``STARTED`` -> ``DONE`` or ``MISSED``, with a sentence saying how it ended."""
        if status not in ("DONE", "MISSED"):
            raise ValueError("a started plan finishes as DONE or MISSED")
        with self.session() as s:
            row = s.get(PlannedObservation, planned_id)
            if row is None or row.status != "STARTED":
                return False
            row.status, row.outcome = status, outcome
            return True

    def miss_planned(self, planned_id: int, outcome: str) -> bool:
        """``PLANNED`` -> ``MISSED`` without a mission (the window is no longer reachable).  Returns whether it was ``PLANNED``."""
        with self.session() as s:
            row = s.get(PlannedObservation, planned_id)
            if row is None or row.status != "PLANNED":
                return False
            row.status, row.outcome = "MISSED", outcome
            return True

    def expire_planned(self, now: datetime) -> int:
        """Mark every ``PLANNED`` record whose window ended before ``now`` as ``MISSED``.  Returns how many."""
        cutoff = _utc_naive(now)
        with self.session() as s:
            rows = list(s.scalars(select(PlannedObservation).where(
                PlannedObservation.status == "PLANNED", PlannedObservation.window_end < cutoff,
            )))
            for row in rows:
                row.status, row.outcome = "MISSED", "The window passed before the observation was started."
            return len(rows)

    def cancel_planned(self, planned_id: int) -> bool:
        """Cancel a planned observation.  Returns whether there was a ``PLANNED`` one to cancel."""
        with self.session() as s:
            row = s.get(PlannedObservation, planned_id)
            if row is None or row.status != "PLANNED":
                return False
            row.status = "CANCELLED"
            return True

    # ------------------------------------------------------------------ #
    # Observations
    # ------------------------------------------------------------------ #
    def create_observation(self, mission_id: str, target_id: int | None, **fields: Any) -> int:
        with self.session() as s:
            row = Observation(mission_id=mission_id, target_id=target_id, **fields)
            s.add(row)
            s.flush()
            return row.id

    def discard_incomplete_observations(self, mission_id: str, target_id: int | None) -> int:
        """Delete unfinished rows left by a crash so a resumed node doesn't create duplicates."""
        with self.session() as s:
            stmt = select(Observation).where(
                Observation.mission_id == mission_id,
                Observation.target_id == target_id,
                Observation.ended_at.is_(None),
            )
            rows = list(s.scalars(stmt))
            for row in rows:
                s.delete(row)
            return len(rows)

    def update_observation(self, observation_id: int, **fields: Any) -> None:
        with self.session() as s:
            row = s.get(Observation, observation_id)
            if row is not None:
                for key, value in fields.items():
                    setattr(row, key, value)

    def list_observations(self, mission_id: str) -> list[dict]:
        with self.session() as s:
            stmt = select(Observation).where(Observation.mission_id == mission_id).order_by(Observation.id)
            return [o.to_dict() for o in s.scalars(stmt)]

    # ------------------------------------------------------------------ #
    # Weather
    # ------------------------------------------------------------------ #
    def add_weather(self, mission_id: str, report: dict, clear: bool, reason: str | None) -> None:
        with self.session() as s:
            s.add(
                WeatherReading(
                    mission_id=mission_id,
                    cloud_cover_pct=report.get("cloud_cover_pct"),
                    seeing_arcsec=report.get("seeing_arcsec"),
                    wind_kmh=report.get("wind_kmh"),
                    source=str(report.get("source", "")),
                    clear=clear,
                    reason=reason,
                    raw=report,
                )
            )

    def list_weather(self, mission_id: str, limit: int = 200) -> list[dict]:
        with self.session() as s:
            stmt = (
                select(WeatherReading)
                .where(WeatherReading.mission_id == mission_id)
                .order_by(WeatherReading.id.desc())
                .limit(limit)
            )
            return [w.to_dict() for w in reversed(list(s.scalars(stmt)))]

    # ------------------------------------------------------------------ #
    # Logs / telemetry
    # ------------------------------------------------------------------ #
    def add_log(
        self,
        mission_id: str,
        message: str,
        *,
        node: str | None = None,
        level: str = "INFO",
        kind: str = "log",
    ) -> None:
        try:
            with self.session() as s:
                s.add(LogEvent(mission_id=mission_id, message=message, node=node, level=level, kind=kind))
        except Exception:  # logging must never take the mission down
            log.debug("failed to persist log event", exc_info=True)

    def list_logs(
        self,
        mission_id: str,
        limit: int = 300,
        kinds: Iterable[str] | None = None,
    ) -> list[dict]:
        with self.session() as s:
            stmt = select(LogEvent).where(LogEvent.mission_id == mission_id)
            if kinds is not None:
                stmt = stmt.where(LogEvent.kind.in_(list(kinds)))
            stmt = stmt.order_by(LogEvent.id.desc()).limit(limit)
            return [e.to_dict() for e in reversed(list(s.scalars(stmt)))]

    # ------------------------------------------------------------------ #
    # LLM ledger
    # ------------------------------------------------------------------ #
    def add_llm_call(self, mission_id: str, **fields: Any) -> int:
        fields.pop("mission_id", None)
        with self.session() as s:
            row = LLMCall(mission_id=mission_id, **fields)
            s.add(row)
            s.flush()
            return row.id

    def list_llm_calls(self, mission_id: str, include_text: bool = True) -> list[dict]:
        with self.session() as s:
            stmt = select(LLMCall).where(LLMCall.mission_id == mission_id).order_by(LLMCall.id)
            rows = [c.to_dict() for c in s.scalars(stmt)]
        if not include_text:
            for r in rows:
                r.pop("prompt", None)
                r.pop("response", None)
        return rows

    def llm_usage(self, mission_id: str) -> dict:
        return summarise_llm_calls(self.list_llm_calls(mission_id, include_text=False))

    # ------------------------------------------------------------------ #
    # Artifacts (upload files and the mission log)
    # ------------------------------------------------------------------ #
    def replace_artifacts(self, mission_id: str, kind: str, rows: list[dict]) -> list[int]:
        """Swap every ``kind`` artifact of a mission for ``rows``, so a node that re-runs after a crash never duplicates them.

        Each row has ``title`` and optionally ``path``, ``status`` and ``meta``.
        """
        with self.session() as s:
            for old in s.scalars(select(Artifact).where(Artifact.mission_id == mission_id, Artifact.kind == kind)):
                s.delete(old)
            ids = []
            for row in rows:
                art = Artifact(
                    mission_id=mission_id, kind=kind, title=row["title"], path=row.get("path"),
                    status=row.get("status"), meta=row.get("meta"),
                )
                s.add(art)
                s.flush()
                ids.append(art.id)
            return ids

    def list_artifacts(self, mission_id: str | None = None, kind: str | None = None) -> list[dict]:
        with self.session() as s:
            stmt = select(Artifact).order_by(Artifact.created_at.asc(), Artifact.id.asc())
            if mission_id is not None:
                stmt = stmt.where(Artifact.mission_id == mission_id)
            if kind is not None:
                stmt = stmt.where(Artifact.kind == kind)
            return [a.to_dict() for a in s.scalars(stmt)]
