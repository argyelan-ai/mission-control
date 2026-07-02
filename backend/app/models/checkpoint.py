"""Task Checkpoints — Agent-gespeicherte Zwischenstaende fuer Crash Recovery.

Minimales V1: Agent schreibt Checkpoint, Recovery liest ihn.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, JSON, text
from sqlmodel import Column, Field, SQLModel


class TaskCheckpoint(SQLModel, table=True):
    __tablename__ = "task_checkpoints"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    agent_id: uuid.UUID = Field(foreign_key="agents.id")

    # auto | manual (Agent entscheidet)
    checkpoint_type: str = Field(default="manual")

    # Knapper Arbeitsstand (Freitext, ~200 Zeichen)
    state_summary: str

    # Strukturierte Daten: erledigte Schritte, naechste Schritte, Artefakte
    context_data: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"))
    )
