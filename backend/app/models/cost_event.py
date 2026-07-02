"""Cost Events — Token-/Kosten-Tracking pro Task und Agent.

Jeder Eintrag ist ein Snapshot einer Gateway-Session zu einem Zeitpunkt.
Delta-Berechnung: neuer Snapshot minus letzter Snapshot = neue Tokens seit letzter Messung.
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

    # Session-Info
    session_key: str  # z.B. "agent:cody:task:abc123:work"
    event_type: str = Field(default="session_snapshot")  # session_snapshot | manual

    # Token-Counts (Delta seit letztem Snapshot)
    tokens_in: int = 0
    tokens_out: int = 0

    # Modell
    provider: str | None = None  # openai-codex, lmstudio-spark, ollama-cloud
    model: str | None = None  # gpt-5.4, nemotron-3-super, glm-5

    # Geschaetzte Kosten (USD, kann null sein wenn Modell unbekannt)
    cost_usd: float | None = None

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"))
    )


# ── Einfache Modellpreise (USD pro 1M Tokens) ────────────────────────────
# Quelle: Offizielle Preislisten, Stand Maerz 2026
# Nur fuer bekannte Modelle. Unbekannte → cost_usd = None
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # (input_per_1m, output_per_1m)
    "gpt-5.4": (2.50, 10.00),
    "gpt-5.3-codex": (1.50, 6.00),
    "glm-5": (0.00, 0.00),  # Lokal/Cloud-gratis
    "nemotron-3-super": (0.00, 0.00),  # Lokal auf DGX Spark
}


def estimate_cost(model: str | None, tokens_in: int, tokens_out: int) -> float | None:
    """Schaetzt Kosten in USD basierend auf Modell und Token-Counts."""
    if not model:
        return None
    # Model-Name normalisieren (provider/ prefix entfernen)
    short = model.split("/")[-1] if "/" in model else model
    prices = MODEL_PRICES.get(short)
    if not prices:
        return None
    input_price, output_price = prices
    return (tokens_in * input_price / 1_000_000) + (tokens_out * output_price / 1_000_000)
