"""Tests fuer TelegramReportsService + /agent/telegram/send Endpoint."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_agent_with_chat_scope():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Test", slug=f"tg-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Reporter", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["chat:write"],
            provision_status="provisioned",
        ))
        await s.commit()
    return board_id, token_raw


@pytest.mark.asyncio
async def test_telegram_report_sends_when_configured(client, fake_redis):
    """Happy-Path: konfigurierter Bot sendet, API-Response wird durchgereicht."""
    _, token = await _setup_agent_with_chat_scope()

    mock_service = AsyncMock()
    mock_service.configured = True
    mock_service.send.return_value = {"ok": True, "result": {"message_id": 42}}

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "🔍 Researcher · Test ✅\n\nAlles lief gut."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message_id": 42}
    mock_service.send.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_report_503_when_not_configured(client, fake_redis):
    """Nicht konfigurierter Bot → 503 mit Hinweis."""
    _, token = await _setup_agent_with_chat_scope()

    mock_service = AsyncMock()
    mock_service.configured = False

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "anything"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 503
    assert "TELEGRAM_REPORTS_BOT_TOKEN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_telegram_report_rejects_empty_text(client, fake_redis):
    _, token = await _setup_agent_with_chat_scope()

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "   "},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 422
    assert "leer" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_telegram_report_rejects_over_4000_chars(client, fake_redis):
    _, token = await _setup_agent_with_chat_scope()

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "x" * 4001},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 422
    assert "4000" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_telegram_report_surfaces_parse_error(client, fake_redis):
    """Telegram parse_mode Fehler wird 1:1 an Agent durchgereicht fuer Self-Correct."""
    _, token = await _setup_agent_with_chat_scope()

    mock_service = AsyncMock()
    mock_service.configured = True
    mock_service.send.return_value = {
        "ok": False,
        "description": "Bad Request: can't parse entities: Unexpected end tag",
    }

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "<b>oops unclosed"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 422
    assert "parse entities" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_telegram_report_requires_chat_write_scope(client, fake_redis):
    """Agent ohne chat:write bekommt 403."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="NoScope", slug=f"ns-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=uuid.uuid4(), name="Silent", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read"],  # KEIN chat:write
            provision_status="provisioned",
        ))
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/telegram/send",
        json={"text": "hi"},
        headers={"Authorization": f"Bearer {token_raw}"},
    )
    assert resp.status_code == 403


# ── /telegram/send mit --photo (Screenshot-Deliverable) ────────────────────────


