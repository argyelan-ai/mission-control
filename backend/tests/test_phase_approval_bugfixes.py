"""Regression tests for 2 bugs discovered on 2026-04-22:

Bug 1: create_phase_approval_task created duplicates
  — Push path (agent_scoped) had idempotency, but the watchdog sweep and the
    function itself did not. Two parallel calls created two phase approvals
    that Boss both had to work through.

Bug 2: handle_phase_approval_decision ALWAYS set the parent to review
  — On trust-by-default boards (mc-dev: require_review_before_done=false)
    review sits idle because no reviewer comes along. Parent hangs forever.
    Fix: parent stays in_progress, orchestrator closes it via hard gate.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_board_lead_parent(require_review: bool = False):
    """Board + board lead + root task in status in_progress."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    parent_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id, name="PhaseTest", slug=f"pt-{uuid.uuid4().hex[:6]}",
            require_review_before_done=require_review,
        ))
        s.add(Agent(
            id=lead_id, name="Boss", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent",
            status="in_progress",
            assigned_agent_id=lead_id, owner_agent_id=lead_id,
        ))
        await s.commit()

    return {"board_id": board_id, "lead_id": lead_id, "parent_id": parent_id}


# ────────────────────────────────────────────────────────────────────
# Bug 1: Idempotency
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_phase_approval_task_is_idempotent():
    """Second call for the same parent returns the existing one, creates no new one."""
    from app.services.task_lifecycle import create_phase_approval_task
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        parent = await s.get(Task, data["parent_id"])

        # First call — creates one
        a1 = await create_phase_approval_task(s, parent, lead)
        assert a1 is not None
        assert a1.delegation_type == "phase_approval"

        # Second call — MUST return the same one, no duplicate
        a2 = await create_phase_approval_task(s, parent, lead)
        assert a2 is not None
        assert a2.id == a1.id, "Idempotenz verletzt — zweiter Approval-Task erstellt"

        # Exactly 1 phase approval task in DB
        approvals = (await s.exec(
            select(Task).where(
                Task.parent_task_id == parent.id,
                Task.delegation_type == "phase_approval",
            )
        )).all()
        assert len(approvals) == 1


# ────────────────────────────────────────────────────────────────────
# Bug 2: Trust-by-default board
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_approved_trust_by_default_keeps_parent_in_progress():
    """On a trust-by-default board (require_review_before_done=false), the
    parent stays in_progress after phase_approved — not review.
    """
    from app.services.task_lifecycle import (
        create_phase_approval_task, handle_phase_approval_decision,
    )
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        parent = await s.get(Task, data["parent_id"])
        approval = await create_phase_approval_task(s, parent, lead)

        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
            result = await handle_phase_approval_decision(
                s, approval, lead, "phase_approved", "looks good",
            )

        assert result["decision"] == "approved"
        assert result["parent_promoted"] is False, \
            "Parent darf auf Trust-by-Default NICHT auf review promoted werden"

        await s.refresh(parent)
        assert parent.status == "in_progress", \
            f"Parent muss in_progress bleiben, ist aber '{parent.status}'"


@pytest.mark.asyncio
async def test_phase_approved_with_required_review_promotes_to_review():
    """On boards with require_review_before_done=true: old path — parent goes to review."""
    from app.services.task_lifecycle import (
        create_phase_approval_task, handle_phase_approval_decision,
    )
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent(require_review=True)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        parent = await s.get(Task, data["parent_id"])
        approval = await create_phase_approval_task(s, parent, lead)

        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
            result = await handle_phase_approval_decision(
                s, approval, lead, "phase_approved", "review me",
            )

        assert result["parent_promoted"] is True
        await s.refresh(parent)
        assert parent.status == "review"


# ────────────────────────────────────────────────────────────────────
# Bug 3: Orchestrator nudge after phase_approved (discovered 2026-04-22)
#
# Follow-up bug to the Bug 2 fix: parent stays in_progress, but the
# orchestrator misses it (approval task is done, nothing more shows up
# from its view). Fix: active re-dispatch nudge + watchdog safety net
# ────────────────────────────────────────────────────────────────────




















