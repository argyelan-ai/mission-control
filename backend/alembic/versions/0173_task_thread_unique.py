"""One task thread per task — merge duplicates, then enforce uniqueness.

Live incident 2026-08-04: the dispatcher held a task object loaded at claim
time while the operator posted onto the task's thread. `ensure_task_thread`
trusted the stale `task.thread_id` (None) and created a SECOND kind='task'
thread for the same task; `task.thread_id` was overwritten and the operator's
message became invisible to the agent (thread scope walks `task.thread_id`).

This migration merges existing duplicates into the oldest thread per task
(messages are appended with a seq offset; chat mappings carried over when the
keeper lacks them; per-thread cursors of the duplicate are dropped — the
at-least-once delivery contract makes a redelivery after merge acceptable)
and then adds the partial unique index that makes the race lose cleanly.
The index definition must stay identical to the Thread model's
`uq_threads_task_per_task` — tests build tables from the model, production
from this migration.

Revision ID: 0173_task_thread_unique
Revises: 0172_reference_agent_id
"""
import sqlalchemy as sa

from alembic import op

revision = "0173_task_thread_unique"
down_revision = "0172_reference_agent_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    dupes = bind.execute(
        sa.text(
            """
            SELECT task_id FROM threads
            WHERE kind = 'task' AND task_id IS NOT NULL
            GROUP BY task_id HAVING COUNT(*) > 1
            """
        )
    ).fetchall()

    for (task_id,) in dupes:
        rows = bind.execute(
            sa.text(
                """
                SELECT id, telegram_topic_id, slack_thread_ts FROM threads
                WHERE kind = 'task' AND task_id = :tid
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"tid": task_id},
        ).fetchall()
        keeper = rows[0][0]
        for dup_id, dup_topic, dup_slack_ts in rows[1:]:
            offset = bind.execute(
                sa.text("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE thread_id = :k"),
                {"k": keeper},
            ).scalar()
            bind.execute(
                sa.text(
                    "UPDATE messages SET thread_id = :k, seq = seq + :off WHERE thread_id = :d"
                ),
                {"k": keeper, "off": offset, "d": dup_id},
            )
            # Chat mappings are 1:1 (unique) — free them on the duplicate
            # FIRST, then hand them to the keeper only where it has none.
            bind.execute(
                sa.text(
                    "UPDATE threads SET telegram_topic_id = NULL, slack_thread_ts = NULL "
                    "WHERE id = :d"
                ),
                {"d": dup_id},
            )
            if dup_topic is not None:
                bind.execute(
                    sa.text(
                        "UPDATE threads SET telegram_topic_id = :v "
                        "WHERE id = :k AND telegram_topic_id IS NULL"
                    ),
                    {"v": dup_topic, "k": keeper},
                )
            if dup_slack_ts is not None:
                bind.execute(
                    sa.text(
                        "UPDATE threads SET slack_thread_ts = :v "
                        "WHERE id = :k AND slack_thread_ts IS NULL"
                    ),
                    {"v": dup_slack_ts, "k": keeper},
                )
            # Duplicate cursors: drop. Messages moved under the keeper sit
            # above its cursors' acked seq and simply redeliver (at-least-once).
            bind.execute(
                sa.text("DELETE FROM agent_thread_cursor WHERE thread_id = :d"),
                {"d": dup_id},
            )
            bind.execute(
                sa.text("DELETE FROM user_thread_cursor WHERE thread_id = :d"),
                {"d": dup_id},
            )
            bind.execute(
                sa.text("UPDATE tasks SET thread_id = :k WHERE thread_id = :d"),
                {"k": keeper, "d": dup_id},
            )
            bind.execute(sa.text("DELETE FROM threads WHERE id = :d"), {"d": dup_id})

    op.create_index(
        "uq_threads_task_per_task",
        "threads",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'task'"),
        sqlite_where=sa.text("kind = 'task'"),
    )


def downgrade() -> None:
    # The merge is not reversible (duplicate threads are gone by design);
    # only the uniqueness guarantee is rolled back.
    op.drop_index("uq_threads_task_per_task", table_name="threads")
