"""RuntimeHost — membership of a host in a multi-box runtime ("Verbund").

One row per member box: the head is rank 0 (role ``head``, the same box as
``runtimes.host_id``), workers are rank 1..n-1 (role ``worker``). A solo
runtime has ZERO rows — nothing about the single-host path reads this table.

This table is the only truth for which boxes a multi-box runtime uses
(ADR-096 §1). ``runtimes.topology`` keeps only the count (``nodes``) and the
recipe; the old ``topology.worker_host_id`` is no longer written and is
derived from rank 1 in API responses.

Exactly one module writes this table: ``app.services.runtime_membership``
(``set_members`` / ``clear_members``). An architecture test rejects any other
writer. Readers that need the head should still use ``runtimes.host_id``.
"""
import uuid
from datetime import datetime
from app.utils import utcnow

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, text
from sqlmodel import Column, Field, SQLModel

RUNTIME_HOST_ROLES = ("head", "worker")


class RuntimeHost(SQLModel, table=True):
    __tablename__ = "runtime_hosts"
    __table_args__ = (
        # A host can only be a member of a given runtime once.
        UniqueConstraint("runtime_id", "host_id", name="uq_runtime_hosts_runtime_host"),
        # Two hosts can never claim the same rank within one runtime — ranks
        # are how the underlying engine (e.g. torchrun/TP) addresses nodes.
        UniqueConstraint("runtime_id", "node_rank", name="uq_runtime_hosts_runtime_rank"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # CASCADE both ways: a membership row without its runtime or its host is
    # meaningless — deleting either side cleans up the membership for free,
    # the same reasoning as GroupMember (models/group.py).
    runtime_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("runtimes.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    host_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    # "head" | "worker" — the runtime's own host_id is ALWAYS the head; a row
    # here with role="head" duplicating that is not expected but not
    # prevented at the DB level either (no CHECK constraint yet — v1 keeps
    # this a plain string like Runtime.runtime_type does).
    role: str = Field(max_length=16)
    # Node rank the underlying engine uses to address this host (e.g. the
    # torchrun/TP rank). 0 is conventionally the head, but this table does
    # not enforce that — it only guarantees ranks are unique per runtime.
    node_rank: int
    # Per-member endpoint override, for a worker whose own API port differs
    # from the runtime's main `endpoint` (headless workers usually have
    # none at all — nullable covers that, the common case).
    endpoint_override: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), nullable=False),
    )
