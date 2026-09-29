"""0209 — drop board groups (E5).

Board groups had an API but no page, script or row (0 groups, 0 boards in a
group on the reference install); the router, model and client helpers were
removed in the same change, together with the uncalled phase
create/update/delete endpoints, the board-scoped approval list and two board
SSE streams (no tables behind those).

Drops (data included — take a backup first if an install still holds any):
  the column ``boards.board_group_id`` (its FK pointed into board_groups),
  then the table ``board_groups``.

Downgrade recreates the table and the column exactly as they were — empty.
The DDL below is a pg_dump of a fresh ``alembic upgrade 0208`` database; the
column comes back at the end of ``boards`` (column order is cosmetic).

Revision ID: 0209_drop_board_groups
Revises: 0208_drop_webhooks_meetings
"""
from alembic import op

revision = "0209_drop_board_groups"
down_revision = "0208_drop_webhooks_meetings"
branch_labels = None
depends_on = None

_SCHEMA = """
CREATE TABLE board_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    description text,
    icon text,
    color text,
    sort_order integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);
ALTER TABLE ONLY board_groups
    ADD CONSTRAINT board_groups_pkey PRIMARY KEY (id);
ALTER TABLE ONLY board_groups
    ADD CONSTRAINT board_groups_slug_key UNIQUE (slug);
"""


def upgrade() -> None:
    # The only FK into board_groups; drop it (and the column) first so the
    # table drop needs no CASCADE.
    op.execute("ALTER TABLE boards DROP CONSTRAINT IF EXISTS boards_board_group_id_fkey")
    op.execute("ALTER TABLE boards DROP COLUMN IF EXISTS board_group_id")
    op.execute("DROP TABLE IF EXISTS board_groups")


def downgrade() -> None:
    for statement in _SCHEMA.split(";\n"):
        if statement.strip():
            op.execute(statement.strip().rstrip(";"))
    op.execute("ALTER TABLE boards ADD COLUMN board_group_id uuid")
    op.execute(
        "ALTER TABLE boards ADD CONSTRAINT boards_board_group_id_fkey "
        "FOREIGN KEY (board_group_id) REFERENCES board_groups(id) ON DELETE SET NULL"
    )
