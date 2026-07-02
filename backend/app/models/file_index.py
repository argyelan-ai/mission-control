"""FileIndexEntry — the listing/search accelerator for the Files page.

One row per file/directory under a browsable ``~/.mc`` root. Populated two
ways: (1) capture-at-write when a deliverable is registered, (2) a periodic
background walk (``services.file_indexer``). This table is ONLY an accelerator
for listing/search — file *bytes* always stream live from disk via
``fs_service``, so the index can lag without ever serving stale content.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, text
from sqlmodel import Column, Field, SQLModel


class FileIndexEntry(SQLModel, table=True):
    __tablename__ = "file_index"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Location within a browsable root (fs_roots.FsRoot.key + path under it).
    root_key: str = Field(index=True)
    rel_path: str  # relative to the root, "" for the root itself
    name: str = Field(index=True)
    is_directory: bool = False
    size: int = 0
    mime: str | None = None
    mtime: float = 0.0

    # Provenance (nullable — walked entries that aren't deliverables have none).
    agent_slug: str | None = Field(default=None, index=True)
    runtime: str | None = None  # cli-bridge | host | claude-code | manual
    task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", index=True, nullable=True
    )
    deliverable_id: uuid.UUID | None = Field(
        default=None, foreign_key="task_deliverables.id", index=True, nullable=True
    )

    indexed_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )

    __table_args__ = (
        Index("ix_file_index_root_relpath", "root_key", "rel_path", unique=True),
    )
