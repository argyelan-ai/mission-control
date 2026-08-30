"""host_pairing_codes.host_id FK -> ON DELETE SET NULL (review fix #6).

0185 created host_pairing_codes.host_id as a plain FK to hosts.id with no
ondelete clause (NO ACTION in Postgres). A pairing code for a pre-created
host (the "vor-erstelltes Gerät" case — see 0185's docstring) then blocks
DELETE /api/v1/hosts/{id} with a raw 500 the moment that host has ever had
a pairing code minted for it, instead of the clean 404/409 the rest of the
API returns for foreign-key conflicts.

A NEW migration rather than editing 0185/0186 in place: those are already
applied on the live DB (per Mark/Orchestrator, 30.08.2026) — same rule as
0121 (task_comments FK fix) for exactly the same reason.

Revision ID: 0187_pairing_fk_set_null
Revises: 0186_node_agent_inventory

NOTE (verified against a real Postgres 16 container, 30.08.2026): the
revision id itself must fit VARCHAR(32) — alembic_version.version_num.
"0187_pairing_code_host_fk_set_null" (34 chars) blew that column and
alembic upgrade failed on writing the bookkeeping row AFTER the DDL had
already run — this shorter id is the fix, not a stylistic choice.
"""
from alembic import op

revision = "0187_pairing_fk_set_null"
down_revision = "0186_node_agent_inventory"
branch_labels = None
depends_on = None

# Postgres auto-generated name (0185 did not pass an explicit name to
# sa.ForeignKeyConstraint) — same convention as 0121's _FK_NAME.
_FK_NAME = "host_pairing_codes_host_id_fkey"


def upgrade() -> None:
    with op.batch_alter_table("host_pairing_codes") as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.create_foreign_key(
            _FK_NAME,
            "hosts",
            ["host_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("host_pairing_codes") as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.create_foreign_key(
            _FK_NAME,
            "hosts",
            ["host_id"],
            ["id"],
        )
