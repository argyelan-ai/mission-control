"""seed anthropic-claude-opus + anthropic-claude-sonnet runtimes

Revision ID: 0080
Revises: 0079
Create Date: 2026-04-20

Idempotent seed for the two Anthropic Claude runtimes used by the Fleet-
Migration (see docs/superpowers/plans/2026-04-20-anthropic-claude-fleet-
migration.md). Opus 4.7 is used by Boss (host runtime, macOS Keychain OAuth),
Sonnet 4.6 by the 9 Docker-Container agents (CLAUDE_CODE_OAUTH_TOKEN env).

This migration only inserts the runtime rows — it does NOT re-bind any
existing agents. Re-provisioning is done in a later migration/operator step
once the claude-fleet image build and docker-compose update have landed.
"""
import uuid
from alembic import op


revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    opus_id = str(uuid.uuid4())
    sonnet_id = str(uuid.uuid4())

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
            '{opus_id}'::uuid,
            'anthropic-claude-opus',
            'Claude Opus 4.7 (Anthropic Pro/Max)',
            'cloud',
            'https://api.anthropic.com/v1/messages',
            NULL,
            'claude-opus-4-7',
            '["lead", "orchestrator", "reasoning"]'::jsonb,
            true, true, true,
            200000, 1000000,
            'Anthropic Pro/Max Subscription via CLAUDE_CODE_OAUTH_TOKEN (env) oder macOS Keychain (host). 1M Context-Window. Rate-Limit pro Account pooled.',
            'Kein Lifecycle — Anthropic betreibt den Endpoint. Auth via ''claude setup-token'' (1-Jahres-OAuth-Token) oder Keychain.',
            7, true,
            NOW(), NOW()
        WHERE NOT EXISTS (SELECT 1 FROM runtimes WHERE slug = 'anthropic-claude-opus')
        """
    )

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
            '{sonnet_id}'::uuid,
            'anthropic-claude-sonnet',
            'Claude Sonnet 4.6 (Anthropic Pro/Max)',
            'cloud',
            'https://api.anthropic.com/v1/messages',
            NULL,
            'claude-sonnet-4-6',
            '["general", "coder", "researcher"]'::jsonb,
            true, true, true,
            200000, 1000000,
            'Anthropic Pro/Max Subscription via CLAUDE_CODE_OAUTH_TOKEN (env) oder macOS Keychain (host). 1M Context-Window. Rate-Limit pro Account pooled.',
            'Kein Lifecycle — Anthropic betreibt den Endpoint. Auth via ''claude setup-token'' (1-Jahres-OAuth-Token) oder Keychain.',
            8, true,
            NOW(), NOW()
        WHERE NOT EXISTS (SELECT 1 FROM runtimes WHERE slug = 'anthropic-claude-sonnet')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE agents SET runtime_id = NULL
        WHERE runtime_id IN (
            SELECT id FROM runtimes
            WHERE slug IN ('anthropic-claude-opus', 'anthropic-claude-sonnet')
        )
        """
    )
    op.execute(
        "DELETE FROM runtimes WHERE slug IN ('anthropic-claude-opus', 'anthropic-claude-sonnet')"
    )
