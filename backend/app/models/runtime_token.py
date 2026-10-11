"""Box token ledger — what a local inference box itself counted, per hour.

Insights used to see local tokens only through MC's own transcripts
(``model_usage_events``). Requests sent straight to a box's API from another
machine never show up there. The engine's Prometheus counters do see them, so
the runtime pulse (services/runtime_pulse.py) also books the counter deltas
here (services/runtime_token_ledger.py):

  runtime_token_hours     one row per host × engine × UTC hour: prompt,
                          cached and generated token DELTAS.
  runtime_token_counters  the last counter values booked per host × engine,
                          so a backend restart neither double counts nor
                          loses tokens.

``host_id`` is a plain UUID, not a foreign key: removing a host must not erase
the history of what its box served.
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, UniqueConstraint, text
from sqlmodel import Column, Field, SQLModel


class RuntimeTokenHour(SQLModel, table=True):
    __tablename__ = "runtime_token_hours"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    host_id: uuid.UUID = Field(index=True)
    engine: str = Field(max_length=32)
    hour: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))
    prompt_tokens: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    cached_tokens: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    generated_tokens: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    )

    __table_args__ = (
        UniqueConstraint("host_id", "engine", "hour", name="uq_runtime_token_hours_host_engine_hour"),
    )


class RuntimeTokenCounter(SQLModel, table=True):
    __tablename__ = "runtime_token_counters"

    host_id: uuid.UUID = Field(primary_key=True)
    engine: str = Field(primary_key=True, max_length=32)
    # Last booked raw counter values; NULL = the engine does not expose it.
    last_prompt: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    last_cached: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    last_generated: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    # When this box was first seen: counting starts here, nothing before.
    first_seen_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
