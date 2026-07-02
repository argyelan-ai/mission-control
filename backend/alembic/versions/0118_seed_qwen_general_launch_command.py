"""Seed runtimes.qwen-general.launch_command for vllm sparkrun re-launch.

Revision ID: 0118
Revises: 0117
Create Date: 2026-05-15

The 2026-05-15 incident showed that sparkrun-launched vllm containers are
auto-removed (--rm) after `docker stop`, so the existing `docker start
<container_name>` path in start_runtime() 404'd. Migration 0117 added the
`launch_command` column; this migration seeds the qwen-general row with the
verified sparkrun invocation so fresh deployments pick up the right command
without an operator having to remember it.

Idempotent: WHERE launch_command IS NULL — re-running won't overwrite a
hand-edited value. Downgrade clears only the seeded payload, not any
manual edits.
"""
import sqlalchemy as sa
from alembic import op


revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


QWEN_GENERAL_LAUNCH_COMMAND = (
    "uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm "
    "--solo --no-rm --ensure --no-follow "
    "--label mc.runtime.slug=qwen-general"
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE runtimes SET launch_command = :cmd "
            "WHERE slug = 'qwen-general' AND launch_command IS NULL"
        ).bindparams(cmd=QWEN_GENERAL_LAUNCH_COMMAND)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE runtimes SET launch_command = NULL "
            "WHERE slug = 'qwen-general' AND launch_command = :cmd"
        ).bindparams(cmd=QWEN_GENERAL_LAUNCH_COMMAND)
    )
