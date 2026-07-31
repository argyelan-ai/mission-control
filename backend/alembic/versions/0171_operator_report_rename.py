"""Channel-neutral operator reports: rename the telegram-specific columns.

The report-back contract ("the operator gets a final report before `done`")
was never about Telegram — Telegram was just the only place he read them.
With the OperatorReports adapter the same contract serves Slack too, so the
column names stop lying:

    tasks.report_sent_to_telegram -> tasks.report_sent_to_operator
    tasks.autonomous_telegram     -> tasks.autonomous_report
    loops.telegram_reports        -> loops.operator_reports

Pure renames — values survive, no backfill needed. Downgrade restores the
old names verbatim.

Revision ID: 0171_operator_report_rename
Revises: 0170_thread_slack_thread_ts
Create Date: 2026-07-31
"""
from alembic import op

revision = "0171_operator_report_rename"
down_revision = "0170_thread_slack_thread_ts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tasks", "report_sent_to_telegram", new_column_name="report_sent_to_operator"
    )
    op.alter_column(
        "tasks", "autonomous_telegram", new_column_name="autonomous_report"
    )
    op.alter_column(
        "loops", "telegram_reports", new_column_name="operator_reports"
    )


def downgrade() -> None:
    op.alter_column(
        "tasks", "report_sent_to_operator", new_column_name="report_sent_to_telegram"
    )
    op.alter_column(
        "tasks", "autonomous_report", new_column_name="autonomous_telegram"
    )
    op.alter_column(
        "loops", "operator_reports", new_column_name="telegram_reports"
    )
