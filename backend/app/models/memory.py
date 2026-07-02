import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class BoardMemory(SQLModel, table=True):
    __tablename__ = "board_memory"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID | None = Field(default=None, foreign_key="boards.id", index=True)
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id", index=True)
    title: str | None = None
    content: str
    tags: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    source: str  # agent name or 'user' or 'system'
    memory_type: str = "knowledge"  # knowledge | lesson | reference | journal | weekly_review | research | insight
    is_pinned: bool = False
    auto_generated: bool = False
    linked_ids: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    # Phase 5 MSY-02: SHA-256 of normalized title+content for hash-dedup
    content_hash: str | None = Field(default=None, index=True)
    # Phase 5 MSY-02: FK self-reference for cosine-≥-0.9 MERGE-candidate flagging
    merge_candidate_id: uuid.UUID | None = Field(
        default=None, foreign_key="board_memory.id", index=True,
    )
    # Phase 5 MSY-03: JSON array of {path, mime_type, size_bytes, original_name}
    # Pitfall 5: default=None (NOT default_factory=list) — None means "not set",
    # distinct from "explicit empty list".
    attachments: list[Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True),
    )
    # Vault Cleanup Programme — soft-archive fields (migration 0113)
    archived_at: datetime | None = Field(default=None, index=True)
    archive_reason: str | None = Field(default=None, max_length=64)
    archive_bucket: str | None = Field(default=None, max_length=8)

    # Memory Next-Level (migration 0126)
    status: str = Field(default="published", max_length=16)
    confidence: str = Field(default="medium", max_length=8)
    updated_at_content: datetime | None = Field(default=None)
    last_viewed_at: datetime | None = Field(default=None)
    contradiction_ids: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, server_default="[]"),
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            onupdate=datetime.utcnow,
        ),
    )
