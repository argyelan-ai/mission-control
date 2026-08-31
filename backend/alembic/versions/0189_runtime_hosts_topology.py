"""Verbund-UI Phase 1b — runtime_hosts (multi-node membership) + runtimes.topology.

Beschluss (Mark, 30.08.2026): MC kennt "ein Modell über mehrere Geräte"
bisher nicht — runtimes.host_id ist eine EINZELNE UUID. Marks GLM-Verbund
(alpha=head/rank0 mit API, Beta=worker/rank1 headless, TP=2) zeigt das
konkret: Beta hat 0 gebundene Runtimes und wirkt auf der Runtimes-Seite wie
ein leeres Gerät, obwohl es die Hälfte eines laufenden Modells trägt.

`runtimes.host_id` BLEIBT der Head — host_resolver bleibt unveraendert,
Solo-Runtimes (die ganz ueberwiegende Mehrheit) haben 0 Zeilen in der neuen
Tabelle. Nullbreaking Change: nichts an bestehendem Verhalten aendert sich,
bevor jemand tatsaechlich eine Zeile in runtime_hosts anlegt.

`runtime_hosts` ist rein deklarativ (Phase 1b) — keine Multi-Host-
Orchestrierung, die kommt bewusst spaeter. `runtimes.topology` (nullable
JSON) ist die SOLL-Seite (z.B. {"nodes": 2, "tp_total": 2, "roles": [...]}),
`runtime_hosts` ist die IST-Seite (welche Hosts tatsaechlich Mitglied sind).

Constraints stehen identisch im Modell (app/models/runtime_host.py) UND
hier — Konvention aus models/group.py: Tests bauen die Tabellen aus dem
Modell, Produktion aus der Migration; nur wenn beide dieselben Constraints
tragen, prueft der Test das Produktionsverhalten.

Revision ID: 0189_runtime_hosts_topology
Revises: 0188_host_ssh_credential
"""
import sqlalchemy as sa
from alembic import op

revision = "0189_runtime_hosts_topology"
down_revision = "0188_host_ssh_credential"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtimes", sa.Column("topology", sa.JSON(), nullable=True))

    op.create_table(
        "runtime_hosts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("runtime_id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("node_rank", sa.Integer(), nullable=False),
        sa.Column("endpoint_override", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["runtime_id"], ["runtimes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("runtime_id", "host_id", name="uq_runtime_hosts_runtime_host"),
        sa.UniqueConstraint("runtime_id", "node_rank", name="uq_runtime_hosts_runtime_rank"),
    )
    op.create_index("ix_runtime_hosts_runtime_id", "runtime_hosts", ["runtime_id"])
    op.create_index("ix_runtime_hosts_host_id", "runtime_hosts", ["host_id"])


def downgrade() -> None:
    op.drop_index("ix_runtime_hosts_host_id", table_name="runtime_hosts")
    op.drop_index("ix_runtime_hosts_runtime_id", table_name="runtime_hosts")
    op.drop_table("runtime_hosts")
    op.drop_column("runtimes", "topology")
