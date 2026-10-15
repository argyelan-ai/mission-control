"""VoiceLiveCall — one live voice call and what it cost (voice PR 6, ADR-091 draft).

A row is written when MC mints the call's client secret (``POST
/voice/live/session``) and updated while the call runs: the browser reads the
token usage OpenAI reports in every ``response.done`` on the data channel and
posts the call's running totals (``POST /voice/live/usage``). MC computes the
cost from them with the prices in settings (``voice_live_price_*``) — never
the browser.

What the row is for: the daily budget (sum of today's cost, Europe/Zurich
midnight), the Insights "Voice" row, and the measured $/min the ADR asks for.

``ref_hash`` is the SHA-256 of the call's ``call_ref`` (the browser's handle
for this call): MC finds the row from the handle without storing the handle
itself. ``openai_call_id`` is OpenAI's id for the WebRTC call when the browser
could read it — then MC can hang up server-side at max length or budget.

``agent_id`` is nullable: deleting an agent sets it to NULL (the schema
sweep in ``routers/agents.py``), so cost history outlives the agent;
``agent_name`` keeps who it was.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, text
from sqlmodel import Column, Field, SQLModel

from app.utils import utcnow


class VoiceLiveCall(SQLModel, table=True):
    __tablename__ = "voice_live_calls"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    ref_hash: str = Field(max_length=64, unique=True, index=True)
    agent_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(ForeignKey("agents.id"), nullable=True, index=True),
    )
    agent_name: str = Field(default="", max_length=128)
    model: str = Field(default="", max_length=64)
    openai_call_id: Optional[str] = Field(default=None, max_length=160)
    started_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, index=True, server_default=text("CURRENT_TIMESTAMP")
        ),
    )
    #: Last usage report (or the start): the call ran at least until here.
    last_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
    ended_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    end_reason: Optional[str] = Field(default=None, max_length=32)
    duration_s: int = Field(default=0)
    input_text_tokens: int = Field(default=0)
    input_audio_tokens: int = Field(default=0)
    cached_text_tokens: int = Field(default=0)
    cached_audio_tokens: int = Field(default=0)
    output_text_tokens: int = Field(default=0)
    output_audio_tokens: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
