"""0160 — Interaction Model 2.0 pilot flag `agents.comm_v2` + thread backfill.

`comm_v2` gates the pilot rollout of Interaction Model 2.0 (§8.1/§3.3):
new_messages in the poll payload (routers/agents.py), poll.sh's message
path, and the auto-promote-on-resolution shutoff (agent_comments.py /
task_runner.py). False by default — flipped per-agent at deploy time, same
mechanism as use_operating_card (Migration 0151).

Data step: every pre-existing Task without a thread_id gets a
Thread(kind="task") + a seed system Message at seq 1 ("Migration: bisheriger
Verlauf liegt in den Kommentaren dieses Tasks."), so last_message_at is never
NULL for a task that predates threads (§10.4). No comment-to-message mapping
here — that is the fleet-wide W2 migration, out of scope for this pilot flag.

Implemented as plain SQL over `op.get_bind()` (mirrors 0152's
migrate_connection pattern) rather than the ORM, so it works uniformly
against Postgres (prod) and SQLite (tests) without pulling in async session
machinery inside a sync Alembic step. `app.services.messaging.
backfill_task_threads` is the async/ORM-equivalent of this function, kept in
sync by hand and covered by the async test suite (tests/test_comm_v2_flag.py)
since the sync core variant below has no direct unit test of its own.

Revision ID: 0160
Revises: 0159
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0160"
down_revision = "0159"
branch_labels = None
depends_on = None

SEED_MESSAGE_BODY = "Migration: bisheriger Verlauf liegt in den Kommentaren dieses Tasks."


def backfill_task_threads(conn) -> int:
    """For every Task without a thread_id: create a Thread(kind="task") + a
    seed system Message at seq 1, then point the Task at the new thread.

    Idempotent — only touches tasks where thread_id IS NULL, so a second run
    is a no-op; tasks that already carry a thread are left untouched.

    created_at is set explicitly rather than left to the columns'
    server_default=NOW() — NOW() is a Postgres-only function and this same
    function runs against SQLite in tests (mirrors the ORM models'
    default_factory=datetime.utcnow, which likewise bypasses the server
    default on the ORM insert path). New ids are generated as bare hex
    (uuid.hex, no dashes) — SQLite's CHAR(32) storage for sa.Uuid columns
    is dash-free, and Postgres accepts either form for its native uuid
    type, so hex is the one representation that round-trips correctly on
    both dialects.
    """
    from sqlalchemy import text

    task_rows = conn.execute(
        text("SELECT id FROM tasks WHERE thread_id IS NULL")
    ).fetchall()

    count = 0
    for (task_id,) in task_rows:
        now = datetime.now(timezone.utc)
        thread_id = uuid.uuid4().hex
        conn.execute(
            text(
                "INSERT INTO threads (id, kind, task_id, created_at) "
                "VALUES (:id, 'task', :task_id, :created_at)"
            ),
            {"id": thread_id, "task_id": task_id, "created_at": now},
        )
        conn.execute(
            text(
                "INSERT INTO messages "
                "(id, thread_id, seq, sender_type, message_type, body, mentions, created_at) "
                "VALUES (:id, :thread_id, 1, 'system', 'system', :body, '[]', :created_at)"
            ),
            {
                "id": uuid.uuid4().hex,
                "thread_id": thread_id,
                "body": SEED_MESSAGE_BODY,
                "created_at": now,
            },
        )
        conn.execute(
            text("UPDATE tasks SET thread_id = :thread_id WHERE id = :task_id"),
            {"thread_id": thread_id, "task_id": task_id},
        )
        count += 1
    return count


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("comm_v2", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    backfill_task_threads(op.get_bind())


def downgrade() -> None:
    # Only comm_v2 is this migration's schema. The backfilled threads/messages
    # are data, not schema owned by this revision — they stay in place on
    # downgrade (dropping them would also delete conversation history for
    # threads created by real comm_v2 usage after upgrade, which this
    # migration has no way to distinguish from its own backfill rows).
    op.drop_column("agents", "comm_v2")
