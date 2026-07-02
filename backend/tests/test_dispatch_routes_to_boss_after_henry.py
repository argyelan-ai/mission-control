"""Phase 28 Plan 28-03 - D-11 + OCS-03 full-chain dispatch verification.

CONTEXT.md D-11 says find_dispatch_target() code is UNCHANGED in Phase 28
- Boss is selected purely by DB state (is_board_lead=True AND
non_gateway_runtime).

Checker revision (v0.9-ROADMAP OCS-03 acceptance):
  The success criterion is: "A freshly-created Task without explicit
  assignment is dispatched to Boss (Test: POST /tasks + Check
  dispatched_at + Boss as recipient in Activity-Log)."

The shallow find_dispatch_target() call doesn't prove this end-to-end -
it only proves the LEAF selector. The deep test mirrors
test_unified_dispatch.py::test_dispatch_non_lead_uses_push by invoking
`auto_dispatch_task()` - the service-layer entry point that
POST /api/v1/tasks delegates to as a BackgroundTask. This proves:
  1. Task starts unassigned (no assigned_agent_id).
  2. After auto_dispatch_task, task.assigned_agent_id == Boss.id.
  3. task.dispatched_at is set.
  4. An ActivityEvent row exists with agent_id == Boss.id (Boss appears
     as recipient in the activity log).

Schema note: agents table uses `name` (no `slug` column). Test seeds
use name="Boss" / name="Henry" and assert by name.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sa_text
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _seed_post_henry_state(make_board, make_agent):
    """Board with auto_dispatch_enabled + Boss as Board Lead. NO Henry row.
    Simulates the post-migration DB state.

    Boss has role="orchestrator" so find_dispatch_target returns reason
    "orchestrator" (top prio per dispatch.py:278-280). If role were "lead"
    instead, reason would be "board_lead" - both are valid per PATTERNS.md.
    """
    board = await make_board(
        name="Post-Sunset Board",
        slug=f"post-sunset-{uuid.uuid4().hex[:8]}",
        auto_dispatch_enabled=True,
    )
    boss = await make_agent(
        name="Boss",
        role="orchestrator",
        board_id=board.id,
        is_board_lead=True,          # Promoted by Phase 28 migration 0122
        provision_status="provisioned",
        agent_runtime="host",         # Non-gateway runtime (NON_GATEWAY_RUNTIMES)
        scopes=[],                    # [] = ALL_SCOPES (backward-compat)
    )
    return board, boss


@pytest.mark.asyncio
async def test_auto_dispatch_unassigned_task_routes_to_boss_with_activity_event(
    make_board, make_agent, make_task
):
    """OCS-03 full chain: a freshly-created unassigned task ends up
    assigned to Boss with dispatched_at set, AND the activity_event log
    contains Boss as agent_id.

    Mirrors test_unified_dispatch.py:55-82 (auto_dispatch_task path),
    plus an extra assertion that an ActivityEvent references Boss (the
    v0.9-ROADMAP OCS-03 success criterion's third clause "Boss als
    Empfaenger im Activity-Log").
    """
    board, boss = await _seed_post_henry_state(make_board, make_agent)

    # Freshly-created unassigned task - same shape as POST /api/v1/tasks
    # produces when caller omits assigned_agent_id.
    task = await make_task(
        board_id=board.id, status="inbox",
        title="OCS-03 full chain - unassigned",
        # assigned_agent_id intentionally omitted (None).
    )

    # Phase 29 / Gateway-Sunset: Boss runs on agent_runtime="host" — the
    # host branch in _deliver_dispatch_message sets dispatched_at directly
    # via launchd-managed poll-loop (no RPC needed). No rpc mock required.
    with patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch.engine", test_engine):
        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task.id, board.id)

    # Assert: full chain happened.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        assert refreshed is not None
        assert refreshed.assigned_agent_id == boss.id, (
            f"Boss should be the dispatch target post-Henry; "
            f"got {refreshed.assigned_agent_id}"
        )
        assert refreshed.dispatched_at is not None, (
            "dispatched_at must be set after auto_dispatch_task - "
            "this proves the full chain (find_dispatch_target -> "
            "rpc.chat_send -> task.dispatched_at = utcnow()) ran."
        )

        # Activity-log clause: at least one ActivityEvent row references
        # Boss as agent_id for this task. SQLite stores UUIDs as 32-char
        # hex (no hyphens). Bind the hex form for the task_id and JOIN
        # by name to dodge the agent_id UUID-stringification quirk —
        # mirrors the Wave 2 migration 0122 E2E test fix.
        tid_hex = str(task.id).replace("-", "")
        ev_rows_by_name = (await s.exec(sa_text(
            "SELECT count(*) FROM activity_events ae "
            "JOIN agents a ON a.id = ae.agent_id "
            "WHERE ae.task_id = :tid AND a.name = 'Boss'"
        ).bindparams(tid=tid_hex))).scalar_one()
        assert ev_rows_by_name >= 1, (
            f"Expected >=1 activity_event with agent=Boss for this task; "
            f"got {ev_rows_by_name}. The auto_dispatch_task path must "
            f"emit at least one event tagged with the recipient (Boss). "
            f"This satisfies the v0.9-ROADMAP OCS-03 'Activity-Log' clause."
        )

        # Sanity: Henry is not in the system, so no Henry-tagged events.
        henry_rows = (await s.exec(sa_text(
            "SELECT count(*) FROM activity_events ae "
            "JOIN agents a ON a.id = ae.agent_id "
            "WHERE a.name = 'Henry'"
        ))).scalar_one()
        assert henry_rows == 0, "Henry must not appear in any activity event"


@pytest.mark.asyncio
async def test_find_dispatch_target_leaf_returns_boss_after_henry_removed(
    make_board, make_agent, make_task
):
    """Leaf-level D-11 verification (kept as a focused unit test alongside
    the full-chain test above). find_dispatch_target() returns Boss with
    reason in {'orchestrator', 'board_lead'}.

    D-11: code unchanged; this proves the property holds via DB state.
    """
    from app.services.dispatch import find_dispatch_target

    board, boss = await _seed_post_henry_state(make_board, make_agent)

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await make_task(
                board_id=board.id, status="inbox",
                title="Leaf-level D-11 dispatch test",
            )
            db_task = await s.get(type(task), task.id)
            assert db_task is not None
            agent, reason = await find_dispatch_target(s, db_task, board.id)

    assert agent is not None, "Dispatch must find Boss as default target"
    assert agent.name == "Boss", f"Expected Boss; got name={agent.name}"
    # Boss has role=orchestrator -> reason 'orchestrator' (top prio per
    # dispatch.py:278-280). If role were 'lead', reason would be
    # 'board_lead'. Both are valid per PATTERNS.md.
    assert reason in ("orchestrator", "board_lead"), (
        f"Expected dispatch reason in (orchestrator, board_lead); "
        f"got {reason}"
    )


@pytest.mark.asyncio
async def test_no_henry_in_target_pool(make_board, make_agent, make_task):
    """Sanity: a board with only Boss must dispatch to Boss (not None,
    not a phantom Henry)."""
    from app.services.dispatch import find_dispatch_target

    board, boss = await _seed_post_henry_state(make_board, make_agent)

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await make_task(
                board_id=board.id, status="inbox",
                title="Empty-pool dispatch",
            )
            db_task = await s.get(type(task), task.id)
            assert db_task is not None
            agent, _ = await find_dispatch_target(s, db_task, board.id)

    assert agent is not None
    assert agent.name != "Henry", "Henry must not appear as a target"
    assert agent.name == "Boss"
