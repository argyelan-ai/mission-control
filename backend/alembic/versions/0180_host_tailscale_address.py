"""Host.tailscale_host — separate Tailscale address per host.

Live incident (see services/address_classify docstring): a runtime endpoint
was built straight from Host.ssh_host, so whichever address an operator had
entered there — LAN IP or Tailscale IP — became the endpoint every agent
talks to. SSH from the backend container tolerated the LAN IP; an HTTP call
from a host agent did not.

This column gives a host a second, optional address dedicated to that
purpose. Nullable, no backfill — existing rows keep exactly today's
behaviour (ssh_host is still what endpoint construction falls back to) until
an operator fills it in for boxes that have one.

Numbered 0180 (not 0179) and chained behind 0179_prestart_mem_available_wait
(open PR #299) to avoid a two-heads collision — #299 claimed 0179 first with
the same down_revision (0178_prestart_memory_prep). If #299 ends up NOT
merging, this file's down_revision needs to move back to
0178_prestart_memory_prep and the revision back to 0179.
"""
import sqlalchemy as sa

from alembic import op

revision = "0180_host_tailscale_address"
down_revision = "0179_prestart_mem_available_wait"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hosts", sa.Column("tailscale_host", sa.String(length=128), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("hosts", "tailscale_host")
