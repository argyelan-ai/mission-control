"""runtimes.launch_command — recipe-aware vllm_docker start.

Revision ID: 0117
Revises: 0116
Create Date: 2026-05-15

Motivation: vllm_docker runtimes were started via `docker start <container_name>`
on a name persisted at first registration. Tools like `sparkrun run`
(NVIDIA DGX) and other recipe-driven launchers default to `--rm` semantics —
the container is auto-removed after `docker stop`. Subsequent `/runtimes/{id}/start`
clicks then 404 the container.

`launch_command` stores the shell command to (re-)create the container when
`docker start` finds nothing. `start_runtime()` falls back to executing it via
SSH; the recipe is responsible for labelling the container (e.g.
`--label mc.runtime.slug=...`) so future lifecycle calls can find it again.

Nullable: existing rows keep the docker-start-only path until an operator
fills the field in via the UI or a seed update.
"""
import sqlalchemy as sa
from alembic import op


revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtimes",
        sa.Column("launch_command", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runtimes", "launch_command")
