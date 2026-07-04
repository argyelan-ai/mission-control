"""tasks.repo_id — Registry-Repo-Auswahl für Ad-hoc-Tasks (ADR-052).

Die Task-Maske wählt Repos jetzt einheitlich aus der Repos-Registry
(ADR-050) statt über den binären use_separate_repo-Toggle. Bei Tasks ohne
Projekt bestimmt repo_id, welches Repo geklont wird und wessen
Arbeitsregeln in die Dispatch-Directive fliessen.

Revision ID: 0139
Revises: 0138
"""
import sqlalchemy as sa
from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("repo_id", sa.Uuid(), nullable=True))
    op.create_index("ix_tasks_repo_id", "tasks", ["repo_id"])
    op.create_foreign_key("fk_tasks_repo_id", "tasks", "repos", ["repo_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_tasks_repo_id", "tasks", type_="foreignkey")
    op.drop_index("ix_tasks_repo_id", table_name="tasks")
    op.drop_column("tasks", "repo_id")
