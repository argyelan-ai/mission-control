"""remove Neo and Planner agents (Workstream E)

Revision ID: 0086
Revises: 0085
Create Date: 2026-04-20

The operator decided to remove both agents:
  - Planner: never had a DB-backed role anyway — orchestration sits with
    Boss since Phase 6 (Boss-Autonomy). The Docker container was always
    idle.
  - Neo: "too fuzzy, we have enough coders". FreeCode + Sparky cover the
    developer slots; Tester + Deployer cover QA + DevOps.

This migration is destructive but carefully staged:
  1. Promote any Neo/Planner-owned lessons to team scope (SET agent_id
     NULL on board_memory) so the team keeps the learnings.
  2. NULL out every FK that points at the two agents (task.assigned /
     owner / callback, agent.current_task_id). No task gets deleted —
     they stay as history, just unassigned.
  3. Delete the agents themselves.
  4. Delete the seeded `Neo` / `Planner` agent_templates if present.

The corresponding docker-compose.agents.yml entries are removed in the
same PR — the `${HOME}/.openclaw/agents/{neo,planner}/` workspace
directories can be pruned by hand afterwards (not data; just scratch).

Downgrade is a no-op: restoring deleted agents from scratch would be
guesswork and the templates are already gone.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


TARGET_NAMES = ("Neo", "Planner")


def upgrade() -> None:
    bind = op.get_bind()
    # 1. Collect the IDs so we can reference them in the FK cleanups.
    rows = bind.execute(
        sa.text("SELECT id FROM agents WHERE name = ANY(:names)"),
        {"names": list(TARGET_NAMES)},
    ).fetchall()
    target_ids = [str(r[0]) for r in rows]
    if not target_ids:
        # Nothing to do — migration is a no-op on this database.
        return

    params = {"ids": target_ids}

    # 2. Promote their lessons to team-scope (keep the content, drop the
    #    per-agent attribution so it doesn't hang on a dead FK).
    bind.execute(
        sa.text(
            "UPDATE board_memory SET agent_id = NULL WHERE agent_id::text = ANY(:ids)"
        ),
        params,
    )

    # 3. Inventory from pg_constraint (see review on PR #50): ~20 FKs
    #    reference `agents.id`. Some have ON DELETE SET NULL / CASCADE
    #    (safe to pre-null), others have no action and would block the
    #    DELETE FROM agents. For nullable columns we SET NULL; for NOT
    #    NULL columns we DELETE the referencing row (event / history /
    #    audit rows — acceptable loss for two deprecated agents).
    nullable_fks: list[str] = [
        "UPDATE tasks SET assigned_agent_id = NULL WHERE assigned_agent_id::text = ANY(:ids)",
        "UPDATE tasks SET owner_agent_id = NULL WHERE owner_agent_id::text = ANY(:ids)",
        "UPDATE tasks SET callback_agent_id = NULL WHERE callback_agent_id::text = ANY(:ids)",
        "UPDATE tasks SET help_request_from = NULL WHERE help_request_from::text = ANY(:ids)",
        "UPDATE agents SET current_task_id = NULL "
        "  WHERE current_task_id IN (SELECT id FROM tasks WHERE assigned_agent_id::text = ANY(:ids))",
        "UPDATE task_comments SET author_agent_id = NULL WHERE author_agent_id::text = ANY(:ids)",
        "UPDATE activity_events SET agent_id = NULL WHERE agent_id::text = ANY(:ids)",
        "UPDATE task_events SET agent_id = NULL WHERE agent_id::text = ANY(:ids)",
        "UPDATE approvals SET agent_id = NULL WHERE agent_id::text = ANY(:ids)",
        "UPDATE chat_messages SET sender_agent_id = NULL WHERE sender_agent_id::text = ANY(:ids)",
        "UPDATE content_pipelines SET research_agent_id = NULL WHERE research_agent_id::text = ANY(:ids)",
        "UPDATE content_pipelines SET writing_agent_id = NULL WHERE writing_agent_id::text = ANY(:ids)",
        "UPDATE content_pipelines SET review_agent_id = NULL WHERE review_agent_id::text = ANY(:ids)",
        "UPDATE scheduled_jobs SET agent_id = NULL WHERE agent_id::text = ANY(:ids)",
        "UPDATE playbooks SET default_agent_id = NULL WHERE default_agent_id::text = ANY(:ids)",
        "UPDATE project_phases SET default_agent_id = NULL WHERE default_agent_id::text = ANY(:ids)",
        "UPDATE install_log SET requester_agent_id = NULL WHERE requester_agent_id::text = ANY(:ids)",
    ]
    for sql in nullable_fks:
        try:
            bind.execute(sa.text(sql), params)
        except Exception:
            # Some tables may not exist on older schemas, or the column
            # was renamed. Best-effort: continue with the rest.
            pass

    # 4. NOT NULL columns — delete the referencing row. These are history/
    #    event/audit tables where losing rows for two deprecated agents
    #    is acceptable. `install_log.target_agent_id` has CASCADE, the row
    #    gets removed automatically with the agent.
    not_null_fks: list[str] = [
        "DELETE FROM chat_messages WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM agent_metrics WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM agent_meeting_messages WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM agent_messages WHERE from_agent_id::text = ANY(:ids) OR to_agent_id::text = ANY(:ids)",
        "DELETE FROM task_checkpoints WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM task_deliverables WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM task_checklist_items WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM cost_events WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM deploy_history WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM skill_runs WHERE agent_id::text = ANY(:ids)",
        "DELETE FROM agent_task_comment_cursor WHERE agent_id::text = ANY(:ids)",
    ]
    for sql in not_null_fks:
        try:
            bind.execute(sa.text(sql), params)
        except Exception:
            pass

    # 5. Delete the agents themselves.
    bind.execute(
        sa.text("DELETE FROM agents WHERE id::text = ANY(:ids)"),
        params,
    )

    # 6. Drop seeded templates if present. `agent_templates` is name-keyed.
    bind.execute(
        sa.text("DELETE FROM agent_templates WHERE name = ANY(:names)"),
        {"names": list(TARGET_NAMES)},
    )


def downgrade() -> None:
    # Restoring deleted agents would be guesswork — downgrade is a no-op.
    # If you need them back, restore from a pre-0086 DB backup.
    pass
