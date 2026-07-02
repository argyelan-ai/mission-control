"""Token/Cost-Tracking Phase 1: model_usage_events + model_prices + harvest_state.

Neue, saubere Append-Event-Tabelle mit Message-Granularitaet (uuid UNIQUE).
Ersetzt CostEvent-Writes fuer den Cost-Endpoint; CostEvent bleibt fuer
check_budget_warnings erhalten.

Revision ID: 0127
Revises: 0126
Create Date: 2026-06-11
"""
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None

# Epoch fuer valid_from der Seed-Rows
# Echte datetime, KEIN isoformat-String — Postgres castet VARCHAR nicht zu TIMESTAMP
_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def upgrade() -> None:
    # ── 1. model_usage_events ──────────────────────────────────────────────
    op.create_table(
        "model_usage_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("harness", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("message_uuid", sa.String(), nullable=False, unique=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "harvested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("source_file", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_model_usage_events_agent_id", "model_usage_events", ["agent_id"])
    op.create_index("ix_model_usage_events_harness", "model_usage_events", ["harness"])
    op.create_index("ix_model_usage_events_model", "model_usage_events", ["model"])
    op.create_index("ix_model_usage_events_session_id", "model_usage_events", ["session_id"])
    op.create_index("ix_model_usage_events_ts", "model_usage_events", ["ts"])
    op.create_index("ix_model_usage_model_ts", "model_usage_events", ["model", "ts"])
    op.create_index("ix_model_usage_agent_ts", "model_usage_events", ["agent_id", "ts"])
    op.create_index("ix_model_usage_harness_ts", "model_usage_events", ["harness", "ts"])
    # UNIQUE constraint on message_uuid — Idempotenz-Garant
    op.create_unique_constraint(
        "uq_model_usage_message_uuid", "model_usage_events", ["message_uuid"]
    )

    # ── 2. model_prices ────────────────────────────────────────────────────
    op.create_table(
        "model_prices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("model_pattern", sa.String(), nullable=False),
        sa.Column("input_per_mtok", sa.Float(), nullable=False, server_default="0"),
        sa.Column("output_per_mtok", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cache_read_per_mtok", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cache_write_per_mtok", sa.Float(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(), nullable=True),
    )
    op.create_index("ix_model_prices_model_pattern", "model_prices", ["model_pattern"])
    op.create_index("ix_model_prices_priority", "model_prices", ["priority"])

    # Default-Seeds — an echter Flotte ausgerichtet (Preise lt. Spec §4.2)
    seeds = [
        # (pattern, input, output, cache_r, cache_w, priority, note)
        ("claude-opus-4-*",   15.0,   75.0,  1.5,  18.75, 80,
         "Anthropic Opus 4 — API Listenpreis (Boss nutzt Subscription = Schattenkosten)"),
        ("claude-sonnet-4-*",  3.0,   15.0,  0.3,   3.75, 80,
         "Anthropic Sonnet 4 — API Listenpreis"),
        ("claude-fable-*",     3.0,   15.0,  0.3,   3.75, 75,
         "Anthropic Fable — Platzhalter, bitte Preis nachpflegen"),
        ("glm-*",              0.0,    0.0,  0.0,   0.0, 60,
         "GLM / Ollama-Cloud-Flatrate — keine Grenzkosten"),
        ("qwen2.5-coder*",     0.0,    0.0,  0.0,   0.0, 60,
         "Backend Ollama lokal"),
        ("*PrismaQuant*",      0.0,    0.0,  0.0,   0.0, 60,
         "Lokal DGX Spark"),
        ("*Qwen*",             0.0,    0.0,  0.0,   0.0, 50,
         "Lokal DGX Spark / LM Studio"),
        ("qwen*",              0.0,    0.0,  0.0,   0.0, 50,
         "Lokal DGX Spark / LM Studio"),
        ("*",                  0.0,    0.0,  0.0,   0.0,  0,
         "Fallback — unbekanntes Modell, bitte Preis setzen"),
    ]
    mp_table = sa.table(
        "model_prices",
        sa.column("id", sa.Uuid()),
        sa.column("model_pattern", sa.String()),
        sa.column("input_per_mtok", sa.Float()),
        sa.column("output_per_mtok", sa.Float()),
        sa.column("cache_read_per_mtok", sa.Float()),
        sa.column("cache_write_per_mtok", sa.Float()),
        sa.column("currency", sa.String()),
        sa.column("valid_from", sa.DateTime(timezone=True)),
        sa.column("priority", sa.Integer()),
        sa.column("note", sa.String()),
    )
    op.bulk_insert(
        mp_table,
        [
            {
                "id": uuid.uuid4(),
                "model_pattern": pattern,
                "input_per_mtok": inp,
                "output_per_mtok": out,
                "cache_read_per_mtok": cr,
                "cache_write_per_mtok": cw,
                "currency": "USD",
                "valid_from": _EPOCH,
                "priority": prio,
                "note": note,
            }
            for pattern, inp, out, cr, cw, prio, note in seeds
        ],
    )

    # ── 3. model_usage_harvest_state ──────────────────────────────────────
    op.create_table(
        "model_usage_harvest_state",
        sa.Column("file_path", sa.String(), primary_key=True),
        sa.Column("mtime", sa.Float(), nullable=False, server_default="0"),
        sa.Column("processed_lines", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("model_usage_harvest_state")
    op.drop_table("model_prices")
    op.drop_table("model_usage_events")
