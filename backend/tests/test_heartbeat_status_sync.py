"""Heartbeat sets agent.status — important for non-gateway agents (cli-bridge, host).

Bug before the fix: /agent/me/heartbeat only set last_seen_at + run_state,
but NOT agent.status. As a result, Docker cli-bridge agents stayed
'offline' forever, even though they send a heartbeat every 30s.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_agent(session: AsyncSession, *, status="offline", gateway_id=None):
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name="Davinci",
        agent_runtime="cli-bridge",
        status=status,
        agent_token_hash=token_hash,
        scopes=["heartbeat", "tasks:read"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


@pytest.mark.asyncio
async def test_heartbeat_sets_status_idle(client: AsyncClient):
    """Heartbeat with status='idle' sets agent.status=idle."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, status="offline")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.status == "idle"
    assert fresh.last_seen_at is not None


@pytest.mark.asyncio
async def test_heartbeat_sets_status_working_only_with_active_task(client: AsyncClient):
    """Bug 18 fix (2026-05-14): 'working' without an in_progress task is forced
    to 'idle'. Sparky symptom: agent.status='working' + current_task_id=None
    after Voice-Foundation review because claude in the pane is still
    rendering memories → detect_turn_state='working' → heartbeat sends
    'working' → but backend has no active task → previously: status stayed
    'working' forever.

    Correct behavior: without an in_progress task, 'working' is an invalid
    self-report — coerce to 'idle'.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, status="idle")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    # Bug 18: without a task assigned → 'working' is coerced to 'idle'
    assert fresh.status == "idle"
    assert fresh.run_state == "idle"
    assert fresh.current_task_id is None


@pytest.mark.asyncio
async def test_heartbeat_ignores_unknown_status(client: AsyncClient):
    """status='restarting' via heartbeat is ignored (only idle/working/online allowed)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, status="idle")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "restarting"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    # Status must not be overwritten
    assert fresh.status == "idle"


# ── Bug 2 fix: heartbeat self-heals when DB drifts from Task table ────────


async def _agent_with_active_task(session, *, task_status="in_progress", agent_status="idle"):
    """Helper: creates agent + assigned task in the desired status."""
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.auth import generate_agent_token
    import datetime as _dt

    board = Board(id=uuid.uuid4(), name="Test", slug="t")
    session.add(board)
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name="Sparky",
        board_id=board.id,
        agent_runtime="cli-bridge",
        status=agent_status,
        run_state="idle",
        current_task_id=None,  # the stale constellation that triggers Bug 2
        agent_token_hash=token_hash,
        scopes=["heartbeat", "tasks:read"],
    )
    session.add(agent)
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Voice-Foundation Sub",
        status=task_status,
        assigned_agent_id=agent.id,
        dispatched_at=now,
        ack_at=now,
    )
    session.add(task)
    await session.commit()
    await session.refresh(agent)
    await session.refresh(task)
    return agent, task, raw_token


@pytest.mark.asyncio
async def test_heartbeat_idle_with_active_task_syncs_current_task_id(client: AsyncClient):
    """Bug 2 (refined 2026-05-13): agent has an in_progress task assigned,
    poll.sh sends `status: idle`. current_task_id MUST be synced from the
    task table (drift fix), BUT agent.status/run_state mirror the payload —
    NOT blanket-masked to "working". Otherwise the operator sees fake
    activity in the UI when Sparky is sitting at the prompt without cooking.

    poll.sh is responsible for sending `heartbeat "working"` when claude is
    actually active (see Bug 13: detect_turn_state-based).
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_active_task(s)

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    # current_task_id self-heal: derived from the task table
    assert fresh.current_task_id == task.id, "current_task_id muss aus Task-Tabelle abgeleitet werden"
    # Status mirrors payload, not masked
    assert fresh.status == "idle", (
        "Bug 2 refined: payload=idle bei active task → status=idle. "
        "Operator erkennt damit dass Task assigned ist aber Agent nicht aktiv."
    )
    assert fresh.run_state == "idle"


@pytest.mark.asyncio
async def test_heartbeat_working_with_active_task_stamps_activity(client: AsyncClient):
    """Bug 2 refined: with payload=working + active task → status=working,
    current_task_id synced, last_task_activity_at stamped."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_active_task(s)

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.status == "working"
    assert fresh.run_state == "running"
    assert fresh.current_task_id == task.id
    assert fresh.last_task_activity_at is not None


