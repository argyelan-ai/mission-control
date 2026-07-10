"""Lead-first Blocker-Triage (Autonomy Hardening Fix A).

Incident 2026-07-04: Jeder Agent-Blocker erzeugte sofort ein Operator-Approval
(+ Telegram), obwohl der Board-Lead die Blocker in Minuten selbst loeste — und
das 403-Gate sperrte ausgerechnet den Lead aus (Workaround: blocked→inbox,
das ungegated war). Diese Tests pinnen die neue Eskalations-Leiter:

  Stufe 1: technischer Blocker → Lead-Triage (kein Approval, kein Telegram)
  Stufe 2: Triage-Timeout (Watchdog) oder Lead-Eskalation → Operator-Approval
  Direkt:  decision_needed/permission_needed, triage=0, kein Lead
  Gate:    Worker bleibt gegated (in_progress UND inbox), Lead darf loesen
           und supersedet dabei das Approval.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.models.task import TaskComment

from tests.conftest import test_engine


def _naive_utcnow() -> datetime:
    # SQLite speichert tz-naiv; die Altersmathematik im Watchdog braucht im
    # Test eine konsistente naive Uhr (Produktion: timestamptz, beide aware).
    return datetime.utcnow()


async def _make_board_with_agents(
    *,
    with_lead: bool = True,
    triage_minutes: int = 15,
):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id, name="TB", slug=f"tb-{uuid.uuid4().hex[:6]}",
            blocker_triage_minutes=triage_minutes,
        ))
        await s.commit()

        worker_token, worker_hash = generate_agent_token()
        worker = Agent(
            id=uuid.uuid4(), name="Worker", role="developer",
            is_board_lead=False, board_id=board_id, agent_runtime="host",
            agent_token_hash=worker_hash,
            scopes=["heartbeat", "tasks:read", "tasks:write", "tasks:help"],
            model="x", provision_status="provisioned",
        )
        s.add(worker)

        lead = None
        lead_token = None
        if with_lead:
            lead_token, lead_hash = generate_agent_token()
            lead = Agent(
                id=uuid.uuid4(), name="Lead", role="orchestrator",
                is_board_lead=True, board_id=board_id, agent_runtime="host",
                agent_token_hash=lead_hash,
                scopes=["heartbeat", "tasks:read", "tasks:write", "tasks:help"],
                model="x", provision_status="provisioned",
            )
            s.add(lead)
        await s.commit()
        if lead:
            await s.refresh(lead)
        await s.refresh(worker)

    return board_id, worker, worker_token, lead, lead_token


async def _make_task(
    board_id, *, status="in_progress", assigned_agent_id=None,
    blocker_to_operator=None,
):
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            board_id=board_id, title="Blockierbarer Task", description="x",
            status=status, assigned_agent_id=assigned_agent_id,
            blocker_to_operator=blocker_to_operator,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
    return task


def _block_payload(blocker_type: str = "technical_problem") -> dict:
    return {
        "status": "blocked",
        "blocker_type": blocker_type,
        "blocker_question": "Runtime-Abbruch — weitermachen oder abbrechen?",
        "blocker_description": "omp-Turn endete ohne Sentinel",
    }


async def _approvals_for(task_id) -> list[Approval]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        res = await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "blocker_decision",
            )
        )
        return list(res.all())


async def _comments_for(task_id, comment_type: str) -> list[TaskComment]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        res = await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == task_id,
                TaskComment.comment_type == comment_type,
            )
        )
        return list(res.all())


def _patch_triage_redis(fake_redis):
    return patch(
        "app.services.blocker_triage.get_redis",
        AsyncMock(return_value=fake_redis),
    )


def _patch_telegram():
    return patch(
        "app.services.telegram_bot.telegram_bot.send_approval_telegram",
        new_callable=AsyncMock,
    )


# ── Stufe 1: Lead-Triage ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_technical_blocker_goes_to_lead_not_operator(
    client: AsyncClient, fake_redis,
):
    """technical_problem + Lead vorhanden → KEIN Approval, KEIN Telegram,
    actionable Lead-Kommentar + Triage-Payload in Redis."""
    board_id, worker, worker_token, lead, _ = await _make_board_with_agents()
    task = await _make_task(board_id, assigned_agent_id=worker.id)

    with _patch_triage_redis(fake_redis), _patch_telegram() as tg:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload(),
        )
    assert resp.status_code == 200, resp.text

    assert await _approvals_for(task.id) == [], (
        "Stufe 1 darf KEIN Operator-Approval erzeugen"
    )
    tg.assert_not_awaited()

    notes = await _comments_for(task.id, "blocker_lead_notify")
    assert len(notes) == 1
    assert "DU bist zustaendig" in notes[0].content
    assert "escalate_to_operator" in notes[0].content

    stored = await fake_redis.get(f"mc:blocker:triage:{task.id}")
    assert stored is not None, "Triage-Payload muss in Redis liegen"


@pytest.mark.asyncio
async def test_decision_needed_goes_direct_to_operator(
    client: AsyncClient, fake_redis,
):
    """decision_needed ist ein echter Operator-Entscheid → sofort Approval +
    Telegram + Lead-FYI (wie im Alt-Flow)."""
    board_id, worker, worker_token, lead, _ = await _make_board_with_agents()
    task = await _make_task(board_id, assigned_agent_id=worker.id)

    with _patch_triage_redis(fake_redis), _patch_telegram() as tg:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload("decision_needed"),
        )
    assert resp.status_code == 200, resp.text

    approvals = await _approvals_for(task.id)
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert approvals[0].payload.get("blocker_type") == "decision_needed"
    tg.assert_awaited_once()

    notes = await _comments_for(task.id, "blocker_lead_notify")
    assert len(notes) == 1
    assert "Operator-Entscheid" in notes[0].content


@pytest.mark.asyncio
async def test_triage_disabled_board_goes_direct_to_operator(
    client: AsyncClient, fake_redis,
):
    """blocker_triage_minutes=0 → altes Verhalten (direkt Operator)."""
    board_id, worker, worker_token, _, _ = await _make_board_with_agents(
        triage_minutes=0,
    )
    task = await _make_task(board_id, assigned_agent_id=worker.id)

    with _patch_triage_redis(fake_redis), _patch_telegram() as tg:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload(),
        )
    assert resp.status_code == 200, resp.text
    assert len(await _approvals_for(task.id)) == 1
    tg.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocker_to_operator_flag_skips_lead_triage(
    client: AsyncClient, fake_redis,
):
    """task.blocker_to_operator=True → technischer Blocker geht DIREKT an den
    Operator, obwohl ein Lead da ist und Triage aktiv wäre (das Flag gewinnt).
    Approval + Telegram sofort, KEIN Triage-Payload in Redis."""
    board_id, worker, worker_token, lead, _ = await _make_board_with_agents()
    task = await _make_task(
        board_id, assigned_agent_id=worker.id, blocker_to_operator=True,
    )

    with _patch_triage_redis(fake_redis), _patch_telegram() as tg:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload(),  # technical_problem — würde sonst triagiert
        )
    assert resp.status_code == 200, resp.text

    assert len(await _approvals_for(task.id)) == 1, (
        "blocker_to_operator muss ein Operator-Approval sofort erzeugen"
    )
    tg.assert_awaited_once()
    stored = await fake_redis.get(f"mc:blocker:triage:{task.id}")
    assert stored is None, "Triage muss übersprungen sein (kein Payload)"


@pytest.mark.asyncio
async def test_blocker_to_operator_false_still_triages(
    client: AsyncClient, fake_redis,
):
    """Regression: blocker_to_operator=False/None ändert nichts — technischer
    Blocker geht weiter zuerst an den Lead (Triage-Payload liegt in Redis)."""
    board_id, worker, worker_token, lead, _ = await _make_board_with_agents()
    task = await _make_task(
        board_id, assigned_agent_id=worker.id, blocker_to_operator=False,
    )

    with _patch_triage_redis(fake_redis), _patch_telegram() as tg:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload(),
        )
    assert resp.status_code == 200, resp.text
    assert await _approvals_for(task.id) == []
    tg.assert_not_awaited()
    assert await fake_redis.get(f"mc:blocker:triage:{task.id}") is not None


@pytest.mark.asyncio
async def test_no_lead_on_board_goes_direct_to_operator(
    client: AsyncClient, fake_redis,
):
    """Ohne Lead kann niemand triagieren → direkt Operator."""
    board_id, worker, worker_token, _, _ = await _make_board_with_agents(
        with_lead=False,
    )
    task = await _make_task(board_id, assigned_agent_id=worker.id)

    with _patch_triage_redis(fake_redis), _patch_telegram():
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload(),
        )
    assert resp.status_code == 200, resp.text
    assert len(await _approvals_for(task.id)) == 1


# ── Gate: Wege aus `blocked` heraus ─────────────────────────────────────


async def _blocked_task_with_pending_approval(board_id, worker):
    task = await _make_task(board_id, status="blocked", assigned_agent_id=worker.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            board_id=board_id, task_id=task.id, agent_id=worker.id,
            action_type="blocker_decision",
            description="Worker ist blockiert",
            payload={"blocker_type": "technical_problem"},
        ))
        await s.commit()
    return task


@pytest.mark.asyncio
async def test_worker_cannot_unblock_with_pending_approval(
    client: AsyncClient, fake_redis,
):
    """Worker: blocked→in_progress UND blocked→inbox sind bei pending
    Approval beide 403 (die inbox-Luecke war der Gate-Bypass im Incident)."""
    board_id, worker, worker_token, _, _ = await _make_board_with_agents()
    task = await _blocked_task_with_pending_approval(board_id, worker)

    for target in ("in_progress", "inbox"):
        with _patch_triage_redis(fake_redis):
            resp = await client.patch(
                f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
                headers={"Authorization": f"Bearer {worker_token}"},
                json={"status": target},
            )
        assert resp.status_code == 403, (
            f"blocked→{target} muss fuer Worker bei pending Approval 403 sein"
        )


@pytest.mark.asyncio
async def test_lead_unblock_supersedes_approval(
    client: AsyncClient, fake_redis,
):
    """Lead: blocked→in_progress ist erlaubt und supersedet das Approval."""
    board_id, worker, _, lead, lead_token = await _make_board_with_agents()
    task = await _blocked_task_with_pending_approval(board_id, worker)

    with _patch_triage_redis(fake_redis):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"status": "in_progress"},
        )
    assert resp.status_code == 200, resp.text

    approvals = await _approvals_for(task.id)
    assert len(approvals) == 1
    assert approvals[0].status == "superseded"
    assert "Lead" in (approvals[0].resolver_note or "")


@pytest.mark.asyncio
async def test_worker_self_heal_without_approval_is_allowed(
    client: AsyncClient, fake_redis,
):
    """Stufe 1 (kein Approval existiert): der Worker darf sich selbst
    entblocken — Selbstheilung nach transientem Problem."""
    board_id, worker, worker_token, _, _ = await _make_board_with_agents()
    task = await _make_task(board_id, status="blocked", assigned_agent_id=worker.id)

    with _patch_triage_redis(fake_redis):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json={"status": "in_progress"},
        )
    assert resp.status_code == 200, resp.text


# ── Stufe 2: Watchdog-Eskalation nach Triage-Timeout ────────────────────


@pytest.mark.asyncio
async def test_watchdog_escalates_after_triage_window(fake_redis):
    """Blocked-Task ohne Approval, aelter als das Triage-Fenster →
    Watchdog erzeugt das Operator-Approval aus dem Redis-Payload."""
    board_id, worker, _, lead, _ = await _make_board_with_agents(
        triage_minutes=15,
    )
    task = await _make_task(board_id, status="blocked", assigned_agent_id=worker.id)

    # Triage-Payload wie von start_lead_triage hinterlegt
    import json
    await fake_redis.set(
        f"mc:blocker:triage:{task.id}",
        json.dumps({
            "blocker_type": "technical_problem",
            "question": "Runtime-Abbruch",
            "blocker_comment": "omp-Turn endete ohne Sentinel",
        }),
    )

    # Task-Alter ueber das Fenster heben
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        t = await s.get(Task, task.id)
        t.updated_at = _naive_utcnow() - timedelta(minutes=20)
        # Review fix B-1: the watchdog escalation clock is keyed off the
        # dedicated blocked_at timestamp now (updated_at only as fallback).
        t.blocked_at = _naive_utcnow() - timedelta(minutes=20)
        s.add(t)
        await s.commit()

    from app.services.watchdog.core import WatchdogService

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.watchdog.task_monitor.get_redis",
                   AsyncMock(return_value=fake_redis)), \
             patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
             _patch_triage_redis(fake_redis), \
             patch("app.services.blocker_triage.utcnow", _naive_utcnow), \
             patch("app.services.blocker_triage.emit_event", new_callable=AsyncMock), \
             _patch_telegram() as tg:
            svc = WatchdogService()
            await svc._check_blocked_tasks(s)

    approvals = await _approvals_for(task.id)
    assert len(approvals) == 1, "Triage-Timeout muss ein Operator-Approval erzeugen"
    assert approvals[0].payload.get("escalation_reason") == "triage_timeout"
    assert approvals[0].payload.get("blocker_type") == "technical_problem"
    tg.assert_awaited_once()


@pytest.mark.asyncio
async def test_watchdog_waits_inside_triage_window(fake_redis):
    """Innerhalb des Fensters passiert nichts — der Lead hat noch Zeit."""
    board_id, worker, _, _, _ = await _make_board_with_agents(triage_minutes=15)
    task = await _make_task(board_id, status="blocked", assigned_agent_id=worker.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        t = await s.get(Task, task.id)
        t.updated_at = _naive_utcnow() - timedelta(minutes=5)
        s.add(t)
        await s.commit()

    from app.services.watchdog.core import WatchdogService

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.watchdog.task_monitor.get_redis",
                   AsyncMock(return_value=fake_redis)), \
             patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
             _patch_triage_redis(fake_redis), \
             _patch_telegram() as tg:
            svc = WatchdogService()
            await svc._check_blocked_tasks(s)

    assert await _approvals_for(task.id) == []
    tg.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_skips_callback_waits(fake_redis):
    """Orchestrierungs-Waits (blocked_by_task_id) eskalieren NIE."""
    board_id, worker, _, _, _ = await _make_board_with_agents(triage_minutes=1)
    sub = await _make_task(board_id, status="in_progress")
    task = await _make_task(board_id, status="blocked", assigned_agent_id=worker.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        t = await s.get(Task, task.id)
        t.blocked_by_task_id = sub.id
        t.updated_at = _naive_utcnow() - timedelta(minutes=200)
        s.add(t)
        await s.commit()

    from app.services.watchdog.core import WatchdogService

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.watchdog.task_monitor.get_redis",
                   AsyncMock(return_value=fake_redis)), \
             patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
             _patch_telegram() as tg:
            svc = WatchdogService()
            await svc._check_blocked_tasks(s)

    assert await _approvals_for(task.id) == []
    tg.assert_not_awaited()


# ── Stufe 2: explizite Lead-Eskalation ──────────────────────────────────


@pytest.mark.asyncio
async def test_lead_escalation_comment_creates_approval_immediately(
    client: AsyncClient, fake_redis,
):
    """comment_type=escalate_to_operator vom Lead → Approval sofort,
    Frist wird nicht abgewartet."""
    board_id, worker, worker_token, lead, lead_token = await _make_board_with_agents()
    task = await _make_task(board_id, assigned_agent_id=worker.id)

    # Worker blockiert (Stufe 1)
    with _patch_triage_redis(fake_redis), _patch_telegram():
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload(),
        )
        assert resp.status_code == 200, resp.text
        assert await _approvals_for(task.id) == []

        # Lead eskaliert explizit
        with patch("app.services.blocker_triage.utcnow", _naive_utcnow):
            resp = await client.post(
                f"/api/v1/agent/boards/{board_id}/tasks/{task.id}/comments",
                headers={"Authorization": f"Bearer {lead_token}"},
                json={
                    "content": "Das ist ein Infrastruktur-Entscheid fuer den Operator.",
                    "comment_type": "escalate_to_operator",
                },
            )
    assert resp.status_code in (200, 201), resp.text

    approvals = await _approvals_for(task.id)
    assert len(approvals) == 1
    assert approvals[0].payload.get("escalation_reason") == "lead_escalated"
