"""Tests: /agent/me/{pdf,deliverable,telegram} auto-task-resolution endpoints.

Verifies the resolution paths:
  1. body_task_id override (explicit, with ownership check)
  2. agent.current_task_id (board lead + worker via cli-bridge)
  3. Fallback 422 when no task is found

Phase 30: The legacy `task.spawn_session_key` reverse lookup was removed with
the gateway_agent_id cleanup in Plan 30-01 — cli-bridge workers now use
current_task_id (set by the dispatcher) or body_task_id.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Fixtures ──────────────────────────────────────────────────────────────────

async def _setup_board_lead_with_current_task():
    """Board lead with current_task_id set."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="MeBoard", slug=f"me-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="LeadAgent", role="lead",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            is_board_lead=True,
            current_task_id=task_id,
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Lead Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()
    return board_id, agent_id, task_id, token_raw


async def _setup_worker_with_spawn_session():
    """Worker with an active task via current_task_id (cli-bridge subagent mode).

    Phase 30: Workers write current_task_id on dispatch (poll-based
    runtimes ack it and set it). Previously spawn_session_key was
    reverse-looked-up via the OpenClaw pattern instead — that path was
    dropped with Plan 30-01. The `gw_id` return value remains for
    backwards compat of the tuple; the tests themselves no longer check
    the session-key path.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    gw_id = f"gw-{uuid.uuid4().hex[:8]}"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="WorkerBoard", slug=f"wk-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="WorkerAgent", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            is_board_lead=False,
            current_task_id=task_id,  # Phase 30: workers have current_task_id like leads
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Worker Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()
    return board_id, agent_id, task_id, token_raw, gw_id


async def _setup_agent_no_task():
    """Agent without an active task (neither current_task_id nor spawn_session_key)."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="IdleBoard", slug=f"idle-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="IdleAgent", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            current_task_id=None,
            provision_status="provisioned",
        ))
        await s.commit()
    return board_id, agent_id, token_raw


# ── /me/pdf tests ─────────────────────────────────────────────────────────────

def _fake_sidecar(task_id: uuid.UUID):
    return {
        "path": f"/shared-deliverables/{task_id}/report.pdf",
        "bytes": 8000,
        "title": "Test Report",
        "task_id": str(task_id),
        "pages": 2,
    }


@pytest.mark.asyncio
async def test_me_pdf_board_lead_path(client, fake_redis):
    """POST /me/pdf uses agent.current_task_id (board-lead path)."""
    board_id, agent_id, task_id, token = await _setup_board_lead_with_current_task()

    with patch("app.services.pdf_generator.generate_pdf", AsyncMock(return_value=_fake_sidecar(task_id))):
        resp = await client.post(
            "/api/v1/agent/me/pdf",
            json={"title": "Test Report", "markdown": "# Hello"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["title"] == "Test Report"


@pytest.mark.asyncio
async def test_me_pdf_worker_spawn_session_key_path(client, fake_redis):
    """POST /me/pdf uses the spawn_session_key reverse lookup (worker path)."""
    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    with patch("app.services.pdf_generator.generate_pdf", AsyncMock(return_value=_fake_sidecar(task_id))):
        resp = await client.post(
            "/api/v1/agent/me/pdf",
            json={"title": "Test Report", "markdown": "# Hello"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_me_pdf_body_override(client, fake_redis):
    """POST /me/pdf accepts task_id in the body as an explicit override."""
    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    with patch("app.services.pdf_generator.generate_pdf", AsyncMock(return_value=_fake_sidecar(task_id))):
        resp = await client.post(
            "/api/v1/agent/me/pdf",
            json={"title": "Test Report", "markdown": "# Hello", "task_id": str(task_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_me_pdf_422_no_active_task(client, fake_redis):
    """POST /me/pdf returns 422 when no active task is found."""
    board_id, agent_id, token = await _setup_agent_no_task()

    resp = await client.post(
        "/api/v1/agent/me/pdf",
        json={"title": "Test Report", "markdown": "# Hello"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 422, resp.text
    assert "aktive Task" in resp.json()["detail"]


# ── /me/deliverable tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_deliverable_board_lead_path(client, fake_redis):
    """POST /me/deliverable uses agent.current_task_id (board-lead path)."""
    board_id, agent_id, task_id, token = await _setup_board_lead_with_current_task()

    resp = await client.post(
        "/api/v1/agent/me/deliverable",
        json={"deliverable_type": "document", "title": "Research", "content": "# Content"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, resp.text
    assert "id" in resp.json()


@pytest.mark.asyncio
async def test_me_deliverable_worker_spawn_session_key_path(client, fake_redis):
    """POST /me/deliverable uses the spawn_session_key reverse lookup (worker path)."""
    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    resp = await client.post(
        "/api/v1/agent/me/deliverable",
        json={"deliverable_type": "document", "title": "Worker Research", "content": "# Data"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "id" in data

    # Verify that the deliverable belongs to the correct task
    from app.models.deliverable import TaskDeliverable
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        d = await s.get(TaskDeliverable, uuid.UUID(data["id"]))
        assert d is not None
        assert d.task_id == task_id


@pytest.mark.asyncio
async def test_me_deliverable_body_override(client, fake_redis):
    """POST /me/deliverable accepts task_id in the body as an override."""
    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    resp = await client.post(
        "/api/v1/agent/me/deliverable",
        json={
            "deliverable_type": "document", "title": "Override Test",
            "content": "# Hello", "task_id": str(task_id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_me_deliverable_422_no_active_task(client, fake_redis):
    """POST /me/deliverable returns 422 when no active task is found."""
    board_id, agent_id, token = await _setup_agent_no_task()

    resp = await client.post(
        "/api/v1/agent/me/deliverable",
        json={"deliverable_type": "document", "title": "No Task", "content": "# X"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 422, resp.text
    assert "aktive Task" in resp.json()["detail"]


# ── /me/telegram tests ────────────────────────────────────────────────────────

def _mock_telegram():
    """Patch telegram_reports.configured + send."""
    from unittest.mock import MagicMock, AsyncMock, patch
    import contextlib

    @contextlib.asynccontextmanager
    async def _patches():
        with patch("app.services.telegram_reports.telegram_reports") as mock_tg:
            mock_tg.configured = True
            mock_tg.send = AsyncMock(return_value={"ok": True, "result": {"message_id": 42}})
            mock_tg.send_photo = AsyncMock(return_value={"ok": True, "result": {"message_id": 42}})
            mock_tg.send_document = AsyncMock(return_value={"ok": True, "result": {"message_id": 42}})
            yield mock_tg

    return _patches()


@pytest.mark.asyncio
async def test_me_telegram_board_lead_path(client, fake_redis):
    """POST /me/telegram uses agent.current_task_id and sets report_sent_to_telegram."""
    from app.models.task import Task

    board_id, agent_id, task_id, token = await _setup_board_lead_with_current_task()

    async with _mock_telegram():
        resp = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "Recherche fertig. Ergebnisse im Deliverable."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # Flag set
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.report_sent_to_telegram is True


@pytest.mark.asyncio
async def test_me_telegram_worker_spawn_session_key_path(client, fake_redis):
    """POST /me/telegram uses the spawn_session_key reverse lookup (worker path)."""
    from app.models.task import Task

    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    async with _mock_telegram():
        resp = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "Worker done. PDF ready."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # Flag set on the correct task (worker path)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.report_sent_to_telegram is True


@pytest.mark.asyncio
async def test_me_telegram_no_task_still_works(client, fake_redis):
    """POST /me/telegram also works without an active task (required=False)."""
    board_id, agent_id, token = await _setup_agent_no_task()

    async with _mock_telegram():
        resp = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "Allgemeine Meldung ohne Task-Kontext."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_me_telegram_body_override(client, fake_redis):
    """POST /me/telegram accepts task_id in the body as an explicit override."""
    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    async with _mock_telegram():
        resp = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "Done.", "task_id": str(task_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