@pytest.mark.asyncio
async def test_heartbeat_idle_without_active_task_stays_idle(client: AsyncClient):
    """Backward-compat: no in_progress task → heartbeat=idle sets idle as before."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # task_status=done -> no active task for the agent
        agent, task, token = await _agent_with_active_task(s, task_status="done")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.status == "idle"
    assert fresh.run_state == "idle"


@pytest.mark.asyncio
async def test_heartbeat_idle_with_blocked_task_stays_idle(client: AsyncClient):
    """A blocked task is not 'actively working' — the agent may stay idle.
    Otherwise the UI shows it as working even though it's waiting on the operator."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_active_task(s, task_status="blocked")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.status == "idle"


@pytest.mark.asyncio
async def test_heartbeat_working_without_task_coerces_to_idle(client: AsyncClient):
    """Bug 18 (2026-05-14): heartbeat=working WITHOUT an in_progress task →
    coerce to idle. Previously: status=working stayed stale forever (Sparky
    live symptom).

    Rationale: poll.sh should only send `working` when CURRENT_TASK_ID is
    set locally AND detect_turn_state='working' (see poll.sh:651-660). If
    the backend has no in_progress task, the self-report is inconsistent.
    Likely race: task done → poll.sh CURRENT_TASK_ID still set → claude
    renders memory save → working heartbeat without an assigned task.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, _token = await _create_agent(s, status="idle")
        # Re-fetch fresh token
        from app.models.agent import Agent
        from app.auth import generate_agent_token

        raw_token, token_hash = generate_agent_token()
        agent2 = await s.get(Agent, agent.id)
        agent2.agent_token_hash = token_hash
        s.add(agent2)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.status == "idle"  # NOT "working"
    assert fresh.run_state == "idle"


@pytest.mark.asyncio
async def test_heartbeat_clears_stale_current_task_id_path_b(client: AsyncClient):
    """Bug 18 (2026-05-14): Path B (no active task) explicitly clears
    current_task_id. Guard against a pointer that lingers after a task is
    done/failed (Sparky symptom 2026-05-14: agent.current_task_id was the
    old task pointer before the task was cleared as done, heartbeat ran
    before that).
    """
    import uuid as _uuid
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, _token = await _create_agent(s, status="idle")
        # Re-fetch fresh token + set stale current_task_id
        from app.models.agent import Agent
        from app.auth import generate_agent_token

        raw_token, token_hash = generate_agent_token()
        agent2 = await s.get(Agent, agent.id)
        agent2.agent_token_hash = token_hash
        agent2.current_task_id = _uuid.uuid4()  # Stale pointer
        s.add(agent2)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.current_task_id is None  # Stale pointer cleared


# ── Interaction 2.0 (Task 12): waiting-hold survives heartbeats ───────────


@pytest.mark.asyncio
async def test_heartbeat_preserves_waiting_hold(client: AsyncClient):
    """Live pilot finding 2026-07-20: a blocking ask parks the task `waiting`
    while /me/poll's session-hold is keyed off agent.current_task_id. The
    heartbeat's else-branch (Bug 18 self-heal) treated `waiting` as "no
    active task" and cleared the pointer — poll.sh saw state=idle, reset the
    session, and the blocking answer went through parked re-dispatch instead
    of live injection. A waiting task pointed at by current_task_id must
    survive the heartbeat."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_active_task(s, task_status="waiting")
        from app.models.agent import Agent
        a = await s.get(Agent, agent.id)
        a.current_task_id = task.id
        s.add(a)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.current_task_id == task.id, (
        "waiting-Hold: current_task_id darf vom Heartbeat nicht geloescht "
        "werden solange der Task waiting ist (blocking ask, Task 12)"
    )
    assert fresh.status == "idle"  # Status folgt weiter dem Payload


@pytest.mark.asyncio
async def test_heartbeat_does_not_resurrect_parked_waiting_task(client: AsyncClient):
    """Counterpart: once _maybe_park_waiting_task released the session
    (current_task_id=None), the heartbeat must NOT re-point the agent at the
    still-waiting task — the park is deliberate (agent freed for inbox
    work). Preserve-only, never resurrect."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task, token = await _agent_with_active_task(s, task_status="waiting")
        # Helper leaves current_task_id=None — exactly the parked state.

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.current_task_id is None, (
        "geparkter waiting-Task darf vom Heartbeat nicht wiederbelebt werden"
    )
