"""Model Usage Events — Token/Kosten-Tracking pro Modell × Agent × Harness.

Ersetzt das Gateway-era CostEvent-Schema fuer neue Writes.
CostEvent bleibt fuer Budget-Warnungen erhalten (check_budget_warnings liest davon).

Datenquelle: JSONL-Transkripte die Claude Code / openclaude schreiben.
Dedup-Key: top-level `uuid` (UNIQUE) — NIEMALS message.id (hat Kollisionen).
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, text
from sqlmodel import Column, Field, SQLModel


class ModelUsageEvent(SQLModel, table=True):
    """Eine assistant-Zeile aus einem JSONL-Transkript.

    Ein Row = eine Message (uuid UNIQUE). Idempotent: kann beliebig oft
    ueber dieselben Dateien laufen ohne Duplikate.
    """

    __tablename__ = "model_usage_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Attribution
    agent_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="agents.id",
        nullable=True,
        index=True,
    )
    task_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="tasks.id",
        nullable=True,
        index=True,
    )
    harness: str = Field(index=True)
    # "cli-bridge" | "host" | "sparky" | "backend-ollama"

    # Modell
    model: str = Field(index=True)
    provider: str | None = Field(default=None, nullable=True)

    # Session / Message Identifiers
    session_id: str = Field(index=True)
    message_uuid: str = Field(unique=True)  # top-level `uuid` aus JSONL

    # Token-Counts
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cache_read_tokens: int = Field(default=0)   # cache_read_input_tokens
    cache_write_tokens: int = Field(default=0)  # cache_creation_input_tokens (5m+1h)

    # Berechnete Kosten (aus model_prices bei Insert)
    cost_usd: float | None = Field(default=None, nullable=True)

    # Zeitstempel
    ts: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    harvested_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
        )
    )

    # Provenance
    source_file: str = Field()  # JSONL-Pfad fuer Debug + Re-Harvest-Skip

    __table_args__ = (
        Index("ix_model_usage_model_ts", "model", "ts"),
        Index("ix_model_usage_agent_ts", "agent_id", "ts"),
        Index("ix_model_usage_harness_ts", "harness", "ts"),
    )


class ModelPrice(SQLModel, table=True):
    """Editierbare Preistabelle fuer Modell-Kosten.

    Pattern-Matching via fnmatch-Glob. Spezifischere Patterns (hoehere priority)
    gewinnen. valid_from ermoeglicht Preishistorie und rueckwirkende Neuberechnung.
    """

    __tablename__ = "model_prices"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    model_pattern: str = Field(index=True)  # exakt oder glob (claude-opus-4-*, qwen*)
    input_per_mtok: float = Field(default=0.0)   # USD / 1M Input-Tokens
    output_per_mtok: float = Field(default=0.0)  # USD / 1M Output-Tokens
    cache_read_per_mtok: float = Field(default=0.0)   # ca. 0.1x Input
    cache_write_per_mtok: float = Field(default=0.0)  # ca. 1.25x Input

    currency: str = Field(default="USD")
    valid_from: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    priority: int = Field(default=0, index=True)
    # Hoehere priority = spezifischeres Pattern gewinnt beim Matching.
    # Fallback `*` hat priority=0 (niedrigst).

    note: str | None = Field(default=None, nullable=True)


class ModelUsageHarvestState(SQLModel, table=True):
    """Persistenter Offset-State pro JSONL-Datei (Inkremental-Harvesting).

    file_path = PK. Append-only JSONL: processed_lines Zeilen wurden bereits
    gelesen. Naechster Lauf beginnt ab dieser Zeilennummer.
    mtime-Skip: wenn mtime unveraendert → Datei wird komplett uebersprungen.
    """

    __tablename__ = "model_usage_harvest_state"

    file_path: str = Field(primary_key=True)
    mtime: float = Field(default=0.0)
    processed_lines: int = Field(default=0)
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
        )
    )
