"""Hosts remember their SSH host key (trust on first use).

Until now every SSH connection MC made to a box ran with host-key checking
switched off (asyncssh ``known_hosts=None``): a different machine answering
on the box's address got MC's commands — and, during password onboarding,
the password. Now the first connection stores the box's public host key on
its host row and every later connection must present that same key
(services/ssh_host_keys.py).

  ssh_host_key                 OpenSSH public key line ("ssh-ed25519 AAAA…");
                               NULL = not learned yet (every existing row —
                               learned once on the next connection)
  ssh_host_key_learned_at      when it was learned or last re-trusted
  ssh_host_key_rejected        the DIFFERENT key a box presented the last
                               time it was refused; the operator can accept
                               exactly this key (POST /hosts/{id}/ssh-host-key/trust)
  ssh_host_key_rejected_at     when that refusal happened

Additive only: no existing value changes.

Revision ID: 0216_host_ssh_host_key
Revises: 0215_voice_live_calls
"""
import sqlalchemy as sa
from alembic import op

revision = "0216_host_ssh_host_key"
down_revision = "0215_voice_live_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("ssh_host_key", sa.Text(), nullable=True))
    op.add_column("hosts", sa.Column("ssh_host_key_learned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("hosts", sa.Column("ssh_host_key_rejected", sa.Text(), nullable=True))
    op.add_column("hosts", sa.Column("ssh_host_key_rejected_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "ssh_host_key_rejected_at")
    op.drop_column("hosts", "ssh_host_key_rejected")
    op.drop_column("hosts", "ssh_host_key_learned_at")
    op.drop_column("hosts", "ssh_host_key")
