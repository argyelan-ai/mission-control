"""Bench W1 schema (concept 30 §7, the single schema PR — one owner).

bench_challenges: preset, prompt_sha256, protocol_version (old rows "v0"),
episode_no, trigger, rerun_of (SET NULL), plan (frozen start plan),
wait_reason.

bench_entries: runtime_id (SET NULL), harness, run_class, effort_requested,
repeat_idx, attempt_no, head_run_id, receipt, gate.

bench_receipts (new): receipt history, one row per version, never updated.

Backfill (W0 metrics moved, not copied by guesswork):
- every existing challenge -> protocol_version "v0", prompt_sha256 computed
  from its frozen prompt_text;
- entries -> effort_requested from metrics["effort_requested"] (W0.6 wrote it
  there), harness "api" + run_class "A" for direct-API (source_kind "spark")
  rows, run_class "B" for agent rows (harness stays NULL: the agent's harness
  at run time is not recorded for old rows and is printed as unknown).
  repeat_idx / attempt_no default 1.

Additive only. Downgrade drops the new table and columns (the W0 values stay
in metrics, nothing is lost).

Revision ID: 0224_bench_w1_schema
Revises: 0223_message_from_chat
"""
import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0224_bench_w1_schema"
down_revision = "0223_message_from_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bench_challenges", sa.Column("preset", sa.String(), nullable=True))
    op.add_column("bench_challenges", sa.Column("prompt_sha256", sa.String(), nullable=True))
    op.add_column(
        "bench_challenges",
        sa.Column("protocol_version", sa.Text(), nullable=False, server_default="v0"),
    )
    op.add_column("bench_challenges", sa.Column("episode_no", sa.Integer(), nullable=True))
    op.add_column("bench_challenges", sa.Column("trigger", sa.String(), nullable=True))
    op.add_column(
        "bench_challenges",
        sa.Column(
            "rerun_of",
            sa.Uuid(),
            sa.ForeignKey("bench_challenges.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("bench_challenges", sa.Column("plan", sa.JSON(), nullable=True))
    op.add_column("bench_challenges", sa.Column("wait_reason", sa.String(), nullable=True))

    op.add_column(
        "bench_entries",
        sa.Column(
            "runtime_id",
            sa.Uuid(),
            sa.ForeignKey("runtimes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_bench_entries_runtime_id", "bench_entries", ["runtime_id"])
    op.add_column("bench_entries", sa.Column("harness", sa.String(), nullable=True))
    op.add_column("bench_entries", sa.Column("run_class", sa.String(), nullable=True))
    op.add_column("bench_entries", sa.Column("effort_requested", sa.String(), nullable=True))
    op.add_column(
        "bench_entries",
        sa.Column("repeat_idx", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "bench_entries",
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("bench_entries", sa.Column("head_run_id", sa.String(), nullable=True))
    op.add_column("bench_entries", sa.Column("receipt", sa.JSON(), nullable=True))
    op.add_column("bench_entries", sa.Column("gate", sa.JSON(), nullable=True))

    op.create_table(
        "bench_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "entry_id",
            sa.Uuid(),
            sa.ForeignKey("bench_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False, server_default="run_end"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "version", name="uq_bench_receipts_entry_version"),
    )
    op.create_index("ix_bench_receipts_entry_id", "bench_receipts", ["entry_id"])

    _backfill()


def _backfill() -> None:
    """Small tables (tens of rows) — row by row in Python, identical on
    Postgres and SQLite (no dialect-specific JSON operators)."""
    bind = op.get_bind()
    challenges = sa.table(
        "bench_challenges",
        sa.column("id", sa.Uuid()),
        sa.column("prompt_text", sa.Text()),
        sa.column("prompt_sha256", sa.String()),
    )
    for cid, prompt_text in bind.execute(
        sa.select(challenges.c.id, challenges.c.prompt_text)
    ).fetchall():
        sha = hashlib.sha256((prompt_text or "").encode("utf-8")).hexdigest()
        bind.execute(
            challenges.update().where(challenges.c.id == cid).values(prompt_sha256=sha)
        )

    entries = sa.table(
        "bench_entries",
        sa.column("id", sa.Uuid()),
        sa.column("source_kind", sa.String()),
        sa.column("metrics", sa.JSON()),
        sa.column("harness", sa.String()),
        sa.column("run_class", sa.String()),
        sa.column("effort_requested", sa.String()),
    )
    for eid, source_kind, metrics in bind.execute(
        sa.select(entries.c.id, entries.c.source_kind, entries.c.metrics)
    ).fetchall():
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except ValueError:
                metrics = {}
        metrics = metrics or {}
        values = {
            "effort_requested": metrics.get("effort_requested"),
            "run_class": "A" if source_kind == "spark" else "B",
            "harness": "api" if source_kind == "spark" else None,
        }
        bind.execute(entries.update().where(entries.c.id == eid).values(**values))


def downgrade() -> None:
    op.drop_index("ix_bench_receipts_entry_id", table_name="bench_receipts")
    op.drop_table("bench_receipts")
    for col in (
        "gate", "receipt", "head_run_id", "attempt_no", "repeat_idx",
        "effort_requested", "run_class", "harness",
    ):
        op.drop_column("bench_entries", col)
    op.drop_index("ix_bench_entries_runtime_id", table_name="bench_entries")
    op.drop_column("bench_entries", "runtime_id")
    for col in (
        "wait_reason", "plan", "rerun_of", "trigger", "episode_no",
        "protocol_version", "prompt_sha256", "preset",
    ):
        op.drop_column("bench_challenges", col)
