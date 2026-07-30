"""Merge the two parallel 0167 branches back into one head.

PR #183 (0167_runtime_display_names -> 0168_merge_lmstudio_rows) and
PR #184 (0167_project_telegram_topic_id) both branched off
0166_thread_telegram_topic_id — `alembic upgrade head` refused with
"Multiple head revisions" and blocked the 28.07. deploy. No-op merge
revision; both branches touch independent tables.

Since #194 this is no longer a merge: that PR renumbered the lmstudio
migration to 0169_merge_lmstudio_rows and re-pointed it at
0168_runtime_display_names, which already descends from
0167_project_telegram_topic_id. Both former branches are therefore in one
line, and this revision has a single parent. It kept the old two-parent
tuple naming a revision id that no longer existed — two heads and a dead
pointer on main, i.e. a broken `upgrade head` for every fresh install.

Revision ID: 0169_merge_heads
Revises: 0169_merge_lmstudio_rows
Create Date: 2026-07-28
"""

revision = "0169_merge_heads"
down_revision = "0169_merge_lmstudio_rows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
