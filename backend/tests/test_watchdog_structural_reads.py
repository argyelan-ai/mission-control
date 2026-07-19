"""Tests for Task 10 — Watchdog liest strukturell + Auto-Promote-Abbau (comm_v2-gated).

Covers the three brief scenarios:
  (a) `last_task_activity` takes the max of comment-time and message-time
      (dual-read, §8.1).
  (b) A comm_v2 agent's resolution-signal does NOT auto-promote the task —
      instead a system Nudge message ("... bitte `mc finish` ausführen")
      lands on the task's thread, deduped so a repeat signal doesn't spam it.
      Covered at both call sites: the agent_comments.py POST endpoint (live
      resolution comment) and task_runner._check_stale_in_progress (the
      watchdog's own mirror check on a pre-existing resolution comment).
  (c) Regression: a non-pilot (no comm_v2) agent's resolution comment still
      auto-promotes exactly as today, at both call sites.
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
    async def test_max_of_comment_and_message(self, make_board, make_agent, make_task):
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
            activity = await last_task_activity(s, db_task)

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
            activity = await last_task_activity(s, db_task)

        assert activity is not None
        assert abs((activity - ensure_aware(cutoff)).total_seconds()) < 5

    async def test_none_when_no_activity_at_all(self, make_board, make_task):
        board = await make_board(name="LTA3 Board", slug=f"lta3-{uuid.uuid4().hex[:6]}")
        task = await make_task(board_id=board.id, status="in_progress")
        async with _session() as s:
            db_task = await s.get(Task, task.id)
            activity = await last_task_activity(s, db_task)
        assert activity is None


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

    async def test_comm_v2_agent_nudged_not_promoted(self, make_board, make_agent, make_task, monkeypatch, fake_redis):
        """(b) comm_v2 agent's resolution comment → task stays in_progress,
        a system Nudge message appears after a watchdog tick."""
        monkeypatch.setattr(Agent, "comm_v2", True, raising=False)
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

    async def test_comm_v2_nudge_dedupes_across_ticks(self, make_board, make_agent, make_task, monkeypatch, fake_redis):
        """A second watchdog tick with the same resolution comment does NOT
        post a second nudge — the thread already has the nudge as its last
        system message."""
        monkeypatch.setattr(Agent, "comm_v2", True, raising=False)
        board, agent, task = await _setup_resolution_task(make_board, make_agent, make_task, comm_v2=True)

        async with _session() as s:
            await _run_stale_check(fake_redis, s)
        async with _session() as s:
            await _run_stale_check(fake_redis, s)

        db_task = await _reload_task(task.id)
        messages = await _thread_messages(db_task.thread_id)
        system_msgs = [m for m in messages if m.message_type == "system"]
        assert len(system_msgs) == 1, "dedupe must prevent a second identical nudge"

    async def test_non_pilot_agent_still_auto_promotes(self, make_board, make_agent, make_task, monkeypatch, fake_redis):
        """(c) Regression: non-pilot (no comm_v2) agent's resolution comment
        still auto-promotes exactly as today."""
        monkeypatch.setattr(Agent, "comm_v2", False, raising=False)
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

    async def test_comm_v2_agent_nudged_not_promoted(self, client: AsyncClient, make_board, make_task, monkeypatch):
        """(b) Live POST path: comm_v2 agent's resolution comment → task
        stays in_progress, nudge message posted (once)."""
        monkeypatch.setattr(Agent, "comm_v2", True, raising=False)

        board = await make_board(name="APC Board", slug=f"apc-{uuid.uuid4().hex[:6]}")
        raw_token, token_hash = generate_agent_token()
        async with _session() as s:
            agent = Agent(
                id=uuid.uuid4(), name="Comm2Worker", role="developer", board_id=board.id,
                scopes=[], provision_status="provisioned", agent_token_hash=token_hash,
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

    async def test_non_pilot_agent_still_auto_promotes(self, client: AsyncClient, make_board, make_task, monkeypatch):
        """(c) Regression: non-pilot agent's resolution comment via the live
        endpoint still auto-promotes root tasks to review."""
        monkeypatch.setattr(Agent, "comm_v2", False, raising=False)

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
