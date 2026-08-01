"""ReferenceFile — hochgeladene Referenz-/Asset-Dateien für Tasks & Projekte (ADR-053).

Der Operator lädt Beispiel-Dateien hoch (Layout-Screenshot, Beispiel-CSV,
Spezifikations-PDF …); Agenten lesen sie direkt vom gemounteten ~/.mc-Pfad
(Backend- und Agent-Container mounten ${HOME}/.mc 1:1 — gleiche absolute
Pfade). Die Dispatch-Directive listet die Pfade auf.

Genau EINES von task_id/project_id/agent_id ist gesetzt. Projekt-Referenzen
gelten für alle Tasks des Projekts (Vererbung im Dispatch-Kontext).
Agent-Referenzen (Migration 0172, Slack-Datei-Ingest) hängen an keinem Task:
der Operator schickt eine Datei top-level im Chat — sie gehört dem Agenten
(Boss), nicht einer Aufgabe. board_id ist seither nullable, weil ein Agent keinem
Board angehören muss.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Uuid, text
from sqlmodel import Column, Field, SQLModel


class ReferenceFile(SQLModel, table=True):
    __tablename__ = "reference_files"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID | None = Field(
        default=None, foreign_key="boards.id", nullable=True, index=True
    )
    task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True, index=True
    )
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True, index=True
    )
    # ondelete=SET NULL: eine Referenz darf das Löschen ihres Agenten nie
    # blockieren (delete_agent hatte schon FK-Lücken — hier keine neue). Die
    # verwaiste Row räumt reference_cleanup ab.
    agent_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid,
            ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
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
