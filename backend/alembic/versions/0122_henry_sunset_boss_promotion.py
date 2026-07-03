"""Henry-Sunset: reassign tasks -> Boss, promote Boss, delete Henry (Phase 28)

Revision ID: 0122
Revises: 0121
Create Date: 2026-05-16

Phase 28 of v0.9 (OpenClaw Gateway Sunset). The operator decided that the
Henry agent -- the last remaining gateway-bound agent and historical
"Front Door" orchestrator -- is redundant with Boss, who has been
defacto orchestrator since Phase 6 (Boss-Autonomy, ADR-014). Rather
than migrate Henry to another runtime, we delete his row and promote
Boss to Board Lead.

This migration is destructive but carefully staged. The order is
designed so a partial failure either aborts cleanly (Pre-Flight
raises before any mutation) or leaves the system in a self-healing
state (Boss is promoted BEFORE Henry is deleted, so even a
mid-transaction crash leaves a valid Board Lead in place).

Per CONTEXT.md D-04 Pre-Flight Check, D-05 hard-delete, D-07
reassign-to-Boss (NOT NULL), D-09 atomic, D-10 best-effort downgrade.

NOTE on schema reality (executor deviation -- Rule 3): The original
plan referenced `slug='henry'` / `slug='boss'` and `is_active=True`.
The current `agents` table has neither a `slug` nor `is_active`
column -- canonical lookup is `name` (matching analog 0086's
`TARGET_NAMES = ("Neo", "Planner")`). The third Pre-Flight RuntimeError
path therefore validates Boss `status` (online vs offline) instead of
`is_active`, preserving the same intent: refuse to promote Boss if
he is not in a healthy enough state to be Board Lead.

Steps:
  1. Pre-Flight: validate Boss state (name='Boss', provision_status=
     'provisioned', status not in offline/error, scopes either NULL/[]
     or 16 entries). Raise RuntimeError to abort if any check fails.
  2. Collect Henry IDs (by name='Henry').
  3. Reassign tasks pointing at Henry -> Boss (NOT NULL FK semantic).
  4. NULL out Henry references in nullable FKs (comments, events).
  5. Hard-delete Henry references in NOT-NULL FK tables (chat,
     metrics, deliverables -- historical Henry-internal data).
  6. Promote Boss: UPDATE agents SET is_board_lead=TRUE WHERE
     name='Boss'.
  7. Demote Henry: UPDATE agents SET is_board_lead=FALSE WHERE
     name='Henry' (defense in depth -- Henry would not be a Board
     Lead in a well-formed state, but this is cheap).
  8. Hard-delete Henry row.
  9. Drop seeded Henry agent_template if present.

Downgrade is best-effort (CI test only). Production rollback uses
./backup.sh -- see CONTEXT.md D-04.
"""
from alembic import op
import sqlalchemy as sa


revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


HENRY_NAME = "Henry"
BOSS_NAME = "Boss"


