"""A delivered task whose model does not answer waits — with the reason
(follow-up to ADR-097, 19.10.2026).

The omp bridge claims a task only once the model answers (ADR-097), and with
an unreachable model it now leaves the card delivered — no retry, no blocker
(docker/omp-bridge, ``awaiting_model``). The card stays ``inbox`` with
``dispatched_at`` set and no ``ack_at``. The ACK ladder
(``task_runner._check_dispatch_ack``) used to answer that with a silent
attempt rotation and later an operator approval "no ACK after dispatch" —
the wrong story: the agent is fine, its model is not there.

Contract under test:

1. From half the ACK timeout on (where the ladder would rotate), a delivered,
   unclaimed task whose agent's runtime does not answer goes back to the
   inbox: ``dispatched_at`` / attempt id cleared, one system comment with the
   reason, one event, no rotation, no approval. The poll gate then holds it
   until the model answers and delivers it fresh. Also while the bridge
   heartbeats "working" (omp retrying a dead endpoint must not hide it).
2. A runtime that answers keeps the old ladder.
3. ``GET /boards/{b}/tasks/{t}/model-wait`` tells the task card whether an
   assigned inbox task waits for its model — a read: no timeline event.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.utils import utcnow

REASON = "Runtime 'BOX-A :8000' antwortet nicht (http://192.0.2.10:8000/v1)"


@pytest.fixture(autouse=True)
def _approval_path_flag_off():
    from app.config import settings
    with patch.object(settings, "notice_only_escalations_enabled", False):
        yield


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession

    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _fixtures(make_board, make_agent, make_task, *, minutes: float,
                    agent_status: str = "idle", delivered: bool = True,
                    status: str = "inbox"):
    board = await make_board(name="ModelWait", slug=f"mw-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name="Worker-MW", board_id=board.id, is_board_lead=False, role="developer",
        agent_runtime="cli-bridge", status=agent_status,
        last_seen_at=utcnow() - timedelta(seconds=10),
        dispatch_config={"ack_timeout_minutes": 10},
    )
    task = await make_task(board_id=board.id, title="MW task", status=status,
                           assigned_agent_id=agent.id)
    if delivered:
        async with _session() as s:
            from app.models.task import Task
            t = await s.get(Task, task.id)
            t.dispatched_at = utcnow() - timedelta(minutes=minutes)
            t.dispatch_attempt_id = str(uuid.uuid4())
            s.add(t)
            await s.commit()
    return agent, task


async def _check(fake_redis, ready: tuple[bool, str | None]):
    from app.services.task_runner import task_runner

    gate = AsyncMock(return_value=ready)
    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock) as ev, \
         patch("app.services.runtime_readiness.runtime_ready_for_agent", gate), \
         patch("app.services.task_runner.logger"):
        async with _session() as session:
            await task_runner._check_dispatch_ack(session)
    return ev, gate


async def _task(task_id):
    from app.models.task import Task
    async with _session() as s:
        return await s.get(Task, task_id)


async def _comments(task_id):
    from sqlmodel import select

    from app.models.task import TaskComment
    async with _session() as s:
        return list((await s.exec(select(TaskComment).where(TaskComment.task_id == task_id))).all())


async def _approvals(task_id) -> int:
    from sqlmodel import select

    from app.models.approval import Approval
    async with _session() as s:
        return len((await s.exec(select(Approval).where(Approval.task_id == task_id))).all())


# ── 1. the ACK ladder returns the task with the reason ──────────────────────

@pytest.mark.asyncio
async def test_unreachable_model_returns_the_delivered_task_with_the_reason(
    fake_redis, make_board, make_agent, make_task,
):
    _, task = await _fixtures(make_board, make_agent, make_task, minutes=6)
    ev, gate = await _check(fake_redis, (False, REASON))
    t = await _task(task.id)
    assert t.status == "inbox"
    assert t.dispatched_at is None and t.dispatch_attempt_id is None and t.ack_at is None
    comments = await _comments(task.id)
    assert len(comments) == 1, [c.content for c in comments]
    assert comments[0].author_type == "system"
    assert "BOX-A :8000" in comments[0].content and "Inbox" in comments[0].content
    assert await _approvals(task.id) == 0
    assert "task.returned_model_unreachable" in [c.args[1] for c in ev.call_args_list]
    # The same gate the poll uses, local runtimes included, without its own
    # "held" timeline line (the comment above already says why).
    kwargs = gate.call_args.kwargs
    assert kwargs.get("include_local") is True and kwargs.get("notify") is False


@pytest.mark.asyncio
async def test_the_return_is_not_held_back_while_the_bridge_heartbeats_working(
    fake_redis, make_board, make_agent, make_task,
):
    _, task = await _fixtures(make_board, make_agent, make_task, minutes=6,
                              agent_status="working")
    from app.config import settings
    with patch.object(settings, "host_turn_signal_enabled", True):
        await _check(fake_redis, (False, REASON))
    assert (await _task(task.id)).dispatched_at is None


@pytest.mark.asyncio
async def test_before_half_the_ack_timeout_nothing_happens(
    fake_redis, make_board, make_agent, make_task,
):
    _, task = await _fixtures(make_board, make_agent, make_task, minutes=3)
    before = await _task(task.id)
    _, gate = await _check(fake_redis, (False, REASON))
    after = await _task(task.id)
    assert after.dispatched_at is not None
    assert after.dispatch_attempt_id == before.dispatch_attempt_id
    assert await _comments(task.id) == []
    gate.assert_not_called()  # no probe before the deadline


@pytest.mark.asyncio
async def test_a_reachable_model_keeps_the_old_ladder(
    fake_redis, make_board, make_agent, make_task,
):
    """Runtime answers -> the agent itself did not ack: rotate as before."""
    _, task = await _fixtures(make_board, make_agent, make_task, minutes=6)
    before = await _task(task.id)
    await _check(fake_redis, (True, None))
    after = await _task(task.id)
    assert after.dispatched_at is not None
    assert after.dispatch_attempt_id != before.dispatch_attempt_id  # D-1 rotation
    assert all("Inbox" not in c.content for c in await _comments(task.id))


# ── 2. the gate's timeline note can be switched off for reads ───────────────

@pytest.mark.asyncio
async def test_notify_false_never_writes_the_held_line(async_session):
    from types import SimpleNamespace

    from app.models.host import Host
    from app.models.runtime import Runtime
    from app.services import runtime_readiness

    h = Host(slug=f"box-{uuid.uuid4().hex[:6]}", display_name="Box", kind="ssh")
    async_session.add(h)
    await async_session.flush()
    rt = Runtime(slug=f"mw-{uuid.uuid4().hex[:6]}", display_name="BOX-A :8000",
                 runtime_type="openai_compatible",
                 endpoint="http://192.0.2.10:8000/v1", host_id=h.id)
    async_session.add(rt)
    await async_session.commit()
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(return_value=False)), \
         patch.object(runtime_readiness, "_notify_local_down", new=AsyncMock()) as note:
        ok, reason = await runtime_readiness.runtime_ready_for_agent(
            agent, async_session, include_local=True, notify=False,
        )
        assert ok is False and "antwortet nicht" in reason
        note.assert_not_called()
        await runtime_readiness.runtime_ready_for_agent(agent, async_session, include_local=True)
        note.assert_called_once()


@pytest.mark.asyncio
async def test_notify_false_never_writes_the_asleep_event_either(async_session):
    """A read (the task card, the chat's status line every few seconds) must
    not announce "a task waits, wake the box" for a power-managed runtime."""
    from types import SimpleNamespace

    from app.models.runtime import Runtime
    from app.services import runtime_readiness

    rt = Runtime(slug=f"pm-{uuid.uuid4().hex[:6]}", display_name="Sleepy",
                 runtime_type="openai_compatible", endpoint="http://192.0.2.11:8000/v1",
                 power_managed=True)
    async_session.add(rt)
    await async_session.commit()
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(return_value=False)), \
         patch.object(runtime_readiness, "_notify_asleep", new=AsyncMock()) as note:
        ok, _ = await runtime_readiness.runtime_ready_for_agent(agent, async_session, notify=False)
        assert ok is False
        note.assert_not_called()
        await runtime_readiness.runtime_ready_for_agent(agent, async_session)
        note.assert_called_once()


# ── 3. GET .../model-wait for the task card ─────────────────────────────────

async def _bind_local_runtime(agent_id, name: str = "BOX-A :8000"):
    from app.models.agent import Agent
    from app.models.host import Host
    from app.models.runtime import Runtime
    async with _session() as s:
        h = Host(slug=f"box-{uuid.uuid4().hex[:6]}", display_name="Box", kind="ssh")
        s.add(h)
        await s.flush()
        rt = Runtime(slug=f"mw-{uuid.uuid4().hex[:6]}", display_name=name,
                     runtime_type="openai_compatible",
                     endpoint="http://192.0.2.10:8000/v1", host_id=h.id)
        s.add(rt)
        await s.flush()
        a = await s.get(Agent, agent_id)
        a.runtime_id = rt.id
        s.add(a)
        await s.commit()


def _url(task) -> str:
    return f"/api/v1/boards/{task.board_id}/tasks/{task.id}/model-wait"


@pytest.mark.asyncio
async def test_model_wait_names_the_runtime_that_does_not_answer(
    auth_client, fake_redis, make_board, make_agent, make_task,
):
    from app.services import runtime_readiness

    agent, task = await _fixtures(make_board, make_agent, make_task, minutes=2)
    await _bind_local_runtime(agent.id)
    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(return_value=False)), \
         patch.object(runtime_readiness, "_notify_local_down", new=AsyncMock()) as note:
        resp = await auth_client.get(_url(task))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applies"] is True and body["delivered"] is True
    assert body["ready"] is False and body["runtime"] == "BOX-A :8000"
    assert "antwortet nicht" in body["reason"]
    note.assert_not_called()  # a read never writes the timeline


@pytest.mark.asyncio
async def test_model_wait_for_an_answering_model(
    auth_client, fake_redis, make_board, make_agent, make_task,
):
    from app.services import runtime_readiness

    agent, task = await _fixtures(make_board, make_agent, make_task, minutes=2)
    await _bind_local_runtime(agent.id)
    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(return_value=True)):
        body = (await auth_client.get(_url(task))).json()
    assert body["applies"] is True and body["ready"] is True and body["reason"] is None
    assert body["delivered"] is True and body["runtime"] == "BOX-A :8000"


@pytest.mark.asyncio
async def test_model_wait_does_not_apply_to_a_running_task(
    auth_client, make_board, make_agent, make_task,
):
    _, task = await _fixtures(make_board, make_agent, make_task, minutes=2,
                              status="in_progress")
    body = (await auth_client.get(_url(task))).json()
    assert body == {"applies": False, "delivered": False, "ready": True,
                    "runtime": None, "reason": None}


@pytest.mark.asyncio
async def test_model_wait_404_for_a_task_on_another_board(
    auth_client, make_board, make_agent, make_task,
):
    _, task = await _fixtures(make_board, make_agent, make_task, minutes=2)
    resp = await auth_client.get(f"/api/v1/boards/{uuid.uuid4()}/tasks/{task.id}/model-wait")
    assert resp.status_code == 404
