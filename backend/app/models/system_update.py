"""Self-update state in Postgres (ADR-094 decisions 3 and 7).

``system_state`` — small key/value rows the update engine and the backend
share. The engine writes them with ``psql`` from the host
(``scripts/mc_update.py``), so the shape is a contract:

* ``key = 'update_drain'``, ``value = {"run_id", "expires_at", "set_by"}`` —
  the drain: while a row with an ``expires_at`` in the future exists, MC
  hands out no new work (dispatch, group runner, loop runner, scheduler) and
  the Docker gateway refuses container changes. Postgres, not Redis: the
  drain travels with the data the pre-update backup holds, and a ``FLUSHALL``
  must not lift it.
* ``key = 'deferred:<kind>:<id>'`` — work the drain held back (a scheduled
  job that came due, a task dispatch) and that is caught up once the drain is
  gone. One row per item: inserting and consuming stay atomic without
  merging JSON lists.

``system_updates`` — the history the UI shows (Settings → System → history).
The journal on the host stays the truth about a run; the backend mirrors it
here whenever it reads the run's status through the bridge, plus who started
it from the UI. One row per engine run id.
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.utils import utcnow

# jsonb in Postgres (the engine writes ``'…'::jsonb`` and reads
# ``value->>'run_id'``), plain JSON in the SQLite test lane.
_JSON = JSON().with_variant(JSONB(), "postgresql")

DRAIN_KEY = "update_drain"
DEFERRED_PREFIX = "deferred:"


class SystemState(SQLModel, table=True):
    __tablename__ = "system_state"

    key: str = Field(primary_key=True, max_length=120)
    value: dict = Field(default_factory=dict, sa_column=Column(_JSON, nullable=False))
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )


class SystemUpdate(SQLModel, table=True):
    __tablename__ = "system_updates"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # The engine's run id (``YYYYMMDDTHHMMSSZ-xxxxxx``), unique per run.
    run_id: str = Field(max_length=40, unique=True, index=True)
    # ui = started from Settings; bridge/terminal = what the journal says.
    source: str = Field(default="ui", max_length=16)
    from_version: str | None = Field(default=None, max_length=40)
    target_version: str | None = Field(default=None, max_length=40)
    # Last phase seen in the journal (preflight … done | aborted |
    # rolled_back | rollback_failed) and its state inside the phase.
    phase: str | None = Field(default=None, max_length=24)
    phase_state: str | None = Field(default=None, max_length=24)
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    failure_kind: str | None = Field(default=None, max_length=32)
    # File name of the pre-update backup (never a path).
    backup: str | None = Field(default=None, max_length=200)
    # Who pressed the button. No FK: the history outlives a deleted account.
    started_by_user_id: uuid.UUID | None = Field(default=None)
    started_by_name: str | None = Field(default=None, max_length=200)
    rollback_requested_by_user_id: uuid.UUID | None = Field(default=None)
    rollback_requested_by_name: str | None = Field(default=None, max_length=200)
    rollback_requested_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # When the bridge's start routine lifted a drain this run left behind.
    drain_lifted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
