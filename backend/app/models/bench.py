"""Benchmark Studio — production tracking tables.

Per ADR-044 §3 the models live in CORE (schema identical across variants,
stripped installations simply have idle tables). Only the bench_studio
vertical writes to them.

These tables track PRODUCTION only (generate -> render -> compose -> review).
The publish tail reuses the existing Approval(action_type="x_post") +
ContentPipeline lifecycles (ADR-065) — no second lifecycle.

FK policy:
  - bench_entries.challenge_id  -> CASCADE (entries are meaningless alone)
  - bench_entries.task_id       -> SET NULL (mc-task-delete-guard: bench
    history must survive task deletion; nullable FK -> no delete_task() block)
  - bench_entries.agent_id      -> SET NULL (agent removal keeps history)
  - bench_challenges.prompt_template_id / content_pipeline_id -> SET NULL
"""
import uuid
from datetime import datetime
from app.utils import utcnow
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Text, UniqueConstraint, Uuid, text
from sqlmodel import Column, Field, SQLModel


class BenchChallenge(SQLModel, table=True):
    __tablename__ = "bench_challenges"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: str
    prompt_template_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid, ForeignKey("prompt_templates.id", ondelete="SET NULL"), nullable=True
        ),
    )
    # Frozen copy — the template stays editable later without falsifying history.
    prompt_text: str = Field(sa_column=Column(Text, nullable=False))
    mode: str = Field(default="side_by_side")  # single | side_by_side
    # Video length in seconds (5..60), validated at the router — NULL falls
    # back to orchestrator.RECORD_DURATION_S (legacy 10s behaviour).
    record_duration_s: int | None = None
    # generating -> rendering -> composing -> review -> drafted -> published | failed
    status: str = Field(default="generating", index=True)
    series_label: str | None = None
    series_no: int | None = None  # auto-increment per series_label ("Spark Bench #7")
    composed_video_path: str | None = None
    content_pipeline_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid, ForeignKey("content_pipelines.id", ondelete="SET NULL"), nullable=True
        ),
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # Operator archive (soft-hide): list endpoint excludes archived challenges
    # by default. Only terminal/review states may be archived.
    archived_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            onupdate=utcnow,
        ),
    )


class BenchEntry(SQLModel, table=True):
    __tablename__ = "bench_entries"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    challenge_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid,
            ForeignKey("bench_challenges.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    model_label: str  # display label, also used for the artifact directory
    source_kind: str  # spark | agent (extensible later: "api", spec §8)
    spark_model: str | None = None  # vLLM model name override (spark entries)
    # Custom chip tag for the branded video frame (e.g. "OMP · DGX SPARK").
    # NULL -> harness-derived default (orchestrator._build_branding_payload).
    display_tag: str | None = None
    agent_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
    )
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid,
            ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    # pending -> generating -> generated -> rendered | failed
    status: str = Field(default="pending")
    artifact_path: str | None = None  # .../index.html
    video_path: str | None = None
    screenshot_path: str | None = None
    # duration_ms always; spark entries additionally tokens_in/tokens_out/tok_per_s
    metrics: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class BenchProbe(SQLModel, table=True):
    """Which thinking key the served model honours (bench W0.6).

    One row per (runtime, served model, serving_since): a restart of the
    engine (new ``serving_since``) or another model gets a fresh probe; the
    same running engine is probed once. Filled by
    ``bench_studio.effort_probe`` with 2-4 tiny requests.

    - ``thinking_key``: the key that switched thinking (dotted for nested
      fields, see ``services.reasoning_params.THINKING_KEYS``); NULL = no
      tried key had an effect.
    - ``effort_param``: the key that carries graded levels
      (``reasoning_effort``) — NULL when only on/off works.
    - ``levels``: what the bench may offer besides "default".
    - ``default_observed``: "on" | "off" | "unknown" — thinking without any key.
    - ``status``: "ok" | "no_effect" | "failed".
    - ``raw``: every attempt (key, value, http status, reasoning seen).

    serving_since is nullable (the watcher clears it while an engine is
    down), so the key is a unique constraint over a surrogate id, not a
    composite primary key."""

    __tablename__ = "bench_probes"
    __table_args__ = (
        # NULLS NOT DISTINCT: runtime_id / serving_since can be NULL, and a
        # plain unique key would let two probes of the same engine coexist.
        UniqueConstraint(
            "runtime_id",
            "served_model_id",
            "serving_since",
            name="uq_bench_probes_serving",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    runtime_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid, ForeignKey("runtimes.id", ondelete="CASCADE"), nullable=True, index=True
        ),
    )
    served_model_id: str = Field(sa_column=Column(Text, nullable=False))
    serving_since: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    thinking_key: str | None = None
    effort_param: str | None = None
    levels: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    default_observed: str = Field(default="unknown")
    status: str = Field(default="ok")
    raw: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    probed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
