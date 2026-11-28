"""The agent's way to ask for a capability (extensions concept, PR 1).

Covers what `mc ext` / the MCP tool `ext_request` rely on:

- an optional ``chat_id`` on the request: stored in the approval payload, and
  only the requester's own chat is accepted;
- one system line in that chat when the request is rejected, installed or
  fails (MC's chat record, the same store a chat switch writes into);
- without chat_id (and without task_id) nothing is written — old behaviour;
- a GitHub skill is inspected at its pinned commit: only plain text passes,
  anything executable is refused before the operator ever sees it;
- GET status (own requests only) and GET search (local skills library).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

PINNED = "0123456789abcdef0123456789abcdef01234567"


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_network(tmp_path, monkeypatch):
    """Skills library in a temp dir; pin and fetch without GitHub. The fake
    fetch writes whatever ``files`` holds into the clone."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    files: dict[str, str | bytes] = {"SKILL.md": "---\nname: x\n---\nplain text\n"}

    async def fake_resolve(source, ref):
        return PINNED

    async def fake_fetch(url, commit, dest):
        dest.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            p = Path(dest) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content)
        return commit or PINNED

    monkeypatch.setattr("app.services.install_executor.resolve_skill_commit", fake_resolve)
    monkeypatch.setattr("app.services.install_executor._fetch_commit", fake_fetch)
    return files


@pytest.fixture
def record_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


async def _board() -> uuid.UUID:
    from app.models.board import Board

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        b = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(b)
        await s.commit()
        return b.id


async def _agent(name: str, board_id: uuid.UUID, scopes: list[str] | None = None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw, hashed = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = Agent(name=name, role="developer", scopes=scopes or [], board_id=board_id,
                  agent_token_hash=hashed, cli_skills=None)
        s.add(a)
        await s.commit()
        await s.refresh(a)
    return a, raw


async def _chat(agent_id: uuid.UUID, *, with_session: bool = True) -> uuid.UUID:
    from app.models.chat_record import Chat, ChatSession

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = Chat(title="c", harness="claude", legacy_agent_id=agent_id,
                    native_session_id="native-1")
        s.add(chat)
        await s.flush()
        if with_session:
            s.add(ChatSession(chat_id=chat.id, native_session_id="native-1",
                              harness="claude"))
        await s.commit()
        return chat.id


async def _chat_lines(chat_id: uuid.UUID) -> list[dict]:
    from app.models.chat_record import ChatEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(ChatEvent).where(ChatEvent.chat_id == chat_id)
                             .order_by(ChatEvent.seq))).all()
        return [r.payload for r in rows]


async def _request(client: AsyncClient, token: str, target_id, **extra):
    body = {
        "type": "skill",
        "source": "github:anthropic/some-skill",
        "name": "some-skill",
        "target_agent_id": str(target_id),
        "reason": "needed for the paperless filing chat",
        **extra,
    }
    return await client.post("/api/v1/agent/install-requests",
                             headers={"Authorization": f"Bearer {token}"}, json=body)


async def _payload(approval_id: str) -> dict:
    from app.models.approval import Approval

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.get(Approval, uuid.UUID(approval_id))).payload


def _result(result: str = "success", error: str | None = None):
    r = MagicMock()
    r.result = result
    r.error = error
    r.installed_version = "abc123"
    r.install_log_id = uuid.uuid4()
    return r


async def _resolve(auth_client, approval_id, status, result=None, crash=None):
    with patch("app.routers.approvals.InstallExecutor") as cls:
        ex = MagicMock()
        ex.execute = AsyncMock(return_value=result, side_effect=crash)
        cls.return_value = ex
        resp = await auth_client.patch(f"/api/v1/approvals/{approval_id}",
                                       json={"status": status, "resolver_note": "n"})
    assert resp.status_code == 200, resp.text
    return resp


