"""Tests for Task 9 — Waiting-Timeout Parken + Resume via Dispatch-Pfad.

Brief scenarios:
  (b) waiting past timeout + another task queued for the agent → parked
      (system line on the thread, agent released, task stays waiting).
  (c) waiting past timeout, NO other task queued → NOT parked (§4.2).
  (d) waiting over Nachtruhe → the paused timer keeps active time below the
      timeout → NOT parked.
Plus: the Nachtruhe active-time math is a pure, directly-tested function, and
the parked-resume path re-delivers via auto_dispatch_task with a bounded recap.
"""
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment, TaskEvent
from app.models.thread import Message
from app.models.user import User
from app.services.messaging import ensure_task_thread, post_message
from app.services.task_runner import TaskRunnerService, active_waiting_seconds

from .conftest import test_engine


# ── Pure Nachtruhe math (no DB, no freezegun) ────────────────────────────

class TestActiveWaitingSeconds:
    def test_daytime_full_count(self):
        """A pure daytime span counts every second."""
        since = dt.datetime(2026, 7, 1, 9, 0)
        now = dt.datetime(2026, 7, 1, 11, 0)
        assert active_waiting_seconds(since, now) == 7200

    def test_quiet_window_reaches_timeout_at_0800(self):
        """22:00→08:00: 22–23 (1h) + 07–08 (1h) count, 23–07 pauses → 7200s."""
        since = dt.datetime(2026, 7, 1, 22, 0)
        now = dt.datetime(2026, 7, 2, 8, 0)
        assert active_waiting_seconds(since, now) == 7200

    def test_quiet_window_partial(self):
        """22:00→07:30: 22–23 (1h) + 07:00–07:30 (0.5h) = 5400s active."""
        since = dt.datetime(2026, 7, 1, 22, 0)
        now = dt.datetime(2026, 7, 2, 7, 30)
        assert active_waiting_seconds(since, now) == 5400

    def test_fully_overnight_barely_counts(self):
        """(d) 22:30→06:00 next day: only 22:30–23:00 (0.5h) is active."""
        since = dt.datetime(2026, 7, 1, 22, 30)
        now = dt.datetime(2026, 7, 2, 6, 0)
        assert active_waiting_seconds(since, now) == 1800

    def test_now_before_since_is_zero(self):
        since = dt.datetime(2026, 7, 1, 12, 0)
        now = dt.datetime(2026, 7, 1, 11, 0)
        assert active_waiting_seconds(since, now) == 0


# ── DB helpers ───────────────────────────────────────────────────────────

