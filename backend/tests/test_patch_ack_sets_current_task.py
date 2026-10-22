"""Tests for bug fix 2026-04-24: push-dispatch ACK (PATCH status:in_progress) must
set agent.current_task_id, otherwise 409 on `mc delegate` / `mc help-request` /
`mc clarification`.

Live bug: Boss received a task via push-dispatch, ACKed via PATCH, but
current_task_id stayed null. mc delegate → 409 "No active task". Workaround was
direct POST /tasks. Fix: PATCH ACK sets current_task_id analogous to pull-dispatch.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent_with_task(
    *,
    is_board_lead: bool = True,
    use_subagent_dispatch: bool = False,
    task_status: str = "inbox",
):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="TestBoard", slug=f"tb-{uuid.uuid4().hex[:6]}"))
        await s.commit()

        agent = Agent(
            id=uuid.uuid4(),
            name="TestBoss",
            role="orchestrator",
            is_board_lead=is_board_lead,
            board_id=board_id,
            agent_runtime="host",
            agent_token_hash=token_hash,
            scopes=["heartbeat", "tasks:read", "tasks:write"],
            current_task_id=None,
            model="x",
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

        task = Task(
            board_id=board_id,
            title="Pushed Task",
            description="x",
            status=task_status,
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

    return agent, raw_token, board_id, task


@pytest.mark.asyncio
async def test_patch_ack_sets_current_task_id_for_board_lead(client: AsyncClient):
    """Push-dispatch board lead: PATCH status:in_progress → current_task_id set."""
    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=True)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id, (
            f"current_task_id nicht gesetzt — Bug nicht gefixt! "
            f"current_task_id={fresh.current_task_id}, expected={task.id}"
        )


@pytest.mark.asyncio
async def test_patch_ack_sets_current_task_id_for_worker_when_subagent_off(
    client: AsyncClient, monkeypatch,
):
    """Legacy mode (USE_SUBAGENT_DISPATCH=false): workers get the lock too."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", False)

    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=False)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id


@pytest.mark.asyncio
async def test_patch_ack_sets_current_task_id_for_worker_subagent_mode(
    client: AsyncClient, monkeypatch,
):
    """Subagent-dispatch mode: a worker's ACK takes the pointer when it is free.

    Turned around on purpose (was ``..._skips_...``, which asserted None). The
    old rule was already void after at most 30 s: the heartbeat self-heal
    (``agents.py`` ``agent_heartbeat``) sets current_task_id for workers too.
    ADR-097 review Z1 wants the pointer "from the first second" — the chat's
    "Working on" row, the sessions sidebar, the runtime-switch busy check and
    ``mc ask``/``mc msg`` all read it. Live gap before this fix: ACK → next
    heartbeat = 0–30 s without a pointer.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", True)

    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=False)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id, (
            f"Worker ACK must take the free pointer — "
            f"current_task_id={fresh.current_task_id}, expected={task.id}"
        )
        assert fresh.run_state == "running"


async def _add_task(board_id, agent_id, *, status: str, title: str = "Other", parent_task_id=None):
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = Task(
            board_id=board_id,
            title=title,
            description="x",
            status=status,
            assigned_agent_id=agent_id,
            parent_task_id=parent_task_id,
        )
        s.add(t)
        await s.commit()
        await s.refresh(t)
    return t


async def _set_pointer(agent_id, task_id):
    from app.models.agent import Agent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, agent_id)
        a.current_task_id = task_id
        s.add(a)
        await s.commit()


async def _add_reflection(task_id, agent_id):
    """The review gate wants a reflection before leaving in_progress."""
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="agent",
            author_agent_id=agent_id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\nThe change was built and tested end to end.\n\n"
                "## Was hat funktioniert\nThe red test first, then the fix.\n\n"
                "## Was war unklar\nNothing was unclear in this synthetic run.\n\n"
                "## Lesson für Agent-Memory\nKeep the pointer in step with the status."
            ),
        ))
        await s.commit()


async def _pointer(agent_id):
    from app.models.agent import Agent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, agent_id)
        return a.current_task_id, a.run_state


@pytest.mark.asyncio
async def test_patch_ack_keeps_existing_pointer_for_worker_subagent_mode(
    client: AsyncClient, monkeypatch,
):
    """Parallel sessions: the first running task keeps the pointer — a second
    ACK must not steal it (first come stays, same value the heartbeat keeps)."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", True)

    agent, token, board_id, task_a = await _make_agent_with_task(is_board_lead=False)
    task_b = await _add_task(board_id, agent.id, status="in_progress", title="Running B")
    await _set_pointer(agent.id, task_b.id)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_a.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    pointer, _ = await _pointer(agent.id)
    assert pointer == task_b.id, f"pointer stolen: {pointer} (expected B {task_b.id})"


