"""REX-Review-Proben zu PR #511 (W5-E) — Serverseite.

Belegt drei Dinge, die der Auftrag wissen will:
  S1 Der Guard feuert wirklich bei einem stale Header (409) — die Pruefung,
     die der Client-Rebind fuer den ack-Pfad umgeht, ist real.
  S2 Genau derselbe Request mit der VOM SERVER GELESENEN ID geht durch —
     der Header ist damit kein Besitznachweis mehr, sondern ein Lookup.
  S3 Der missing-header-Pfad bei task.dispatch_attempt_id=None ist
     ausdruecklich erlaubt (Guard-Bedingung `if task.dispatch_attempt_id`),
     keine Luecke.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup(*, attempt_id: str | None, status: str | None = None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token_raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Probe", slug=f"pr-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(id=agent_id, name="Worker", role="researcher",
                    board_id=board_id, agent_token_hash=token_hash,
                    scopes=["tasks:read", "tasks:write"],
                    provision_status="provisioned", current_task_id=task_id))
        s.add(Task(id=task_id, board_id=board_id, title="Karte",
                   status=status or ("inbox" if attempt_id is None else "in_progress"),
                   assigned_agent_id=agent_id, dispatch_attempt_id=attempt_id))
        await s.commit()
    return {"board_id": board_id, "task_id": task_id,
            "attempt_id": attempt_id, "token": token_raw}


@pytest.mark.asyncio
async def test_probe_s1_stale_header_is_rejected(client, fake_redis):
    """S1: alter Run sendet seine ALTE Attempt-ID -> 409 stale."""
    new_attempt = str(uuid.uuid4())
    d = await _setup(attempt_id=new_attempt, status="inbox")
    old_attempt = str(uuid.uuid4())

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{d['board_id']}/tasks/{d['task_id']}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {d['token']}",
                     "X-Dispatch-Attempt-Id": old_attempt},
        )
    print(f"\n[S1] stale header -> http={resp.status_code}")
    print(f"[S1] detail={resp.json().get('detail','')[:160]}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_probe_s2_header_read_from_server_always_passes(client, fake_redis):
    """S2: derselbe Aufrufer sendet die ID, die er gerade per GET gelesen
    hat -> geht durch. Der Guard kann fuer diesen Pfad nicht mehr feuern."""
    new_attempt = str(uuid.uuid4())
    d = await _setup(attempt_id=new_attempt, status="inbox")

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        detail = await client.get(
            f"/api/v1/agent/boards/{d['board_id']}/tasks/{d['task_id']}/detail",
            headers={"Authorization": f"Bearer {d['token']}"},
        )
        read_back = detail.json().get("dispatch_attempt_id")
        resp = await client.patch(
            f"/api/v1/agent/boards/{d['board_id']}/tasks/{d['task_id']}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {d['token']}",
                     "X-Dispatch-Attempt-Id": read_back},
        )
    print(f"\n[S2] GET /detail liefert dispatch_attempt_id={read_back}")
    print(f"[S2] PATCH mit genau dieser ID -> http={resp.status_code}")
    assert read_back == new_attempt, "Detail-GET gibt die erwartete ID heraus"
    assert resp.status_code < 400, (
        "Wer die erwartete ID lesen darf, kommt immer durch — der Header ist "
        "fuer diesen Pfad kein Besitznachweis mehr."
    )


@pytest.mark.asyncio
async def test_probe_s3_missing_header_allowed_when_task_has_no_attempt(client, fake_redis):
    """S3: Karte zugewiesen, nie dispatcht (dispatch_attempt_id=None).
    PATCH ohne Header -> erlaubt. Das ist die Guard-Bedingung selbst
    (`if task.dispatch_attempt_id and ...`), keine Luecke."""
    d = await _setup(attempt_id=None)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{d['board_id']}/tasks/{d['task_id']}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {d['token']}"},
        )
    print(f"\n[S3] task.dispatch_attempt_id=None, PATCH ohne Header -> http={resp.status_code}")
    print(f"[S3] body={resp.text[:160]}")
    assert resp.status_code < 400

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, d["task_id"])
        print(f"[S3] danach: status={t.status} ack_at={'gesetzt' if t.ack_at else 'None'} "
              f"dispatch_attempt_id={t.dispatch_attempt_id}")


@pytest.mark.asyncio
async def test_probe_s3b_missing_header_rejected_when_task_has_attempt(client, fake_redis):
    """S3b: Gegenprobe — hat die Karte eine Attempt-ID, ist der fehlende
    Header ein harter 409. Der erlaubte Pfad aus S3 ist also eng begrenzt."""
    d = await _setup(attempt_id=str(uuid.uuid4()))

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{d['board_id']}/tasks/{d['task_id']}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {d['token']}"},
        )
    print(f"\n[S3b] task hat Attempt-ID, PATCH ohne Header -> http={resp.status_code}")
    assert resp.status_code == 409