# ── chat_id on the request ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_id_is_stored_in_the_approval_payload(client, fake_redis):
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id)
    resp = await _request(client, tok, me.id, chat_id=str(chat_id))
    assert resp.status_code == 201, resp.text
    payload = await _payload(resp.json()["approval_id"])
    assert payload["requester_chat_id"] == str(chat_id)


@pytest.mark.asyncio
async def test_chat_of_another_agent_is_refused(client, fake_redis):
    board = await _board()
    me, tok = await _agent("A", board)
    other, _ = await _agent("B", board)
    foreign_chat = await _chat(other.id)
    resp = await _request(client, tok, me.id, chat_id=str(foreign_chat))
    assert resp.status_code == 404, resp.text
    assert "chat" in resp.text.lower()


# ── feedback line in the chat ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_request_posts_one_line_into_the_chat(client, auth_client, fake_redis, record_on):
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id)
    approval_id = (await _request(client, tok, me.id, chat_id=str(chat_id))).json()["approval_id"]

    await _resolve(auth_client, approval_id, "rejected")

    lines = await _chat_lines(chat_id)
    assert len(lines) == 1
    line = lines[0]
    assert line["role"] == "teammate" and line["source"]["kind"] == "system"
    assert line["install"]["outcome"] == "rejected"
    assert "some-skill" in line["text"] and "rejected" in line["text"]


@pytest.mark.asyncio
async def test_installed_request_posts_one_line_into_the_chat(client, auth_client, fake_redis, record_on):
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id)
    approval_id = (await _request(client, tok, me.id, chat_id=str(chat_id))).json()["approval_id"]

    await _resolve(auth_client, approval_id, "approved", result=_result("success"))

    lines = await _chat_lines(chat_id)
    assert [ln["install"]["outcome"] for ln in lines] == ["installed"]
    assert "next chat start" in lines[0]["text"]


@pytest.mark.asyncio
async def test_failed_install_posts_the_reason_into_the_chat(client, auth_client, fake_redis, record_on):
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id)
    approval_id = (await _request(client, tok, me.id, chat_id=str(chat_id))).json()["approval_id"]

    await _resolve(auth_client, approval_id, "approved", result=_result("failed", "no SKILL.md"))

    lines = await _chat_lines(chat_id)
    assert [ln["install"]["outcome"] for ln in lines] == ["failed"]
    assert "no SKILL.md" in lines[0]["text"]


@pytest.mark.asyncio
async def test_executor_crash_still_posts_a_failed_line(client, auth_client, fake_redis, record_on):
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id)
    approval_id = (await _request(client, tok, me.id, chat_id=str(chat_id))).json()["approval_id"]

    await _resolve(auth_client, approval_id, "approved", crash=RuntimeError("boom"))

    lines = await _chat_lines(chat_id)
    assert [ln["install"]["outcome"] for ln in lines] == ["failed"]


@pytest.mark.asyncio
async def test_without_chat_id_no_chat_line_is_written(client, auth_client, fake_redis, record_on):
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id)
    approval_id = (await _request(client, tok, me.id)).json()["approval_id"]
    assert (await _payload(approval_id)).get("requester_chat_id") is None

    await _resolve(auth_client, approval_id, "rejected")

    assert await _chat_lines(chat_id) == []


@pytest.mark.asyncio
async def test_chat_without_any_session_is_skipped_not_crashed(client, auth_client, fake_redis, record_on):
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id, with_session=False)
    approval_id = (await _request(client, tok, me.id, chat_id=str(chat_id))).json()["approval_id"]

    await _resolve(auth_client, approval_id, "rejected")

    assert await _chat_lines(chat_id) == []


# ── text only: the pinned commit is inspected ────────────────────────────


@pytest.mark.asyncio
async def test_skill_with_a_script_is_refused(client, fake_redis, _no_network):
    _no_network["scripts/run.sh"] = "#!/bin/sh\necho hi\n"
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _request(client, tok, me.id)
    assert resp.status_code == 400, resp.text
    assert "scripts/run.sh" in resp.text


