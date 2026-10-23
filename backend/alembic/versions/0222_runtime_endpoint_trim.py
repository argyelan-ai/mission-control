"""Trim the address fields of existing runtime rows (round-3 user test t4 S3).

A runtime row carried its endpoint with a leading space
(``" http://<box>:5559/v1"``). The add-runtime wizard's probe trims its URL,
the save did not — so the probe succeeded and every later request to the row
failed with "UnsupportedProtocol" (the Models tab called the box unreachable).
``RuntimeCreate`` / ``RuntimeUpdate`` trim these fields from now on; this
migration trims the rows saved before.

Data only, no schema change. Postgres ``btrim`` with an explicit set (space,
tab, CR, LF — the default trims spaces only); SQLite's ``trim`` takes the
same two-argument form. Only rows that change are touched. Downgrade is a
no-op: the untrimmed values were never wanted.

Revision ID: 0222_runtime_endpoint_trim
Revises: 0221_bench_entry_position
"""
from alembic import op

revision = "0222_runtime_endpoint_trim"
down_revision = "0221_bench_entry_position"
branch_labels = None
depends_on = None

_COLUMNS = ("endpoint", "control_url", "healthcheck_path", "model_identifier")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        trimmed = "btrim({col}, E' \\t\\r\\n')"
    else:  # SQLite (test suites): trim(x, chars) with the same set
        trimmed = "trim({col}, ' ' || char(9) || char(13) || char(10))"
    for col in _COLUMNS:
        expr = trimmed.format(col=col)
        op.execute(
            f"UPDATE runtimes SET {col} = {expr} "
            f"WHERE {col} IS NOT NULL AND {col} <> {expr}"
        )


def downgrade() -> None:
    pass
