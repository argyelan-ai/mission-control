"""Merge the two parallel 0167 branches back into one head.

PR #183 (0167_runtime_display_names -> 0168_merge_lmstudio_rows) and
PR #184 (0167_project_telegram_topic_id) both branched off
0166_thread_telegram_topic_id — `alembic upgrade head` refused with
"Multiple head revisions" and blocked the 28.07. deploy. No-op merge
revision; both branches touch independent tables.

Revision ID: 0169_merge_heads
Revises: 0168_merge_lmstudio_rows, 0167_project_telegram_topic_id
Create Date: 2026-07-28
"""

revision = "0169_merge_heads"
down_revision = ("0168_merge_lmstudio_rows", "0167_project_telegram_topic_id")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
