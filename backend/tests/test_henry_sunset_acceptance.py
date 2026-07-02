"""Phase 28 Plan 28-03 - the operator's hard acceptance smoketests.

D-14: Telegram-Approval-Flow works post-Henry-removal - FULL CHAIN.
      Mirrors test_d2_dispatch_escalation_telegram.py:22-58: seed stale
      inbox task -> invoke task_runner._check_dispatch_ack() (the entry
      that production uses every 60s) -> assert Approval row created
      AND telegram_bot.send_approval_telegram was called with the right
      approval_id + agent_name.
D-15: Discord-OPS-Webhook emit_event works post-Henry-removal.
D-16: per-Agent Discord channels survive (non-Henry agents retain
      discord_channel_id).

These tests pass against a seeded post-migration state - they do NOT
run the migration. The full E2E (migration + these smoketests) is
the operator's manual step before declaring Phase 28 done.

Schema notes:
- Agent model uses `name` (no slug column).
- Approval model uses `agent_id` to reference the escalation target
  (the worker who failed to ACK). The original plan text said
  `target_agent_id` — that field does not exist. _create_dispatch_approval
  sets Approval.agent_id=agent.id (task_runner.py:594).
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sa_text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import telegram_bot  # noqa: F401 — import for symbol presence grep
from tests.conftest import test_engine


# ===== D-14 FULL CHAIN: stale dispatch -> escalation -> telegram push ======

@pytest.mark.asyncio
async def test_dispatch_escalation_calls_telegram_post_henry_removal(
    make_board, make_agent, make_task, fake_redis
):
    """D-14 full chain (mirrors test_d2_dispatch_escalation_telegram.py:22-58):

    1. Seed a Board with Boss (Board Lead) and Sparky (cli-bridge worker)
       - NO Henry. This is the post-Phase-28 DB state.
    2. Seed an inbox task assigned to Sparky with dispatched_at 20min ago
       (past the 15min default ack_timeout for openclaw/cli-bridge).
    3. Invoke `task_runner._check_dispatch_ack(session)` - the production
       entry point that runs every 60s in the watchdog loop.
    4. Assert:
       - exactly ONE dispatch_escalation Approval row exists for the task,
         with agent_id pointing at Sparky.
       - telegram_bot.send_approval_telegram was called exactly ONCE with
         approval_id matching the Approval.id and agent_name='Sparky'.

    This proves the FULL chain (stale task -> escalation logic -> Approval
    creation -> telegram push) still works after Henry-removal. Mock the
    OUTBOUND telegram HTTP, not the chain.
    """
    from app.models.approval import Approval
    from app.services.task_runner import task_runner
    from app.utils import utcnow

    # Seed post-Henry board with Boss (board lead) + Sparky (worker).
    board = await make_board(auto_dispatch_enabled=True)
    await make_agent(
        name="Boss", board_id=board.id,
        role="orchestrator",
        is_board_lead=True, provision_status="provisioned",
        agent_runtime="host",         scopes=[],
    )
    sparky = await make_agent(
        name="Sparky", board_id=board.id,
        agent_runtime="cli-bridge",         provision_status="provisioned",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    # Stale: dispatched 20min ago, no ACK - past the 15min default ack_timeout
    # for cli-bridge.
    task = await make_task(
        board_id=board.id, status="inbox",
        title="Post-Henry escalation full-chain test",
        assigned_agent_id=sparky.id,
        dispatched_at=utcnow() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    captured_telegram_calls: list[dict] = []

    async def _fake_send_approval(approval_id, agent_name, task_title,
                                   blocker_comment=None):
        captured_telegram_calls.append({
            "approval_id": approval_id,
            "agent_name": agent_name,
            "task_title": task_title,
            "blocker_comment": blocker_comment,
        })

    # Patch:
    #  - telegram_bot.send_approval_telegram (outbound HTTP — DON'T hit real API)
    #  - get_redis -> fake_redis (escalation uses Redis 24h cooldown key)
    #  - activity.broadcast -> AsyncMock (fakeredis pub/sub stub)
    with patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
               new=AsyncMock(side_effect=_fake_send_approval)):
        with patch("app.services.task_runner.get_redis",
                   return_value=fake_redis):
            with patch("app.services.activity.broadcast",
                       new_callable=AsyncMock):
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    # Invoke the FULL escalation chain entry point.
                    # skip_pending=True matches subagent-dispatch production
                    # mode; only the ACK-timeout path runs.
                    await task_runner._check_dispatch_ack(s, skip_pending=True)

    # === Assertion 1: Approval row created ===========================
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 1, (
        f"Expected exactly 1 dispatch_escalation Approval; got {len(approvals)}. "
        f"The chain task_runner._check_dispatch_ack -> _handle_ack_timeout -> "
        f"_create_dispatch_approval must produce an Approval row."
    )
    approval = approvals[0]
    assert approval.action_type == "dispatch_escalation"
    # Approval.agent_id references the escalation target (the agent who
    # failed to ACK). NOTE: plan text said `target_agent_id` but the actual
    # model field is `agent_id` (see task_runner.py:594 +
    # backend/app/models/approval.py:17).
    assert approval.agent_id == sparky.id, (
        f"Approval.agent_id must reference Sparky as the escalation target "
        f"(the agent who failed to ACK); got {approval.agent_id}"
    )

    # === Assertion 2: Telegram push was called (full-chain proof) ====
    assert len(captured_telegram_calls) == 1, (
        f"Expected exactly 1 telegram_bot.send_approval_telegram call; got "
        f"{len(captured_telegram_calls)}. The full chain MUST reach telegram."
    )
    call = captured_telegram_calls[0]
    assert call["approval_id"] == approval.id, (
        f"Telegram call's approval_id ({call['approval_id']}) must match "
        f"the created Approval.id ({approval.id})"
    )
    assert call["agent_name"] == "Sparky"
    assert call["task_title"] == task.title
    assert "ACK" in (call["blocker_comment"] or "")


@pytest.mark.asyncio
async def test_telegram_failure_does_not_block_approval_creation_post_henry(
    make_board, make_agent, make_task, fake_redis
):
    """D-14 resilience: if telegram is down post-Henry, Approval row
    still gets created. Mirrors test_d2's resilience test (line 80+) and
    proves the chain doesn't regress to gateway-coupling."""
    from app.models.approval import Approval
    from app.services.task_runner import task_runner
    from app.utils import utcnow

    board = await make_board(auto_dispatch_enabled=True)
    sparky = await make_agent(
        name="Sparky", board_id=board.id,
        agent_runtime="cli-bridge",         provision_status="provisioned",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=sparky.id,
        dispatched_at=utcnow() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
               new=AsyncMock(side_effect=RuntimeError("Telegram API down"))):
        with patch("app.services.task_runner.get_redis",
                   return_value=fake_redis):
            with patch("app.services.activity.broadcast",
                       new_callable=AsyncMock):
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    # MUST NOT raise even when telegram fails.
                    await task_runner._check_dispatch_ack(s, skip_pending=True)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 1, (
        "Approval row must be created even if telegram push fails"
    )