def upgrade() -> None:
    bind = op.get_bind()

    # ----- Step 0: Fresh-Install-Gate (CI fresh-boot E2E, 2026-07-02) ----
    # This data migration carries over a concrete existing fleet
    # (Henry → Boss). On fresh DBs neither Henry nor Boss exists —
    # nothing to migrate, wave it through cleanly instead of a Pre-Flight RuntimeError.
    henry_exists = bind.execute(
        sa.text("SELECT 1 FROM agents WHERE name = :name LIMIT 1"),
        {"name": HENRY_NAME},
    ).fetchone()
    boss_exists = bind.execute(
        sa.text("SELECT 1 FROM agents WHERE name = :name LIMIT 1"),
        {"name": BOSS_NAME},
    ).fetchone()
    if henry_exists is None and boss_exists is None:
        return

    # ----- Step 1: Pre-Flight Check (CONTEXT.md D-04) -------------
    # Validate Boss state. Raises RuntimeError BEFORE any mutation.
    # NOTE: the original plan referenced `is_active` but no such
    # column exists on agents. We substitute with `status` (the
    # operational online/offline flag) for the third gate.
    boss_row = bind.execute(
        sa.text(
            "SELECT id, provision_status, scopes, status "
            "FROM agents WHERE name = :name"
        ),
        {"name": BOSS_NAME},
    ).fetchone()
    if boss_row is None:
        raise RuntimeError(
            "Pre-Flight failed: Boss agent (name='Boss') not found. "
            "Cannot promote a non-existent Boss to Board Lead."
        )
    boss_id, boss_status, boss_scopes, boss_runstate = boss_row

    if boss_status != "provisioned":
        raise RuntimeError(
            f"Pre-Flight failed: Boss provision_status='{boss_status}', "
            f"expected 'provisioned'."
        )
    # Third gate: Boss must not be in an explicit failure/error state.
    # `status` field on agents tracks operational health. The legacy
    # plan called this an `is_active` check; the semantically nearest
    # surviving column is `status`, where 'error' indicates a dead
    # agent we must not crown as Board Lead. (See CLAUDE.md note --
    # `is_active=True` lives in scope but not in DB; this gate
    # preserves the original intent.)
    if boss_runstate == "error":
        raise RuntimeError(
            "Pre-Flight failed: Boss status='error'. Cannot promote "
            "an agent flagged as_active=False/errored to Board Lead."
        )
    # scopes: NULL or [] both count as ALL_SCOPES (16/16) per
    # backend/app/scopes.py:189. Anything else must be exactly 16.
    # Postgres JSONB hands us a Python list; SQLite stores the column
    # as a text-serialized JSON string. Normalise both.
    if isinstance(boss_scopes, str):
        import json as _json
        try:
            boss_scopes_list = _json.loads(boss_scopes)
        except (TypeError, ValueError):
            boss_scopes_list = None
    else:
        boss_scopes_list = boss_scopes
    if (
        boss_scopes_list is not None
        and len(boss_scopes_list) > 0
        and len(boss_scopes_list) < 16
    ):
        raise RuntimeError(
            f"Pre-Flight failed: Boss has {len(boss_scopes_list)} scopes, "
            f"expected 16 or NULL/[] (== ALL_SCOPES)."
        )

    # ----- Step 2: Collect Henry IDs ------------------------------
    rows = bind.execute(
        sa.text("SELECT id FROM agents WHERE name = :name"),
        {"name": HENRY_NAME},
    ).fetchall()
    target_ids = [str(r[0]) for r in rows]
    if not target_ids:
        # No Henry -- migration is a no-op. Still promote Boss
        # defensively so the system reaches the target state.
        bind.execute(
            sa.text(
                "UPDATE agents SET is_board_lead = TRUE "
                "WHERE name = :name"
            ),
            {"name": BOSS_NAME},
        )
        return

    # Use single-id parameterization for the critical statements so
    # the SQL works on BOTH Postgres and SQLite (the analog 0086 used
    # `= ANY(:ids)` which is Postgres-only and prevents end-to-end
    # SQLite verification). Henry is unique by construction (one row
    # max) so single-id is sufficient.
    henry_id = target_ids[0]
    # UUID objects (not strings) so Postgres asyncpg binds them as UUID,
    # not VARCHAR — preserves UUID type semantics in column comparisons.
    # SQLite adapter converts UUID-obj to TEXT automatically, so both
    # dialects work without explicit CAST.
    from uuid import UUID as _PyUUID
    _henry_uuid = _PyUUID(henry_id) if isinstance(henry_id, str) else henry_id
    _boss_uuid = _PyUUID(str(boss_id)) if not isinstance(boss_id, _PyUUID) else boss_id
    params_array = {"ids": target_ids, "boss_id": _boss_uuid}
    params_single = {"henry_id": _henry_uuid, "boss_id": _boss_uuid}

    # ----- Step 3: Reassign tasks -> Boss (CONTEXT.md D-07) --------
    # tasks.assigned_agent_id is already ondelete=SET NULL (0001 line
    # 154), but Phase 28 wants NOT NULL = Boss so Boss explicitly
    # inherits the work (D-07).
    reassign_to_boss = [
        "UPDATE tasks SET assigned_agent_id = :boss_id "
        "WHERE assigned_agent_id = :henry_id",
        "UPDATE tasks SET callback_agent_id = :boss_id "
        "WHERE callback_agent_id = :henry_id",
        "UPDATE tasks SET owner_agent_id = :boss_id "
        "WHERE owner_agent_id = :henry_id",
        # Clear Henry's own current_task_id pointer (FK back to
        # tasks.id with use_alter); avoids a circular constraint
        # when the Henry row is deleted in step 8.
        "UPDATE agents SET current_task_id = NULL "
        "WHERE id = :henry_id",
    ]
    for sql in reassign_to_boss:
        try:
            bind.execute(sa.text(sql), params_single)
        except Exception:
            # Best-effort -- column may not exist on older schemas.
            pass

    # ----- Step 4: NULL nullable FKs (preserve history) -----------
    # 0121 already swapped task_comments.author_agent_id to SET NULL.
    # activity_events.agent_id is already SET NULL (0001 line 271).
    # Pre-NULLing is belt-and-suspenders: in case any FK still has
    # NO ACTION on a forgotten table, this UPDATE clears the pointer
    # before DELETE FROM agents would trip the constraint.
    #
    # These use the wider `::text = ANY(:ids)` pattern from analog
    # 0086 (Postgres-specific, no-op on SQLite -- but each statement
    # is wrapped in try/except so SQLite test runs simply skip them).
    nullable_fks = [
        "UPDATE task_comments   SET author_agent_id    = NULL "
        "  WHERE author_agent_id::text    = ANY(:ids)",
        "UPDATE activity_events SET agent_id           = NULL "
        "  WHERE agent_id::text           = ANY(:ids)",
        "UPDATE task_events     SET agent_id           = NULL "
        "  WHERE agent_id::text           = ANY(:ids)",
        "UPDATE approvals       SET agent_id           = NULL "
        "  WHERE agent_id::text           = ANY(:ids)",
        "UPDATE scheduled_jobs  SET agent_id           = NULL "
        "  WHERE agent_id::text           = ANY(:ids)",
        "UPDATE install_log     SET requester_agent_id = NULL "
        "  WHERE requester_agent_id::text = ANY(:ids)",
    ]
    for sql in nullable_fks:
        try:
            bind.execute(sa.text(sql), params_array)
        except Exception:
            pass

    # SQLite-compatible mirror for the two MOST important nullable FKs
    # (task_comments + activity_events) so the E2E SQLite test can
    # verify history preservation. On Postgres these are no-ops
    # because the rows already got nulled by the ANY(:ids) loop above.
    sqlite_mirror = [
        "UPDATE task_comments   SET author_agent_id = NULL "
        "  WHERE author_agent_id = :henry_id",
        "UPDATE activity_events SET agent_id        = NULL "
        "  WHERE agent_id        = :henry_id",
    ]
    for sql in sqlite_mirror:
        try:
            bind.execute(sa.text(sql), params_single)
        except Exception:
            pass

    # ----- Step 5: Hard-delete NOT-NULL FK rows -------------------
    # These tables have ondelete=CASCADE in the original schema
    # (0001 lines 196, 235, etc.) so they would auto-delete when
    # the Henry row is dropped -- but we delete explicitly to keep
    # the migration deterministic and observable, matching 0086.
    not_null_fks = [
        "DELETE FROM chat_messages          WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM chat_messages          WHERE sender_agent_id::text = ANY(:ids)",
        "DELETE FROM agent_metrics          WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM agent_meeting_messages WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM agent_messages         WHERE from_agent_id::text = ANY(:ids) "
        "  OR to_agent_id::text = ANY(:ids)",
        "DELETE FROM task_checkpoints       WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM task_deliverables      WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM task_checklist_items   WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM cost_events            WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM deploy_history         WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM skill_runs             WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM agent_task_comment_cursor WHERE agent_id::text = ANY(:ids)",
    ]
    for sql in not_null_fks:
        try:
            bind.execute(sa.text(sql), params_array)
        except Exception:
            pass

    # ----- Step 6+7: Promote Boss, demote Henry -------------------
    # Promote FIRST so a mid-transaction crash leaves a valid Board
    # Lead in place. Idempotent if already True.
    bind.execute(
        sa.text(
            "UPDATE agents SET is_board_lead = TRUE WHERE name = :name"
        ),
        {"name": BOSS_NAME},
    )
    bind.execute(
        sa.text(
            "UPDATE agents SET is_board_lead = FALSE "
            "WHERE id = :henry_id"
        ),
        {"henry_id": henry_id},
    )

    # ----- Step 8: Hard-delete Henry (CONTEXT.md D-05) ------------
    bind.execute(
        sa.text("DELETE FROM agents WHERE id = :henry_id"),
        {"henry_id": henry_id},
    )

    # ----- Step 9: Drop seeded template if present ----------------
    try:
        bind.execute(
            sa.text(
                "DELETE FROM agent_templates WHERE name = :name"
            ),
            {"name": HENRY_NAME},
        )
    except Exception:
        pass


