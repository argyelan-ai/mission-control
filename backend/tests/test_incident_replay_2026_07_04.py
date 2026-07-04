"""E2E-Replay des Incidents vom 2026-07-04 (Autonomy Hardening, Regressionstest).

Original-Ablauf (Minecraft-Kette): Phase-Rewrite oeffnete alle 3 Subtasks
parallel → Verifier lief vor dem Fix (falscher Blocker → Approval #1) →
Coder-Turn endete ohne Sentinel (Blocker → Approval #2 + Telegram) →
dependency_zombie 60s spaeter (Approval #3) → Lead loeste korrekt, wurde aber
vom 403-Gate ausgesperrt → Kette stand ~45min bis zum Operator-Klick.

Ziel-Assertion nach den Fixes A+C+D: derselbe Ablauf erzeugt
**0 Operator-Approvals** — der Lead loest alles selbst, die Kette laeuft.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.models.task import Task, TaskComment, TaskDependency

from tests.conftest import test_engine


def _naive_utcnow() -> datetime:
    return datetime.utcnow()


async def _pending_approvals(board_id) -> list[Approval]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        res = await s.exec(
            select(Approval).where(
                Approval.board_id == board_id,
                Approval.status == "pending",
            )
        )
        return list(res.all())


@pytest.mark.asyncio
async def test_incident_replay_zero_operator_approvals(client: AsyncClient, fake_redis):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    board_id = uuid.uuid4()
    tokens: dict[str, str] = {}
    agents: dict[str, Agent] = {}

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id, name="Replay", slug=f"rp-{uuid.uuid4().hex[:6]}",
            blocker_triage_minutes=15,
        ))
        await s.commit()
        specs = [
            ("Lead", "orchestrator", True),
            ("Coder", "Workhorse Developer", False),
            ("Deployer", "Deployment Specialist", False),
            ("Verifier", "QA Tester", False),
        ]
        for name, role, is_lead in specs:
            raw, th = generate_agent_token()
            a = Agent(
                id=uuid.uuid4(), name=name, role=role, is_board_lead=is_lead,
                board_id=board_id, agent_runtime="host", agent_token_hash=th,
                scopes=["heartbeat", "tasks:read", "tasks:write", "tasks:help"],
                model="x", provision_status="provisioned",
            )
            s.add(a)
            await s.commit()
            await s.refresh(a)
            tokens[name] = raw
            agents[name] = a

        parent = Task(board_id=board_id, title="Game", status="in_progress",
                      assigned_agent_id=agents["Lead"].id)
        s.add(parent)
        await s.commit()
        await s.refresh(parent)

        code = Task(board_id=board_id, title="Code game", status="done",
                    parent_task_id=parent.id, assigned_agent_id=agents["Coder"].id)
        deploy = Task(board_id=board_id, title="Deploy game", status="done",
                      parent_task_id=parent.id, assigned_agent_id=agents["Deployer"].id)
        verify = Task(board_id=board_id, title="Verify game", status="done",
                      parent_task_id=parent.id, assigned_agent_id=agents["Verifier"].id)
        s.add_all([code, deploy, verify])
        await s.commit()
        for t in (code, deploy, verify):
            await s.refresh(t)
        s.add_all([
            TaskDependency(task_id=deploy.id, depends_on_task_id=code.id),
            TaskDependency(task_id=verify.id, depends_on_task_id=deploy.id),
        ])
        approval_task = Task(
            board_id=board_id, title="Phase Approval: Game", status="in_progress",
            parent_task_id=parent.id, assigned_agent_id=agents["Lead"].id,
            delegation_type="phase_approval",
        )
        s.add(approval_task)
        await s.commit()
        await s.refresh(approval_task)

    triage_redis = patch(
        "app.services.blocker_triage.get_redis", AsyncMock(return_value=fake_redis),
    )
    telegram = patch(
        "app.services.telegram_bot.telegram_bot.send_approval_telegram",
        new_callable=AsyncMock,
    )

    dispatched: list = []

    async def _capture_dispatch(task_id, *_args, **_kw):
        dispatched.append(task_id)

    # ── Akt 1: Reviewer fand einen Bug → Lead löst Phase-Rewrite aus ────
    with triage_redis, telegram as tg, \
         patch("app.services.dispatch.auto_dispatch_task", new=_capture_dispatch):
        r = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{approval_task.id}/comments",
            headers={"Authorization": f"Bearer {tokens['Lead']}"},
            json={
                "content": (
                    f"subtask: {code.id}, grund: Bug in Kernmechanik\n"
                    f"subtask: {deploy.id}, grund: Nach Fix re-deployen\n"
                    f"subtask: {verify.id}, grund: Nach Re-Deploy verifizieren\n"
                ),
                "comment_type": "phase_rewrite_request",
            },
        )
        assert r.status_code in (200, 201), r.text
        import asyncio as _aio
        await _aio.sleep(0)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        code_f = await s.get(Task, code.id)
        deploy_f = await s.get(Task, deploy.id)
        verify_f = await s.get(Task, verify.id)
    # Fix C: Nur der Upstream startet — kein Verifier-Race mehr.
    assert code_f.status == "in_progress"
    assert deploy_f.status == "inbox"
    assert verify_f.status == "inbox"
    assert dispatched == [code.id]
    assert await _pending_approvals(board_id) == []

    # ── Akt 2: Coder-Runtime bricht ab (im Original: Turn ohne Sentinel;
    # die Bridge nudged jetzt 2x — hier der Restfall: Budget erschoepft,
    # Blocker technical_problem wird gemeldet) ──────────────────────────
    with triage_redis, telegram as tg:
        r = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{code.id}",
            headers={"Authorization": f"Bearer {tokens['Coder']}"},
            json={
                "status": "blocked",
                "blocker_type": "technical_problem",
                "blocker_question": "omp-Turn endete ohne Sentinel — fortsetzen?",
            },
        )
        assert r.status_code == 200, r.text
        tg.assert_not_awaited()

    # Fix A Stufe 1: kein Operator-Approval, Lead wurde actionable informiert.
    assert await _pending_approvals(board_id) == []
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        notes = (await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == code.id,
                TaskComment.comment_type == "blocker_lead_notify",
            )
        )).all()
    assert len(notes) == 1

    # ── Akt 3: dependency_zombie-Watchdog laeuft WAEHREND des Blockers ──
    from app.services.watchdog.core import WatchdogService
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.watchdog.task_monitor.get_redis",
                   AsyncMock(return_value=fake_redis)), \
             patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
             patch("app.services.watchdog.task_monitor.emit_event",
                   new_callable=AsyncMock):
            await WatchdogService()._check_dependency_zombies(s)
    # Fix D: kein zweites Approval fuer denselben Vorfall.
    assert await _pending_approvals(board_id) == []

    # ── Akt 4: Lead triagiert und loest den Blocker selbst ──────────────
    with triage_redis, telegram:
        r = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{code.id}/comments",
            headers={"Authorization": f"Bearer {tokens['Lead']}"},
            json={
                "content": "Runtime-Abbruch, kein echtes Problem — weiter mit dem Fix.",
                "comment_type": "resolution",
            },
        )
        assert r.status_code in (200, 201), r.text
        r = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{code.id}",
            headers={"Authorization": f"Bearer {tokens['Lead']}"},
            json={"status": "in_progress"},
        )
        # Der Incident-Kern: KEIN 403 mehr fuer den Lead.
        assert r.status_code == 200, f"Lead-Unblock darf nicht 403en: {r.text}"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        code_f = await s.get(Task, code.id)
    assert code_f.status == "in_progress"

    # ── Finale Assertion: der gesamte Vorfall lief OHNE Operator ────────
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        all_approvals = (await s.exec(
            select(Approval).where(Approval.board_id == board_id)
        )).all()
    assert all_approvals == [], (
        f"Der Incident-Replay darf KEIN Operator-Approval erzeugen, "
        f"fand aber: {[(a.action_type, a.status) for a in all_approvals]}"
    )
