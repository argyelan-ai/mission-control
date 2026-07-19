"""Watchdog: Ein Vorfall = ein Approval (Fix D) + kein stiller Approval-Tod (Fix E).

Incident 2026-07-04:
- `_check_dependency_zombies` behandelte `blocked` als terminal und feuerte
  60 Sekunden nach dem Ursprungs-Blocker ein ZWEITES Approval fuer denselben
  Vorfall (3 Approvals fuer einen Vorfall).
- `_check_expired_approvals` setzte Blocker-Approvals nach 24h still auf
  `expired` — der Task blieb fuer immer blocked, niemand erinnerte mehr.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.models.board import Board
from app.models.task import Task, TaskDependency

from tests.conftest import test_engine


def _naive_utcnow() -> datetime:
    return datetime.utcnow()


async def _setup_chain(*, upstream_status: str, upstream_age_minutes: int,
                       with_pending_approval: bool):
    """Dependent (in_progress) wartet auf Upstream mit gegebenem Zustand."""
    from app.models.agent import Agent

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"zb-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(id=agent_id, name="Worker", board_id=board_id, model="x"))
        upstream = Task(
            board_id=board_id, title="Upstream", status=upstream_status,
            assigned_agent_id=agent_id,
            updated_at=_naive_utcnow() - timedelta(minutes=upstream_age_minutes),
        )
        dependent = Task(
            board_id=board_id, title="Dependent", status="in_progress",
            assigned_agent_id=agent_id,
        )
        s.add_all([upstream, dependent])
        await s.commit()
        await s.refresh(upstream)
        await s.refresh(dependent)
        s.add(TaskDependency(task_id=dependent.id, depends_on_task_id=upstream.id))
        if with_pending_approval:
            s.add(Approval(
                board_id=board_id, task_id=upstream.id, agent_id=agent_id,
                action_type="blocker_decision", description="blockiert",
                payload={"blocker_type": "technical_problem"},
            ))
        await s.commit()
    return board_id, upstream, dependent


async def _run_zombie_check(fake_redis):
    from app.services.watchdog.core import WatchdogService
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.watchdog.task_monitor.get_redis",
                   AsyncMock(return_value=fake_redis)), \
             patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
             patch("app.services.watchdog.task_monitor.emit_event",
                   new_callable=AsyncMock):
            svc = WatchdogService()
            await svc._check_dependency_zombies(s)


async def _zombie_approvals(task_id) -> list[Approval]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        res = await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "dependency_zombie",
            )
        )
        return list(res.all())


# ── Fix D: dependency_zombie ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_zombie_when_upstream_blocker_has_pending_approval(fake_redis):
    """Upstream blocked + pending Approval → der Vorfall hat schon einen Fall
    beim Operator. KEIN zweites Approval (das war der Incident-Spam)."""
    _, upstream, dependent = await _setup_chain(
        upstream_status="blocked", upstream_age_minutes=200,
        with_pending_approval=True,
    )
    await _run_zombie_check(fake_redis)
    assert await _zombie_approvals(dependent.id) == []


@pytest.mark.asyncio
async def test_no_zombie_while_lead_triage_runs(fake_redis):
    """Upstream frisch blocked (Lead-Triage laeuft, <60min) → kein Zombie."""
    _, upstream, dependent = await _setup_chain(
        upstream_status="blocked", upstream_age_minutes=10,
        with_pending_approval=False,
    )
    await _run_zombie_check(fake_redis)
    assert await _zombie_approvals(dependent.id) == []


@pytest.mark.asyncio
async def test_zombie_safety_net_for_dead_blocked_upstream(fake_redis):
    """Upstream >60min blocked OHNE offenen Fall → Leiter faktisch tot,
    Safety-Net-Approval ist korrekt."""
    _, upstream, dependent = await _setup_chain(
        upstream_status="blocked", upstream_age_minutes=120,
        with_pending_approval=False,
    )
    await _run_zombie_check(fake_redis)
    assert len(await _zombie_approvals(dependent.id)) == 1


@pytest.mark.asyncio
async def test_zombie_for_failed_upstream_unchanged(fake_redis):
    """failed bleibt terminal → Zombie-Approval wie bisher."""
    _, upstream, dependent = await _setup_chain(
        upstream_status="failed", upstream_age_minutes=5,
        with_pending_approval=False,
    )
    await _run_zombie_check(fake_redis)
    assert len(await _zombie_approvals(dependent.id)) == 1


# ── Fix E: Approval-Renewal statt stilles Expire ────────────────────────


async def _make_expired_approval(action_type: str, payload: dict | None = None) -> Approval:
    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"eb-{uuid.uuid4().hex[:6]}"))
        approval = Approval(
            board_id=board_id, action_type=action_type,
            description="Testfall", payload=payload or {},
            status="pending",
            expires_at=_naive_utcnow() - timedelta(hours=1),
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)
    return approval


async def _run_expiry_check():
    from app.services.watchdog.core import WatchdogService
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.watchdog.health_checks.utcnow", _naive_utcnow), \
             patch("app.services.watchdog.health_checks.emit_event",
                   new_callable=AsyncMock) as emit, \
             patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
                   new_callable=AsyncMock) as tg, \
             patch("app.services.approval_cleanup.reconcile_stale_approvals",
                   new_callable=AsyncMock):
            svc = WatchdogService()
            await svc._check_expired_approvals(s)
    return emit, tg


@pytest.mark.asyncio
async def test_blocker_approval_renews_instead_of_expiring():
    """blocker_decision: Renewal (+24h, renewal_count, pending bleibt) +
    Telegram-Reminder statt stillem Tod."""
    approval = await _make_expired_approval(
        "blocker_decision",
        {"blocked_agent_name": "Sparky", "task_title": "Code game",
         "question": "Weitermachen?"},
    )
    emit, tg = await _run_expiry_check()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Approval, approval.id)
        assert fresh.status == "pending", "Renewal darf den Status nicht aendern"
        assert fresh.payload.get("renewal_count") == 1
        assert fresh.expires_at.replace(tzinfo=None) > _naive_utcnow() + timedelta(hours=23)

    tg.assert_awaited_once()
    renewed_events = [c for c in emit.call_args_list
                      if len(c.args) > 1 and c.args[1] == "approval.renewed"]
    assert renewed_events, "approval.renewed Event muss emittiert werden"


@pytest.mark.asyncio
async def test_clarification_approval_renews_too():
    approval = await _make_expired_approval(
        "clarification_question", {"agent_name": "Deployer", "question": "DNS?"},
    )
    await _run_expiry_check()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Approval, approval.id)
        assert fresh.status == "pending"
        assert fresh.payload.get("renewal_count") == 1


@pytest.mark.asyncio
async def test_other_approval_types_still_expire():
    """Nicht-renewable Typen (z.B. review_stuck) expiren wie bisher."""
    approval = await _make_expired_approval("review_stuck")
    emit, tg = await _run_expiry_check()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Approval, approval.id)
        assert fresh.status == "expired"
    tg.assert_not_awaited()


# ── F1 (review finding, 2026-07-16): expiry was the only approval-resolution
# pathway that never fired the generic vertical hook — an overlay vertical's
# custom approval type (e.g. catalog_publish) expiring silently would leave
# it unaware. _check_expired_approvals now fires run_approval_resolved_hooks
# with resolution_status="expired" for anything outside
# _CORE_HANDLED_ACTION_TYPES, same as the operator-PATCH and Telegram
# quick-resolve pathways. ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_non_core_approval_fires_generic_hook():
    from app.verticals import hooks as vertical_hooks

    approval = await _make_expired_approval("catalog_publish")
    seen: list[tuple] = []

    async def hook(sess, appr, resolution_status):
        seen.append((appr.id, resolution_status))

    vertical_hooks.approval_resolved_hooks.append(hook)
    try:
        await _run_expiry_check()
    finally:
        vertical_hooks.approval_resolved_hooks.remove(hook)

    assert seen == [(approval.id, "expired")]


@pytest.mark.asyncio
async def test_expired_core_handled_approval_does_not_fire_generic_hook():
    """spawn_agent is in _CORE_HANDLED_ACTION_TYPES (has its own dedicated
    approve/reject business logic in resolve_approval) but is NOT in the
    watchdog's renewable_types, so it still lands in status=expired here —
    the generic hook must nonetheless skip it, same as the resolve/
    quick-resolve pathways skip x_post."""
    from app.verticals import hooks as vertical_hooks

    approval = await _make_expired_approval("spawn_agent")
    seen: list[tuple] = []

    async def hook(sess, appr, resolution_status):
        seen.append((appr.id, resolution_status))

    vertical_hooks.approval_resolved_hooks.append(hook)
    try:
        await _run_expiry_check()
    finally:
        vertical_hooks.approval_resolved_hooks.remove(hook)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Approval, approval.id)
        assert fresh.status == "expired"
    assert seen == []
