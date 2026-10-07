"""BrowserSession — one browser area per agent session or head run (ADR-088).

Every head run and every working phase of an agent gets its own browser
session in the shared agent browser (cdp-browser): its own address
`/s/<token>/` at cdp-gateway, and everything created over that address
(browser contexts, tabs, popups) belongs to the session. Ownership is set when
the session is opened — never guessed from traffic afterwards.

This row is MC's truth about a session; the gateway only keeps an in-memory
register that MC fills (and refills after a gateway restart). The token is
never stored: it is derived from the session id and the server secret
(`services/browser_sessions.session_token`), so a database dump hands out no
live browser address.

Owner: `owner_kind` "agent" (`agent_id`) or "head" (`head_run_id` — head runs
are folders on the host, not rows, so it is a plain string, no FK). Not a task
column: the task core is frozen (ADR-085 §4).

Status: "open" (address valid, nothing started in the browser yet) → "live"
(at least one tab; set by the lifecycle loop) → "ended".
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, text
from sqlmodel import Column, Field, SQLModel

from app.utils import utcnow

BROWSER_SESSION_OWNER_KINDS = ("agent", "head")
BROWSER_SESSION_STATUSES = ("open", "live", "ended")

_NOT_ENDED = "status <> 'ended'"


class BrowserSession(SQLModel, table=True):
    __tablename__ = "browser_sessions"
    __table_args__ = (
        CheckConstraint("owner_kind IN ('agent', 'head')", name="ck_browser_sessions_owner_kind"),
        CheckConstraint("status IN ('open', 'live', 'ended')", name="ck_browser_sessions_status"),
        # At most ONE not-ended session per head run and per agent: opening is
        # SELECT-then-INSERT, two starts at the same moment must not give one
        # owner two browser areas. Ended sessions don't count. Must stand
        # identically in migration 0211 — tests build tables from this model,
        # production from the migration (same convention as models/thread.py).
        Index(
            "uq_browser_sessions_open_head_run",
            "head_run_id",
            unique=True,
            sqlite_where=text(f"head_run_id IS NOT NULL AND {_NOT_ENDED}"),
            postgresql_where=text(f"head_run_id IS NOT NULL AND {_NOT_ENDED}"),
        ),
        Index(
            "uq_browser_sessions_open_agent",
            "agent_id",
            unique=True,
            sqlite_where=text(f"agent_id IS NOT NULL AND {_NOT_ENDED}"),
            postgresql_where=text(f"agent_id IS NOT NULL AND {_NOT_ENDED}"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_kind: str = Field(max_length=16)
    # SET NULL (agents are archived, not deleted — but a delete must never be
    # blocked by browser history).
    agent_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True),
    )
    head_run_id: Optional[str] = Field(default=None, max_length=64, index=True)
    status: str = Field(default="open", max_length=16, index=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
    ended_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    end_reason: Optional[str] = Field(default=None, max_length=64)
    # Lifecycle (migration 0212): first tab seen, last browser activity, and
    # the last image (file <browser_sessions_root>/<id>/last.jpg) with the
    # page it showed.
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    last_active_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    last_frame_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    last_url: Optional[str] = Field(default=None, max_length=2048)
    last_title: Optional[str] = Field(default=None, max_length=512)