@pytest.mark.asyncio
async def test_send_orchestrator_close_nudge_host_runtime_posts_system_comment():
    """Host-runtime Boss (no gateway_agent_id, agent_runtime='host'): the nudge
    goes NOT via rpc.chat_send but as a TaskComment(comment_type='system') on
    the parent. It gets delivered to Boss's Claude session via
    /agent/me/poll → poll.sh → tmux paste-buffer.
    """
    from app.services.task_lifecycle import send_orchestrator_close_nudge, ORCH_CLOSE_REMINDER_MARKER
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

        # Phase 29 / gateway sunset: no more rpc module in task_lifecycle —
        # the function exclusively posts a TaskComment + poll path.
        sent = await send_orchestrator_close_nudge(
            s, parent, lead, reason="phase_approved",
        )

        assert sent is True, "Host-Runtime-Pfad muss einen Nudge zustellen"

        # System comment created on parent with marker + hard-gate sequence
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == parent.id)
        )).all()
        assert len(comments) == 1
        c = comments[0]
        assert c.comment_type == "system"
        assert c.author_type == "system"
        assert c.author_agent_id is None  # no echo loop in _is_deliverable_for
        assert ORCH_CLOSE_REMINDER_MARKER in c.content
        assert "mc telegram" in c.content
        assert "mc done" in c.content
        assert str(parent.id) in c.content


@pytest.mark.asyncio
async def test_send_orchestrator_close_nudge_host_runtime_idempotent_within_window():
    """Second call within 10 min does not post a second system comment
    (prevents spam in Boss's tmux session on watchdog re-fires)."""
    from app.services.task_lifecycle import send_orchestrator_close_nudge
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

        sent1 = await send_orchestrator_close_nudge(s, parent, lead, reason="phase_approved")
        sent2 = await send_orchestrator_close_nudge(s, parent, lead, reason="stuck_safety_net")

        assert sent1 is True
        assert sent2 is False, "Zweiter Aufruf im Dedup-Window darf NICHT erneut posten"

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == parent.id)
        )).all()
        assert len(comments) == 1, f"Erwartet 1 Comment, gefunden {len(comments)}"


@pytest.mark.asyncio
async def test_send_orchestrator_close_nudge_host_runtime_discord_skips_telegram_hint():
    """Host runtime + report_back_channel='discord': the reminder still comes,
    but WITHOUT the `mc telegram` hint (hard gate only applies to telegram-routed)."""
    from app.services.task_lifecycle import send_orchestrator_close_nudge
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        parent.report_back_channel = "discord"
        s.add(parent)
        await s.commit()

        sent = await send_orchestrator_close_nudge(s, parent, lead, reason="phase_approved")

        assert sent is True
        c = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == parent.id)
        )).first()
        assert c is not None
        assert "mc done" in c.content
        assert "mc telegram" not in c.content, \
            "Discord-routed Tasks duerfen den telegram-Hinweis nicht enthalten"










@pytest.mark.asyncio
async def test_check_phase_completions_skips_recreation_when_done_approval_exists():
    """Bug 4 (live test 2026-04-22): if a DONE phase_approval already exists
    and the child set is unchanged since approval, _check_phase_completions must
    NOT create a new approval task. stuck_orchestrator_close handles it.
    """
    from datetime import timedelta
    from app.services.watchdog.core import WatchdogService
    from app.models.agent import Agent
    from app.models.task import Task
    from app.utils import utcnow, strip_tz
    from unittest.mock import MagicMock

    data = await _setup_board_lead_parent(require_review=False)

    # Realistic stuck scenario: subtask was modified BEFORE approval → phase
    # is stably approved, no new phase work afterward.
    sub_done_at = strip_tz(utcnow() - timedelta(hours=1))
    approval_done_at = strip_tz(utcnow() - timedelta(minutes=50))  # AFTER subtask

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            board_id=data["board_id"], title="Subtask 1", status="done",
            parent_task_id=data["parent_id"], assigned_agent_id=data["lead_id"],
            updated_at=sub_done_at,
        ))
        s.add(Task(
            board_id=data["board_id"], title="Old Approval (done)", status="done",
            parent_task_id=data["parent_id"], delegation_type="phase_approval",
            assigned_agent_id=data["lead_id"],
            completed_at=approval_done_at, updated_at=approval_done_at,
        ))
        await s.commit()

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True

    with patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.watchdog.task_monitor.get_redis", new_callable=AsyncMock) as mock_get_redis, \
         patch("app.services.auto_memory.record_phase_completion", new_callable=MagicMock), \
         patch("app.services.watchdog.core._create_background_task"):
        mock_get_redis.return_value = mock_redis

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            monitor = WatchdogService()
            await monitor._check_phase_completions(s)

    # Verify: EXACTLY 1 phase approval (the old done one) — no new one
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Task)
            .where(Task.parent_task_id == data["parent_id"])
            .where(Task.delegation_type == "phase_approval")
        )).all()
        assert len(approvals) == 1, \
            f"Erwarte 1 Approval (alter done), bekam {len(approvals)}: {[(a.id, a.status) for a in approvals]}"
        assert approvals[0].status == "done"

    # Dedup key was set so the next watchdog tick doesn't retry
    mock_redis.set.assert_called()


@pytest.mark.asyncio
async def test_check_phase_completions_creates_new_approval_after_rewrite():
    """Bug 5 (rewrite edge case, identified 2026-04-22):
    Boss does a phase_rewrite_request → Approval1=done, subtasks go back to inbox →
    re-worked → done again. The current child set is NEW since Approval1 (subtask
    updated_at > approval.completed_at). _check_phase_completions must not
    incorrectly skip — a new approval must be created (via create_phase_approval_task
    push path OR watchdog run).
    """
    from datetime import timedelta
    from unittest.mock import MagicMock
    from app.services.watchdog.core import WatchdogService
    from app.models.task import Task
    from app.utils import utcnow, strip_tz

    data = await _setup_board_lead_parent(require_review=False)

    # Approval1 was done BEFORE current subtask work (rewrite scenario)
    approval_done_at = strip_tz(utcnow() - timedelta(hours=2))
    # Subtasks were updated after the rewrite → newer than approval
    subtask_recent = strip_tz(utcnow() - timedelta(minutes=5))

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            board_id=data["board_id"], title="Subtask 1 (re-worked)", status="done",
            parent_task_id=data["parent_id"], assigned_agent_id=data["lead_id"],
            updated_at=subtask_recent,
        ))
        s.add(Task(
            board_id=data["board_id"], title="Approval1 (done via rewrite)", status="done",
            parent_task_id=data["parent_id"], delegation_type="phase_approval",
            assigned_agent_id=data["lead_id"],
            completed_at=approval_done_at, updated_at=approval_done_at,
        ))
        await s.commit()

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True

    with patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.watchdog.task_monitor.get_redis", new_callable=AsyncMock) as mock_get_redis, \
         patch("app.services.auto_memory.record_phase_completion", new_callable=MagicMock), \
         patch("app.services.watchdog.core._create_background_task"):
        mock_get_redis.return_value = mock_redis

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            monitor = WatchdogService()
            await monitor._check_phase_completions(s)

    # Verify: NEW approval was created (2 total now — old done + new inbox)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Task)
            .where(Task.parent_task_id == data["parent_id"])
            .where(Task.delegation_type == "phase_approval")
            .order_by(Task.created_at)
        )).all()
        assert len(approvals) == 2, \
            f"Erwarte 2 Approvals (alter done + neuer inbox), bekam {len(approvals)}"
        assert approvals[0].status == "done"
        assert approvals[1].status == "inbox", \
            f"Neuer Approval sollte in inbox sein, ist {approvals[1].status}"