# ===== D-15: Discord OPS webhook reachable post-Henry =====================

@pytest.mark.asyncio
async def test_discord_ops_webhook_emit_works_post_henry_removal():
    """emit_event(severity='warning') reaches Discord OPS after Henry is gone.
    No agent context required."""
    from app.services.activity import emit_event

    sent: list[dict] = []

    async def _capture(title, description, severity="warning", fields=None):
        sent.append({
            "title": title, "description": description,
            "severity": severity, "fields": fields,
        })

    # Patch at activity.py's import site - that's where emit_event resolves
    # the symbol (per PATTERNS.md gotcha #3).
    with patch("app.services.activity.send_discord_notification",
               new=AsyncMock(side_effect=_capture)):
        with patch("app.services.activity.broadcast",
                   new_callable=AsyncMock):
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                await emit_event(
                    s,
                    event_type="phase28.acceptance.test_signal",
                    title="Henry-Sunset acceptance smoke",
                    severity="warning",
                )

    assert len(sent) == 1, (
        "emit_event(severity='warning') must post to Discord OPS"
    )
    assert "Henry-Sunset" in sent[0]["title"]
    assert sent[0]["severity"] == "warning"


@pytest.mark.asyncio
async def test_emit_event_info_severity_does_not_post():
    """Sanity: severity='info' does NOT trigger Discord OPS
    (activity.py:73 gate)."""
    from app.services.activity import emit_event

    sent: list[dict] = []

    async def _capture(title, description, severity="warning", fields=None):
        sent.append({"title": title})

    with patch("app.services.activity.send_discord_notification",
               new=AsyncMock(side_effect=_capture)):
        with patch("app.services.activity.broadcast",
                   new_callable=AsyncMock):
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                await emit_event(
                    s, event_type="info.heartbeat",
                    title="just a heartbeat", severity="info",
                )

    assert len(sent) == 0, (
        "severity='info' must NOT spam OPS channel"
    )


# ===== D-16: Per-Agent Discord channels survive ===========================

@pytest.mark.asyncio
async def test_non_henry_discord_channels_intact_post_henry(
    make_board, make_agent
):
    """After Henry is removed, other agents retain their discord_channel_id
    (no orphaning, no accidental cascade)."""
    board = await make_board()

    # Boss + Rex + Cody each have a discord_channel_id.
    await make_agent(
        name="Boss", board_id=board.id,
        discord_channel_id="111111111111111111",
        agent_runtime="host",
    )
    await make_agent(
        name="Rex", board_id=board.id,
        discord_channel_id="222222222222222222",
        agent_runtime="cli-bridge",     )
    await make_agent(
        name="Cody", board_id=board.id,
        discord_channel_id="333333333333333333",
        agent_runtime="cli-bridge",     )

    # Inventory: 3 agents have a discord_channel_id (no Henry yet).
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = (await s.exec(sa_text(
            "SELECT count(*) FROM agents "
            "WHERE discord_channel_id IS NOT NULL"
        ))).scalar_one()
    assert count == 3, (
        "Test setup: 3 agents with discord_channel_id (no Henry "
        "present in this board)"
    )

    # Now seed a Henry with a discord_channel_id and "delete" it via raw
    # SQL to mimic what migration 0122 does.
    await make_agent(
        name="Henry", board_id=board.id,
        discord_channel_id="999999999999999999",
        agent_runtime="openclaw",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await s.exec(sa_text(
            "DELETE FROM agents WHERE name = 'Henry'"
        ))
        await s.commit()

    # The other 3 channels must still be present.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count_after = (await s.exec(sa_text(
            "SELECT count(*) FROM agents "
            "WHERE discord_channel_id IS NOT NULL"
        ))).scalar_one()
        non_henry_intact = (await s.exec(sa_text(
            "SELECT count(*) FROM agents "
            "WHERE discord_channel_id IS NOT NULL AND name != 'Henry'"
        ))).scalar_one()

    assert count_after == 3, (
        f"3 non-Henry channels must survive; got {count_after}"
    )
    assert non_henry_intact == 3
    # Henry-row gone implies the discord_channel_id is gone with it.
    # (Phase 28 does NOT actively unbind the channel via Discord API
    # - Phase 31 handles channel-cleanup if needed.)
