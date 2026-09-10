"""agents.context_tokens nullable — NULL = Kontextwert unbekannt (10.09.2026).

Vorfall: der Kontext-Scraper erkannte die neuen Statuszeilen nicht, der
Heartbeat liess ``context_pct`` weg und der Backend-Handler behielt den
letzten Wert — die Startseite zeigte stundenlang "100 % Kontext erreicht".
Ab jetzt setzt der Heartbeat den Wert nach drei Meldungen ohne Kontext auf
NULL ("unbekannt"); das Frontend zeigt dann "—" statt einer alten Zahl.

Revision ID: 0197
Revises: 0196
"""
from alembic import op
import sqlalchemy as sa

revision = "0197"
down_revision = "0196"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("agents", "context_tokens", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE agents SET context_tokens = 0 WHERE context_tokens IS NULL")
    op.alter_column("agents", "context_tokens", existing_type=sa.Integer(), nullable=False)
