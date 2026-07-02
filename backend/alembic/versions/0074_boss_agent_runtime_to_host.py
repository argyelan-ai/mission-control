"""boss agent_runtime to host

Revision ID: 0074
Revises: 0073
Create Date: 2026-04-17

Boss läuft auf dem Host (claude-Binary direkt, nicht im Docker-Container).
Setzt agent_runtime von 'cli-bridge' auf 'host', damit Backend-Services
container-managed Agents (cli-bridge) von host-managed Agents (host)
unterscheiden können (z.B. docker_agent_sync.py, Sessions-Endpoint).

Renamed from 0073 to 0074 during merge with main (0073 taken by
agent_task_comment_cursor). Idempotent UPDATE — safe to re-run.
"""

from alembic import op


revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE agents SET agent_runtime = 'host' WHERE name = 'Boss'")


def downgrade():
    op.execute("UPDATE agents SET agent_runtime = 'cli-bridge' WHERE name = 'Boss'")
