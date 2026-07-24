"""Task status `waiting` — answer-wait for `ask --blocking` (Task 6/7).

Extends the Postgres transition trigger (0136) with the three `waiting`
edges: in_progress -> waiting (task asks a blocking question), waiting ->
in_progress (answer delivered / resume), waiting -> blocked (escalation).
Deliberately NOT inbox -> waiting — a task must actually be worked before it
can wait on an answer.

Mirrors app/task_status.py::VALID_TRANSITIONS, which is the Python-side
source of truth (checked in `_enforce_board_rules` / `work_context.py`); the
SQLite test engine doesn't run Postgres triggers, so that dict is what tests
exercise. This migration keeps the production DB-level safety net in sync.

Revision ID: 0159
Revises: 0158
"""
from alembic import op

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None

_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.validate_task_transition()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.status = NEW.status THEN RETURN NEW; END IF;

    IF NOT (
        (OLD.status = 'inbox'        AND NEW.status IN ('in_progress', 'blocked'))
        OR (OLD.status = 'in_progress' AND NEW.status IN ('review', 'done', 'blocked', 'inbox', 'failed', 'waiting'))
        OR (OLD.status = 'review'      AND NEW.status IN ('done', 'in_progress', 'inbox', 'blocked', 'failed', 'user_test'))
        OR (OLD.status = 'user_test'   AND NEW.status IN ('done', 'in_progress', 'review'))
        OR (OLD.status = 'waiting'     AND NEW.status IN ('in_progress', 'blocked'))
        OR (OLD.status = 'blocked'     AND NEW.status IN ('inbox', 'in_progress', 'failed'))
        OR (OLD.status = 'failed'      AND NEW.status IN ('inbox'))
        OR (OLD.status = 'done'        AND NEW.status IN ('in_progress'))
        OR (OLD.status = 'aborted'     AND NEW.status IN ('in_progress', 'inbox'))
    ) THEN
        RAISE EXCEPTION 'Invalid task transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$function$
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # plpgsql-Trigger — MC laeuft produktiv nur auf Postgres
    op.execute(_FUNCTION_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Restore the pre-0159 matrix (from 0136), dropping the `waiting` edges.
    op.execute("""
    CREATE OR REPLACE FUNCTION public.validate_task_transition()
     RETURNS trigger
     LANGUAGE plpgsql
    AS $function$
    BEGIN
        IF OLD.status = NEW.status THEN RETURN NEW; END IF;

        IF NOT (
            (OLD.status = 'inbox'        AND NEW.status IN ('in_progress', 'blocked'))
            OR (OLD.status = 'in_progress' AND NEW.status IN ('review', 'done', 'blocked', 'inbox', 'failed'))
            OR (OLD.status = 'review'      AND NEW.status IN ('done', 'in_progress', 'inbox', 'blocked', 'failed', 'user_test'))
            OR (OLD.status = 'user_test'   AND NEW.status IN ('done', 'in_progress', 'review'))
            OR (OLD.status = 'blocked'     AND NEW.status IN ('inbox', 'in_progress', 'failed'))
            OR (OLD.status = 'failed'      AND NEW.status IN ('inbox'))
            OR (OLD.status = 'done'        AND NEW.status IN ('in_progress'))
            OR (OLD.status = 'aborted'     AND NEW.status IN ('in_progress', 'inbox'))
        ) THEN
            RAISE EXCEPTION 'Invalid task transition: % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;

        RETURN NEW;
    END;
    $function$
    """)
