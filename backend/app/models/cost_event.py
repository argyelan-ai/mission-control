"""Cost Events — token/cost tracking per task and agent.

Each entry is a snapshot of a gateway session at a point in time.
Delta calculation: new snapshot minus last snapshot = new tokens since last measurement.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, JSON, text
from sqlmodel import Column, Field, SQLModel


class CostEvent(SQLModel, table=True):
    __tablename__ = "cost_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agents.id", index=True)
    task_id: uuid.UUID | None = Field(default=None, foreign_key="tasks.id", nullable=True, index=True)

    # Session info
    session_key: str  # e.g. "agent:cody:task:abc123:work"
    event_type: str = Field(default="session_snapshot")  # session_snapshot | manual

    # Token counts (delta since last snapshot)
    tokens_in: int = 0
    tokens_out: int = 0

    # Model
    provider: str | None = None  # openai-codex, lmstudio-spark, ollama-cloud
    model: str | None = None  # gpt-5.4, nemotron-3-super, glm-5

    # Estimated cost (USD, can be null if model is unknown)
    cost_usd: float | None = None

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"))
    )


# ── Simple model prices (USD per 1M tokens) ──────────────────────────────
# Source: official price lists, as of March 2026
# Only for known models. Unknown → cost_usd = None
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # (input_per_1m, output_per_1m)
    "gpt-5.4": (2.50, 10.00),
    "gpt-5.3-codex": (1.50, 6.00),
    "glm-5": (0.00, 0.00),  # Local/cloud free
    "nemotron-3-super": (0.00, 0.00),  # Local on DGX Spark
}


def estimate_cost(model: str | None, tokens_in: int, tokens_out: int) -> float | None:
    """Estimates cost in USD based on model and token counts."""
    if not model:
        return None
    # Normalize model name (strip provider/ prefix)
    short = model.split("/")[-1] if "/" in model else model
    prices = MODEL_PRICES.get(short)
    if not prices:
        return None
    input_price, output_price = prices
    return (tokens_in * input_price / 1_000_000) + (tokens_out * output_price / 1_000_000)