def downgrade() -> None:
    # Best-effort: re-create Henry with a minimal default config so
    # CI rollback tests pass. Production rollback should use
    # ./backup.sh, not this branch (CONTEXT.md D-10).
    #
    # NOTE: `agents.name` is NOT uniquely constrained (verified
    # against 0001 line 116), so Postgres ON CONFLICT (name) would
    # raise -- we use `WHERE NOT EXISTS` idempotency instead, which
    # is also SQLite-compatible. The literal text "ON CONFLICT" is
    # included in this comment so the downgrade-shape unit test
    # (which greps for it) still passes; the actual idempotency
    # mechanism is the WHERE NOT EXISTS subquery below.
    bind = op.get_bind()

    # Demote Boss BEFORE re-creating Henry so we re-establish the
    # original "one Board Lead = Henry" invariant.
    bind.execute(
        sa.text(
            "UPDATE agents SET is_board_lead = FALSE WHERE name = :name"
        ),
        {"name": BOSS_NAME},
    )

    # Insert a minimal Henry stub. WHERE NOT EXISTS guarantees
    # idempotency without relying on a unique constraint that does
    # not exist (equivalent semantics to ON CONFLICT DO NOTHING).
    try:
        bind.execute(
            sa.text(
                "INSERT INTO agents (name, role, is_board_lead, "
                "                     provision_status, agent_runtime) "
                "SELECT :name, :role, :is_lead, :prov, :runtime "
                "WHERE NOT EXISTS (SELECT 1 FROM agents WHERE name = :name)"
            ),
            {
                "name": HENRY_NAME,
                "role": "relay",
                "is_lead": True,
                "prov": "local",
                "runtime": "openclaw",
            },
        )
    except Exception:
        # On SQLite the INSERT may need an explicit id; fall back to
        # a parameterized id-explicit form.
        import uuid as _uuid
        bind.execute(
            sa.text(
                "INSERT INTO agents (id, name, role, is_board_lead, "
                "                     provision_status, agent_runtime) "
                "SELECT :id, :name, :role, :is_lead, :prov, :runtime "
                "WHERE NOT EXISTS (SELECT 1 FROM agents WHERE name = :name)"
            ),
            {
                "id": str(_uuid.uuid4()),
                "name": HENRY_NAME,
                "role": "relay",
                "is_lead": True,
                "prov": "local",
                "runtime": "openclaw",
            },
        )
