"""Tests for Telegram approval URL buttons.

Covers:
- send_approval_telegram sends a message with URL buttons (not callback_data)
- Token lifecycle: create, peek, consume, sibling cleanup
- No token configured → skip (no error)
- Quick-resolve GET → confirmation page
- Quick-resolve POST → approval resolved
- Double-click → token consumed, second click intercepted
- UI resolution → Telegram message is edited
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────


async def _create_approval_data():
    """Create board + agent + task + approval."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.approval import Approval
    from app.auth import generate_agent_token
    from app.utils import utcnow

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    approval_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="TG Board", slug="tg-board")
        s.add(board)

        token_raw, token_hash = generate_agent_token()
        agent = Agent(
            id=agent_id,
            name="Cody",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
        )
        s.add(agent)

        task = Task(
            id=task_id,
            board_id=board_id,
            title="Fix critical bug",
            status="blocked",
            assigned_agent_id=agent_id,
        )
        s.add(task)

        approval = Approval(
            id=approval_id,
            board_id=board_id,
            task_id=task_id,
            agent_id=agent_id,
            action_type="blocker_decision",
            description="Cody blocked bei Fix critical bug",
            status="pending",
            payload={
                "blocked_agent_name": "Cody",
                "blocker_comment": "Dependency fehlt",
                "task_title": "Fix critical bug",
            },
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()
        for obj in [board, agent, task, approval]:
            await s.refresh(obj)

    return {
        "board": board,
        "agent": agent,
        "task": task,
        "approval": approval,
        "token": token_raw,
    }


# ── Test: Send with URL Buttons ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_approval_with_url_buttons(fake_redis):
    """send_approval_telegram sends a message with 2 URL buttons (not callback_data)."""
    from app.services.telegram_bot import TelegramBotService

    bot = TelegramBotService()
    approval_id = uuid.uuid4()

    with patch("app.services.telegram_bot.settings") as mock_settings:
        mock_settings.telegram_bot_token = "test-token"
        mock_settings.telegram_chat_id = "12345"
        mock_settings.mc_base_url = "http://100.100.100.100"

        with patch.object(bot, "send_message", new_callable=AsyncMock, return_value=42) as mock_send:
            with patch("app.services.telegram_bot.get_redis", return_value=fake_redis):
                await bot.send_approval_telegram(
                    approval_id, "Cody", "Fix critical bug", "Dependency fehlt"
                )

            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            text = args[0]
            markup = args[1]

            # Check text
            assert "Cody" in text
            assert "Fix critical bug" in text
            assert "Dependency fehlt" in text

            # Check URL buttons (NOT callback_data)
            buttons = markup["inline_keyboard"][0]
            assert len(buttons) == 2
            assert buttons[0]["text"] == "Entblocken"
            assert "url" in buttons[0]
            assert "callback_data" not in buttons[0]
            assert f"/approvals/{approval_id}/quick-resolve" in buttons[0]["url"]
            assert buttons[1]["text"] == "Abbrechen"
            assert "url" in buttons[1]
            assert "callback_data" not in buttons[1]

    # Redis message_id stored
    stored = await fake_redis.get(f"mc:telegram:approval:{approval_id}")
    assert stored == "42"


# ── Test: Token Lifecycle ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_create_peek_consume(fake_redis):
    """Create token, peek (without consuming), consume (once)."""
    from app.services.telegram_bot import (
        create_approval_tokens,
        peek_action_token,
        consume_action_token,
    )

    approval_id = uuid.uuid4()

    with patch("app.services.telegram_bot.get_redis", return_value=fake_redis):
        approve_token, reject_token = await create_approval_tokens(approval_id)

        # Peek: token readable, but not consumed
        data = await peek_action_token(approve_token)
        assert data is not None
        assert data["approval_id"] == str(approval_id)
        assert data["action"] == "approve"

        # Peek again → still there
        data2 = await peek_action_token(approve_token)
        assert data2 is not None

        # Consume: token is deleted + sibling too
        result = await consume_action_token(approve_token)
        assert result is not None
        assert result["action"] == "approve"

        # Second consume → None (already used)
        result2 = await consume_action_token(approve_token)
        assert result2 is None

        # Sibling (reject) also gone
        result3 = await consume_action_token(reject_token)
        assert result3 is None


# ── Test: No Token → skip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_token_skips_silently(fake_redis):
    """When no token is configured, nothing happens (no error)."""
    from app.services.telegram_bot import TelegramBotService

    bot = TelegramBotService()

    with patch.object(bot, "send_message", new_callable=AsyncMock) as mock_send:
        await bot.send_approval_telegram(
            uuid.uuid4(), "Cody", "Task", "Blocker"
        )
        mock_send.assert_not_called()


# ── Test: Quick-Resolve GET (Confirmation Page) ────────────────────────


@pytest.mark.asyncio
async def test_quick_resolve_get_shows_confirmation(client, fake_redis):
    """GET quick-resolve shows confirmation page with approval details."""
    data = await _create_approval_data()
    approval_id = data["approval"].id

    from app.services.telegram_bot import create_approval_tokens
    with patch("app.services.telegram_bot.get_redis", return_value=fake_redis):
        approve_token, _ = await create_approval_tokens(approval_id)

    with patch("app.routers.approvals.peek_action_token") as mock_peek:
        mock_peek.return_value = {"approval_id": str(approval_id), "action": "approve"}
        resp = await client.get(
            f"/api/v1/approvals/{approval_id}/quick-resolve",
            params={"token": approve_token},
        )

    assert resp.status_code == 200
    body = resp.text
    assert "Entblocken" in body
    assert "Cody" in body
    assert "Fix critical bug" in body


# ── Test: Quick-Resolve POST (Consume Token) ────────────────────────


@pytest.mark.asyncio
async def test_quick_resolve_post_resolves_approval(client, fake_redis):
    """POST quick-resolve consumes token and resolves approval."""
    data = await _create_approval_data()
    approval_id = data["approval"].id

    from app.services.telegram_bot import create_approval_tokens
    with patch("app.services.telegram_bot.get_redis", return_value=fake_redis):
        approve_token, _ = await create_approval_tokens(approval_id)

    with (
        patch("app.routers.approvals.consume_action_token") as mock_consume,
        patch("app.routers.approvals.emit_event", new_callable=AsyncMock),
        patch("app.utils.create_tracked_task"),
        patch("app.routers.approvals.telegram_bot") as mock_tg,
    ):
        mock_consume.return_value = {"approval_id": str(approval_id), "action": "approve"}
        mock_tg.update_resolved_telegram = AsyncMock()
        resp = await client.post(
            f"/api/v1/approvals/{approval_id}/quick-resolve/confirm",
            data={"token": approve_token},
        )

    assert resp.status_code == 200
    assert "Entblockt" in resp.text

    # Check DB
    from app.models.approval import Approval
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = await s.get(Approval, approval_id)
        assert approval.status == "approved"
        assert "Telegram link" in approval.resolver_note

        task = await s.get(Task, data["task"].id)
        assert task.status == "inbox"  # Blocker → inbox → auto_dispatch (background)


# ── Test: Double-Click → Token Consumed ─────────────────────────────────


@pytest.mark.asyncio
async def test_double_click_token_consumed(client, fake_redis):
    """Second click after token consumption is intercepted."""
    data = await _create_approval_data()
    approval_id = data["approval"].id

    call_count = 0

    async def mock_consume_side_effect(token):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"approval_id": str(approval_id), "action": "approve"}
        return None  # Second call: already consumed

    with (
        patch("app.routers.approvals.consume_action_token", side_effect=mock_consume_side_effect),
        patch("app.routers.approvals.emit_event", new_callable=AsyncMock),
        patch("app.routers.approvals.telegram_bot") as mock_tg,
    ):
        mock_tg.update_resolved_telegram = AsyncMock()

        # First click: approve
        resp1 = await client.post(
            f"/api/v1/approvals/{approval_id}/quick-resolve/confirm",
            data={"token": "token-a"},
        )
        assert resp1.status_code == 200

        # Second click: reject token → consumed
        resp2 = await client.post(
            f"/api/v1/approvals/{approval_id}/quick-resolve/confirm",
            data={"token": "token-b"},
        )
        assert resp2.status_code == 410
        assert "abgelaufen" in resp2.text or "benutzt" in resp2.text


