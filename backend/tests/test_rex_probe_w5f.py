"""REX-Review-Proben zu PR #510 (W5-F), 11.09.2026.

Unabhaengig von den Tests des PR geschrieben. Jede Probe formuliert eine im
PR-Text oder im Code-Kommentar BEHAUPTETE Eigenschaft als Assertion. Proben,
die fehlschlagen, sind die Review-Befunde — sie sind bewusst rot.

Gruen (Behauptung haelt):
  - test_probe_sabotage_no_card_no_comment
  - test_probe_root_path_warns_explicitly
Rot (Befund):
  - test_probe_explicit_parent_is_resumed_by_watchdog_despite_claim  (B2)
  - test_probe_cross_board_delegation_via_explicit_parent            (B1)
  - test_probe_worker_without_active_task_bypasses_gate_via_parent   (B3)
"""

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
        s.add(Agent(
            id=aid, name=name, role="orchestrator" if lead else "worker",
            board_id=board_id, agent_token_hash=thash, is_board_lead=lead,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            current_task_id=current_task_id, provision_status="provisioned",
        ))
        await s.commit()
    return aid, token


async def _mk_task(board_id, title, status, assigned_agent_id=None, blocked_by=None):
    from app.models.task import Task
    tid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=tid, board_id=board_id, title=title, status=status,
            assigned_agent_id=assigned_agent_id, blocked_by_task_id=blocked_by,
        ))
        await s.commit()
    return tid


# ── Gruen: die zwei Kernbehauptungen des PR halten ──────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", ["waiting", "blocked", "review", "done"])
async def test_probe_sabotage_no_card_no_comment(client, fake_redis, bad_status):
    """Sabotage-Probe: aktive Karte nicht in_progress -> 409 mit Parent-ID +
    Status, UND nachweislich keine Karte und kein Kommentar angelegt."""
    from app.models.task import Task, TaskComment

    board = await _mk_board("Sab")
    parent = await _mk_task(board, "Boss-Karte", bad_status)
    _, boss_token = await _mk_agent(board, "Boss", lead=True, current_task_id=parent)
    worker_id, _ = await _mk_agent(board, "Researcher", lead=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t_before = (await s.exec(select(func.count()).select_from(Task))).one()
        c_before = (await s.exec(select(func.count()).select_from(TaskComment))).one()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(worker_id)},
                headers={"Authorization": f"Bearer {boss_token}"},
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t_after = (await s.exec(select(func.count()).select_from(Task))).one()
        c_after = (await s.exec(select(func.count()).select_from(TaskComment))).one()

    detail = resp.json().get("detail", "")
    assert resp.status_code == 409
    assert str(parent) in detail, "Parent-ID fehlt im Klartext"
    assert bad_status in detail, "Status fehlt im Klartext"
    assert t_after == t_before, "409 hat trotzdem eine Karte angelegt"
    assert c_after == c_before, "409 hat trotzdem einen Kommentar geschrieben"


@pytest.mark.asyncio
async def test_probe_root_path_warns_explicitly(client, fake_redis):
    """Wurzel-Pfad: Board Lead, keine aktive Karte, kein --parent.
    Anlage klappt UND die Antwort sagt ausdruecklich 'kein Parent, kein
    Callback' — nicht nur `no_task`."""
    from app.models.task import Task

    board = await _mk_board("Root")
    _, boss_token = await _mk_agent(board, "Boss", lead=True)
    worker_id, _ = await _mk_agent(board, "Researcher", lead=False)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Wurzelkarte", "description": "x",
                      "assigned_agent_id": str(worker_id)},
                headers={"Authorization": f"Bearer {boss_token}"},
            )
    body = resp.json()
    assert resp.status_code == 201
    assert body["parent_task_id"] is None
    w = body["warning"] or ""
    assert "Parent" in w and "Callback" in w, "Warnung nennt Parent/Callback nicht"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub = await s.get(Task, uuid.UUID(body["subtask_id"]))
        assert sub.parent_task_id is None


