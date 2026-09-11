"""Bug fixes 2026-09-09:

Bug 1 — `assigned_agent_id` was silently discarded by `AgentTaskUpdate`
(agent-scoped PATCH answered 200 without changing the assignment). Now:
- Board Leads can reassign via the agent PATCH (dispatch cycle reset, parity
  with the user-scoped TaskUpdate endpoint).
- Non-leads get an explicit 403 — never a silent no-op 200.

Bug 2 — checklist items could not be deleted at all (405), so a parked
inbox task with open items could not be closed without falsifying history
(foreign items marked done). Now: DELETE on a single item (user endpoint +
Board-Lead agent endpoint), counters recomputed, and the done-guard passes.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup(*, lead: bool = False):
    """Board + caller agent (+ worker agent) + task assigned to the worker."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token
    from app.utils import utcnow

    board_id, caller_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # lead=False: the caller IS the assigned worker; lead=True: separate worker.
    worker_id = caller_id if not lead else uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:8]}"))
        # Flush the Board on its own before the Agent rows that FK to it:
        # these models carry no relationship() (see conftest.py's "SQLite:
        # do NOT enable foreign keys" note), so SQLAlchemy's unit-of-work has
        # no dependency edge between Board and Agent and does not order the
        # Agent INSERTs after the Board INSERT within one flush. SQLite never
        # enforces the FK either way, so this was invisible there; Postgres
        # does enforce it (agents_board_id_fkey) and rejected the batch.
        await s.flush()
        raw_token, token_hash = generate_agent_token()
        s.add(Agent(
            id=caller_id, name="Lead" if lead else "Worker", board_id=board_id,
            agent_token_hash=token_hash, is_board_lead=lead,
            scopes=["tasks:read", "tasks:write"],
        ))
        if lead:
            s.add(Agent(
                id=worker_id, name="Worker", board_id=board_id,
                is_board_lead=False, scopes=["tasks:read", "tasks:write"],
            ))
        await s.flush()
        s.add(Task(
            id=task_id, board_id=board_id, title="Reassign me",
            status="in_progress", assigned_agent_id=worker_id,
            dispatched_at=utcnow(), ack_at=utcnow(),
            dispatch_attempt_id=str(uuid.uuid4()),
        ))
        await s.commit()
    return {
        "board_id": board_id, "caller_id": caller_id, "worker_id": worker_id,
        "task_id": task_id, "token": raw_token,
    }


def _headers(ids):
    return {"Authorization": f"Bearer {ids['token']}"}


def _headers(ids, attempt: str | None = None):
    h = {"Authorization": f"Bearer {ids['token']}"}
    if attempt:
        h["X-Dispatch-Attempt-Id"] = attempt
    return h

@pytest.mark.asyncio
async def _task(ids):
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Task, ids["task_id"])

