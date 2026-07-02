"""Tests for the consolidated /agent/me/telegram endpoint with vault_path.

Voice-Concierge ships vault wrappers to the operator's Telegram via the existing
/me/telegram endpoint by passing ``vault_path`` (instead of deliverable_id
or document_deliverable_id). Backend reads the wrapper frontmatter, resolves
attachment_path to a real binary under vault/attachments/, and hands off to
telegram_reports.send_document().

This file replaces the deleted /vault/deliver-to-telegram endpoint tests —
one Telegram endpoint, three input modes.
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import frontmatter as fm_lib
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.scopes import Scope
from tests.conftest import test_engine


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "vault"
    p.mkdir()
    for kind in ("files", "images", "audio"):
        (p / "attachments" / kind).mkdir(parents=True)
    monkeypatch.setattr(app.config.settings, "vault_path", p)
    return p


@pytest.fixture
async def voice_setup(vault_path):
    """Agent with chat:write scope + a board + a current task (so the
    flag-claim path doesn't trip)."""
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="VoiceBoard", slug=f"voice-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id, name="Jarvis", role="orchestrator",
            board_id=board_id,
            agent_token_hash=token_hash,
            scopes=[Scope.CHAT_WRITE.value, Scope.TASKS_READ.value, Scope.TASKS_WRITE.value],
            is_board_lead=True,
            current_task_id=task_id,
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Jarvis Concierge Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()

    return raw_token, vault_path


@contextlib.asynccontextmanager
async def _mock_telegram(send_document_return=None):
    """Patch telegram_reports across both import paths so the in-route
    `from app.services.telegram_reports import telegram_reports` lookup
    sees the mock."""
    payload = send_document_return if send_document_return is not None else {
        "ok": True, "result": {"message_id": 4242},
    }
    mock_tg = MagicMock()
    mock_tg.configured = True
    mock_tg.send = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
    mock_tg.send_photo = AsyncMock(return_value={"ok": True, "result": {"message_id": 2}})
    mock_tg.send_document = AsyncMock(return_value=payload)
    with patch("app.services.telegram_reports.telegram_reports", mock_tg):
        yield mock_tg


def _make_wrapper_with_pdf(vault_path: Path, *, pdf_size: int = 100) -> tuple[Path, Path]:
    """Build a deliverable wrapper + its hardlinked PDF attachment."""
    pdf_id = "abcd0001-0000-0000-0000-000000000001"
    pdf_abs = vault_path / "attachments" / "files" / f"{pdf_id}.pdf"
    pdf_abs.write_bytes(b"%PDF-1.4 " + b"X" * (pdf_size - 9))

    wrapper_abs = (
        vault_path / "agents" / "researcher" / "deliverables" / f"weather-{pdf_id}.md"
    )
    wrapper_abs.parent.mkdir(parents=True)
    rel = f"../../../attachments/files/{pdf_id}.pdf"
    post = fm_lib.Post(
        f"# Weather\n\n![[{rel}]]\n",
        id=f"deliverable-{pdf_id}",
        title="Weather Report",
        agent="researcher",
        type="deliverable",
        deliverable_kind="file",
        deliverable_id=pdf_id,
        date="2026-05-15T13:00:00+00:00",
        attachment_path=rel,
        attachment_mime="application/pdf",
        attachment_size=pdf_size,
    )
    wrapper_abs.write_text(fm_lib.dumps(post))
    return wrapper_abs, pdf_abs


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vault_path_happy_path(client, fake_redis, voice_setup):
    """POST /me/telegram with vault_path → resolves wrapper → ships attachment."""
    raw_token, vault_path = voice_setup
    wrapper, pdf = _make_wrapper_with_pdf(vault_path)
    rel_wrapper = str(wrapper.relative_to(vault_path))

    async with _mock_telegram() as tg:
        r = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "Weather Report", "vault_path": rel_wrapper},
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["message_id"] == 4242

    tg.send_document.assert_awaited_once()
    args, kwargs = tg.send_document.call_args
    assert args[0] == str(pdf)
    assert kwargs["caption"] == "Weather Report"


@pytest.mark.asyncio
async def test_vault_path_blocks_traversal(client, fake_redis, voice_setup):
    """vault_path with .. components must be rejected before any read."""
    raw_token, _ = voice_setup
    async with _mock_telegram():
        r = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "x", "vault_path": "../../etc/passwd"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_vault_path_404_for_missing_wrapper(client, fake_redis, voice_setup):
    raw_token, _ = voice_setup
    async with _mock_telegram():
        r = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "x", "vault_path": "agents/x/deliverables/does-not-exist.md"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_vault_path_rejects_wrapper_without_attachment(client, fake_redis, voice_setup):
    """Document/url kinds have no binary — return 400 with a clear hint
    so Jarvis can read inline instead of promising a file."""
    raw_token, vault_path = voice_setup
    wrapper = vault_path / "agents" / "researcher" / "deliverables" / "doc.md"
    wrapper.parent.mkdir(parents=True)
    post = fm_lib.Post(
        "# Doc\n\nbody",
        id="deliverable-doc",
        title="Doc",
        agent="researcher",
        type="deliverable",
        deliverable_kind="document",
        date="2026-05-15T13:00:00+00:00",
    )
    wrapper.write_text(fm_lib.dumps(post))

    async with _mock_telegram():
        r = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "x", "vault_path": str(wrapper.relative_to(vault_path))},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert r.status_code == 400
    assert "attachment_path" in r.text


@pytest.mark.asyncio
async def test_vault_path_surfaces_telegram_too_large(client, fake_redis, voice_setup):
    """send_document returns {ok:False, description:...} on 50MB-overflow.
    The endpoint surfaces that as 422 (existing /telegram/send semantics)."""
    raw_token, vault_path = voice_setup
    wrapper, _ = _make_wrapper_with_pdf(vault_path)
    rel_wrapper = str(wrapper.relative_to(vault_path))

    async with _mock_telegram(send_document_return={
        "ok": False,
        "description": "file too large: 60_000_000 bytes exceeds Telegram limit",
    }):
        r = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "x", "vault_path": rel_wrapper},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert r.status_code == 422
    assert "too large" in r.text


@pytest.mark.asyncio
async def test_vault_path_mutex_with_deliverable_id(client, fake_redis, voice_setup):
    """vault_path + deliverable_id at once → 422 (mutex rule)."""
    raw_token, _ = voice_setup
    async with _mock_telegram():
        r = await client.post(
            "/api/v1/agent/me/telegram",
            json={
                "text": "x",
                "vault_path": "agents/x/anything.md",
                "deliverable_id": str(uuid.uuid4()),
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert r.status_code == 422
    assert "schliessen sich aus" in r.text


@pytest.mark.asyncio
async def test_vault_path_requires_chat_write_scope(client, fake_redis, vault_path):
    """Agent with VAULT_READ but no CHAT_WRITE must get 403 — the consolidated
    endpoint inherits /me/telegram's chat:write gate."""
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="NoChatBoard", slug=f"nc-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=uuid.uuid4(), name="NoScope", role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            scopes=[Scope.VAULT_READ.value],  # no CHAT_WRITE
            provision_status="provisioned",
        ))
        await s.commit()

    async with _mock_telegram():
        r = await client.post(
            "/api/v1/agent/me/telegram",
            json={"text": "x", "vault_path": "agents/x/anything.md"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert r.status_code == 403
