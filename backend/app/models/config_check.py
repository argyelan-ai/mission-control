"""ConfigCheck — the latest result of one configuration check (Welle 2, PR 2-1).

One row per checked thing: ``kind`` names the registry entry
(``services/config_checks``) and ``subject_id`` the thing inside it (a secret
key, a host id, ``command_bot`` …). The row is overwritten by the next check
of the same thing, so its ``checked_at`` is the age the UI shows
("tested · 3 h ago"). No row means "never tested".

``value_fingerprint`` is a keyed hash of the value that was tested (never the
value itself). When the value is replaced later — through any path, the
secrets page, the GitHub form or ``.env`` — the fingerprint no longer
matches and the result reads "never tested" again instead of a stale green.

``message`` is a short, human sentence with secrets scrubbed out; it is safe
to show in the UI and in a support report.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, UniqueConstraint, text
from sqlmodel import Column, Field, SQLModel

from app.utils import utcnow


class ConfigCheck(SQLModel, table=True):
    __tablename__ = "config_checks"
    __table_args__ = (UniqueConstraint("kind", "subject_id", name="uq_config_checks_kind_subject"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    kind: str = Field(max_length=48)
    subject_id: str = Field(max_length=160)
    ok: bool = Field(default=False)
    message: str = Field(default="", max_length=300)
    duration_ms: int | None = Field(default=None)
    value_fingerprint: str | None = Field(default=None, max_length=64)
    checked_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
