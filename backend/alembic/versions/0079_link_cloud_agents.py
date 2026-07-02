"""seed ollama-cloud runtime + link unassigned cli-bridge agents

Revision ID: 0079
Revises: 0078
Create Date: 2026-04-19

One-off data migration: creates the `ollama-cloud` runtime (glm-5.1 via
ollama.com) and links every cli-bridge agent that has NULL runtime_id
to it. Preserves explicit assignments (e.g. Sparky → qwen-coder-lms).

Afterwards every cli-bridge agent has an explicit runtime — no more
hidden docker-compose-env fallback. Opens the door for the full
docker-compose cleanup in a follow-up.
"""
import uuid
from alembic import op


revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Insert ollama-cloud runtime (defensive — seeder also inserts it later).
    runtime_id = str(uuid.uuid4())
    op.execute(
        f"""
        INSERT INTO runtimes (
            id, slug, display_name, runtime_type, endpoint, healthcheck_path,
            model_identifier, role_tags,
            supports_tools, supports_reasoning, supports_streaming,
            preferred_context_len, max_context_len,
            memory_notes, startup_notes, ui_order, enabled,
            created_at, updated_at
        )
        SELECT
            '{runtime_id}'::uuid,
            'ollama-cloud',
            'Ollama Cloud (glm-5.1)',
            'cloud',
            'https://ollama.com/v1',
            '/api/tags',
            'glm-5.1:cloud',
            '["general", "fallback"]'::jsonb,
            true, true, true,
            32768, 131072,
            'Remote-hosted Ollama Cloud — authentifiziert via OLLAMA_API_KEY. Fallback für Agents ohne lokale Runtime.',
            'Kein Lifecycle — Ollama betreibt den Endpoint.',
            6, true,
            NOW(), NOW()
        WHERE NOT EXISTS (SELECT 1 FROM runtimes WHERE slug = 'ollama-cloud')
        """
    )

    # Link every cli-bridge agent without an explicit runtime to the
    # cloud runtime. Preserves Sparky (already linked to qwen-coder-lms).
    op.execute(
        """
        UPDATE agents
        SET runtime_id = runtimes.id
        FROM runtimes
        WHERE runtimes.slug = 'ollama-cloud'
          AND agents.agent_runtime = 'cli-bridge'
          AND agents.runtime_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE agents SET runtime_id = NULL
        WHERE runtime_id IN (SELECT id FROM runtimes WHERE slug = 'ollama-cloud')
        """
    )
    op.execute("DELETE FROM runtimes WHERE slug = 'ollama-cloud'")
