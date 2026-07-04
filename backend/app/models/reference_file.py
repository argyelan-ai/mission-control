"""ReferenceFile — hochgeladene Referenz-/Asset-Dateien für Tasks & Projekte (ADR-053).

Der Operator lädt Beispiel-Dateien hoch (Layout-Screenshot, Beispiel-CSV,
Spezifikations-PDF …); Agenten lesen sie direkt vom gemounteten ~/.mc-Pfad
(Backend- und Agent-Container mounten ${HOME}/.mc 1:1 — gleiche absolute
Pfade). Die Dispatch-Directive listet die Pfade auf.

Genau EINES von task_id/project_id ist gesetzt. Projekt-Referenzen gelten
für alle Tasks des Projekts (Vererbung im Dispatch-Kontext).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class ReferenceFile(SQLModel, table=True):
    __tablename__ = "reference_files"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True, index=True
    )
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True, index=True
    )
    rel_path: str  # relativ zum Files-Root "references" (~/.mc/references/)
    original_name: str
    mime: str | None = None
    size: int = 0
    note: str | None = None  # optional: wofür ist die Datei gedacht
    uploaded_by: str = "user"
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
