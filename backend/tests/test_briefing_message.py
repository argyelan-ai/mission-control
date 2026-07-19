"""Tests for Task 9 — Briefing als Message #1 (Interaction 2.0).

Every successful dispatch persists the final dispatch prompt as a `system`
message on the task thread, idempotent per dispatch_attempt_id:
  (a) first persist → exactly one system message that carries the prompt;
      a second persist with the SAME attempt_id → no duplicate;
      a persist with a NEW attempt_id → a second message is allowed.
"""
import uuid

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.task import Task
from app.models.thread import Message
from app.services.dispatch_delivery import persist_briefing_message

from .conftest import test_engine


async def _board(session: AsyncSession) -> Board:
    board = Board(id=uuid.uuid4(), name="BM Board", slug=f"bm-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def _task(board_id: uuid.UUID, **kwargs) -> Task:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(id=uuid.uuid4(), board_id=board_id, title="BM Task", **kwargs)
        s.add(task)
        await s.commit()
        await s.refresh(task)
    return task


@pytest.mark.asyncio
class TestBriefingMessage:
    async def test_briefing_persisted_as_first_system_message(self):
        """(a) Dispatch prompt lands as message #1 (system) on the task thread."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(board.id, dispatch_attempt_id="attempt-1")

        prompt = "# Auftrag\nBaue das Ding. Workspace: /x. Melde dich via mc."
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            msg = await persist_briefing_message(db_task, prompt, s)
            assert msg is not None
            assert msg.seq == 1
            assert msg.message_type == "system"
            assert msg.sender_type == "system"
            assert prompt in msg.body

            msgs = (await s.exec(
                select(Message).where(Message.thread_id == msg.thread_id)
            )).all()
            assert len(msgs) == 1

    async def test_same_attempt_is_idempotent(self):
        """A retry of the SAME dispatch attempt must not duplicate the briefing."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(board.id, dispatch_attempt_id="attempt-1")
        prompt = "Erst-Briefing."

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            first = await persist_briefing_message(db_task, prompt, s)
            assert first is not None
            # Same attempt id → no-op.
            second = await persist_briefing_message(db_task, prompt, s)
            assert second is None

            msgs = (await s.exec(
                select(Message).where(Message.thread_id == first.thread_id)
            )).all()
            assert len(msgs) == 1

    async def test_new_attempt_posts_a_new_briefing(self):
        """A NEW dispatch attempt (re-dispatch/resume) MAY post a fresh briefing."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(board.id, dispatch_attempt_id="attempt-1")

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            m1 = await persist_briefing_message(db_task, "Briefing v1", s)
            assert m1 is not None

            # New attempt id → new message allowed.
            db_task.dispatch_attempt_id = "attempt-2"
            s.add(db_task)
            await s.commit()
            m2 = await persist_briefing_message(db_task, "Briefing v2", s)
            assert m2 is not None
            assert m2.seq == 2

            msgs = (await s.exec(
                select(Message).where(Message.thread_id == m1.thread_id).order_by(Message.seq)
            )).all()
            assert len(msgs) == 2
            assert "Briefing v1" in msgs[0].body
            assert "Briefing v2" in msgs[1].body
