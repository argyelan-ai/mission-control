"""Tests: /agent/me/{pdf,deliverable,telegram} Auto-Task-Resolution Endpoints.

Prueft die Resolution-Pfade:
  1. body_task_id Override (explizit, mit Ownership-Check)
  2. agent.current_task_id (Board-Lead + Worker via cli-bridge)
  3. Fallback 422 wenn keine Task gefunden

Phase 30: Der legacy `task.spawn_session_key` Reverse-Lookup wurde mit der
gateway_agent_id-Bereinigung in Plan 30-01 entfernt — cli-bridge Worker
nutzen jetzt current_task_id (vom Dispatcher gesetzt) oder body_task_id.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Fixtures ──────────────────────────────────────────────────────────────────

async def _setup_board_lead_with_current_task():
    """Board Lead mit gesetztem current_task_id."""
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
    """Worker mit aktiver Task ueber current_task_id (cli-bridge Subagent-Modus).

    Phase 30: Workers schreiben current_task_id beim Dispatch (poll-based
    runtimes ack-en und setzen es). Vorher wurde stattdessen
    spawn_session_key via OpenClaw-Pattern reverse-lookuped — der Pfad ist
    mit Plan 30-01 entfallen. Die `gw_id` Rueckgabe bleibt fuer
    Backwards-Compat des Tuples; die Tests selbst pruefen jetzt nicht
    mehr den session-key path.
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
            current_task_id=task_id,  # Phase 30: Workers haben current_task_id wie Leads
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
    """Agent ohne aktive Task (weder current_task_id noch spawn_session_key)."""
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


# ── /me/pdf Tests ─────────────────────────────────────────────────────────────

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
    """POST /me/pdf nutzt agent.current_task_id (Board-Lead-Pfad)."""
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
    """POST /me/pdf nutzt spawn_session_key Reverse-Lookup (Worker-Pfad)."""
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
    """POST /me/pdf akzeptiert task_id im Body als expliziten Override."""
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
    """POST /me/pdf gibt 422 wenn keine aktive Task gefunden wird."""
    board_id, agent_id, token = await _setup_agent_no_task()

    resp = await client.post(
        "/api/v1/agent/me/pdf",
        json={"title": "Test Report", "markdown": "# Hello"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 422, resp.text
    assert "aktive Task" in resp.json()["detail"]


# ── /me/deliverable Tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_deliverable_board_lead_path(client, fake_redis):
    """POST /me/deliverable nutzt agent.current_task_id (Board-Lead-Pfad)."""
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
    """POST /me/deliverable nutzt spawn_session_key Reverse-Lookup (Worker-Pfad)."""
    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    resp = await client.post(
        "/api/v1/agent/me/deliverable",
        json={"deliverable_type": "document", "title": "Worker Research", "content": "# Data"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "id" in data

    # Verifizieren dass Deliverable zur richtigen Task gehoert
    from app.models.deliverable import TaskDeliverable
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        d = await s.get(TaskDeliverable, uuid.UUID(data["id"]))
        assert d is not None
        assert d.task_id == task_id


@pytest.mark.asyncio
async def test_me_deliverable_body_override(client, fake_redis):
    """POST /me/deliverable akzeptiert task_id im Body als Override."""
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
    """POST /me/deliverable gibt 422 wenn keine aktive Task gefunden wird."""
    board_id, agent_id, token = await _setup_agent_no_task()

    resp = await client.post(
        "/api/v1/agent/me/deliverable",
        json={"deliverable_type": "document", "title": "No Task", "content": "# X"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 422, resp.text
    assert "aktive Task" in resp.json()["detail"]


# ── /me/telegram Tests ────────────────────────────────────────────────────────

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
    """POST /me/telegram nutzt agent.current_task_id und setzt report_sent_to_telegram."""
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

    # Flag gesetzt
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.report_sent_to_telegram is True


@pytest.mark.asyncio
async def test_me_telegram_worker_spawn_session_key_path(client, fake_redis):
    """POST /me/telegram nutzt spawn_session_key Reverse-Lookup (Worker-Pfad)."""
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

    # Flag auf richtigem Task gesetzt (Worker-Pfad)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.report_sent_to_telegram is True


@pytest.mark.asyncio
async def test_me_telegram_no_task_still_works(client, fake_redis):
    """POST /me/telegram funktioniert auch ohne aktive Task (required=False)."""
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
    """POST /me/telegram akzeptiert task_id im Body als expliziten Override."""
    board_id, agent_id, task_id, token, gw_id = await _setup_worker_with_spawn_session()

    async with _mock_telegram():
        resp = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "Done.", "task_id": str(task_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
