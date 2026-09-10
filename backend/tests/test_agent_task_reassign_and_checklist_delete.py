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
