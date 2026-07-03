"""hermes dispatch_config + developer scopes

Revision ID: 0096_hermes_dispatch_scopes
Revises: 0095
Create Date: 2026-04-30

Phase 25 (D-11 + D-14): Hermes agent gets
- Developer default scopes (same set as Cody) -> authorizes PATCH status,
  comments, git, knowledge/memory r/w, project r/w, tasks:help, credentials:read.
- dispatch_config['ack_timeout_minutes']=15 -> per-agent override takes
  precedence over AGENT_RUNTIME_ACK_TIMEOUTS['host']=5 (Boss stays at 5min,
  only Hermes is 15min).

Idempotent: re-running converges. No-op if Hermes row missing (logged WARN).

Source of truth for HERMES_DEVELOPER_SCOPES: backend/app/scopes.py
DEFAULT_SCOPES[AgentRole.DEVELOPER] as of repo HEAD 3c7d7320 (2026-04-30).
Hardcoded here intentionally so the migration is self-contained and survives
any future scopes.py refactor -- D-14 locks the Cody-equivalent set as the
contract. If scopes.py later extends the developer scopes, a new migration
will be needed (not this edit).
"""
from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from alembic import op


# Mirrors backend/app/scopes.py DEFAULT_SCOPES[AgentRole.DEVELOPER] as of
# commit 3c7d7320 (13 scopes -- includes tasks:help + credentials:read which
# the plan snippet originally missed; corrected per source-of-truth check).
HERMES_DEVELOPER_SCOPES = [
    "tasks:read",
    "tasks:write",
    "knowledge:read",
    "knowledge:write",
    "memory:read",
    "memory:write",
    "approvals:create",
    "chat:write",
    "heartbeat",
    "project:read",
    "project:write",
    "tasks:help",
    "credentials:read",
]
HERMES_ACK_TIMEOUT_MINUTES = 15  # D-11 -- vLLM-Latency tolerance

# revision identifiers, used by Alembic.
revision = "0096_hermes_dispatch_scopes"
down_revision = "0095"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def _fetch_hermes(conn) -> dict | None:
    row = conn.execute(
        sa.text("SELECT id, scopes, dispatch_config FROM agents WHERE name = :n"),
        {"n": "Hermes"},
    ).mappings().first()
    return dict(row) if row else None


def upgrade() -> None:
    conn = op.get_bind()
    hermes = _fetch_hermes(conn)
    if hermes is None:
        log.warning(
            "0096: no agent named 'Hermes' found -- Phase 24 not merged? Skipping."
        )
        return

    existing_dc = hermes["dispatch_config"]
    if isinstance(existing_dc, str):
        existing_dc = json.loads(existing_dc) if existing_dc else {}
    elif existing_dc is None:
        existing_dc = {}
    new_dc = {**existing_dc, "ack_timeout_minutes": HERMES_ACK_TIMEOUT_MINUTES}

    conn.execute(
        sa.text(
            "UPDATE agents SET scopes = CAST(:s AS json), "
            "dispatch_config = CAST(:d AS json) "
            "WHERE name = :n"
        ),
        {
            "s": json.dumps(HERMES_DEVELOPER_SCOPES),
            "d": json.dumps(new_dc),
            "n": "Hermes",
        },
    )
    log.info(
        "0096: Hermes scopes=%d items, ack_timeout_minutes=%d",
        len(HERMES_DEVELOPER_SCOPES),
        HERMES_ACK_TIMEOUT_MINUTES,
    )


def downgrade() -> None:
    conn = op.get_bind()
    hermes = _fetch_hermes(conn)
    if hermes is None:
        return

    existing_dc = hermes["dispatch_config"]
    if isinstance(existing_dc, str):
        existing_dc = json.loads(existing_dc) if existing_dc else {}
    elif existing_dc is None:
        existing_dc = {}
    existing_dc.pop("ack_timeout_minutes", None)

    conn.execute(
        sa.text(
            "UPDATE agents SET scopes = CAST(:s AS json), "
            "dispatch_config = CAST(:d AS json) "
            "WHERE name = :n"
        ),
        {
            "s": json.dumps([]),
            "d": json.dumps(existing_dc) if existing_dc else json.dumps({}),
            "n": "Hermes",
        },
    )
