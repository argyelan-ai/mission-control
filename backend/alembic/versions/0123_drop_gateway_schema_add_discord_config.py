"""Drop gateways table + gateway_* FK columns, add discord_config (Phase 30)

Revision ID: 0123
Revises: 0122
Create Date: 2026-05-17

Phase 30 of v0.9 (OpenClaw Gateway Sunset). After Phase 28 (Henry deleted) and
Phase 29 (gateway-bound code removed), the `gateways` table holds a single
residual row whose only useful data is the Discord guild/category/bot_configured
triplet. This migration:

  1. Pre-Flight: assert no agents remain with agent_runtime='openclaw' EXCEPT
     the two known inert test agents (Bug18Test, D2Test from Phase 28
     LIVE-RUN-notes). These two are deleted in step 2.
  2. Hard-delete inert test agents Bug18Test + D2Test.
  3. Create `discord_config` table (single-row, application-enforced).
  4. Seed `discord_config` row from the existing gateways row (NULL fallback
     if the gateways table is empty).
  5. Drop FK columns: agents.gateway_id, agents.gateway_agent_id,
     boards.gateway_id.
  6. Drop `gateways` table.
  7. Add CHECK constraint on agents.agent_runtime excluding 'openclaw' +
     drop the obsolete server_default='openclaw' (pre-flight Rule-3 fix —
     SQLModel default is already 'cli-bridge', the DB default would only
     bite if a future raw-SQL INSERT skipped the column).

NOT DROPPED (per Plan 30-01/02 scope clarification):
  - agents.workspace_path — actively used by cli-bridge + host runtime as
    agent-home-path (live discovery: 10/14 agents have values).
  - agents.discord_channel_id / discord_channel_name — per-agent Discord
    channels (CONTEXT.md D-13).

Per CONTEXT.md D-01 (atomic), D-02 (live-schema-first), D-03 (no try/except
for best-effort), D-04 (PyUUID), D-05–D-08 (discord_config shape), D-09–D-11
(runtime cleanup; D-11 corrected — agent_runtime is plain TEXT, use
CHECK constraint NOT enum-type-swap), D-12 (drop order), D-14–D-15 (downgrade).

Phase 28 LIVE-RUN lessons baked in:
  - asyncpg requires UUID-typed bindparams (use _PyUUID(id_str))
  - try/except around Postgres SQL is a footgun — one fail = whole tx aborts
  - Pre-Flight checks RAISE before any mutation
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from uuid import UUID as _PyUUID

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


INERT_TEST_NAMES = ("Bug18Test", "D2Test")


def upgrade() -> None:
    bind = op.get_bind()

    # ----- Step 1: Pre-Flight (CONTEXT.md D-03 + D-09) -----------------
    # Raises RuntimeError BEFORE any mutation if leftover openclaw agents
    # exist beyond the two known inert test agents (Bug18Test + D2Test).
    leftover_row = bind.execute(
        sa.text(
            "SELECT count(*) FROM agents "
            "WHERE agent_runtime = 'openclaw' "
            "  AND name NOT IN :inert_names"
        ).bindparams(
            sa.bindparam("inert_names", expanding=True)
        ),
        {"inert_names": list(INERT_TEST_NAMES)},
    ).fetchone()
    leftover_count = int(leftover_row[0]) if leftover_row else 0
    if leftover_count > 0:
        raise RuntimeError(
            f"Pre-Flight failed: {leftover_count} agent(s) still have "
            f"agent_runtime='openclaw' (excluding inert test agents "
            f"{INERT_TEST_NAMES}). Migration aborted — clean up manually "
            f"first or re-run Phase 28 follow-up."
        )

    # ----- Step 2: Collect + hard-delete inert test agents -------------
    # PyUUID bindparams per CONTEXT.md D-04 (Phase 28 LIVE-RUN lesson:
    # asyncpg refuses string-typed UUIDs in production).
    rows = bind.execute(
        sa.text("SELECT id FROM agents WHERE name IN :names").bindparams(
            sa.bindparam("names", expanding=True)
        ),
        {"names": list(INERT_TEST_NAMES)},
    ).fetchall()
    test_uuids = [_PyUUID(str(r[0])) for r in rows]

    # Single-id loop (NOT ANY(:ids) — Postgres-only + fails SQLite tests).
    # No try/except around SQL (Phase 28 hard lesson — Postgres aborts
    # the whole tx on any failure regardless).
    for _uid in test_uuids:
        params = {"id": _uid}
        # Clear self-referential FK first (current_task_id) before delete.
        bind.execute(
            sa.text("UPDATE agents SET current_task_id = NULL WHERE id = :id"),
            params,
        )
        # NOT-NULL audit-table refs — explicit hard delete (analog 0122
        # lines 231-255). Inert test agents have <10 rows here in practice.
        bind.execute(
            sa.text("DELETE FROM chat_messages WHERE sender_agent_id = :id"),
            params,
        )
        bind.execute(
            sa.text("DELETE FROM cost_events WHERE agent_id = :id"),
            params,
        )
        bind.execute(
            sa.text("DELETE FROM approvals WHERE agent_id = :id"),
            params,
        )
        # Finally delete the agent row itself. CASCADE / SET NULL FKs
        # handle themselves (e.g. agent_metrics, task_comments).
        bind.execute(
            sa.text("DELETE FROM agents WHERE id = :id"),
            params,
        )

    # ----- Step 3: Create discord_config table (CONTEXT.md D-05) -------
    op.create_table(
        "discord_config",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("guild_id", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Text(), nullable=True),
        sa.Column(
            "bot_configured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ----- Step 4: Seed discord_config from gateways (CONTEXT.md D-07) -
    # Copy the discord_* triplet out of the gateways row BEFORE we drop
    # the table.
    bind.execute(
        sa.text(
            "INSERT INTO discord_config (guild_id, category_id, bot_configured) "
            "SELECT discord_guild_id, discord_category_id, "
            "       COALESCE(discord_bot_configured, false) "
            "FROM gateways LIMIT 1"
        )
    )
    # Defensive fallback: if gateways was empty, insert a NULL row so the
    # single-row invariant holds from migration onward (CONTEXT.md D-08).
    bind.execute(
        sa.text(
            "INSERT INTO discord_config (guild_id, category_id, bot_configured) "
            "SELECT NULL, NULL, false "
            "WHERE NOT EXISTS (SELECT 1 FROM discord_config)"
        )
    )

    # ----- Step 5: Drop FK columns (D-12) ------------------------------
    # FK column drop order (children of gateways first), so dropping the
    # parent table in step 6 doesn't trip on NO-ACTION FKs.
    # IMPORTANT: agents.workspace_path is NOT dropped here. Live-data
    # for cli-bridge + host runtime agents. Plan 30-01/02 scope clarification.
    # batch_alter_table for SQLite-compat (analog 0121).
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("gateway_id")
        batch_op.drop_column("gateway_agent_id")

    with op.batch_alter_table("boards") as batch_op:
        batch_op.drop_column("gateway_id")

    # ----- Step 6: Drop gateways table ---------------------------------
    op.drop_table("gateways")

    # ----- Step 7: CHECK constraint on agent_runtime (D-11 corrected) -
    # Reality: agent_runtime is plain TEXT (migration 0031:19), not a
    # Postgres ENUM. CHECK constraint achieves the same intent.
    op.create_check_constraint(
        "ck_agents_agent_runtime_not_openclaw",
        "agents",
        "agent_runtime IN ('cli-bridge', 'claude-code', 'manual', 'host')",
    )

    # Pre-flight Rule-3 fix: drop the obsolete server_default='openclaw'
    # left over from migration 0031. SQLModel now supplies 'cli-bridge'
    # explicitly on every INSERT (Plan 30-02), but a raw-SQL INSERT that
    # skipped the column would still try to write 'openclaw' and trip
    # the new CHECK constraint. Drop the default to remove the landmine.
    # batch_alter_table for SQLite-compat (SQLite ignores server_default
    # so the operation is a no-op there).
    with op.batch_alter_table("agents") as batch_op:
        batch_op.alter_column("agent_runtime", server_default=None)


def downgrade() -> None:
    """Best-effort downgrade — production rollback uses ./backup.sh per
    CONTEXT.md D-14. Recreates gateways table + restores FK columns +
    copies discord_config back to gateways.

    Historical Gateway-bound agents are NOT restored (the rows were
    deleted in Phase 28). Test fixtures for migration 0122 cover the
    Henry-restore path.
    """
    bind = op.get_bind()

    # Restore the obsolete server_default first so any post-downgrade
    # insert without explicit agent_runtime doesn't fail. SQLite-no-op.
    with op.batch_alter_table("agents") as batch_op:
        batch_op.alter_column(
            "agent_runtime",
            server_default=sa.text("'openclaw'"),
        )

    op.drop_constraint(
        "ck_agents_agent_runtime_not_openclaw",
        "agents",
        type_="check",
    )

    # Recreate gateways with only the columns Phase 30 cared about.
    op.create_table(
        "gateways",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=False, server_default="legacy"),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), server_default="unknown"),
        sa.Column("discord_guild_id", sa.Text(), nullable=True),
        sa.Column("discord_category_id", sa.Text(), nullable=True),
        sa.Column(
            "discord_bot_configured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )

    # Copy discord_config back to gateways (idempotent).
    bind.execute(
        sa.text(
            "INSERT INTO gateways (name, url, status, discord_guild_id, "
            "                      discord_category_id, discord_bot_configured) "
            "SELECT 'legacy', '', 'unknown', guild_id, category_id, bot_configured "
            "FROM discord_config LIMIT 1"
        )
    )

    # Restore FK columns (nullable — original 0001 schema had
    # agents.gateway_id as NOT NULL but downgrade is CI-only).
    op.add_column(
        "boards",
        sa.Column("gateway_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("gateway_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("gateway_agent_id", sa.Text(), nullable=True),
    )

    op.drop_table("discord_config")
