"""0158 — Interaction Model 2.0: threads, messages, agent_thread_cursor.

threads: conversation container (kind = task | side | dm). Optionally bound
to a task and/or project. summary/summary_through_seq support future
context-compaction (older messages folded into a running summary).
threads.task_id uses ondelete=SET NULL (mc-task-delete-guard): a thread
outlives its task's deletion instead of RESTRICTing it, same rationale as
bench_entries.task_id.

messages: append-only log per thread, uniquely ordered via (thread_id, seq).
question_meta carries the structured "awaiting answer" state for
message_type='question' rows — the partial index below lets the poll/inbox
flow cheaply find open questions without scanning the whole table.

agent_thread_cursor: per-agent delivery/ack position within a thread,
composite PK (agent_id, thread_id) — same shape as the pre-existing
agent_task_comment_cursor table.

tasks.thread_id: nullable FK added via ALTER (breaks the tasks<->threads
cycle; Thread.task_id points back at tasks.id). Matches the use_alter=True
technique already used for tasks.triggered_by_deliverable_id.

Revision ID: 0158
Revises: 0157
"""
import sqlalchemy as sa
from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True
        ),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_through_seq", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_threads_task_id", "threads", ["task_id"])
    op.create_index("ix_threads_project_id", "threads", ["project_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("sender_type", sa.String(), nullable=False),
        sa.Column(
            "sender_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True
        ),
        sa.Column("message_type", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "reply_to", sa.Uuid(), sa.ForeignKey("messages.id"), nullable=True
        ),
        sa.Column(
            "mentions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column("question_meta", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"])
    op.create_index("ix_messages_thread_seq", "messages", ["thread_id", "seq"], unique=True)
    op.create_index(
        "ix_messages_open_questions", "messages", ["thread_id"],
        postgresql_where=sa.text("message_type = 'question' AND (question_meta->>'awaiting')::boolean IS TRUE"),
    )

    op.create_table(
        "agent_thread_cursor",
        sa.Column(
            "agent_id",
            sa.Uuid(),
            sa.ForeignKey("agents.id"),
            primary_key=True,
        ),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_delivered_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_acked_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    op.add_column(
        "tasks",
        sa.Column("thread_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_thread_id", "tasks", "threads", ["thread_id"], ["id"]
    )
    op.create_index("ix_tasks_thread_id", "tasks", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_thread_id", table_name="tasks")
    op.drop_constraint("fk_tasks_thread_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "thread_id")

    op.drop_table("agent_thread_cursor")

    op.drop_index("ix_messages_open_questions", table_name="messages")
    op.drop_index("ix_messages_thread_seq", table_name="messages")
    op.drop_index("ix_messages_thread_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_threads_project_id", table_name="threads")
    op.drop_index("ix_threads_task_id", table_name="threads")
    op.drop_table("threads")