# ── Test: UI Resolution Edits Telegram Message ────────────────────────


@pytest.mark.asyncio
async def test_ui_resolution_updates_telegram(fake_redis):
    """When the operator approves in the dashboard, the Telegram message is edited."""
    from app.services.telegram_bot import TelegramBotService

    bot = TelegramBotService()
    approval_id = uuid.uuid4()

    # Simulate message_id in Redis
    await fake_redis.set(f"mc:telegram:approval:{approval_id}", "42")

    with patch("app.services.telegram_bot.settings") as mock_settings:
        mock_settings.telegram_bot_token = "test-token"
        mock_settings.telegram_chat_id = "12345"

        with patch("app.services.telegram_bot.get_redis", return_value=fake_redis):
            with patch.object(bot, "edit_message_text", new_callable=AsyncMock, return_value=True) as mock_edit:
                await bot.update_resolved_telegram(
                    approval_id, "approved", "Go installieren"
                )

                mock_edit.assert_called_once()
                args = mock_edit.call_args[0]
                assert args[0] == 42  # message_id
                text = args[1]
                assert "approved" in text
                assert "Dashboard" in text

    # Redis key deleted
    stored = await fake_redis.get(f"mc:telegram:approval:{approval_id}")
    assert stored is None


# ── Test: Expired Token GET → Error Page ────────────────────────────────


@pytest.mark.asyncio
async def test_expired_token_get_shows_error(client, fake_redis):
    """GET with invalid/expired token shows error page."""
    approval_id = uuid.uuid4()

    with patch("app.routers.approvals.peek_action_token") as mock_peek:
        mock_peek.return_value = None  # Token expired/invalid
        resp = await client.get(
            f"/api/v1/approvals/{approval_id}/quick-resolve",
            params={"token": "invalid-token-xyz"},
        )

    assert resp.status_code == 410
    assert "abgelaufen" in resp.text or "ungueltig" in resp.text.lower()


# ── Test: Polling Disabled ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_polling_disabled():
    """start() no longer starts a poller (no-op)."""
    from app.services.telegram_bot import TelegramBotService

    bot = TelegramBotService()

    with patch("app.services.telegram_bot.settings") as mock_settings:
        mock_settings.telegram_bot_token = "test-token"
        mock_settings.telegram_chat_id = "12345"

        await bot.start()

    # No task started
    assert bot._task is None
    assert not bot._running
