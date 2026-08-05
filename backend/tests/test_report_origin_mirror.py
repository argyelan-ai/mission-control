"""A report finds its way back to the conversation that ordered the work.

Option 2 of the thread-anchor fix (2026-08-05): a task can carry
``origin_thread_id`` — the conversation the order came from. When the final
report is delivered, the server mirrors its text into that thread as a plain
message; the chat pipeline then delivers it as a Slack thread reply under the
operator's original message. Deterministic — no SOUL discipline required for
the result to reach the thread.

The link itself is set at creation (``origin_thread_id`` on task create /
delegate) and inherited by delegated subtasks, so the orchestrator's
consolidation report lands right even when a whole task tree worked the order.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup(with_origin=True, current_task=True):
    """Board lead with an in_progress task, optionally linked to a chat thread."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.models.thread import Thread

    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    thread_id = None
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="OB", slug=f"ob-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(
            Agent(
                id=agent_id, name="Boss", slug=f"boss-{uuid.uuid4().hex[:6]}",
                role="lead", board_id=board_id, agent_token_hash=token_hash,
                scopes=["tasks:read", "tasks:write", "tasks:create", "chat:write"],
                is_board_lead=True, current_task_id=task_id if current_task else None,
                provision_status="provisioned",
            )
        )
        if with_origin:
            thread = Thread(
                kind="chat", agent_id=agent_id, title="Chat Boss",
                slack_thread_ts=f"175369{uuid.uuid4().int % 10_000}.000100",
            )
            s.add(thread)
            await s.flush()
            thread_id = thread.id
        s.add(
            Task(
                id=task_id, board_id=board_id, title="Auftrag aus dem Chat",
                status="in_progress", assigned_agent_id=agent_id,
                owner_agent_id=agent_id, origin_thread_id=thread_id,
            )
        )
        await s.commit()
    return board_id, agent_id, task_id, thread_id, token_raw


@asynccontextmanager
async def _telegram_ok():
    with patch("app.services.telegram_reports.telegram_reports") as tg:
        tg.configured = True
        tg.send = AsyncMock(return_value={"ok": True, "result": {"message_id": 42}})
        tg.send_photo = AsyncMock(
            return_value={"ok": True, "result": {"message_id": 42}}
        )
        tg.send_document = AsyncMock(
            return_value={"ok": True, "result": {"message_id": 42}}
        )
        yield tg


async def _thread_bodies(thread_id) -> list[str]:
    from app.models.thread import Message

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return [
            m.body
            for m in (
                await s.exec(select(Message).where(Message.thread_id == thread_id))
            ).all()
        ]


# ── 1. The mirror ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_is_mirrored_into_the_origin_thread(client, fake_redis):
    _, _, _, thread_id, token = await _setup()

    async with _telegram_ok():
        resp = await client.post(
            "/api/v1/agent/me/report",
            json={"text": "Film liegt auf dem NAS — 4K, Deutsch."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    bodies = await _thread_bodies(thread_id)
    assert len(bodies) == 1
    assert "Film liegt auf dem NAS" in bodies[0]


@pytest.mark.asyncio
async def test_without_origin_no_thread_gets_a_copy(client, fake_redis):
    """No link, no guessing — the SOUL consolidation rule covers this case."""
    from app.models.thread import Message

    _, _, _, _, token = await _setup(with_origin=False)

    async with _telegram_ok():
        resp = await client.post(
            "/api/v1/agent/me/report",
            json={"text": "Bericht ohne Herkunft."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await s.exec(select(Message))).all() == []


@pytest.mark.asyncio
async def test_a_failing_mirror_never_fails_the_report(client, fake_redis):
    """The report reached the operator; the thread copy is best-effort."""
    _, _, _, thread_id, token = await _setup()

    async with _telegram_ok():
        with patch(
            "app.services.messaging.post_message",
            new_callable=AsyncMock, side_effect=RuntimeError("db weg"),
        ):
            resp = await client.post(
                "/api/v1/agent/me/report",
                json={"text": "Bericht trotz kaputtem Spiegel."},
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200, resp.text


# ── 2. The link ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_create_accepts_and_validates_origin_thread(client, fake_redis):
    board_id, agent_id, _, thread_id, token = await _setup()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks",
        json={"title": "Folgeauftrag", "origin_thread_id": str(thread_id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text

    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        created = (
            await s.exec(select(Task).where(Task.title == "Folgeauftrag"))
        ).one()
        assert created.origin_thread_id == thread_id


@pytest.mark.asyncio
async def test_task_create_rejects_a_foreign_origin_thread(client, fake_redis):
    """An agent may only link conversations it takes part in — the same rule
    that governs where it may listen and speak (thread_scope)."""
    from app.models.thread import Thread

    board_id, _, _, _, token = await _setup()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        foreign = Thread(kind="chat", title="fremd")
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks",
        json={"title": "Eingeschmuggelt", "origin_thread_id": str(foreign_id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_delegate_inherits_the_origin_link(client, fake_redis):
    """The orchestrator reports for the tree ('whoever dispatches, sends') —
    but a subtask with autonomous_report needs the link too, so it inherits."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.task import Task

    board_id, agent_id, task_id, thread_id, token = await _setup()
    worker_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, worker_hash = generate_agent_token()
        s.add(
            Agent(
                id=worker_id, name="Worker", slug=f"w-{uuid.uuid4().hex[:6]}",
                role="developer", board_id=board_id, agent_token_hash=worker_hash,
                provision_status="provisioned",
            )
        )
        await s.commit()

    with patch(
        "app.services.operations.check_dispatch_allowed",
        new_callable=AsyncMock, return_value=(True, None),
    ), patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/delegate",
            json={
                "title": "Teilauftrag",
                "description": "Bitte den Download erledigen und verifizieren.",
                "assigned_agent_id": str(worker_id),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub = (
            await s.exec(select(Task).where(Task.title == "Teilauftrag"))
        ).one()
        assert sub.origin_thread_id == thread_id
