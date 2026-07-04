"""Repo — first-class GitHub repository registry (ADR-050).

Historically a repo was just two strings on Project (github_repo_url/name).
This model makes repos manageable: one row per GitHub repo, shareable by
multiple projects, carrying per-repo working rules (rules_md) that are
injected into every dispatch directive for tasks working in that repo.

The legacy Project.github_repo_url/github_repo_name fields stay populated
(synced on link) so all existing clone/PR/merge flows keep working unchanged.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class Repo(SQLModel, table=True):
    __tablename__ = "repos"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    full_name: str = Field(index=True, unique=True)  # "owner/name"
    url: str  # https://github.com/owner/name
    default_branch: str = "main"
    description: str | None = None
    rules_md: str | None = None  # Arbeitsregeln — injected into dispatch directives
    visibility: str = "private"  # private|public (informational, from GitHub)
    is_active: bool = True  # False = archived in MC (hidden from pickers)
    source: str = "mc"  # mc (created by MC) | imported (existing GitHub repo)
    last_synced_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )
