"""Tests: TaskEvent gets an actor (actor_user_id / actor_label).

Today task_events.changed_by='user' carries no author — Mark clicking in
the browser and a Claude session using the service account look identical.
record_task_event() gains two optional kwargs (actor_user_id, actor_label)
that get written onto the event, operator routes pass the logged-in user
through, Telegram paths pass a channel label, and the task timeline
surfaces actor_label per event.
"""

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from tests.conftest import test_engine


async def _make_board_and_task(status: str = "in_progress", **task_kwargs):
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Actor Board", slug=f"actor-{board_id.hex[:8]}")
        s.add(board)
        task = Task(id=task_id, board_id=board_id, title="Actor Task", status=status, **task_kwargs)
        s.add(task)
        await s.commit()
    return board_id, task_id


@pytest.mark.asyncio
async def test_record_task_event_stores_actor():
    """Direct call with both new kwargs stores them on the TaskEvent row."""
    from app.services.task_lifecycle import record_task_event
    from app.models.task import TaskEvent
    from app.models.user import User

    board_id, task_id = await _make_board_and_task()
    user_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # A real users row: actor_user_id is a foreign key to users.id, and
        # on the Postgres test lane (MC_TEST_DATABASE_URL) an FK is enforced
        # for real, unlike SQLite (tests/conftest.py:156).
        s.add(User(id=user_id, email="actor-test@mc.local", name="Mark"))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await record_task_event(
            s, task_id, "in_progress", "review",
            changed_by="user", reason="manual_update",
            actor_user_id=user_id, actor_label="Mark",
        )
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(TaskEvent).where(TaskEvent.task_id == task_id))
        event = result.one()
        assert event.actor_user_id == user_id
        assert event.actor_label == "Mark"


@pytest.mark.asyncio
async def test_record_task_event_without_actor_stays_none():
    """Existing call sites that don't pass the new kwargs keep working, actor stays None."""
    from app.services.task_lifecycle import record_task_event
    from app.models.task import TaskEvent

    board_id, task_id = await _make_board_and_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await record_task_event(
            s, task_id, "in_progress", "review",
            changed_by="agent", reason="handoff",
        )
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(TaskEvent).where(TaskEvent.task_id == task_id))
        event = result.one()
        assert event.actor_user_id is None
        assert event.actor_label is None


@pytest.mark.asyncio
async def test_operator_patch_status_records_actor(auth_client):
    """PATCH .../tasks/{id} with status writes the logged-in operator as actor."""
    from app.models.task import TaskEvent

    board_id, task_id = await _make_board_and_task(status="in_progress")

    resp = await auth_client.patch(
        f"/api/v1/boards/{board_id}/tasks/{task_id}",
        json={"status": "review"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .where(TaskEvent.changed_by == "user")
        )
        events = result.all()
        assert len(events) >= 1
        event = events[-1]
        assert str(event.actor_user_id) == "00000000-0000-0000-0000-000000000099"
        assert event.actor_label == "Test Admin"


@pytest.mark.asyncio
async def test_timeline_exposes_actor_label(auth_client):
    """The task-timeline endpoint surfaces actor_label for a task_event entry."""
    board_id, task_id = await _make_board_and_task(status="in_progress")

    resp = await auth_client.patch(
        f"/api/v1/boards/{board_id}/tasks/{task_id}",
        json={"status": "review"},
    )
    assert resp.status_code == 200

    resp = await auth_client.get(f"/api/v1/boards/{board_id}/tasks/{task_id}/timeline")
    assert resp.status_code == 200
    entries = resp.json()["entries"] if isinstance(resp.json(), dict) else resp.json()
    task_event_entries = [e for e in entries if e.get("source") == "task_event"]
    assert task_event_entries, "expected at least one task_event timeline entry"
    assert any(e.get("actor_label") == "Test Admin" for e in task_event_entries)


@pytest.mark.asyncio
async def test_manual_stop_records_actor(auth_client):
    """POST .../tasks/{id}/stop writes the logged-in operator as actor on the manual_stop event."""
    from app.models.task import TaskEvent

    board_id, task_id = await _make_board_and_task(status="in_progress")

    resp = await auth_client.post(f"/api/v1/boards/{board_id}/tasks/{task_id}/stop", json={})
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .where(TaskEvent.reason == "manual_stop")
        )
        event = result.one()
        assert str(event.actor_user_id) == "00000000-0000-0000-0000-000000000099"
        assert event.actor_label == "Test Admin"


def _load_0202_migration():
    """Load alembic revision 0202_task_event_actor as a plain Python module.

    Same op-shim technique as test_runtime_serving_since.py:51-69 /
    test_migration_0091.py:37-67: the file does `from alembic import op`,
    which only resolves inside an alembic command context. Injecting a
    no-op shim into sys.modules before exec lets upgrade()/downgrade()
    actually run (proving the file is syntactically valid and calls the
    right op.* functions) without touching any real database.
    """
    import importlib.util
    import sys
    import types
    from pathlib import Path

    versions_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    path = versions_dir / "0202_task_event_actor.py"
    if not path.is_file():
        pytest.fail(f"Migration 0202 not present at {path}")

    calls: dict[str, list] = {"add_column": [], "drop_column": []}
    op_shim = types.SimpleNamespace(
        add_column=lambda *a, **k: calls["add_column"].append((a, k)),
        drop_column=lambda *a, **k: calls["drop_column"].append((a, k)),
    )
    import alembic as _alembic
    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0202", str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_migration_0202_exists_and_chains():
    """Migration 0202_task_event_actor exists and chains onto 0201."""
    import re
    from pathlib import Path

    versions_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"

    src_0201 = (versions_dir / "0201_agent_op_work_language.py").read_text()
    revision_0201 = re.search(r'^revision\s*=\s*"([^"]+)"', src_0201, re.MULTILINE).group(1)

    path_0202 = versions_dir / "0202_task_event_actor.py"
    assert path_0202.exists(), f"missing migration file: {path_0202}"
    src_0202 = path_0202.read_text()
    down_revision_0202 = re.search(r'^down_revision\s*=\s*"([^"]+)"', src_0202, re.MULTILINE).group(1)

    assert down_revision_0202 == revision_0201


def test_migration_0202_upgrade_adds_both_columns():
    """upgrade() actually runs (module loads + executes) and adds exactly
    the two expected columns onto task_events."""
    module, calls = _load_0202_migration()
    module.upgrade()

    assert len(calls["add_column"]) == 2
    (table_actor_id, col_actor_id), _ = calls["add_column"][0]
    assert table_actor_id == "task_events"
    assert col_actor_id.name == "actor_user_id"

    (table_actor_label, col_actor_label), _ = calls["add_column"][1]
    assert table_actor_label == "task_events"
    assert col_actor_label.name == "actor_label"


def test_migration_0202_downgrade_drops_both_columns_reversed():
    module, calls = _load_0202_migration()
    module.downgrade()

    assert calls["drop_column"] == [
        (("task_events", "actor_label"), {}),
        (("task_events", "actor_user_id"), {}),
    ]