async def _board(session: AsyncSession) -> Board:
    board = Board(id=uuid.uuid4(), name="WT Board", slug=f"wt-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def _agent(board_id: uuid.UUID, current_task_id: uuid.UUID | None = None):
    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Cody-{uuid.uuid4().hex[:4]}",
            role="developer",
            board_id=board_id,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
            agent_token_hash=token_hash,
            current_task_id=current_task_id,
            run_state="running",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, raw_token


async def _waiting_task(board_id: uuid.UUID, agent_id: uuid.UUID, *, waited_days: int = 5) -> Task:
    """A task parked `waiting`, with a waiting-transition event `waited_days` ago."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=uuid.uuid4(), board_id=board_id, title="WT waiting task",
            status="waiting", assigned_agent_id=agent_id,
            dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=dt.datetime.now(tz=dt.timezone.utc),
        )
        s.add(task)
        s.add(TaskEvent(
            task_id=task.id, from_status="in_progress", to_status="waiting",
            changed_by="agent",
            created_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=waited_days),
        ))
        await s.commit()
        await s.refresh(task)
    return task


async def _inbox_task(board_id: uuid.UUID, agent_id: uuid.UUID) -> Task:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=uuid.uuid4(), board_id=board_id, title="Queued follow-up",
            status="inbox", assigned_agent_id=agent_id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
    return task


async def _run_waiting_check(runner: TaskRunnerService, fake_redis):
    from app import redis_client
    from app.services import task_runner as _tr
    from app.services import sse as _sse

    async def _fake_get_redis():
        return fake_redis

    with patch.object(redis_client, "get_redis", _fake_get_redis), \
         patch.object(_tr, "get_redis", _fake_get_redis), \
         patch.object(_sse, "get_redis", _fake_get_redis):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await runner._check_waiting_timeouts(s)


# ── Park / no-park scenarios ─────────────────────────────────────────────

@pytest.mark.asyncio
class TestWaitingTimeoutPark:
    async def test_parks_when_other_task_queued(self, fake_redis):
        """(b) waiting past timeout + queued task → parked."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        agent, _ = await _agent(board.id)
        task = await _waiting_task(board.id, agent.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            a = await s.get(Agent, agent.id)
            a.current_task_id = task.id
            s.add(a)
            await s.commit()
        await _inbox_task(board.id, agent.id)

        runner = TaskRunnerService()
        await _run_waiting_check(runner, fake_redis)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            parked_task = await s.get(Task, task.id)
            assert parked_task.status == "waiting"  # stays waiting
            assert parked_task.assigned_agent_id == agent.id  # assignment intact

            a = await s.get(Agent, agent.id)
            assert a.current_task_id is None  # agent released
            assert a.run_state == "idle"

            thread = await ensure_task_thread(s, parked_task)
            systems = (await s.exec(
                select(Message).where(
                    Message.thread_id == thread.id,
                    Message.message_type == "system",
                )
            )).all()
            assert any("Geparkt" in m.body for m in systems)

        assert await fake_redis.get(f"mc:task:{task.id}:waiting_parked")

    async def test_no_park_without_other_task(self, fake_redis):
        """(c) waiting past timeout but NO other task queued → not parked."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        agent, _ = await _agent(board.id)
        task = await _waiting_task(board.id, agent.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            a = await s.get(Agent, agent.id)
            a.current_task_id = task.id
            s.add(a)
            await s.commit()
        # No inbox task for this agent.

        runner = TaskRunnerService()
        await _run_waiting_check(runner, fake_redis)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            a = await s.get(Agent, agent.id)
            assert a.current_task_id == task.id  # NOT released — session stays put

        assert await fake_redis.get(f"mc:task:{task.id}:waiting_parked") is None

    async def test_no_park_when_overnight_keeps_active_below_timeout(self, fake_redis):
        """(d) waiting only over Nachtruhe → active time < timeout → not parked."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        agent, _ = await _agent(board.id)
        # Waiting transition 30 min before quiet start, "now" is inside the
        # night → active time is ~0.5h, far below the 2h timeout. Simulate by
        # placing the waiting event at a point where active_waiting_seconds
        # stays small: 20 minutes ago, all counting (still < 2h).
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = Task(
                id=uuid.uuid4(), board_id=board.id, title="Overnight waiter",
                status="waiting", assigned_agent_id=agent.id,
                dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            )
            s.add(task)
            s.add(TaskEvent(
                task_id=task.id, from_status="in_progress", to_status="waiting",
                changed_by="agent",
                created_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=20),
            ))
            a = await s.get(Agent, agent.id)
            a.current_task_id = task.id
            s.add(a)
            await s.commit()
        await _inbox_task(board.id, agent.id)  # queued work exists, but timer not up

        runner = TaskRunnerService()
        await _run_waiting_check(runner, fake_redis)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            a = await s.get(Agent, agent.id)
            assert a.current_task_id == task.id  # not parked — timer not elapsed
        assert await fake_redis.get(f"mc:task:{task.id}:waiting_parked") is None


# ── Bounded resume recap + parked-resume routing ─────────────────────────

@pytest.mark.asyncio
class TestWaitingResumeRecap:
    async def test_recap_is_bounded_and_structured(self):
        from app.services.task_context_builder import (
            WAITING_RESUME_RECAP_MAX_CHARS,
            build_waiting_resume_recap,
        )

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        agent, _ = await _agent(board.id)
        task = await _waiting_task(board.id, agent.id)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.workspace_path = "/ws/x"
            s.add(db_task)
            await s.commit()
            thread = await ensure_task_thread(s, db_task)
            await post_message(
                s, thread_id=thread.id, sender_type="user",
                message_type="decision", body="Wir nehmen Postgres." + ("x" * 4000),
            )
            q = await post_message(
                s, thread_id=thread.id, sender_type="agent", sender_id=agent.id,
                message_type="question", body="Redis oder Postgres?",
                question_meta={"awaiting": False, "blocking": True, "to": "boss"},
            )
            await post_message(
                s, thread_id=thread.id, sender_type="user",
                message_type="message", body="Postgres, sagte ich.", reply_to=q.id,
            )
            recap = await build_waiting_resume_recap(s, db_task)

        assert len(recap) <= WAITING_RESUME_RECAP_MAX_CHARS
        assert "/ws/x" in recap
        assert "Redis oder Postgres?" in recap
        assert "Postgres, sagte ich." in recap

    async def test_parked_answer_redispatches_with_recap(self, client):
        """A blocking answer to a task whose agent moved on re-delivers via
        auto_dispatch_task + writes the bounded recap, rather than assuming a
        live poll session."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        # Agent is NOT on this task (parked/absent): current_task_id=None.
        agent, _ = await _agent(board.id, current_task_id=None)
        task = await _waiting_task(board.id, agent.id)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            thread = await ensure_task_thread(s, db_task)
            question = await post_message(
                s, thread_id=thread.id, sender_type="agent", sender_id=agent.id,
                message_type="question", body="Deploy jetzt?",
                question_meta={"awaiting": True, "blocking": True, "to": "boss", "priority": "high"},
            )

        user_id = uuid.uuid4()
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(User(id=user_id, email=f"u-{user_id.hex[:6]}@mc.local",
                       name="Op", role="admin", is_active=True))
            await s.commit()
        user_token = create_access_token(str(user_id), "admin")

        mock_dispatch = AsyncMock()
        with patch("app.routers.tasks.auto_dispatch_task", mock_dispatch):
            client.headers["Authorization"] = f"Bearer {user_token}"
            resp = await client.post(
                f"/api/v1/tasks/{task.id}/thread/messages",
                json={"body": "Ja, deploy.", "reply_to": str(question.id)},
            )
        assert resp.status_code == 201, resp.text

        # Re-delivery was routed through the dispatch path.
        assert mock_dispatch.called
        called_task_id = mock_dispatch.call_args.args[0]
        assert called_task_id == task.id
        # The recap carrying the open question AND the operator answer is passed
        # to auto_dispatch_task verbatim (extra_recovery_context) — it reaches
        # the prompt intact (proven by test_recap_reaches_built_prompt_verbatim).
        passed_recap = mock_dispatch.call_args.kwargs["extra_recovery_context"]
        assert "Deploy jetzt?" in passed_recap
        assert "Ja, deploy." in passed_recap

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            resumed = await s.get(Task, task.id)
            assert resumed.status == "in_progress"
            assert resumed.dispatched_at is None  # reset for the re-dispatch
            # Bounded recap persisted durably as a recovery_recap comment
            # (a type build_recovery_context does NOT truncate+surface).
            recaps = (await s.exec(
                select(TaskComment).where(
                    TaskComment.task_id == task.id,
                    TaskComment.comment_type == "recovery_recap",
                )
            )).all()
            assert any("Weiter geht" in c.content for c in recaps)

    async def test_recap_reaches_built_prompt_verbatim(self):
        """The REAL dispatch message build (no mock) injects the recovery_context
        recap verbatim — so the parked-resume answer reaches the agent's prompt.
        This is the delivery link the recovery-comment truncation would break."""
        from app.services.dispatch_message_builder import _build_dispatch_message
        from app.services.task_context_builder import build_waiting_resume_recap

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        agent, _ = await _agent(board.id)
        task = await _waiting_task(board.id, agent.id)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_agent = await s.get(Agent, agent.id)
            thread = await ensure_task_thread(s, db_task)
            q = await post_message(
                s, thread_id=thread.id, sender_type="agent", sender_id=agent.id,
                message_type="question", body="Deploy jetzt?",
                question_meta={"awaiting": False, "blocking": True, "to": "boss"},
            )
            await post_message(
                s, thread_id=thread.id, sender_type="user",
                message_type="message", body="Ja, deploy.", reply_to=q.id,
            )
            recap = await build_waiting_resume_recap(s, db_task)
            # No mocking of the message build — the real builder runs.
            message = await _build_dispatch_message(
                db_task, db_agent, s, recovery_context=recap,
            )

        assert "Deploy jetzt?" in message   # the open question
        assert "Ja, deploy." in message      # the operator's answer, verbatim