# ── Rot: die drei Befunde ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_explicit_parent_is_resumed_by_watchdog_despite_claim(client, fake_redis):
    """B2 — PR-Behauptung: 'The named parent is linked via parent_task_id but
    never auto-blocked/resumed'.

    Probe: Agent ohne aktive Karte delegiert mit --parent auf eine FREMDE
    Karte, die `blocked` mit blocked_by_task_id=NULL steht, mit callback=True.
    Subtask geht auf done -> _handle_callback_resume greift ueber den
    parent_task_id-Fallback und weckt die fremde Karte.
    """
    from app.models.task import Task
    from app.routers.agent_task_status import _handle_callback_resume

    board = await _mk_board("P1Board")
    _, boss_token = await _mk_agent(board, "Boss", lead=True)
    victim_id, _ = await _mk_agent(board, "Victim", lead=False)
    worker_id, _ = await _mk_agent(board, "Researcher", lead=False)

    foreign = await _mk_task(board, "Fremde Karte von Victim", "blocked",
                             assigned_agent_id=victim_id, blocked_by=None)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(worker_id),
                      "parent_task_id": str(foreign), "callback": True},
                headers={"Authorization": f"Bearer {boss_token}"},
            )
    assert resp.status_code == 201, resp.text
    sub_id = uuid.UUID(resp.json()["subtask_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        status_before = (await s.get(Task, foreign)).status
        sub = await s.get(Task, sub_id)
        sub.status = "done"
        s.add(sub)
        await s.commit()
        with patch("app.routers.agent_task_status.dispatch_callback_to_parent",
                   new_callable=AsyncMock):
            with patch("app.services.activity.emit_event", new_callable=AsyncMock):
                await _handle_callback_resume(s, sub)
        status_after = (await s.get(Task, foreign)).status

    assert status_after == "blocked", (
        f"PR behauptet: --parent-Task wird nie auto-resumed. "
        f"Tatsaechlich: {status_before} -> {status_after}"
    )


@pytest.mark.asyncio
async def test_probe_cross_board_delegation_via_explicit_parent(client, fake_redis):
    """B1 — Board-Isolation: Agent gehoert Board A, delegiert per --parent
    auf eine Karte in Board B an einen Agenten in Board B.

    Der Root-Zweig prueft dafuer explizit `agent.board_id != board_id` (403);
    der neue --parent-Zweig prueft nur den Parent, nicht den Agenten.
    """
    from app.models.task import Task

    board_a = await _mk_board("BoardA")
    board_b = await _mk_board("BoardB")
    _, intruder_token = await _mk_agent(board_a, "Intruder", lead=True)
    bworker_id, _ = await _mk_agent(board_b, "BWorker", lead=False)
    parent_b = await _mk_task(board_b, "Fremdes Board, fremde Karte", "in_progress")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        before = (await s.exec(select(func.count()).select_from(Task))).one()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board_b}/delegate",
                json={"title": "Cross-board Karte", "description": "x",
                      "assigned_agent_id": str(bworker_id),
                      "parent_task_id": str(parent_b)},
                headers={"Authorization": f"Bearer {intruder_token}"},
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after = (await s.exec(select(func.count()).select_from(Task))).one()

    assert resp.status_code == 403, (
        f"Agent von Board A konnte per --parent in Board B delegieren "
        f"(status {resp.status_code}). Der Root-Zweig verbietet genau das."
    )
    assert after == before, "Abgelehnter Request darf keine Karte anlegen"


@pytest.mark.asyncio
async def test_probe_worker_without_active_task_bypasses_gate_via_parent(client, fake_redis):
    """B3 — Invariante vorher: 'Delegation nur aus aktiver Arbeit heraus'
    (Worker ohne aktive Karte -> 409). --parent oeffnet dieses Gate, ohne
    dass irgendwo Ownership am genannten Parent geprueft wird.
    """
    board = await _mk_board("P3Board")
    _, worker_token = await _mk_agent(board, "Worker", lead=False)  # KEINE aktive Karte
    target_id, _ = await _mk_agent(board, "Target", lead=False)
    someone_elses = await _mk_task(board, "Karte eines anderen", "in_progress")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(target_id),
                      "parent_task_id": str(someone_elses)},
                headers={"Authorization": f"Bearer {worker_token}"},
            )

    assert resp.status_code == 409, (
        f"Worker ohne aktive Karte konnte per --parent an einer fremden Karte "
        f"delegieren (status {resp.status_code}) — das alte 409-Gate ist umgehbar."
    )