@pytest.mark.asyncio
async def test_worker_leaving_in_progress_hands_pointer_to_other_running_task(
    client: AsyncClient, monkeypatch,
):
    """Mirror: A → review while B still runs ⇒ pointer moves to B (not 30 s
    of "Working on A" for a card that is already in review)."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", True)

    agent, token, board_id, task_a = await _make_agent_with_task(
        is_board_lead=False, task_status="in_progress",
    )
    task_b = await _add_task(board_id, agent.id, status="in_progress", title="Running B")
    await _set_pointer(agent.id, task_a.id)
    await _add_reflection(task_a.id, agent.id)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_a.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "review"},
    )
    assert resp.status_code == 200, resp.text

    pointer, _ = await _pointer(agent.id)
    assert pointer == task_b.id, f"pointer should move to B, got {pointer}"


@pytest.mark.asyncio
async def test_worker_leaving_in_progress_clears_pointer_without_other_task(
    client: AsyncClient, monkeypatch,
):
    """Mirror without a second running task ⇒ pointer None, run_state idle."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", True)

    agent, token, board_id, task_a = await _make_agent_with_task(
        is_board_lead=False, task_status="in_progress",
    )
    await _set_pointer(agent.id, task_a.id)
    await _add_reflection(task_a.id, agent.id)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_a.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "review"},
    )
    assert resp.status_code == 200, resp.text

    pointer, run_state = await _pointer(agent.id)
    assert pointer is None, f"pointer should be cleared, got {pointer}"
    assert run_state == "idle"


