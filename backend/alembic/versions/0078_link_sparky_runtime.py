"""seed qwen-coder-lms runtime + link sparky to it

Revision ID: 0078
Revises: 0077
Create Date: 2026-04-19

Creates the `qwen-coder-lms` runtime row (LM Studio on DGX Spark, Qwen3 Coder)
and links the existing Sparky agent to it. Before this migration Sparky had a
hardcoded OPENAI_BASE_URL + OPENAI_MODEL in docker-compose.agents.yml; Phase 2
replaces that with a DB-driven runtime assignment.

The runtime row is also shipped in backend/config/runtimes.json so fresh
deployments seed it identically. Here we insert it defensively so the
subsequent UPDATE finds it even if the seeder hasn't run yet.
"""
import uuid
from alembic import op


revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    runtime_id = str(uuid.uuid4())

    op.execute(
        f"""
        INSERT INTO runtimes (
            id, slug, display_name, runtime_type, endpoint, healthcheck_path,
            model_identifier, lms_identifier, lms_cli_path, role_tags,
            supports_tools, supports_reasoning, supports_streaming,
            preferred_context_len, max_context_len, gpu_profile,
            memory_notes, startup_notes, ui_order, enabled,
            created_at, updated_at
        )
        SELECT
            '{runtime_id}'::uuid,
            'qwen-coder-lms',
            'Qwen3 Coder (LM Studio)',
            'lmstudio',
            'http://192.0.2.10:1234/v1',
            '/v1/models',
            'qwen3-coder-next',
            'qwen/qwen3-coder-next',
            '~/.lmstudio/bin/lms',
            '["coder"]'::jsonb,
            true, false, true,
            32768, 131072,
            'dgx_spark_heavy',
            'LM Studio — Coder-Modell für Sparky. Parallel ladbar neben dem Embedding-Modell.',
            'Braucht ~1 Minute zum Laden.',
            4, true,
            NOW(), NOW()
        WHERE NOT EXISTS (SELECT 1 FROM runtimes WHERE slug = 'qwen-coder-lms')
        """
    )

    # Link Sparky (only if runtime_id is NULL, i.e. hasn't been manually changed)
    op.execute(
        """
        UPDATE agents
        SET runtime_id = runtimes.id
        FROM runtimes
        WHERE runtimes.slug = 'qwen-coder-lms'
          AND lower(agents.name) = 'sparky'
          AND agents.runtime_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE agents SET runtime_id = NULL
        WHERE runtime_id IN (SELECT id FROM runtimes WHERE slug = 'qwen-coder-lms')
          AND lower(name) = 'sparky'
        """
    )
    op.execute("DELETE FROM runtimes WHERE slug = 'qwen-coder-lms'")
