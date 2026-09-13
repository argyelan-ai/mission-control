"""Explizites PR-Reference-Feld auf Task (Task dd4bf92c, 2026-09-13).

Boss-Entscheidung (Option B) fuer "Review-Karten bekommen einen vorbereiteten
Arbeitsordner": die bisherige Ermittlung der PR-Nummer fuer eine Review-Karte
lief ueber das Absuchen von TaskComments nach dem Marker "PR erstellt:"
(agent_git.py, Pitfall H). Dieser Marker wird aber nur fuer `project_id`-
basierte Tasks automatisch geschrieben (`handle_review_pr_creation`) —
Registry-Repo-Tasks (`task.repo_id`, ADR-052, der Pfad, den dieses Board
fuer Code-Tasks nutzt) bekommen ihn nicht, weil der Entwickler dort selbst
pusht/PR erstellt. Die Heuristik dort zu erweitern macht sie breiter statt
zuverlaessiger — die ehrliche Antwort ist ein explizites Feld.

`pr_number`/`pr_url`: additiv, NULLABLE, kein Backfill (es gibt keine
zuverlaessige Quelle fuer bestehende Zeilen — genau das ist der Punkt).
Leer bleibt leer, bis ein Schreiber (handle_review_pr_creation oder die
Agent-PATCH auf status=review) den Wert explizit setzt. Der bestehende
"PR erstellt:"-Kommentar-Contract bleibt unangetastet und in Betrieb fuer
seine bisherigen Leser (_merge_pr_if_exists, handle_done_pr_merge).

Absichtlich NICHT `0197` (die naechste freie Nummer zum Zeitpunkt dieser
Migration): `0197_task_hold_reason` ist in PR #533 (C2) reserviert, noch
nicht auf main. `0198` haengt bewusst direkt an `0196_runtime_supports_
vision`, um keinen Namenskonflikt zu erzeugen — beim Mergen von #533 zuerst
ist ein Rebase von `down_revision` auf `0197_task_hold_reason` der normale,
erwartete Schritt (Alembic-Merge zweier Branches vom selben Head), keine
Kollision zweier gleich benannter Revisionen.

Revision ID: 0198_task_pr_reference
Revises: 0196_runtime_supports_vision
"""
import sqlalchemy as sa
from alembic import op

revision = "0198_task_pr_reference"
down_revision = "0196_runtime_supports_vision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("pr_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "pr_url")
    op.drop_column("tasks", "pr_number")