async def _setup_agent_with_screenshot(deliverable_path: str, agent_id_override=None):
    """Setup: Agent + Task + Screenshot-Deliverable."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.deliverable import TaskDeliverable
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = agent_id_override or uuid.uuid4()
    task_id = uuid.uuid4()
    deliv_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="PhotoTest", slug=f"pt-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Photographer", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["chat:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Photo Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        s.add(TaskDeliverable(
            id=deliv_id, task_id=task_id, agent_id=agent_id,
            title="Test Screenshot",
            deliverable_type="screenshot",
            path=deliverable_path,
        ))
        await s.commit()
    return board_id, token_raw, deliv_id, agent_id


@pytest.mark.asyncio
async def test_telegram_photo_attach_calls_send_photo(client, fake_redis, tmp_path):
    """Mit deliverable_id ruft Backend send_photo statt send."""
    # File anlegen so dass _resolve_deliverable_fs_path es findet
    fake_png = tmp_path / "test.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    # Path im Format /shared-mcp/<task_id>/test.png — Resolver nutzt /shared-mcp prefix
    # Wir mocken _resolve_deliverable_fs_path damit wir nicht mit echten Mounts kämpfen.
    _, token, deliv_id, _ = await _setup_agent_with_screenshot(
        f"/shared-mcp/sometask/test.png"
    )

    mock_service = AsyncMock()
    mock_service.configured = True
    mock_service.send_photo.return_value = {"ok": True, "result": {"message_id": 99}}

    with patch("app.services.telegram_reports.telegram_reports", mock_service), \
         patch("app.routers.tasks._resolve_deliverable_fs_path", AsyncMock(return_value=str(fake_png))):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "Caption hier", "deliverable_id": str(deliv_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    mock_service.send_photo.assert_called_once()
    # send (text-only) wurde NICHT aufgerufen
    mock_service.send.assert_not_called()
    # Caption ist der text-Wert
    call_args = mock_service.send_photo.call_args
    assert call_args.kwargs.get("caption") == "Caption hier" or "Caption hier" in str(call_args)


@pytest.mark.asyncio
async def test_telegram_photo_rejects_non_screenshot(client, fake_redis):
    """Deliverable mit type != screenshot → 422."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.deliverable import TaskDeliverable
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    deliv_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="PhotoRej", slug=f"pr-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Reporter", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["chat:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        s.add(TaskDeliverable(
            id=deliv_id, task_id=task_id, agent_id=agent_id,
            title="Doc not photo",
            deliverable_type="document",  # NICHT screenshot
            content="some text",
        ))
        await s.commit()

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "x", "deliverable_id": str(deliv_id)},
            headers={"Authorization": f"Bearer {token_raw}"},
        )

    assert resp.status_code == 422
    assert "screenshot" in resp.json()["detail"].lower()
    mock_service.send_photo.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_photo_404_for_missing_deliverable(client, fake_redis):
    """Nicht-existierende deliverable_id → 404."""
    _, token = await _setup_agent_with_chat_scope()
    fake_id = uuid.uuid4()

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "x", "deliverable_id": str(fake_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404


# ── /telegram/send mit --file (Document-Deliverable: PDF/Office/etc.) ──────────


async def _setup_agent_with_document(deliverable_path: str, dtype: str = "document"):
    """Setup: Agent + Task + File-Deliverable (default type=document)."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.deliverable import TaskDeliverable
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    deliv_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="DocTest", slug=f"dt-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Documenter", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["chat:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Doc Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        s.add(TaskDeliverable(
            id=deliv_id, task_id=task_id, agent_id=agent_id,
            title="Test Document",
            deliverable_type=dtype,
            path=deliverable_path,
        ))
        await s.commit()
    return board_id, token_raw, deliv_id, agent_id


@pytest.mark.asyncio
async def test_telegram_file_attach_calls_send_document(client, fake_redis, tmp_path):
    """Mit document_deliverable_id ruft Backend send_document statt send."""
    fake_pdf = tmp_path / "report.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n" + b"\x00" * 200)

    _, token, deliv_id, _ = await _setup_agent_with_document(
        f"/deliverables/sometask/report.pdf"
    )

    mock_service = AsyncMock()
    mock_service.configured = True
    mock_service.send_document.return_value = {"ok": True, "result": {"message_id": 123}}

    with patch("app.services.telegram_reports.telegram_reports", mock_service), \
         patch("app.routers.tasks._resolve_deliverable_fs_path", AsyncMock(return_value=str(fake_pdf))):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "Q1 Report anbei", "document_deliverable_id": str(deliv_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["message_id"] == 123
    mock_service.send_document.assert_called_once()
    mock_service.send.assert_not_called()
    mock_service.send_photo.assert_not_called()
    call_args = mock_service.send_document.call_args
    assert call_args.kwargs.get("caption") == "Q1 Report anbei" or "Q1 Report anbei" in str(call_args)


@pytest.mark.asyncio
async def test_telegram_file_rejects_url_type(client, fake_redis):
    """Deliverable mit type=url → 422 (kein File-Pfad sendbar)."""
    _, token, deliv_id, _ = await _setup_agent_with_document(
        "https://example.com/foo", dtype="url"
    )

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "x", "document_deliverable_id": str(deliv_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 422
    assert "url" in resp.json()["detail"].lower()
    mock_service.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_file_404_for_missing_deliverable(client, fake_redis):
    """Nicht-existierende document_deliverable_id → 404."""
    _, token = await _setup_agent_with_chat_scope()
    fake_id = uuid.uuid4()

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "x", "document_deliverable_id": str(fake_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_telegram_photo_and_file_mutex(client, fake_redis):
    """deliverable_id + document_deliverable_id gleichzeitig → 422."""
    _, token = await _setup_agent_with_chat_scope()

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={
                "text": "x",
                "deliverable_id": str(uuid.uuid4()),
                "document_deliverable_id": str(uuid.uuid4()),
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 422
    assert "schliessen sich aus" in resp.json()["detail"].lower() or "mutex" in resp.json()["detail"].lower() or "schließen" in resp.json()["detail"].lower()
    mock_service.send_photo.assert_not_called()
    mock_service.send_document.assert_not_called()
    mock_service.send.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_file_unresolved_path_returns_422(client, fake_redis):
    """Deliverable ohne resolvbaren Pfad → 422."""
    _, token, deliv_id, _ = await _setup_agent_with_document(
        "/deliverables/unknown/missing.pdf"
    )

    mock_service = AsyncMock()
    mock_service.configured = True

    with patch("app.services.telegram_reports.telegram_reports", mock_service), \
         patch("app.routers.tasks._resolve_deliverable_fs_path", AsyncMock(return_value=None)):
        resp = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "x", "document_deliverable_id": str(deliv_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 422
    assert "aufloesbar" in resp.json()["detail"].lower() or "auflösbar" in resp.json()["detail"].lower()
    mock_service.send_document.assert_not_called()
