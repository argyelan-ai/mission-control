"""per-agent idle_timeout_minutes for heavy workers (FND-06)

Revision ID: 0097_per_agent_idle_timeout_minutes
Revises: 0096_hermes_dispatch_scopes
Create Date: 2026-05-01

Phase 26 (FND-06): Long-running workers (Deployer with npm install + next build +
Vercel deploy chains; heavy Coders FreeCode/Davinci/Neo with multi-minute builds)
get per-agent idle_timeout_minutes overrides to prevent watchdog reset during
legitimate work.

Pattern mirrors Migration 0096 (ack_timeout_minutes for Hermes). Same shape,
different key. The watchdog (_idle_threshold_for in task_runner.py) reads
idle_timeout_minutes as Stufe 1, falls back to stale_progress_minutes (existing
key, backwards-compat), then to role/runtime defaults.

Defaults set:
  - Deployer:  30 minutes (deploy chains often >15min total)
  - FreeCode:  20 minutes (heavy coder, multi-step builds)
  - Davinci:   20 minutes (heavy coder)
  - Neo:       20 minutes (heavy coder)

Idempotent: re-running converges. No-op for missing agents (logged WARN).
Backwards-compat: agents WITHOUT idle_timeout_minutes continue to use existing
role-based default via task_runner._idle_threshold_for fallback chain.
"""
from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0097_per_agent_idle_timeout"
down_revision = "0096_hermes_dispatch_scopes"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

AGENT_IDLE_OVERRIDES = [
    ("Deployer", 30),
    ("FreeCode", 20),
    ("Davinci", 20),
    ("Neo", 20),
]


def upgrade() -> None:
    conn = op.get_bind()
    for name, idle_min in AGENT_IDLE_OVERRIDES:
        row = conn.execute(
            sa.text("SELECT id, dispatch_config FROM agents WHERE name = :n"),
            {"n": name},
        ).mappings().first()
        if row is None:
            log.warning("0097: no agent named %r found -- skipping.", name)
            continue
        existing = row["dispatch_config"]
        if isinstance(existing, str):
            existing = json.loads(existing) if existing else {}
        elif existing is None:
            existing = {}
        new_dc = {**existing, "idle_timeout_minutes": idle_min}
        conn.execute(
            sa.text(
                "UPDATE agents SET dispatch_config = CAST(:d AS json) "
                "WHERE name = :n"
            ),
            {"d": json.dumps(new_dc), "n": name},
        )
        log.info("0097: %s idle_timeout_minutes=%d", name, idle_min)


def downgrade() -> None:
    conn = op.get_bind()
    for name, _ in AGENT_IDLE_OVERRIDES:
        row = conn.execute(
            sa.text("SELECT dispatch_config FROM agents WHERE name = :n"),
            {"n": name},
        ).mappings().first()
        if row is None:
            continue
        existing = row["dispatch_config"]
        if isinstance(existing, str):
            existing = json.loads(existing) if existing else {}
        elif existing is None:
            existing = {}
        existing.pop("idle_timeout_minutes", None)
        conn.execute(
            sa.text(
                "UPDATE agents SET dispatch_config = CAST(:d AS json) "
                "WHERE name = :n"
            ),
            {"d": json.dumps(existing), "n": name},
        )
