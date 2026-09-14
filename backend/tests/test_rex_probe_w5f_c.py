"""REX-Probe C: Nebenwirkungen von --parent auf fremde/geschlossene Karten."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from tests.conftest import test_engine


async def _mk_board(name):
    from app.models.board import Board
    bid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=bid, name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}"))
        await s.commit()
    return bid


async def _mk_agent(board_id, name, *, lead=False, current_task_id=None):
    from app.models.agent import Agent
    from app.auth import generate_agent_token
    aid = uuid.uuid4()
    token, thash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(id=aid, name=name, role="orchestrator" if lead else "worker",
                    board_id=board_id, agent_token_hash=thash, is_board_lead=lead,
                    scopes=["tasks:read", "tasks:write", "tasks:create"],
                    current_task_id=current_task_id, provision_status="provisioned"))
        await s.commit()
    return aid, token


async def _mk_task(board_id, title, status, assigned_agent_id=None):
    from app.models.task import Task
    tid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=tid, board_id=board_id, title=title, status=status,
                   assigned_agent_id=assigned_agent_id))
        await s.commit()
    return tid


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_status", ["done", "archived", "failed"])
async def test_probe_parent_may_be_a_closed_card(client, fake_redis, parent_status):
    """--parent auf eine bereits abgeschlossene Karte: erlaubt?"""
    from app.models.task import Task
    board = await _mk_board("Closed")
    boss_id, boss_token = await _mk_agent(board, "Boss", lead=True)
    worker_id, _ = await _mk_agent(board, "W", lead=False)
    closed = await _mk_task(board, "Abgeschlossene Karte", parent_status)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub unter toter Karte", "description": "x",
                      "assigned_agent_id": str(worker_id),
                      "parent_task_id": str(closed)},
                headers={"Authorization": f"Bearer {boss_token}"},
            )
    print(f"\n[CLOSED {parent_status}] http={resp.status_code} warning={resp.json().get('warning')}")
    assert resp.status_code != 201, (
        f"Kind wurde unter einer '{parent_status}'-Karte angelegt — die Arbeit "
        f"haengt damit unter einer geschlossenen Karte, ohne Warnung."
    )


@pytest.mark.asyncio
async def test_probe_comment_written_into_foreign_card(client, fake_redis):
    """Schreibt --parent einen Kommentar in eine fremde Karte?"""
    from app.models.task import TaskComment
    board = await _mk_board("Foreign")
    boss_id, boss_token = await _mk_agent(board, "Boss", lead=True)
    victim_id, _ = await _mk_agent(board, "Victim", lead=False)
    worker_id, _ = await _mk_agent(board, "W", lead=False)
    foreign = await _mk_task(board, "Karte von Victim", "in_progress", assigned_agent_id=victim_id)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(worker_id),
                      "parent_task_id": str(foreign)},
                headers={"Authorization": f"Bearer {boss_token}"},
            )
    assert resp.status_code == 201, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(TaskComment).where(TaskComment.task_id == foreign))).all()
    print(f"\n[FOREIGN] Kommentare in fremder Karte: {len(rows)}")
    for r in rows:
        print(f"[FOREIGN] content={r.content[:150]!r}")
    assert len(rows) == 0, "Delegation schreibt in eine Karte, die dem Agenten nicht gehoert"
