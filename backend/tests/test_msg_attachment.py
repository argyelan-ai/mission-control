"""mc msg --vault-path — a file travels with a chat message (Slack-Umbau R3).

Boss shows the operator a file IN the conversation: the CLI sends a vault
wrapper path, the endpoint resolves it (same guards as the report path — one
resolver), appends a visible 📎 line to the stored body and hands the
attachment to the chat mirror. The mirror strips it for channels without the
``files`` capability (TCK law) — here we pin the endpoint half: resolution,
the 📎 line, the guard, and that the attachment actually reaches the mirror.
"""
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import frontmatter as fm_lib
import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.thread import Message
from tests.conftest import test_engine


async def _agent(session: AsyncSession) -> tuple[Agent, str]:
    raw, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Boss-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        comm_v2=True,
        scopes=["chat:write", "tasks:read", "tasks:write"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "vault"
    (p / "attachments" / "files").mkdir(parents=True)
    monkeypatch.setattr(app.config.settings, "vault_path", p)
    return p


def _wrapper_with_pdf(vault: Path) -> tuple[str, Path]:
    """Wrapper + PDF wie im Voice-Concierge-Pfad. Returns (rel wrapper, pdf)."""
    pdf_abs = vault / "attachments" / "files" / "beleg.pdf"
    pdf_abs.write_bytes(b"%PDF-1.4 msg-attachment")
    wrapper_abs = vault / "agents" / "boss" / "deliverables" / "beleg.md"
    wrapper_abs.parent.mkdir(parents=True)
    rel = "../../../attachments/files/beleg.pdf"
    post = fm_lib.Post(
        "# Beleg\n", type="deliverable", attachment_path=rel,
        attachment_mime="application/pdf",
    )
    wrapper_abs.write_text(fm_lib.dumps(post))
    return str(wrapper_abs.relative_to(vault)), pdf_abs


@pytest.mark.asyncio
async def test_msg_with_vault_path_mirrors_the_file_and_says_so(
    client: AsyncClient, vault: Path
):
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        thread = await ensure_dm_thread(s, agent)
    wrapper_rel, pdf_abs = _wrapper_with_pdf(vault)

    with patch(
        "app.services.chat_outbound.mirror_message_to_all", new_callable=AsyncMock,
        return_value=1,
    ) as mirror:
        resp = await client.post(
            f"/api/v1/agent/threads/{thread.id}/messages",
            json={"body": "Beleg wie besprochen", "vault_path": wrapper_rel},
            headers=_auth(token),
        )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        msg = (
            await s.exec(select(Message).where(Message.thread_id == thread.id))
        ).one()
    assert "Beleg wie besprochen" in msg.body
    assert "📎 beleg.pdf" in msg.body, (
        "the stored body must name the file — thread readers have no mirror"
    )

    mirror.assert_awaited_once()
    attachment = mirror.await_args.kwargs["attachment"]
    assert attachment is not None and attachment.path == str(pdf_abs)
    assert attachment.mime == "application/pdf"
    assert attachment.title == "beleg.pdf"


@pytest.mark.asyncio
async def test_msg_on_current_task_route_carries_the_attachment_too(
    client: AsyncClient, vault: Path
):
    """Beide mc-msg-Routen nutzen denselben Aufloeser — ohne aktiven Task
    faellt /tasks/current/messages auf den DM-Thread zurueck (sofern einer
    existiert) und muss die Datei genauso mitnehmen."""
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        await ensure_dm_thread(s, agent)
    wrapper_rel, pdf_abs = _wrapper_with_pdf(vault)

    with patch(
        "app.services.chat_outbound.mirror_message_to_all", new_callable=AsyncMock,
        return_value=1,
    ) as mirror:
        resp = await client.post(
            "/api/v1/agent/tasks/current/messages",
            json={"body": "Beleg anbei", "vault_path": wrapper_rel},
            headers=_auth(token),
        )

    assert resp.status_code == 201, resp.text
    mirror.assert_awaited_once()
    attachment = mirror.await_args.kwargs["attachment"]
    assert attachment is not None and attachment.path == str(pdf_abs)


@pytest.mark.asyncio
async def test_msg_vault_path_outside_the_root_is_refused(
    client: AsyncClient, vault: Path
):
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        thread = await ensure_dm_thread(s, agent)

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "x", "vault_path": "../../../etc/passwd"},
        headers=_auth(token),
    )

    assert resp.status_code in (400, 404), resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        msgs = (
            await s.exec(select(Message).where(Message.thread_id == thread.id))
        ).all()
    assert msgs == [], "a refused attachment must not half-post the message"


@pytest.mark.asyncio
async def test_msg_without_vault_path_is_byte_for_byte_unchanged(
    client: AsyncClient,
):
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        thread = await ensure_dm_thread(s, agent)

    with patch(
        "app.services.chat_outbound.mirror_message_to_all", new_callable=AsyncMock,
        return_value=1,
    ) as mirror:
        resp = await client.post(
            f"/api/v1/agent/threads/{thread.id}/messages",
            json={"body": "nur Text"},
            headers=_auth(token),
        )

    assert resp.status_code == 201, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        msg = (
            await s.exec(select(Message).where(Message.thread_id == thread.id))
        ).one()
    assert msg.body == "nur Text"
    assert mirror.await_args.kwargs["attachment"] is None