@pytest.mark.asyncio
async def test_lead_can_reassign_via_agent_patch(client):
    """Board Lead PATCHes assigned_agent_id → assignment actually changes."""
    ids = await _setup(lead=True)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        attempt = (await s.get(Task, ids["task_id"])).dispatch_attempt_id
    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        headers=_headers(ids, attempt=attempt),
        json={"assigned_agent_id": str(ids["caller_id"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_agent_id"] == str(ids["caller_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.assigned_agent_id == ids["caller_id"]
        # New dispatch cycle: old tracking data reset
        assert task.dispatched_at is None
        assert task.ack_at is None

@pytest.mark.asyncio
async def test_non_lead_reassign_gets_403_not_silent_200(client):
    """Worker tries to reassign → explicit 403, assignment unchanged."""
    ids = await _setup(lead=False)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        attempt = (await s.get(Task, ids["task_id"])).dispatch_attempt_id
    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        headers=_headers(ids, attempt=attempt),
        json={"assigned_agent_id": str(ids["caller_id"])},
    )
    assert resp.status_code == 403, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.assigned_agent_id == ids["worker_id"]


@pytest.mark.asyncio
async def test_unknown_patch_field_no_longer_silent_200(client):
    """Regression pin: a field unknown to AgentTaskUpdate must 422, not a
    200 that changes nothing (the original bug class). Worker PATCHes its
    OWN task — no ownership/permission guard in the way, pure schema test."""
    ids = await _setup(lead=False)
    attempt = (await _task(ids)).dispatch_attempt_id
    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        headers=_headers(ids, attempt=attempt),
        json={"assigned_agent_id_xyz": str(uuid.uuid4())},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_lead_combined_status_and_reassign_both_apply(client):
    """PATCH with `status` AND `assigned_agent_id` in ONE request must apply
    both — not silently drop one depending on which block ran first.

    Every other test in this file sends `assigned_agent_id` alone; this pins
    the combination itself. See
    `test_lead_reassign_ordering_sees_fresh_assignee_not_stale_copy` below
    (Postgres lane) for the counter-run that proves the block ORDER — not
    just this combination — actually matters.
    """
    ids = await _setup(lead=True)
    attempt = (await _task(ids)).dispatch_attempt_id

    # Evidence guard (in_progress -> review) needs >= 1 progress/resolution/
    # reflection comment from the caller — unrelated to this test's subject,
    # just satisfying a separate precondition.
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=ids["task_id"], author_type="agent",
            author_agent_id=ids["caller_id"], comment_type="progress",
            content="Update — reassigning + moving to review",
        ))
        await s.commit()

    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        headers=_headers(ids, attempt=attempt),
        json={"status": "review", "assigned_agent_id": str(ids["caller_id"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "review"
    assert body["assigned_agent_id"] == str(ids["caller_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.status == "review"
        assert task.assigned_agent_id == ids["caller_id"]
        # Dispatch cycle reset from the reassignment (dispatched_at, same as
        # the reassign-only test above). ack_at is NOT asserted None here:
        # the retroactive-ACK branch for status=review re-sets it right
        # after, independent of the reassignment — asserting it would test
        # that unrelated branch, not this fix.
        assert task.dispatched_at is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lead_reassign_ordering_sees_fresh_assignee_not_stale_copy(client, monkeypatch):
    """Counter-proof for W3: `lock_task()` must run BEFORE the reassignment
    block reads `_old_assigned = task.assigned_agent_id` (agent_task_status.py
    :1721) — not because of `populate_existing` losing in-memory writes (it
    doesn't, see the comment above that call), but because `_old_assigned`
    must reflect the CURRENT DB row, not whatever this request's `task`
    object still held from its earlier ownership-check load.

    Simulates the concurrent writer Rex's review names (DB=B, memory=A,
    PATCH targets B): a hook on `lock_task()` moves the row's
    `assigned_agent_id` to `third_id` on a SEPARATE connection right before
    the real fresh re-read — standing in for "another request already
    reassigned + dispatched this task to `third_id` between this request's
    early load and its lock_task() call". The PATCH also targets `third_id`
    (the lead re-confirming an assignment that, unknown to it, already
    happened concurrently).

    Correct order (lock_task before the reassignment block): `_old_assigned`
    is read off the freshly-locked task, so it already equals `third_id` ==
    the PATCH target -> no real change -> the dispatch-cycle fields
    (`dispatched_at`) are left untouched, not reset.

    SABOTAGE-PROBE: moving the reassignment block to run BEFORE
    `lock_task()` makes `_old_assigned` read the STALE pre-concurrent-write
    value (`worker_id`) instead — `worker_id != third_id` -> the block
    wrongly takes the "changed" branch and resets `dispatched_at`/`ack_at`,
    killing the dispatch that (per the DB) already went to `third_id`. This
    test's assertion on `dispatched_at` is what catches that regression —
    see the PR comment for the sabotage-probe run confirming it (same
    pattern as this file's sibling assertions and as
    `test_lock_and_set_sees_fresh_status_not_stale_identity_map` in
    test_task_status_postgres.py, which pins the identical
    `lock_task`/`populate_existing` primitive for the `status` field).
    Postgres lane only (`@pytest.mark.postgres`): the concurrent write
    needs NullPool's genuinely separate connection — see conftest.py's
    "Test engine" section on why the SQLite lane's shared connection makes
    identity-map traps like this look harmless.
    """
    from app.models.agent import Agent
    import app.services.task_state as task_state_module

    ids = await _setup(lead=True)
    third_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(
            id=third_id, name="Third", board_id=ids["board_id"],
            is_board_lead=False, scopes=["tasks:read", "tasks:write"],
        ))
        await s.commit()
    attempt = (await _task(ids)).dispatch_attempt_id

    original_lock_task = task_state_module.lock_task

    async def _lock_task_after_concurrent_reassign(session, task_id):
        # Separate connection/transaction — a genuinely different writer,
        # not this request's own session (mirrors test_task_status_postgres.py's
        # concurrent-writer pattern for the analogous `status` staleness trap).
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET assigned_agent_id = :aid WHERE id = :id"),
                {"aid": str(third_id), "id": str(task_id)},
            )
        return await original_lock_task(session, task_id)

    monkeypatch.setattr(task_state_module, "lock_task", _lock_task_after_concurrent_reassign)

    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        headers=_headers(ids, attempt=attempt),
        json={"status": "blocked", "assigned_agent_id": str(third_id),
              "blocker_type": "other", "blocker_question": "ordering probe"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_agent_id"] == str(third_id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.assigned_agent_id == third_id
        # The proof: target already matched the FRESH DB value, so this must
        # be treated as a no-op reassignment — dispatched_at survives.
        # Reversing lock_task()/reassignment order reads the stale
        # pre-concurrent-write assignee instead, wrongly takes the
        # "changed" branch, and resets this to None (see docstring above).
        assert task.dispatched_at is not None


@pytest.mark.asyncio
async def test_lead_reassign_to_unknown_agent_gets_422(client):
    """Lead reassigns to a non-existent agent → 422, not a broken state."""
    ids = await _setup(lead=True)
    attempt = (await _task(ids)).dispatch_attempt_id
    ghost = uuid.uuid4()
    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        headers=_headers(ids, attempt=attempt),
        json={"assigned_agent_id": str(ghost)},
    )
    assert resp.status_code == 422, resp.text




# ── Bug 2: checklist item DELETE ────────────────────────────────────────────


async def _setup_task_with_items(*, status="inbox"):
    """Board + worker/lead agents + task (default: parked inbox) + 2 items."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.checklist import TaskChecklistItem
    from app.auth import generate_agent_token

    board_id, agent_id, lead_id, task_id = (uuid.uuid4() for _ in range(4))
    item1_id, item2_id = uuid.uuid4(), uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:8]}"))
        raw_token, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Worker", board_id=board_id,
            agent_token_hash=token_hash, is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        ))
        lead_token, lead_hash = generate_agent_token()
        s.add(Agent(
            id=lead_id, name="Lead", board_id=board_id,
            agent_token_hash=lead_hash, is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
        ))
        await s.flush()
        s.add(Task(
            id=task_id, board_id=board_id, title="Parked",
            status=status, assigned_agent_id=agent_id,
        ))
        s.add(TaskChecklistItem(
            id=item1_id, task_id=task_id, title="Alt 1", sort_order=0,
        ))
        s.add(TaskChecklistItem(
            id=item2_id, task_id=task_id, title="Alt 2", sort_order=1,
        ))
        await s.commit()
    return {
        "board_id": board_id, "agent_id": agent_id, "lead_id": lead_id,
        "task_id": task_id, "token": raw_token, "lead_token": lead_token,
        "item_ids": [item1_id, item2_id],
    }


@pytest.mark.asyncio
async def test_lead_can_delete_checklist_item_and_close_parked_task(client):
    """Full Bug 2 scenario: a parked card with stale open items. The lead
    deletes the items via the agent endpoint (previously 405), then the
    card closes without falsifying history by marking foreign items done."""
    ids = await _setup_task_with_items(status="in_progress")
    lead_headers = {"Authorization": f"Bearer {ids['lead_token']}"}
    base = f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}"

    # Before the fix this DELETE was 405 — items could only be "done"-marked.
    for item_id in ids["item_ids"]:
        resp = await client.delete(f"{base}/checklist/{item_id}", headers=lead_headers)
        assert resp.status_code == 204, resp.text

    # Checklist guard no longer trips: task closes
    resp = await client.patch(base, headers=lead_headers, json={"status": "done"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "done"


@pytest.mark.asyncio
async def test_open_checklist_still_blocks_done(client):
    """The completion guard stays intact: open items still block done."""
    ids = await _setup_task_with_items(status="in_progress")
    lead_headers = {"Authorization": f"Bearer {ids['lead_token']}"}
    base = f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}"
    resp = await client.patch(base, headers=lead_headers, json={"status": "done"})
    assert resp.status_code == 422, resp.text
    assert "Checklist-Item" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_user_can_delete_checklist_item_and_counters_recompute(auth_client):
    """User endpoint deletes an item; denormalized counters follow."""
    ids = await _setup_task_with_items(status="in_progress")
    from app.models.checklist import TaskChecklistItem
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        item = await s.get(TaskChecklistItem, ids["item_ids"][0])
        item.status = "done"
        s.add(item)
        await s.commit()

    resp = await auth_client.delete(
        f"/api/v1/boards/{ids['board_id']}/tasks/{ids['task_id']}"
        f"/checklist/{ids['item_ids'][0]}",
    )
    assert resp.status_code == 204, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.checklist_total == 1
        assert task.checklist_done == 0
        assert await s.get(TaskChecklistItem, ids["item_ids"][0]) is None
