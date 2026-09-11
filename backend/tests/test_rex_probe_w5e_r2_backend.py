"""REX-Runde-2-Probe zu PR #511 — die tragende Annahme der Fallunterscheidung.

_cmd_ack lehnt Fall (b) NICHT selbst ab: es laesst den PATCH mit dem eigenen
(alten) Header rausgehen und uebersetzt erst den Server-409 in Klartext. Das
traegt nur, wenn der Attempt-Guard VOR der Status-Uebergangspruefung greift.
Sonst antwortet der Server auf die in_progress-Karte mit 400 "In Progress ->
In Progress" — und genau dieser String ist in _cmd_ack der Idempotenz-Erfolg:
der Zombie-ACK gaelte als gelungen.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup(*, attempt_id: str, status: str):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token_raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Probe R2", slug=f"pr2-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(id=agent_id, name="Worker R2", role="researcher",
                    board_id=board_id, agent_token_hash=token_hash,
                    scopes=["tasks:read", "tasks:write"],
                    provision_status="provisioned", current_task_id=task_id))
        s.add(Task(id=task_id, board_id=board_id, title="Karte R2",
                   status=status, assigned_agent_id=agent_id,
                   dispatch_attempt_id=attempt_id))
        await s.commit()
    return {"board_id": board_id, "task_id": task_id, "token": token_raw}


@pytest.mark.asyncio
async def test_probe_r2_attempt_guard_beats_status_validation(client, fake_redis):
    """Karte ist in_progress, Zombie sendet alten Header, PATCH in_progress.

    Erwartet 409 (Attempt-Guard). Kaeme 400 "In Progress -> In Progress",
    wuerde _cmd_ack den Zombie-ACK als Idempotenz-Erfolg durchwinken.
    """
    d = await _setup(attempt_id=str(uuid.uuid4()), status="in_progress")
    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{d['board_id']}/tasks/{d['task_id']}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {d['token']}",
                     "X-Dispatch-Attempt-Id": str(uuid.uuid4())},
        )
    detail = resp.json().get("detail", "")
    print(f"\n[R2-1] in_progress + stale header -> http={resp.status_code}")
    print(f"[R2-1] detail={detail[:140]}")
    assert resp.status_code == 409, (
        f"Attempt-Guard muss VOR der Statuspruefung greifen, bekam "
        f"http={resp.status_code} detail={detail[:200]}"
    )
    assert "In Progress" not in detail, (
        "Antwort enthaelt den Idempotenz-String — _cmd_ack wuerde den "
        "Zombie-ACK als Erfolg werten."
    )


@pytest.mark.asyncio
async def test_probe_r2_same_agent_same_card_is_not_a_free_pass(client, fake_redis):
    """Gegenprobe: derselbe Agent auf seiner EIGENEN Karte bekommt keinen
    Rabatt — der Guard haengt nur an der Attempt-ID, nicht am Besitz."""
    attempt = str(uuid.uuid4())
    d = await _setup(attempt_id=attempt, status="in_progress")
    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        ok = await client.patch(
            f"/api/v1/agent/boards/{d['board_id']}/tasks/{d['task_id']}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {d['token']}",
                     "X-Dispatch-Attempt-Id": attempt},
        )
    print(f"[R2-2] korrekte ID auf eigener Karte -> http={ok.status_code}")
    assert ok.status_code < 400 or "Reflexion" in str(ok.json()), ok.json()