@pytest.mark.asyncio
async def test_skill_with_a_binary_file_is_refused(client, fake_redis, _no_network):
    _no_network["notes.md"] = b"\x00\x01\x02binary"
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _request(client, tok, me.id)
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_text_only_skill_passes_and_names_its_files(client, fake_redis, _no_network):
    _no_network["reference/guide.md"] = "# guide\n"
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _request(client, tok, me.id)
    assert resp.status_code == 201, resp.text
    payload = await _payload(resp.json()["approval_id"])
    assert payload["files"] == ["SKILL.md", "reference/guide.md"]


@pytest.mark.asyncio
async def test_only_the_requested_skill_folder_of_a_monorepo_is_inspected(client, fake_redis, _no_network):
    _no_network.clear()
    _no_network["skills/some-skill/SKILL.md"] = "---\nname: some-skill\n---\n"
    _no_network["skills/other/tool.py"] = "print('x')\n"
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _request(client, tok, me.id)
    assert resp.status_code == 201, resp.text
    assert (await _payload(resp.json()["approval_id"]))["files"] == ["SKILL.md"]


# ── status + search ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_shows_the_requesters_own_request(client, auth_client, fake_redis):
    board = await _board()
    me, tok = await _agent("A", board)
    approval_id = (await _request(client, tok, me.id)).json()["approval_id"]
    await _resolve(auth_client, approval_id, "rejected")

    resp = await client.get(f"/api/v1/agent/install-requests/{approval_id}",
                            headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["name"] == "some-skill"
    assert body["commit"] == PINNED

    listed = await client.get("/api/v1/agent/install-requests",
                              headers={"Authorization": f"Bearer {tok}"})
    assert [r["approval_id"] for r in listed.json()["requests"]] == [approval_id]


@pytest.mark.asyncio
async def test_status_of_a_foreign_request_is_not_found(client, fake_redis):
    board = await _board()
    me, tok = await _agent("A", board)
    other, other_tok = await _agent("B", board)
    approval_id = (await _request(client, tok, me.id)).json()["approval_id"]

    resp = await client.get(f"/api/v1/agent/install-requests/{approval_id}",
                            headers={"Authorization": f"Bearer {other_tok}"})
    assert resp.status_code == 404
    listed = await client.get("/api/v1/agent/install-requests",
                              headers={"Authorization": f"Bearer {other_tok}"})
    assert listed.json()["requests"] == []


@pytest.mark.asyncio
async def test_search_finds_a_library_skill_by_its_description(client, fake_redis, tmp_path):
    lib = tmp_path / ".mc" / "skills" / "paperless-filing"
    lib.mkdir(parents=True)
    (lib / "SKILL.md").write_text(
        "---\nname: paperless-filing\ndescription: File scanned letters into Paperless\n---\n"
    )
    (tmp_path / ".mc" / "skills" / "unrelated").mkdir()
    board = await _board()
    me, tok = await _agent("A", board)

    resp = await client.get("/api/v1/agent/install-requests/search",
                            params={"q": "scanned letters"},
                            headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["name"] for s in body["library"]] == ["paperless-filing"]
    assert body["library"][0]["source"] == "~/.mc/skills/paperless-filing"
    assert body["allowed_sources"]


# ── Agents learn the verb (TOOLS.md) ─────────────────────────────────────


def test_every_agent_learns_mc_ext_in_tools_md():
    from app.services.tools_md_builder import generate_tools_md

    tools_md = generate_tools_md(
        name="Cody", emoji="🤖", raw_token="tok", board_id="b1",
        is_board_lead=False, scopes=["tasks:read"],
    )
    assert "mc ext request" in tools_md
    assert "mc ext search" in tools_md and "mc ext status" in tools_md
    assert "never" in tools_md.lower()  # never curl / own shell / self-install
    # Non-managers still see no raw install endpoint (existing guarantee).
    assert "install-requests" not in tools_md
