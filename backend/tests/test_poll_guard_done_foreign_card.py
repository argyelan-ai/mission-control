"""Guard 2 — 14.09.2026 incident: a hermes-bridge restart (in-memory dedup
cache empty after -9/restart) polled a card that was, at that exact moment,
status=done and assigned to a different agent (a review handoff had reassigned
it, and the reviewer had already closed it hours earlier). The card was still
delivered as state=new_task, so the bridge pasted a second review.

Root cause, confirmed by direct reproduction before any fix existed: every
SQL query in agent_poll already whitelists non-terminal statuses (status=done
never appears in a `.in_()` filter anywhere in the function) — a stale
`agent.current_task_id` pointing at a self- or foreign-assigned done task
alone is NOT enough to reach state=new_task; the endpoint correctly reports
`idle` for that (see the two `..._stale_lock_reports_idle` tests below, which
guard that finding against regressing).

The actual hole is a TOCTOU race: several code paths fetch a Task object,
then `await` at least once more (dependencies_met per inbox candidate, the
blocked grace-window board lookup, the orphan-redispatch helper's own
commit+refresh) before using that same Python object to build a new_task
response. If a concurrent request finishes or reassigns the SAME card in
that window, the stale in-memory object still looks dispatchable.

Fix: `_task_still_dispatchable()` (agents.py) is the single guard, called
right after a fresh `session.refresh(task)` at the two places that can
return `state=new_task`:
  1. `_maybe_redispatch_orphaned_run` — after its own refresh.
  2. the main claim path in `agent_poll` — right after `if task is None`.
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from tests.conftest import test_engine


async def _seed_board_and_agent(
    *, is_board_lead: bool = False, current_task_id: uuid.UUID | None = None,
    last_task_activity_at: dt.datetime | None = None, role: str | None = None,
):
    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id,
            name=f"TestAgent-{uuid.uuid4().hex[:4]}",
            agent_runtime="host",
            agent_token_hash=token_hash,
            board_id=board_id,
            is_board_lead=is_board_lead,
            current_task_id=current_task_id,
            last_task_activity_at=last_task_activity_at,
            role=role,
            scopes=["heartbeat", "tasks:read", "tasks:write"],
        ))
        await s.commit()
    return raw_token, board_id, agent_id


@pytest.mark.asyncio
async def test_stale_lock_on_self_done_task_reports_idle(client: AsyncClient, fake_redis):
    """Baseline finding: a stale current_task_id alone (no race) can never
    reach new_task — status=done is excluded from every query in agent_poll.
    Guards against someone "fixing" this by weakening a query filter instead
    of the real TOCTOU gap.
    """
    task_id = uuid.uuid4()
    raw_token, board_id, agent_id = await _seed_board_and_agent(current_task_id=task_id)
    now = dt.datetime.now(tz=dt.timezone.utc)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=task_id, board_id=board_id, title="Done Review Task", status="done",
            assigned_agent_id=agent_id,
            dispatched_at=now - dt.timedelta(hours=3), ack_at=now - dt.timedelta(hours=3),
            completed_at=now - dt.timedelta(hours=2, minutes=14),
        ))
        await s.commit()

    resp = await client.get(
        "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


@pytest.mark.asyncio
async def test_stale_lock_on_foreign_done_task_reports_idle(client: AsyncClient, fake_redis):
    """Same as above, but the done card now belongs to a DIFFERENT agent —
    the literal "fremde, erledigte Karte" from the incident report."""
    task_id = uuid.uuid4()
    raw_token, board_id, agent_id = await _seed_board_and_agent(current_task_id=task_id)
    now = dt.datetime.now(tz=dt.timezone.utc)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(
            id=uuid.uuid4(), name="Rex", agent_runtime="host",
            agent_token_hash="x" * 64, board_id=board_id, role="reviewer",
            scopes=["heartbeat", "tasks:read", "tasks:write"],
        ))
        other_agent_id = (await s.exec(
            __import__("sqlmodel").select(Agent).where(Agent.name == "Rex")
        )).first().id
        s.add(Task(
            id=task_id, board_id=board_id, title="Reassigned + Done Task", status="done",
            assigned_agent_id=other_agent_id,
            dispatched_at=now - dt.timedelta(hours=3), ack_at=now - dt.timedelta(hours=3),
            completed_at=now - dt.timedelta(hours=2, minutes=14),
        ))
        await s.commit()

    resp = await client.get(
        "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


@pytest.mark.asyncio
async def test_orphan_redispatch_race_does_not_deliver_completed_card(
    client: AsyncClient, fake_redis, monkeypatch,
):
    """Guard 2, call site 1 (`_maybe_redispatch_orphaned_run`).

    A task looks orphaned (in_progress, ack'd long ago, no fresh liveness
    signal). While the helper is mid-redispatch, a concurrent request
    finishes the SAME card. Without the guard, the helper's own
    commit+refresh silently picks up status=done and still returns
    state=new_task with a freshly built prompt.
    """
    task_id = uuid.uuid4()
    now = dt.datetime.now(tz=dt.timezone.utc)
    stale = now - dt.timedelta(hours=1)
    raw_token, board_id, agent_id = await _seed_board_and_agent(
        current_task_id=task_id, last_task_activity_at=stale,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=task_id, board_id=board_id, title="In-flight Review Task",
            status="in_progress", assigned_agent_id=agent_id,
            dispatched_at=stale, ack_at=stale,
        ))
        await s.commit()

    from app.redis_client import try_claim_heal as _real_try_claim_heal

    async def _racing_try_claim_heal(redis, tid):
        claimed = await _real_try_claim_heal(redis, tid)
        if claimed:
            async with AsyncSession(test_engine, expire_on_commit=False) as race_session:
                race_task = await race_session.get(Task, task_id)
                race_task.status = "done"
                race_task.completed_at = dt.datetime.now(tz=dt.timezone.utc)
                race_session.add(race_task)
                await race_session.commit()
        return claimed

    monkeypatch.setattr("app.routers.agents.try_claim_heal", _racing_try_claim_heal)

    resp = await client.get(
        "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw_token}"},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "idle", f"Guard 2 (orphan path) nicht wirksam: {body}"


@pytest.mark.asyncio
async def test_main_claim_path_race_does_not_deliver_reassigned_done_card(
    client: AsyncClient, fake_redis, monkeypatch,
):
    """Guard 2, call site 2 (main claim path in `agent_poll`).

    An inbox candidate is picked, but while `dependencies_met()` awaits, a
    concurrent request finishes AND reassigns the same card to a different
    agent (review handoff mid-flight). Without the guard, the stale
    in-memory `task` (still "inbox" in this session's view) sails through to
    the claim/prompt-build/return block and is delivered as new_task.
    """
    task_id = uuid.uuid4()
    raw_token, board_id, agent_id = await _seed_board_and_agent()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=task_id, board_id=board_id, title="Inbox Task", status="inbox",
            assigned_agent_id=agent_id, run_control=None,
        ))
        await s.commit()

    from app.services.dispatch import dependencies_met as _real_dependencies_met

    async def _racing_dependencies_met(session, candidate):
        result = await _real_dependencies_met(session, candidate)
        async with AsyncSession(test_engine, expire_on_commit=False) as race_session:
            other = Agent(
                id=uuid.uuid4(), name="Rex", agent_runtime="host",
                agent_token_hash="y" * 64, board_id=board_id, role="reviewer",
                scopes=["heartbeat", "tasks:read", "tasks:write"],
            )
            race_session.add(other)
            await race_session.commit()
            race_task = await race_session.get(Task, task_id)
            race_task.status = "done"
            race_task.assigned_agent_id = other.id
            race_task.completed_at = dt.datetime.now(tz=dt.timezone.utc)
            race_session.add(race_task)
            await race_session.commit()
        return result

    # dependencies_met is imported locally inside agent_poll on every call
    # (`from app.services.dispatch import dependencies_met`), so patching
    # the source module is what actually takes effect.
    monkeypatch.setattr(
        "app.services.dispatch.dependencies_met", _racing_dependencies_met,
    )

    resp = await client.get(
        "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw_token}"},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "idle", f"Guard 2 (main claim path) nicht wirksam: {body}"


@pytest.mark.asyncio
async def test_gegenrichtung_open_own_task_still_delivered_after_bridge_restart(
    client: AsyncClient, fake_redis,
):
    """The empty in-memory dedup cache after a bridge restart is DELIBERATE
    (see hermes-bridge.py dispatch_poll_loop) — it's what lets a crashed
    session's card get redelivered. Guard 2 must not swallow that: an open
    card genuinely assigned to this agent, never acked (ack_at IS NULL,
    matching "prompt never delivered" after a restart) must still come back
    as state=new_task.
    """
    task_id = uuid.uuid4()
    raw_token, board_id, agent_id = await _seed_board_and_agent()
    now = dt.datetime.now(tz=dt.timezone.utc)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=task_id, board_id=board_id, title="Still Open Task",
            status="in_progress", assigned_agent_id=agent_id,
            dispatched_at=now, ack_at=None,
        ))
        await s.commit()

    resp = await client.get(
        "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw_token}"},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "new_task", f"Gegenrichtung verletzt: {body}"
    assert body["task"]["id"] == str(task_id)
    assert body["task"]["assigned_agent_id"] == str(agent_id)
    assert body["my_agent_id"] == str(agent_id)
