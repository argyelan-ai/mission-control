"""PromptTemplate — Prompt Library (Benchmark Studio Baustein 3, core).

Generic prompt storage, NOT challenge-bound (design 2026-07-11). The
bench_challenges table (PR 3, vertical) will reference rows here via
prompt_template_id and keep a frozen prompt_text copy, so templates stay
editable without falsifying history.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class PromptTemplate(SQLModel, table=True):
    __tablename__ = "prompt_templates"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: str = Field(max_length=200)
    body: str = Field(sa_column=Column(Text, nullable=False))
    tags: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default=text("'[]'")),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            onupdate=datetime.utcnow,
        ),
    )
