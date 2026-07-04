"""Model Usage Events — token/cost tracking per model × agent × harness.

Replaces the gateway-era CostEvent schema for new writes.
check_budget_warnings (cost_collector) reads from this table too; CostEvent
is retained only as a historical archive (last write 04/2026).

Data source: JSONL transcripts written by Claude Code / openclaude.
Dedup key: top-level `uuid` (UNIQUE) — NEVER message.id (has collisions).
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, text
from sqlmodel import Column, Field, SQLModel


class ModelUsageEvent(SQLModel, table=True):
    """An assistant line from a JSONL transcript.

    One row = one message (uuid UNIQUE). Idempotent: can run over the
    same files any number of times without duplicates.
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

    # Model
    model: str = Field(index=True)
    provider: str | None = Field(default=None, nullable=True)

    # Session / message identifiers
    session_id: str = Field(index=True)
    message_uuid: str = Field(unique=True)  # top-level `uuid` from JSONL

    # Token counts
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cache_read_tokens: int = Field(default=0)   # cache_read_input_tokens
    cache_write_tokens: int = Field(default=0)  # cache_creation_input_tokens (5m+1h)

    # Computed cost (from model_prices at insert time)
    cost_usd: float | None = Field(default=None, nullable=True)

    # Timestamps
    ts: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    harvested_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
        )
    )

    # Provenance
    source_file: str = Field()  # JSONL path for debug + re-harvest skip

    __table_args__ = (
        Index("ix_model_usage_model_ts", "model", "ts"),
        Index("ix_model_usage_agent_ts", "agent_id", "ts"),
        Index("ix_model_usage_harness_ts", "harness", "ts"),
    )


class ModelPrice(SQLModel, table=True):
    """Editable price table for model costs.

    Pattern matching via fnmatch glob. More specific patterns (higher priority)
    win. valid_from enables price history and retroactive recalculation.
    """

    __tablename__ = "model_prices"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    model_pattern: str = Field(index=True)  # exact or glob (claude-opus-4-*, qwen*)
    input_per_mtok: float = Field(default=0.0)   # USD / 1M input tokens
    output_per_mtok: float = Field(default=0.0)  # USD / 1M output tokens
    cache_read_per_mtok: float = Field(default=0.0)   # approx. 0.1x input
    cache_write_per_mtok: float = Field(default=0.0)  # approx. 1.25x input

    currency: str = Field(default="USD")
    valid_from: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    priority: int = Field(default=0, index=True)
    # Higher priority = more specific pattern wins when matching.
    # Fallback `*` has priority=0 (lowest).

    note: str | None = Field(default=None, nullable=True)


class ModelUsageHarvestState(SQLModel, table=True):
    """Persistent offset state per JSONL file (incremental harvesting).

    file_path = PK. Append-only JSONL: processed_lines lines have already
    been read. Next run starts from this line number.
    mtime skip: if mtime is unchanged → file is skipped entirely.
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
