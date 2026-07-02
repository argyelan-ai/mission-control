"""Hermes runtime + single_instance column (Phase 24, HERM-04 D-06/D-07/D-08/D-11)

Revision ID: 0095
Revises: 0094
Create Date: 2026-04-30

Adds:
  1. ``runtimes.single_instance`` BOOLEAN NOT NULL DEFAULT false. Generic
     mechanism (D-08) for host-side workers that may only run one instance
     at a time. Existing rows (cloud / vllm_docker / lmstudio / anthropic /
     unsloth) get the safe default ``false`` and are unaffected.
  2. Idempotent INSERT of the Hermes runtime row (slug=hermes-vllm,
     runtime_type='hermes', single_instance=true, endpoint=DGX vLLM,
     model_identifier=Qwen/Qwen3.6-35B-A3B-FP8). Uses ``ON CONFLICT (slug)
     DO UPDATE SET single_instance = true`` so re-running converges any
     pre-seeded row to the correct flag without duplicating.
  3. Idempotent INSERT of the Hermes agent row (name=Hermes,
     agent_runtime='host' — NOT 'cli-bridge' per Pitfall 1 in CONTEXT.md;
     workspace_path absolute per Pitfall 5; provision_status='local' per
     L-B since the actual host-side provisioning happens in plan 08).
     Uses ``ON CONFLICT (name) DO NOTHING`` for repeat-run safety.

STRIDE T-24-01 mitigation: every INSERT uses parameterized
``sa.text(...)`` bind params — no f-string interpolation.

Downgrade is defensive: drops only the new column. The Hermes runtime
row and Hermes agent row survive a downgrade so the data is not lost
on a rollback. If you need to fully unprovision Hermes, use the
``DELETE FROM agents WHERE name='Hermes'`` + ``DELETE FROM runtimes
WHERE slug='hermes-vllm'`` SQL by hand — that is an operational
decision, not a migration concern.
"""
import logging
import os

import sqlalchemy as sa
from alembic import op


def _home() -> str:
    """Best-effort resolve of the HOST home (same pattern as migration 0087).

    Inside the backend container ``HOME`` points at the container user —
    the stack exports ``HOME_HOST`` with the real host home. Fall back to
    ``expanduser('~')`` only for local pytest setups.
    """
    return os.environ.get("HOME_HOST") or os.path.expanduser("~")


# revision identifiers, used by Alembic.
revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.0095")


def upgrade() -> None:
    # 1. Add single_instance column with safe default false.
    op.add_column(
        "runtimes",
        sa.Column(
            "single_instance",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    conn = op.get_bind()

    # 2. Seed Hermes runtime. ON CONFLICT (slug) DO UPDATE SET single_instance=true
    #    so a pre-seeded row from runtimes.json (via lifespan seeder) converges
    #    to the correct flag without duplication.
    conn.execute(
        sa.text(
            """
            INSERT INTO runtimes (
                id, slug, display_name, runtime_type, endpoint, model_identifier,
                role_tags, supports_tools, supports_streaming,
                ui_order, enabled, single_instance, memory_notes
            ) VALUES (
                gen_random_uuid(),
                :slug, :display_name, :runtime_type, :endpoint, :model_identifier,
                CAST(:role_tags AS json),
                :supports_tools, :supports_streaming,
                :ui_order, :enabled, :single_instance, :memory_notes
            )
            ON CONFLICT (slug) DO UPDATE SET
                single_instance = EXCLUDED.single_instance,
                runtime_type = EXCLUDED.runtime_type,
                model_identifier = EXCLUDED.model_identifier
            """
        ),
        {
            "slug": "hermes-vllm",
            "display_name": "Hermes (vLLM Qwen3.6-35B)",
            "runtime_type": "hermes",
            "endpoint": "http://192.0.2.10:8000/v1",
            "model_identifier": "Qwen/Qwen3.6-35B-A3B-FP8",
            "role_tags": "[]",
            "supports_tools": True,
            "supports_streaming": True,
            "ui_order": 9,
            "enabled": True,
            "single_instance": True,
            "memory_notes": "Single-instance host-side Worker. Shared vLLM endpoint with Sparky.",
        },
    )

    # 3. Seed Hermes agent row, looking up runtime_id from the runtimes table.
    #    agent_runtime='host' (NOT the cli bridge runtime — Pitfall 1).
    #    workspace_path absolute (Pitfall 5).
    #    provision_status='local' per L-B (provisioning lands in plan 08).
    conn.execute(
        sa.text(
            """
            INSERT INTO agents (
                id, name, agent_runtime, runtime_id, workspace_path,
                provision_status, model, run_state, operational_mode,
                scopes, cli_plugins, skills, skill_filter,
                requires_git_workflow, context_max,
                created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                :name,
                :agent_runtime,
                r.id,
                :workspace_path,
                :provision_status,
                :model,
                :run_state,
                :operational_mode,
                CAST(:scopes AS json),
                CAST(:cli_plugins AS json),
                CAST(:skills AS json),
                CAST(:skill_filter AS json),
                :requires_git_workflow,
                :context_max,
                NOW(),
                NOW()
            FROM runtimes r
            WHERE r.slug = :runtime_slug
              AND NOT EXISTS (SELECT 1 FROM agents WHERE name = :name)
            """
        ),
        {
            "name": "Hermes",
            "agent_runtime": "host",
            # Absolute host path (Pitfall 5) — derived from the host home so
            # fresh installs do not inherit a machine-specific literal.
            "workspace_path": f"{_home()}/.openclaw/agents/hermes",
            "provision_status": "local",
            "model": "Qwen/Qwen3.6-35B-A3B-FP8",
            "run_state": "idle",
            "operational_mode": "active",
            "scopes": "[]",
            "cli_plugins": "[]",
            "skills": "[]",
            "skill_filter": "[]",
            "requires_git_workflow": False,
            "context_max": 200000,
            "runtime_slug": "hermes-vllm",
        },
    )

    logger.info("0095: added runtimes.single_instance + seeded Hermes runtime + agent")


def downgrade() -> None:
    # Defensive: drop only the column. Runtime + agent rows are kept on
    # downgrade so data is not lost. See module docstring for manual
    # cleanup SQL if a full rollback is needed.
    op.drop_column("runtimes", "single_instance")
