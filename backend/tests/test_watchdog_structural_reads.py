"""Tests for Task 10 — Watchdog liest strukturell + Auto-Promote-Abbau (comm_v2-gated).

Covers the three brief scenarios, plus two review fixes:
  (a) `last_task_activity` takes the max of comment-time and message-time
      (dual-read, §8.1) — but ONLY when `comm_v2=True` is passed in. Message
      rows exist on threads for non-pilot tasks too (dispatch briefing,
      waiting-resume lines aren't comm_v2-gated), so folding them in
      unconditionally would silently shift stale-check timing fleet-wide.
  (b) A comm_v2 agent's resolution-signal does NOT auto-promote the task —
      instead a system Nudge message ("... bitte `mc finish` ausführen")
      lands on the task's thread, deduped PER FERTIG-SIGNAL EPISODE: other
      system messages (e.g. the waiting-resume "▶ Antwort erhalten" line) may
      interleave without resetting the dedupe; only a new agent Message
      starts a fresh episode and lets the nudge fire again. Covered at both
      call sites: the agent_comments.py POST endpoint (live resolution
      comment) and task_runner._check_stale_in_progress (the watchdog's own
      mirror check on a pre-existing resolution comment).
  (c) Regression: a non-pilot (no comm_v2) agent's resolution comment still
      auto-promotes exactly as today, at both call sites — and a non-pilot
      task's stale-check timing ignores Message rows entirely (briefing +
      resume system messages), byte-identical to pre-dual-read behavior.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.task import Task, TaskComment
from app.models.thread import Message
from app.services.messaging import FINISH_NUDGE_BODY, ensure_task_thread, last_task_activity, post_message
from app.utils import ensure_aware, utcnow

from tests.conftest import test_engine


@asynccontextmanager
async def _session():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _reload_task(task_id) -> Task:
    async with _session() as s:
        return await s.get(Task, task_id)


async def _thread_messages(thread_id) -> list[Message]:
    async with _session() as s:
        res = await s.exec(
            select(Message).where(Message.thread_id == thread_id).order_by(Message.seq)
        )
        return list(res.all())


# ── (a) last_task_activity: dual-read max(TaskComment, Message) ───────────


@pytest.mark.asyncio
class TestLastTaskActivity:
    async def test_max_of_comment_and_message_when_comm_v2(self, make_board, make_agent, make_task):
        """(a) comm_v2=True → Message rows count; the more recent one wins."""
        board = await make_board(name="LTA Board", slug=f"lta-{uuid.uuid4().hex[:6]}")
        task = await make_task(board_id=board.id, status="in_progress")

        now = utcnow()
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            s.add(TaskComment(
                task_id=task.id, author_type="agent", comment_type="progress",
                content="alt", created_at=now - timedelta(minutes=30),
            ))
            await s.commit()
            thread = await ensure_task_thread(s, db_task)
            await post_message(
                s, thread_id=thread.id, sender_type="agent", sender_id=uuid.uuid4(),
                message_type="message", body="neu",
            )

        async with _session() as s:
            db_task = await s.get(Task, task.id)
            activity = await last_task_activity(s, db_task, comm_v2=True)

        # Message is the more recent of the two (just posted) → wins.
        message_row = (await _thread_messages(db_task.thread_id))[0]
        assert activity is not None
        assert abs((activity - ensure_aware(message_row.created_at)).total_seconds()) < 5

    async def test_comment_only_when_no_thread(self, make_board, make_task):
        """No thread ever created (non-pilot task) → falls back to comment time only."""
        board = await make_board(name="LTA2 Board", slug=f"lta2-{uuid.uuid4().hex[:6]}")
        task = await make_task(board_id=board.id, status="in_progress")

        cutoff = utcnow() - timedelta(minutes=10)
        async with _session() as s:
            s.add(TaskComment(
                task_id=task.id, author_type="agent", comment_type="progress",
                content="einzig", created_at=cutoff,
            ))
            await s.commit()

        async with _session() as s:
            db_task = await s.get(Task, task.id)
            assert db_task.thread_id is None
            activity = await last_task_activity(s, db_task, comm_v2=True)

        assert activity is not None
        assert abs((activity - ensure_aware(cutoff)).total_seconds()) < 5

    async def test_none_when_no_activity_at_all(self, make_board, make_task):
        board = await make_board(name="LTA3 Board", slug=f"lta3-{uuid.uuid4().hex[:6]}")
        task = await make_task(board_id=board.id, status="in_progress")
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            activity = await last_task_activity(s, db_task, comm_v2=True)
        assert activity is None

    async def test_message_ignored_for_non_pilot_default(self, make_board, make_task):
        """(c) Regression: comm_v2 defaults False → a task's thread carries a
        MUCH more recent Message (briefing + waiting-resume line, neither
        gated by comm_v2 — see dispatch_delivery.persist_briefing_message /
        tasks.py's "Antwort erhalten" lines) but last_task_activity() must
        ignore it entirely and fall back to the comment/started_at reference,
        byte-identical to the pre-dual-read behavior."""
        board = await make_board(name="LTA4 Board", slug=f"lta4-{uuid.uuid4().hex[:6]}")
        task = await make_task(board_id=board.id, status="in_progress")

        old_comment_time = utcnow() - timedelta(minutes=45)
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            s.add(TaskComment(
                task_id=task.id, author_type="agent", comment_type="progress",
                content="briefing-era progress", created_at=old_comment_time,
            ))
            await s.commit()
            thread = await ensure_task_thread(s, db_task)
            # Briefing message + waiting-resume system line — both post
            # unconditionally, no comm_v2 gate, per dispatch_delivery.py /
            # tasks.py — these must NOT count as activity for this agent.
            await post_message(
                s, thread_id=thread.id, sender_type="system",
                message_type="system", body="<!-- mc:briefing:attempt=x --> Briefing",
            )
            await post_message(
                s, thread_id=thread.id, sender_type="system",
                message_type="system", body="▶ Antwort erhalten — Worker macht weiter",
            )

        async with _session() as s:
            db_task = await s.get(Task, task.id)
            assert db_task.thread_id is not None  # messages DO exist on the thread
            activity = await last_task_activity(s, db_task)  # comm_v2 defaults False

        assert activity is not None
        assert abs((activity - ensure_aware(old_comment_time)).total_seconds()) < 5


# ── (b) maybe_post_finish_nudge: episode-based dedupe ──────────────────────


@pytest.mark.asyncio
class TestMaybePostFinishNudgeEpisodeDedupe:
    async def test_interleaved_system_message_does_not_reset_dedupe(self, make_board, make_agent, make_task):
        """nudge → an unrelated system message interleaves (e.g. the
        waiting-resume "Antwort erhalten" line) → a repeat check does NOT
        re-nudge, since no NEW agent Message started a fresh episode. Then
        the agent posts a Message (new episode) and the resolution-signal
        recurs → the nudge fires once more."""
        board = await make_board(name="ND Board", slug=f"nd-{uuid.uuid4().hex[:6]}")
        agent = await make_agent(name="Nudge-Worker", board_id=board.id)
        task = await make_task(board_id=board.id, title="Nudge task", status="in_progress")

        async with _session() as s:
            db_task = await s.get(Task, task.id)
            from app.services.messaging import maybe_post_finish_nudge
            await maybe_post_finish_nudge(s, db_task)  # 1st nudge

        db_task = await _reload_task(task.id)
        messages = await _thread_messages(db_task.thread_id)
        assert [m.body for m in messages if m.message_type == "system"] == [FINISH_NUDGE_BODY]

        # An unrelated system line interleaves (waiting-resume path).
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            await post_message(
                s, thread_id=db_task.thread_id, sender_type="system",
                message_type="system", body="▶ Antwort erhalten — Worker macht weiter",
            )

        # Repeat check: same episode (no new agent Message since the nudge) → no re-nudge.
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            from app.services.messaging import maybe_post_finish_nudge
            await maybe_post_finish_nudge(s, db_task)

        db_task = await _reload_task(task.id)
        messages = await _thread_messages(db_task.thread_id)
        nudges = [m for m in messages if m.body == FINISH_NUDGE_BODY]
        assert len(nudges) == 1, "interleaved system message must not reset the dedupe episode"

        # Agent posts a new Message → fresh episode. A repeat resolution
        # signal must nudge again.
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            await post_message(
                s, thread_id=db_task.thread_id, sender_type="agent", sender_id=agent.id,
                message_type="message", body="weiter dran, gleich fertig",
            )
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            from app.services.messaging import maybe_post_finish_nudge
            await maybe_post_finish_nudge(s, db_task)

        db_task = await _reload_task(task.id)
        messages = await _thread_messages(db_task.thread_id)
        nudges = [m for m in messages if m.body == FINISH_NUDGE_BODY]
        assert len(nudges) == 2, "a new agent Message must start a fresh episode, allowing one more nudge"


# ── (b) + (c) Auto-promote removal for comm_v2, regression for non-pilots ──


async def _run_stale_check(fake_redis, session):
    from app.services.task_runner import TaskRunnerService

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock) as emit:
        runner = TaskRunnerService()
        await runner._check_stale_in_progress(session)
    return emit


async def _setup_resolution_task(make_board, make_agent, make_task, *, comm_v2: bool):
    board = await make_board(name="AP Board", slug=f"ap-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name=f"Worker-{uuid.uuid4().hex[:4]}", board_id=board.id, role="developer",
        comm_v2=comm_v2,
    )
    task = await make_task(
        board_id=board.id, title="Resolution task", status="in_progress",
        assigned_agent_id=agent.id,
        started_at=utcnow() - timedelta(minutes=5),
    )
    async with _session() as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=agent.id,
            comment_type="resolution", content="Fertig.",
        ))
        await s.commit()
    return board, agent, task


@pytest.mark.asyncio
class TestAutoPromoteWatchdogPath:
    """task_runner._check_stale_in_progress mirror auto-promote."""

    async def test_comm_v2_agent_nudged_not_promoted(self, make_board, make_agent, make_task, fake_redis):
        """(b) comm_v2 agent's resolution comment → task stays in_progress,
        a system Nudge message appears after a watchdog tick."""
        board, agent, task = await _setup_resolution_task(make_board, make_agent, make_task, comm_v2=True)

        async with _session() as s:
            await _run_stale_check(fake_redis, s)

        still = await _reload_task(task.id)
        assert still.status == "in_progress"

        db_task = await _reload_task(task.id)
        assert db_task.thread_id is not None
        messages = await _thread_messages(db_task.thread_id)
        system_msgs = [m for m in messages if m.message_type == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0].body == FINISH_NUDGE_BODY

    async def test_comm_v2_nudge_dedupes_across_ticks(self, make_board, make_agent, make_task, fake_redis):
        """A second watchdog tick with the same resolution comment does NOT
        post a second nudge — the thread already has the nudge as its last
        system message."""
        board, agent, task = await _setup_resolution_task(make_board, make_agent, make_task, comm_v2=True)

        async with _session() as s:
            await _run_stale_check(fake_redis, s)
        async with _session() as s:
            await _run_stale_check(fake_redis, s)

        db_task = await _reload_task(task.id)
        messages = await _thread_messages(db_task.thread_id)
        system_msgs = [m for m in messages if m.message_type == "system"]
        assert len(system_msgs) == 1, "dedupe must prevent a second identical nudge"

    async def test_non_pilot_agent_still_auto_promotes(self, make_board, make_agent, make_task, fake_redis):
        """(c) Regression: non-pilot (no comm_v2) agent's resolution comment
        still auto-promotes exactly as today."""
        board, agent, task = await _setup_resolution_task(make_board, make_agent, make_task, comm_v2=False)

        async with _session() as s:
            await _run_stale_check(fake_redis, s)

        promoted = await _reload_task(task.id)
        assert promoted.status == "review"


@pytest.mark.asyncio
class TestAutoPromoteAgentCommentsPath:
    """POST /boards/{board_id}/tasks/{task_id}/comments resolution auto-promote."""

    async def _post_resolution_comment(self, client: AsyncClient, board_id, task_id, agent_token: str):
        return await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"comment_type": "resolution", "content": "Fertig."},
        )

    async def test_comm_v2_agent_nudged_not_promoted(self, client: AsyncClient, make_board, make_task):
        """(b) Live POST path: comm_v2 agent's resolution comment → task
        stays in_progress, nudge message posted (once)."""
        board = await make_board(name="APC Board", slug=f"apc-{uuid.uuid4().hex[:6]}")
        raw_token, token_hash = generate_agent_token()
        async with _session() as s:
            agent = Agent(
                id=uuid.uuid4(), name="Comm2Worker", role="developer", board_id=board.id,
                scopes=[], provision_status="provisioned", agent_token_hash=token_hash,
                comm_v2=True,
            )
            s.add(agent)
            await s.commit()
            await s.refresh(agent)
        task = await make_task(
            board_id=board.id, title="APC task", status="in_progress",
            assigned_agent_id=agent.id,
        )

        resp = await self._post_resolution_comment(client, board.id, task.id, raw_token)
        assert resp.status_code == 201, resp.text

        still = await _reload_task(task.id)
        assert still.status == "in_progress"

        assert still.thread_id is not None
        messages = await _thread_messages(still.thread_id)
        system_msgs = [m for m in messages if m.message_type == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0].body == FINISH_NUDGE_BODY

        # A second resolution comment does not duplicate the nudge.
        resp2 = await self._post_resolution_comment(client, board.id, task.id, raw_token)
        assert resp2.status_code == 201, resp2.text
        still2 = await _reload_task(task.id)
        messages2 = await _thread_messages(still2.thread_id)
        assert len([m for m in messages2 if m.message_type == "system"]) == 1

    async def test_non_pilot_agent_still_auto_promotes(self, client: AsyncClient, make_board, make_task):
        """(c) Regression: non-pilot agent's resolution comment via the live
        endpoint still auto-promotes root tasks to review."""
        board = await make_board(name="APC2 Board", slug=f"apc2-{uuid.uuid4().hex[:6]}")
        raw_token, token_hash = generate_agent_token()
        async with _session() as s:
            agent = Agent(
                id=uuid.uuid4(), name="LegacyWorker", role="developer", board_id=board.id,
                scopes=[], provision_status="provisioned", agent_token_hash=token_hash,
            )
            s.add(agent)
            await s.commit()
            await s.refresh(agent)
        task = await make_task(
            board_id=board.id, title="APC2 task", status="in_progress",
            assigned_agent_id=agent.id,
        )

        resp = await self._post_resolution_comment(client, board.id, task.id, raw_token)
        assert resp.status_code == 201, resp.text

        promoted = await _reload_task(task.id)
        assert promoted.status == "review"