@pytest.mark.asyncio
async def test_worker_leaving_in_progress_leaves_foreign_pointer_alone(
    client: AsyncClient, monkeypatch,
):
    """A task that does not hold the pointer must not move it on leaving."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", True)

    agent, token, board_id, task_a = await _make_agent_with_task(
        is_board_lead=False, task_status="in_progress",
    )
    task_b = await _add_task(board_id, agent.id, status="in_progress", title="Running B")
    await _set_pointer(agent.id, task_b.id)
    await _add_reflection(task_a.id, agent.id)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_a.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "review"},
    )
    assert resp.status_code == 200, resp.text

    pointer, _ = await _pointer(agent.id)
    assert pointer == task_b.id


@pytest.mark.asyncio
async def test_worker_pointer_not_taken_for_task_not_really_in_progress(monkeypatch):
    """execute_review_decision 'releases' a reviewer with new_status=in_progress
    while the card is still `review` (request_changes hands it back to the
    developer). That call must not point the worker reviewer at the card."""
    from app.config import settings
    from app.models.agent import Agent
    from app.models.task import Task
    from app.services.task_lifecycle import update_agent_active_task

    monkeypatch.setattr(settings, "use_subagent_dispatch", True)
    agent, _token, _board_id, task = await _make_agent_with_task(
        is_board_lead=False, task_status="review",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        await update_agent_active_task(s, agent.id, t, "in_progress", "review")
        a = await s.get(Agent, agent.id)
        assert a.current_task_id is None, (
            f"reviewer release must not take the pointer: {a.current_task_id}"
        )


@pytest.mark.asyncio
async def test_auto_ack_via_comment_sets_current_task_id_for_worker_subagent_mode(
    client: AsyncClient, monkeypatch,
):
    """Implicit ACK by the first agent comment (inbox → in_progress) takes the
    free pointer for a worker too — same rule as the PATCH ACK."""
    from datetime import datetime, timezone

    from app.config import settings
    from app.models.task import Task as _Task

    monkeypatch.setattr(settings, "use_subagent_dispatch", True)
    agent, token, board_id, task = await _make_agent_with_task(
        is_board_lead=False, task_status="inbox",
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(_Task, task.id)
        t.dispatched_at = datetime.now(tz=timezone.utc)
        s.add(t)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}/comments",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "First comment triggers Auto-ACK"},
    )
    assert resp.status_code in (200, 201), resp.text

    pointer, _ = await _pointer(agent.id)
    assert pointer == task.id, f"comment ACK must take the free pointer: {pointer}"


@pytest.mark.asyncio
async def test_pull_claim_sets_current_task_id_for_worker_subagent_mode(
    client: AsyncClient, monkeypatch,
):
    """Pull claim (GET /tasks/next) moves the card to in_progress itself — a
    worker takes the free pointer there too."""
    from app.config import settings

    monkeypatch.setattr(settings, "use_subagent_dispatch", True)
    agent, token, board_id, parent = await _make_agent_with_task(
        is_board_lead=False, task_status="done",
    )
    sub = await _add_task(
        board_id, agent.id, status="inbox", title="Sub", parent_task_id=parent.id,
    )

    resp = await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/next",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    pointer, _ = await _pointer(agent.id)
    assert pointer == sub.id, f"pull claim must take the free pointer: {pointer}"


@pytest.mark.asyncio
async def test_patch_done_clears_current_task_id(client: AsyncClient):
    """When current_task_id is set and the task becomes done → release the lock."""
    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=True)

    # 1. ACK (sets current_task_id)
    r1 = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert r1.status_code == 200

    # 2. Done (clear)
    r2 = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "done"},
    )
    assert r2.status_code == 200, r2.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id is None, (
            f"Lock muss bei done freigegeben werden — "
            f"current_task_id={fresh.current_task_id}"
        )


@pytest.mark.asyncio
async def test_auto_ack_via_comment_sets_current_task_id(client: AsyncClient):
    """Auto-ACK via comment (ack_at=None + dispatched_at != None) → set current_task_id.

    Bug repro 2026-04-24 (Boss Acme-Corp brief): Boss ACKed via the first
    comment (auto-ACK path, NOT via PATCH), current_task_id stayed null,
    mc delegate threw 409. PR #103 only fixed the PATCH path — this covers
    the comment auto-ACK path too.
    """
    agent, token, board_id, task = await _make_agent_with_task(
        is_board_lead=True, task_status="inbox",
    )
    # Set dispatched marker (otherwise no auto-ACK)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task as _Task
        fresh_task = await s.get(_Task, task.id)
        from datetime import datetime, timezone
        fresh_task.dispatched_at = datetime.now(tz=timezone.utc)
        s.add(fresh_task)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}/comments",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "First comment triggers Auto-ACK"},
    )
    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id, (
            f"Auto-ACK muss current_task_id setzen — sonst scheitert mc delegate. "
            f"Got current_task_id={fresh.current_task_id}"
        )


@pytest.mark.asyncio
async def test_patch_ack_idempotent_for_already_locked_task(client: AsyncClient):
    """When current_task_id is already this task (e.g. via pull-dispatch), no overwrite."""
    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=True, task_status="in_progress")

    # Simulate pull-dispatch state: current_task_id already set
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        fresh.current_task_id = task.id
        s.add(fresh)
        await s.commit()

    # Agent sends PATCH status:in_progress even though already in_progress (retry)
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    # We accept 200 (no-op) or 400/409 (status-transition guard).
    # What matters is that current_task_id stays intact, regardless of what the server does.
    assert resp.status_code in (200, 400, 409), resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id
