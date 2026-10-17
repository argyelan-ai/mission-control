"""The power-managed box runtime_type gets a neutral name: ``flask_wol``.

ADR-042 introduced a runtime_type for an OpenAI-compatible server on a box
that sleeps when idle, is woken via Wake-on-LAN and is driven through a Flask
PowerShell control server. The value was named after one operator's machine
(``unsloth_porsche``). The DB columns have been generic since migration 0130;
this migration renames the one remaining value to ``flask_wol`` — the same
word as the host kind it pairs with (``hosts.kind``, ADR-048).

Data only, no schema change. Only ``runtimes.runtime_type`` changes; slug,
display name, host row and every other field stay exactly as the operator
named them. Idempotent: a second run finds nothing to rewrite.

Revision ID: 0217_runtime_type_flask_wol
Revises: 0216_host_ssh_host_key
"""
import sqlalchemy as sa
from alembic import op

revision = "0217_runtime_type_flask_wol"
down_revision = "0216_host_ssh_host_key"
branch_labels = None
depends_on = None

_LEGACY = "unsloth_porsche"
_NEUTRAL = "flask_wol"


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("UPDATE runtimes SET runtime_type = :new WHERE runtime_type = :old"),
        {"new": _NEUTRAL, "old": _LEGACY},
    )


def downgrade() -> None:
    # The old code only understands the old value.
    op.get_bind().execute(
        sa.text("UPDATE runtimes SET runtime_type = :old WHERE runtime_type = :new"),
        {"new": _NEUTRAL, "old": _LEGACY},
    )
