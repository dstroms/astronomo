"""SQLAlchemy ORM models for Astronomo state.

LangGraph's own checkpoints live in a *separate* SQLite file (see ``main.py``); the
tables here are the human/dashboard-facing record of what the mission did.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for column in self.__table__.columns:  # type: ignore[attr-defined]
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                # SQLite drops tzinfo; everything we store is UTC.
                value = value.replace(tzinfo=timezone.utc).isoformat()
            out[column.name] = value
        return out


class Mission(Base):
    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plugin_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="INIT", index=True)
    current_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # -- long-cadence / multi-mission bookkeeping (added later: see Database._migrate) -------- #
    #: "telescope" or "archive".
    data_source: Mapped[str] = mapped_column(String(16), default="telescope")
    #: The survey instant the next visit is due (set while WAITING; survey time, not wall time).
    wake_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: The mission clock now (equals wall time for telescope missions, survey time for replays).
    sim_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Persisted clock state (see core/clock.py) so a resumed process continues the same clock.
    clock: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Live "what is it doing right now": phase, visit i/n, frames done/total, a one-line detail.
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Ownership lease: the process that heartbeats this mission.  Stale heartbeat => orphaned.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_host: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Set by ``python main.py --stop``. The running mission parks and will not resume.
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    targets: Mapped[list["Target"]] = relationship(back_populates="mission", cascade="all, delete-orphan")
    observations: Mapped[list["Observation"]] = relationship(back_populates="mission", cascade="all, delete-orphan")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="mission", cascade="all, delete-orphan")
    logs: Mapped[list["LogEvent"]] = relationship(back_populates="mission", cascade="all, delete-orphan")
    weather: Mapped[list["WeatherReading"]] = relationship(back_populates="mission", cascade="all, delete-orphan")
    llm_calls: Mapped[list["LLMCall"]] = relationship(back_populates="mission", cascade="all, delete-orphan")


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(128))
    ra_deg: Mapped[float] = mapped_column(Float)
    dec_deg: Mapped[float] = mapped_column(Float)
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")  # PENDING/OBSERVED/SKIPPED
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # full plugin target dict
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    mission: Mapped[Mission] = relationship(back_populates="targets")


class PlannedObservation(Base):
    """An observation a person chose from the dashboard's Opportunities page.

    It is a *note of intent*: creating one starts nothing.  ``target`` is the full target dict the plugin would
    have produced for that window (``BaseExperimentPlugin.planned_target``), so a mission can be built from it
    without asking the plugin again (``python main.py --from-plan ID``, a person's decision).

    Life cycle (``status``)::

        PLANNED --start--> STARTED --> DONE      the mission observed it
           |                  +-----> MISSED     the mission did not (window closed, weather, a failure)
           +--window passed unstarted-> MISSED
           +--cancel-------> CANCELLED

    ``mission_id`` links the mission that was built from it; ``outcome`` says in a sentence how it ended.

    A window can be planned again after its plan is ``DONE`` or ``MISSED``. That is a new row and a new
    mission; the earlier mission stays on the earlier row. A window has at most one plan that is still
    ``PLANNED`` or ``STARTED``. A cancelled plan is revived instead of copied.
    """

    __tablename__ = "planned_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_name: Mapped[str] = mapped_column(String(64), index=True)
    target_name: Mapped[str] = mapped_column(String(128))
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="PLANNED", index=True)
    target: Mapped[dict] = mapped_column(JSON)
    network: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    impact: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exposure_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fits_paths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    weather: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    findings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    crossref: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    mission: Mapped[Mission] = relationship(back_populates="observations")


class Artifact(Base):
    """A file a mission produced for a person: an upload file for a network's database, or the mission log.

    ``status`` is the
    submission status from ``core/submission.py`` (``ready_for_review``, ``draft``, ``simulated`` ...)
    and ``meta`` carries the destination, format, target, blockers and checksum.
    """

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # "submission" | "mission_log"
    title: Mapped[str] = mapped_column(String(512))
    path: Mapped[str | None] = mapped_column(Text, nullable=True)  # None for a held-back submission
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    mission: Mapped[Mission] = relationship(back_populates="artifacts")


class WeatherReading(Base):
    __tablename__ = "weather_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cloud_cover_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    seeing_arcsec: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="")
    clear: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    mission: Mapped[Mission] = relationship(back_populates="weather")


class LLMCall(Base):
    """Ledger of every language-model request: what was sent, what came back, what it cost.

    Stores the exact prompt (bounded by ``LLM_MAX_INPUT_CHARS``) so "was any raw data sent?" is
    auditable after the fact.  Cache hits are recorded with ``cached=True`` and zero tokens.
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    response_chars: Mapped[int] = mapped_column(Integer, default=0)
    latency_s: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)  # token counts were estimated
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str] = mapped_column(Text, default="")

    mission: Mapped[Mission] = relationship(back_populates="llm_calls")


class LogEvent(Base):
    """Telemetry stream: free-form logs plus node start/end/error markers.

    ``kind`` is one of ``log``, ``node_start``, ``node_end``, ``node_error``.
    """

    __tablename__ = "log_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    kind: Mapped[str] = mapped_column(String(16), default="log")
    node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")

    mission: Mapped[Mission] = relationship(back_populates="logs")
