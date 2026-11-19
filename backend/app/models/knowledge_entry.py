"""Index of the knowledge root (ADR-104, first slice of "knowledge in one place").

The files are the truth. Each row only says "this Markdown file exists, it
belongs to this drawer (and project), here is its title, its tags and the text
to search" — enough for lists and full-text search without reading every file.
The body is always read from disk. The table can be emptied and rebuilt from
the files at any time (``python -m app.services.knowledge_import``).

``source`` names the folder a file lives in:

- ``knowledge`` — the new root ``~/.mc/knowledge`` (``notes/``, ``skills/``,
  ``projects/<slug>/``). MC writes there: project folders and the README.
- ``vault`` / ``skills`` — the existing vault and skills folders, indexed in
  place and only read (slice 1 moves nothing).

Everything is behind ``settings.knowledge_root_enabled`` (default off).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Index, Text, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from app.utils import utcnow


class KnowledgeEntry(SQLModel, table=True):
    __tablename__ = "knowledge_entries"
    __table_args__ = (
        UniqueConstraint("source", "rel_path", name="uq_knowledge_entries_path"),
        Index("ix_knowledge_entries_drawer_project", "drawer", "project_slug"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    #: knowledge | vault | skills
    source: str = Field(max_length=20)
    #: Path relative to the source folder, always with "/".
    rel_path: str = Field(sa_column=Column(Text, nullable=False))
    #: notes | skills | projects
    drawer: str = Field(max_length=20)
    project_slug: str | None = Field(default=None, max_length=120)
    #: Frontmatter ``project`` as written in the file. Kept so a project that
    #: appears later can claim an unchanged file without reading it again.
    project_hint: str | None = Field(default=None, max_length=120)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)
    #: Frontmatter ``type``, or ``project`` for PROJECT.md, ``skill`` for SKILL.md.
    kind: str = Field(default="note", max_length=40)
    title: str = Field(default="", max_length=300)
    tags: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, server_default=text("'[]'")))
    size_bytes: int = 0
    #: File mtime at the last read — with size_bytes the "unchanged?" check.
    mtime: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    content_hash: str = Field(default="", max_length=64)
    #: Title + tags + body, capped (services/knowledge.SEARCH_TEXT_CAP).
    search_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    #: Why this file is not shown where its path says, e.g. ``project_mismatch``
    #: (frontmatter ``project`` differs from its folder). NULL = fine.
    problem: str | None = Field(default=None, max_length=60)
    indexed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
