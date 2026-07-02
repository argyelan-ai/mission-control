"""Heartbeat setzt agent.status — wichtig fuer Non-Gateway-Agents (cli-bridge, host).

Bug vor dem Fix: /agent/me/heartbeat hat nur last_seen_at + run_state gesetzt,
aber NICHT agent.status. Dadurch blieben Docker-cli-bridge-Agents fuer immer
'offline', obwohl sie alle 30s einen Heartbeat senden.
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
    """Heartbeat mit status='idle' setzt agent.status=idle."""
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
    """Bug 18 fix (2026-05-14): 'working' ohne in_progress Task wird auf 'idle'
    gezwungen. Sparky-Symptom: agent.status='working' + current_task_id=None
    nach Voice-Foundation Review weil claude im Pane noch Memories rendert →
    detect_turn_state='working' → Heartbeat sendet 'working' → Backend hat
    aber keine active Task → war vorher: status bleibt 'working' forever.

    Korrektes Verhalten: ohne in_progress Task ist 'working' ein invalides
    Self-Report — coerce zu 'idle'.
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
    # Bug 18: ohne Task assigned → 'working' wird zu 'idle' coerced
    assert fresh.status == "idle"
    assert fresh.run_state == "idle"
    assert fresh.current_task_id is None


@pytest.mark.asyncio
async def test_heartbeat_ignores_unknown_status(client: AsyncClient):
    """status='restarting' via Heartbeat wird ignoriert (nur idle/working/online erlaubt)."""
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
    # Status darf nicht ueberschrieben werden
    assert fresh.status == "idle"


# ── Bug 2 fix: heartbeat self-heals when DB drifts from Task table ────────


async def _agent_with_active_task(session, *, task_status="in_progress", agent_status="idle"):
    """Helper: erstellt Agent + assigned Task im gewuenschten Status."""
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
        current_task_id=None,  # die Stale-Konstellation, die Bug 2 ausloest
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
    """Bug 2 (refined 2026-05-13): Agent hat in_progress Task assigned, poll.sh
    sendet `status: idle`. current_task_id MUSS aus der Task-Tabelle gesynct
    werden (Drift-Fix), ABER agent.status/run_state spiegeln den Payload —
    NICHT pauschal auf "working" maskieren. Sonst sieht der Operator im UI fake-
    activity wenn Sparky am Prompt steht ohne zu cooken.

    poll.sh ist verantwortlich dafuer `heartbeat "working"` zu senden wenn
    claude wirklich aktiv ist (siehe Bug 13: detect_turn_state-based).
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
    # current_task_id self-heal: aus Task-Tabelle abgeleitet
    assert fresh.current_task_id == task.id, "current_task_id muss aus Task-Tabelle abgeleitet werden"
    # Status spiegelt payload, nicht maskiert
    assert fresh.status == "idle", (
        "Bug 2 refined: payload=idle bei active task → status=idle. "
        "Operator erkennt damit dass Task assigned ist aber Agent nicht aktiv."
    )
    assert fresh.run_state == "idle"


@pytest.mark.asyncio
async def test_heartbeat_working_with_active_task_stamps_activity(client: AsyncClient):
    """Bug 2 refined: bei payload=working + active task → status=working,
    current_task_id gesynct, last_task_activity_at gestempelt."""
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
    """Backward-compat: kein in_progress Task → heartbeat=idle setzt idle wie bisher."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # task_status=done -> kein aktiver Task fuer den Agent
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
    """blocked Task ist nicht 'aktiv arbeitend' — Agent darf idle bleiben.
    Sonst zeigt UI ihn als working obwohl er auf den Operator wartet."""
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
    """Bug 18 (2026-05-14): heartbeat=working OHNE in_progress Task → coerce zu
    idle. Vorher: status=working blieb stale forever (Sparky-Live-Symptom).

    Begruendung: poll.sh sollte nur `working` senden wenn CURRENT_TASK_ID
    lokal gesetzt UND detect_turn_state='working' (siehe poll.sh:651-660).
    Wenn Backend keinen in_progress Task hat, ist der Self-Report inkonsistent.
    Wahrscheinlich Race: Task done → poll.sh CURRENT_TASK_ID noch gesetzt →
    claude rendert Memory-Save → working-Heartbeat ohne assigned Task.
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
    """Bug 18 (2026-05-14): Path B (kein active task) clearet current_task_id
    explizit. Schutz gegen Pointer der nach Task done/failed haengen bleibt
    (Sparky-Symptom 2026-05-14: war agent.current_task_id = alter Task-Pointer
    bevor Task done geclearet, Heartbeat lief vorher).
    """
    import uuid as _uuid
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, _token = await _create_agent(s, status="idle")
        # Re-fetch fresh token + stale current_task_id setzen
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
    assert fresh.current_task_id is None  # Stale pointer geclearet
