"""RuntimeHost — membership of a host in a multi-node runtime "Verbund"
(Fleet & Rezepte v2, Verbund-UI Phase 1b).

A `Runtime` row's own `host_id` stays the HEAD of the verbund (unchanged,
untouched by this table — host_resolver keeps resolving exactly as before).
This table only records the ADDITIONAL member hosts of a multi-node runtime
(e.g. a 2-node GLM TP=2 verbund: alpha as head/rank0 already IS
runtimes.host_id, Beta as a worker/rank1 gets one row here). A solo runtime
(the overwhelming majority today) has ZERO rows in this table — that is the
whole point: nothing about the existing single-host path changes, and
nothing here is read unless a runtime actually has member hosts.

This is deliberately data-only for now (Phase 1b) — no orchestration, no
health rollup across nodes, no "start the whole verbund" button. It exists
so the UI can say "this box is part of <runtime>, role=worker" instead of
inventing a guess, and so a future phase has a real place to hang multi-
host orchestration off of without another migration.
"""
import uuid
from datetime import datetime

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
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), nullable=False),
    )