@pytest.mark.asyncio
async def test_orchestrator_close_nudge_escalates_to_mark_after_threshold():
    """Escalation test: after N unsuccessful close nudges, the operator gets
    notified via the reports bot.
    """
    from app.services.task_lifecycle import (
        send_orchestrator_close_nudge,
        ORCH_CLOSE_ESCALATION_THRESHOLD,
    )
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

    # Mock redis: counter increments, get returns None (not escalated), set OK.
    # We use a real fakeredis-mock via dict semantics.
    redis_state = {}

    async def _fake_get(key):
        return redis_state.get(key)

    async def _fake_set(key, value, ex=None):
        redis_state[key] = value
        return True

    async def _fake_incr(key):
        current = int(redis_state.get(key, 0))
        redis_state[key] = str(current + 1)
        return current + 1

    async def _fake_expire(key, ttl):
        return True

    mock_redis = AsyncMock()
    mock_redis.get.side_effect = _fake_get
    mock_redis.set.side_effect = _fake_set
    mock_redis.incr.side_effect = _fake_incr
    mock_redis.expire.side_effect = _fake_expire

    mock_reports = AsyncMock()
    mock_reports.configured = True
    mock_reports.send.return_value = {"ok": True, "result": {"message_id": 1}}

    async def _mock_get_redis():
        return mock_redis

    with patch("app.services.task_lifecycle.get_redis", side_effect=_mock_get_redis), \
         patch("app.redis_client.get_redis", side_effect=_mock_get_redis), \
         patch("app.services.task_lifecycle.telegram_reports", mock_reports):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            # Threshold-1 nudges: no escalation yet
            parent = await s.get(Task, data["parent_id"])
            lead = await s.get(Agent, data["lead_id"])

            for i in range(ORCH_CLOSE_ESCALATION_THRESHOLD - 1):
                # Bypass the per-call dedup window by explicitly resetting the comment lookup —
                # here we simply hand in a very small dedup_window and a new reason;
                # alternative: delete comments between posts so idempotency doesn't kick in
                from app.models.task import TaskComment
                (await s.exec(
                    select(TaskComment).where(TaskComment.task_id == parent.id)
                )).all()
                # Clear reminders: set all existing system comments to very old (before dedup)
                all_c = (await s.exec(select(TaskComment).where(TaskComment.task_id == parent.id))).all()
                for c in all_c:
                    from datetime import timedelta
                    from app.utils import utcnow, strip_tz
                    c.created_at = strip_tz(utcnow() - timedelta(hours=1))
                    s.add(c)
                await s.commit()

                await send_orchestrator_close_nudge(s, parent, lead, reason="phase_approved")

            # So far: reports bot NOT called
            assert mock_reports.send.await_count == 0, \
                f"Unter Threshold, kein Operator-Ping erwartet; got {mock_reports.send.await_count}"

            # Last nudge -> threshold reached -> operator gets notified
            all_c = (await s.exec(select(TaskComment).where(TaskComment.task_id == parent.id))).all()
            for c in all_c:
                from datetime import timedelta
                from app.utils import utcnow, strip_tz
                c.created_at = strip_tz(utcnow() - timedelta(hours=1))
                s.add(c)
            await s.commit()

            await send_orchestrator_close_nudge(s, parent, lead, reason="stuck_safety_net")

            assert mock_reports.send.await_count == 1, \
                f"Erwartet genau 1 Eskalation-Send, got {mock_reports.send.await_count}"
            sent_text = mock_reports.send.await_args.args[0]
            assert "Eskalation" in sent_text or "eskalation" in sent_text.lower()
            assert str(parent.id) in sent_text


@pytest.mark.asyncio
async def test_orchestrator_close_escalation_is_idempotent():
    """A second wave of nudges after escalation must not spam the operator again
    (until the Redis TTL expires)."""
    from app.services.task_lifecycle import (
        send_orchestrator_close_nudge,
        ORCH_CLOSE_ESCALATION_THRESHOLD,
    )
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from datetime import timedelta
    from app.utils import utcnow, strip_tz

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

    redis_state = {}

    async def _fake_get(key):
        return redis_state.get(key)

    async def _fake_set(key, value, ex=None):
        redis_state[key] = value
        return True

    async def _fake_incr(key):
        current = int(redis_state.get(key, 0))
        redis_state[key] = str(current + 1)
        return current + 1

    async def _fake_expire(key, ttl):
        return True

    mock_redis = AsyncMock()
    mock_redis.get.side_effect = _fake_get
    mock_redis.set.side_effect = _fake_set
    mock_redis.incr.side_effect = _fake_incr
    mock_redis.expire.side_effect = _fake_expire

    mock_reports = AsyncMock()
    mock_reports.configured = True
    mock_reports.send.return_value = {"ok": True, "result": {"message_id": 1}}

    async def _mock_get_redis():
        return mock_redis

    with patch("app.services.task_lifecycle.get_redis", side_effect=_mock_get_redis), \
         patch("app.redis_client.get_redis", side_effect=_mock_get_redis), \
         patch("app.services.task_lifecycle.telegram_reports", mock_reports):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            parent = await s.get(Task, data["parent_id"])
            lead = await s.get(Agent, data["lead_id"])

            # 5 nudges — well over the threshold
            for i in range(5):
                all_c = (await s.exec(select(TaskComment).where(TaskComment.task_id == parent.id))).all()
                for c in all_c:
                    c.created_at = strip_tz(utcnow() - timedelta(hours=1))
                    s.add(c)
                await s.commit()
                await send_orchestrator_close_nudge(s, parent, lead, reason="stuck_safety_net")

            # Only ONE escalation, no matter how often nudged
            assert mock_reports.send.await_count == 1, \
                f"Erwartet genau 1 Eskalation (idempotent), got {mock_reports.send.await_count}"


# Import at the end so the select import is available for the first test
from sqlmodel import select  # noqa: E402
