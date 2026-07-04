"""Task-Status-Transition-Trigger — Invariante aus der Ur-Instanz in Code giessen.

`enforce_task_transition` existierte bisher NUR in der Ur-Instanz-DB (manuell
angelegt, in keiner Migration) — Fresh-Installs liefen ohne die Invariante.
Das adversariale Review 2026-07-04 fand dadurch einen Fix, der auf Fresh-
Installs funktioniert haette, in der Ur-Instanz aber crashte (done→inbox).
Ab jetzt haben alle Installationen dieselbe Transition-Matrix.

Idempotent: CREATE OR REPLACE + DROP TRIGGER IF EXISTS — auf der Ur-Instanz
ein No-op-Aequivalent, auf Fresh-Installs die Neuanlage.

Revision ID: 0136
Revises: 0135
"""
from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None

# 1:1 der Live-Definition (pg_get_functiondef, 2026-07-04).
_FUNCTION_SQL = """
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
"""

_TRIGGER_SQL = """
CREATE TRIGGER enforce_task_transition
BEFORE UPDATE OF status ON public.tasks
FOR EACH ROW EXECUTE FUNCTION validate_task_transition()
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # plpgsql-Trigger — MC laeuft produktiv nur auf Postgres
    op.execute(_FUNCTION_SQL)
    op.execute("DROP TRIGGER IF EXISTS enforce_task_transition ON public.tasks")
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS enforce_task_transition ON public.tasks")
    op.execute("DROP FUNCTION IF EXISTS public.validate_task_transition()")