@pytest.mark.asyncio
async def test_agent_events_endpoint_does_not_leak_actor(client, auth_client, make_agent, make_board):
    """GET /api/v1/agent/boards/{id}/tasks/{id}/events must NOT expose
    actor_user_id/actor_label — that would put the operator's real name
    into every agent's context, including external-model runtimes
    (Pruefbericht B2, privacy review 21.09.2026).

    Sabotage-capable: with `[e.model_dump() for e in ...]` this test fails
    (both keys present and actor_label carries the name).
    """
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.task import Task
    from sqlmodel import select

    board = await make_board()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(id=task_id, board_id=board.id, title="Agent Leak Task", status="in_progress")
        s.add(task)
        await s.commit()

    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task_id}",
        json={"status": "review"},
    )
    assert resp.status_code == 200

    agent = await make_agent(name="LeakCheck Agent", scopes=["tasks:read"])
    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(Agent).where(Agent.id == agent.id))
        db_agent = result.one()
        db_agent.agent_token_hash = token_hash
        s.add(db_agent)
        await s.commit()
    client.headers["Authorization"] = f"Bearer {raw_token}"

    resp = await client.get(f"/api/v1/agent/boards/{board.id}/tasks/{task_id}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert events, "expected at least one task event"
    for e in events:
        assert "actor_user_id" not in e
        assert "actor_label" not in e


@pytest.mark.asyncio
async def test_browser_thread_answer_records_actor_user_id(client):
    """R4 (Pruefbericht B1 follow-up): a browser reply to a blocking
    question resumes the task AND writes the actor on the resulting
    TaskEvent — the eingeloggte User, not a channel guess."""
    import datetime as dt

    from app.auth import create_access_token, generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task, TaskEvent
    from app.models.thread import Message
    from app.models.user import User
    from app.services.messaging import ensure_task_thread, post_message

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Actor Thread Board", slug=f"actor-thread-{board_id.hex[:8]}"))
        s.add(Task(
            id=task_id, board_id=board_id, title="Actor Thread Task", status="waiting",
            dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=dt.datetime.now(tz=dt.timezone.utc),
            assigned_agent_id=agent_id,
        ))
        raw_token, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="ActorThread Agent", role="developer", board_id=board_id,
            scopes=["chat:write"], provision_status="provisioned",
            agent_token_hash=token_hash, current_task_id=task_id, comm_v2=True,
        ))
        s.add(User(id=user_id, email=f"u-{user_id.hex[:6]}@mc.local", name="Mark", role="admin", is_active=True))
        await s.commit()

        task = await s.get(Task, task_id)
        thread = await ensure_task_thread(s, task)
        question = await post_message(
            s, thread_id=thread.id, sender_type="agent", sender_id=agent_id,
            message_type="question", body="Redis oder Postgres?",
            question_meta={"awaiting": True, "blocking": True, "to": "boss", "priority": "high"},
        )
        await s.commit()
        await s.refresh(question)

    token = create_access_token(str(user_id), "admin")
    client.headers["Authorization"] = f"Bearer {token}"
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/thread/messages",
        json={"body": "Nimm Postgres.", "reply_to": str(question.id)},
    )
    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .where(TaskEvent.reason == "answer_received")
        )
        event = result.one()
        assert event.actor_user_id == user_id
        assert event.actor_label == "Mark"


@pytest.mark.asyncio
async def test_telegram_button_resume_records_channel_actor(make_board, make_agent, make_task):
    """R4 (Pruefbericht N3 follow-up): the real Telegram inline-button
    resume path (telegram_bot._resolve_approval, "approve" on a
    blocker_decision) now writes a TaskEvent with actor_label="telegram"
    (no actor_user_id — Telegram doesn't map to a users row)."""
    from datetime import timedelta
    from unittest.mock import patch

    from app.models.approval import Approval
    from app.models.task import TaskEvent
    from app.services.telegram_bot import telegram_bot
    from app.utils import utcnow

    board = await make_board(name="TG Actor Board", slug=f"tg-actor-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(name="TG Actor Worker", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="TG Actor Task", status="blocked",
        assigned_agent_id=agent.id,
    )
    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            id=approval_id, board_id=board.id, task_id=task.id, agent_id=agent.id,
            action_type="blocker_decision", description="blockiert",
            status="pending", payload={"blocker_type": "technical_problem"},
            expires_at=utcnow() + timedelta(hours=24),
        ))
        await s.commit()

    with patch("app.database.engine", test_engine):
        resolved = await telegram_bot._resolve_approval(approval_id, "approve")
    assert resolved == "resolved"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id)
            .where(TaskEvent.reason == "telegram_button_resume")
        )
        event = result.one()
        assert event.actor_user_id is None
        assert event.actor_label == "telegram"
