"""hosts.ssh_credential_id — Vault-backed SSH key for Auto-Onboarding (Phase 2).

Beschluss (Mark, 30.08.2026): "User gibt IP+Username+Passwort ein, MC macht
den Rest selbst" — der generierte Ed25519-Key landet als Credential
(credential_type='ssh_key') im bestehenden Fernet-Vault (models/credential.py,
KEIN neues Krypto), diese Spalte verdrahtet einen Host damit. ssh_key_path
bleibt für Alt-Hosts unverändert nutzbar — services/runtime_manager._ssh_run
probiert credential zuerst, fällt sonst auf ssh_key_path zurück.

ON DELETE SET NULL (nicht CASCADE): ein gelöschtes Credential darf einen Host
nicht mitreissen — der Host bleibt bestehen, verliert nur seinen Auto-Zugang
(genau das gleiche Argument wie 0187 für host_pairing_codes.host_id).

Revision ID: 0188_host_ssh_credential
Revises: 0187_pairing_fk_set_null
"""
import sqlalchemy as sa
from alembic import op

revision = "0188_host_ssh_credential"
down_revision = "0187_pairing_fk_set_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("ssh_credential_id", sa.Uuid(), nullable=True))
    op.create_index("ix_hosts_ssh_credential_id", "hosts", ["ssh_credential_id"])
    op.create_foreign_key(
        "hosts_ssh_credential_id_fkey",
        "hosts",
        "credentials",
        ["ssh_credential_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("hosts_ssh_credential_id_fkey", "hosts", type_="foreignkey")
    op.drop_index("ix_hosts_ssh_credential_id", table_name="hosts")
    op.drop_column("hosts", "ssh_credential_id")
