"""Critic rework for the agent path (extensions concept, PR 1).

- a library source (``~/.mc/skills/<name>``) gets the same plain-text check
  as a GitHub source, at request time AND in the executor;
- ``mc ext search`` marks library skills that would be refused;
- the endpoint refuses ``install`` of plugins and MCP servers this round
  (uninstall stays);
- request check and executor pick the skill folder with ONE helper, and the
  executor checks the folder it actually copies;
- a second chat asking for the same pending request is remembered;
- status filters in SQL, file sizes are capped;
- the chat line is held (not written) until it is localised (PR 2).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

PINNED = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    files: dict[str, str | bytes] = {"SKILL.md": "---\nname: x\n---\nplain\n"}

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


def _lib(tmp_path: Path, name: str) -> Path:
    d = tmp_path / ".mc" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: helps with {name}\n---\n")
    return d


async def _board() -> uuid.UUID:
    from app.models.board import Board

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        b = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(b)
        await s.commit()
        return b.id


async def _agent(name: str, board_id, scopes=None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw, hashed = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = Agent(name=name, role="developer", scopes=scopes or [], board_id=board_id,
                  agent_token_hash=hashed, cli_skills=[])
        s.add(a)
        await s.commit()
        await s.refresh(a)
    return a, raw


async def _chat(agent_id) -> uuid.UUID:
    from app.models.chat_record import Chat, ChatSession

    native = f"native-{uuid.uuid4().hex[:8]}"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = Chat(title="c", harness="claude", legacy_agent_id=agent_id,
                    native_session_id=native)
        s.add(chat)
        await s.flush()
        s.add(ChatSession(chat_id=chat.id, native_session_id=native, harness="claude"))
        await s.commit()
        return chat.id


async def _post(client, token, body):
    return await client.post("/api/v1/agent/install-requests",
                             headers={"Authorization": f"Bearer {token}"}, json=body)


def _skill(target, name, source, **kw):
    return {"type": "skill", "operation": "install", "name": name, "source": source,
            "target_agent_id": str(target), "reason": "needed for this chat", **kw}


async def _payload(approval_id: str) -> dict:
    from app.models.approval import Approval

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.get(Approval, uuid.UUID(approval_id))).payload


# ── 1. library source: plain text only ───────────────────────────────────


@pytest.mark.asyncio
async def test_library_skill_with_a_script_is_refused_at_request_time(client, fake_redis, tmp_path):
    d = _lib(tmp_path, "ingest")
    (d / "setup.sh").write_text("#!/bin/sh\n")
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _post(client, tok, _skill(me.id, "ingest", "~/.mc/skills/ingest"))
    assert resp.status_code == 400, resp.text
    assert "setup.sh" in resp.text


@pytest.mark.asyncio
async def test_library_text_skill_passes_and_names_its_files(client, fake_redis, tmp_path):
    d = _lib(tmp_path, "notes")
    (d / "ref.md").write_text("# ref\n")
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _post(client, tok, _skill(me.id, "notes", "~/.mc/skills/notes"))
    assert resp.status_code == 201, resp.text
    assert (await _payload(resp.json()["approval_id"]))["files"] == ["SKILL.md", "ref.md"]


@pytest.mark.asyncio
async def test_library_source_must_name_the_requested_skill(client, fake_redis, tmp_path):
    _lib(tmp_path, "notes")
    _lib(tmp_path, "other")
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _post(client, tok, _skill(me.id, "other", "~/.mc/skills/notes"))
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_missing_library_skill_is_refused_at_request_time(client, fake_redis):
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await _post(client, tok, _skill(me.id, "ghost", "~/.mc/skills/ghost"))
    assert resp.status_code == 404, resp.text


def test_executor_refuses_a_library_skill_that_gained_a_script(tmp_path):
    """Checked again at install time: the folder may change after the request."""
    from app.services.install_executor import SkillContentError, _call_skill_install

    d = _lib(tmp_path, "later")
    (d / "run.py").write_text("print(1)\n")
    with pytest.raises(SkillContentError):
        asyncio.run(_call_skill_install("~/.mc/skills/later", "later"))


def test_executor_refuses_a_github_skill_with_a_script(tmp_path, _env):
    from app.services.install_executor import SkillContentError, _call_skill_install

    _env["tool.sh"] = "#!/bin/sh\n"
    with pytest.raises(SkillContentError):
        asyncio.run(_call_skill_install("github:obra/skill-x", "x", PINNED))
    assert not (tmp_path / ".mc" / "skills" / "x").exists()


@pytest.mark.asyncio
async def test_search_marks_library_skills_that_would_be_refused(client, fake_redis, tmp_path):
    d = _lib(tmp_path, "scripted-helper")
    (d / "go.sh").write_text("#!/bin/sh\n")
    _lib(tmp_path, "plain-helper")
    board = await _board()
    me, tok = await _agent("A", board)
    resp = await client.get("/api/v1/agent/install-requests/search", params={"q": "helper"},
                            headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200, resp.text
    by_name = {s["name"]: s for s in resp.json()["library"]}
    assert by_name["plain-helper"]["text_only"] is True
    assert by_name["scripted-helper"]["text_only"] is False


# ── 3. no plugin / MCP installs this round ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,source", [
    ("mcp", "github:someone/x-mcp"),
    ("plugin", "claude-plugins-official"),
])
async def test_plugin_and_mcp_installs_are_refused(client, fake_redis, kind, source):
    board = await _board()
    lead, tok = await _agent("L", board, scopes=["agents:manage"])
    resp = await _post(client, tok, {
        "type": kind, "operation": "install", "name": "x", "source": source,
        "target_agent_id": str(lead.id), "reason": "wants executable code",
    })
    assert resp.status_code == 400, resp.text
    assert "not offered" in resp.text


@pytest.mark.asyncio
async def test_plugin_uninstall_is_still_accepted(client, fake_redis):
    board = await _board()
    lead, tok = await _agent("L", board, scopes=["agents:manage"])
    resp = await _post(client, tok, {
        "type": "plugin", "operation": "uninstall", "name": "x",
        "target_agent_id": str(lead.id), "reason": "no longer needed",
    })
    assert resp.status_code == 201, resp.text


# ── 4. one helper picks the folder; the executor checks what it copies ──


def test_skill_md_directory_at_root_is_refused_not_silently_skipped(tmp_path, _env):
    from app.services.install_executor import SkillContentError, skill_source_dir

    clone = tmp_path / "clone"
    (clone / "SKILL.md").mkdir(parents=True)
    (clone / "skills" / "foo").mkdir(parents=True)
    (clone / "skills" / "foo" / "SKILL.md").write_text("x\n")
    with pytest.raises(SkillContentError):
        skill_source_dir(clone, "foo")


def test_symlinked_skill_md_is_refused(tmp_path):
    from app.services.install_executor import SkillContentError, skill_source_dir

    clone = tmp_path / "clone"
    clone.mkdir()
    (tmp_path / "real.md").write_text("x\n")
    os.symlink(tmp_path / "real.md", clone / "SKILL.md")
    with pytest.raises(SkillContentError):
        skill_source_dir(clone, "foo")


def test_executor_with_skill_md_dir_and_script_copies_nothing(tmp_path, _env):
    """The critic's repro: SKILL.md/ as a directory + text skills/foo/ + run.sh.
    Nothing executable may reach the library, by design, not by accident."""
    from app.services.install_executor import _call_skill_install

    _env.clear()
    _env["SKILL.md/keep.md"] = "x\n"
    _env["skills/foo/SKILL.md"] = "---\nname: foo\n---\n"
    _env["run.sh"] = "#!/bin/sh\n"
    with pytest.raises(Exception):
        asyncio.run(_call_skill_install("github:obra/skill-foo", "foo", PINNED))
    assert not (tmp_path / ".mc" / "skills" / "foo").exists()


def test_request_check_and_executor_use_the_same_helper():
    import inspect

    from app.routers import install_requests
    from app.services import install_executor

    assert "skill_source_dir(" in inspect.getsource(install_requests.inspect_skill_commit)
    assert "skill_source_dir(" in inspect.getsource(install_executor._call_skill_install)
    assert "text_only_files(" in inspect.getsource(install_executor._call_skill_install)


# ── 5. a second chat on the same pending request is remembered ───────────


@pytest.mark.asyncio
async def test_second_chat_on_a_pending_request_is_stored(client, fake_redis, tmp_path):
    _lib(tmp_path, "notes")
    board = await _board()
    me, tok = await _agent("A", board)
    c1, c2 = await _chat(me.id), await _chat(me.id)
    r1 = await _post(client, tok, _skill(me.id, "notes", "~/.mc/skills/notes", chat_id=str(c1)))
    r2 = await _post(client, tok, _skill(me.id, "notes", "~/.mc/skills/notes", chat_id=str(c2)))
    assert r2.status_code == 200 and r2.json()["existing"] is True
    assert r2.json()["approval_id"] == r1.json()["approval_id"]
    p = await _payload(r1.json()["approval_id"])
    assert p["requester_chat_id"] == str(c1)
    assert p["extra_chat_ids"] == [str(c2)]


# ── 6. status filters in SQL; file sizes capped ──────────────────────────


@pytest.mark.asyncio
async def test_status_finds_own_request_behind_many_foreign_ones(client, fake_redis, monkeypatch, tmp_path):
    from app.models.approval import Approval
    from app.routers import install_requests

    _lib(tmp_path, "notes")
    board = await _board()
    me, tok = await _agent("A", board)
    mine = (await _post(client, tok, _skill(me.id, "notes", "~/.mc/skills/notes"))).json()["approval_id"]
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        other = str(uuid.uuid4())
        for i in range(510):
            s.add(Approval(agent_id=None, action_type="install_skill", description="x",
                           payload={"name": f"n{i}", "requester_agent_id": other,
                                    "target_agent_id": other},
                           autonomy_level="L2", status="pending"))
        await s.commit()
    # More than 500 newer foreign rows must not push mine out of the list.
    resp = await client.get("/api/v1/agent/install-requests",
                            headers={"Authorization": f"Bearer {tok}"})
    assert [r["approval_id"] for r in resp.json()["requests"]] == [mine]


def test_oversized_text_file_is_refused(tmp_path, monkeypatch):
    from app.services import install_executor

    monkeypatch.setattr(install_executor, "MAX_FILE_BYTES", 100)
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text("x" * 200)
    with pytest.raises(install_executor.SkillContentError) as e:
        install_executor.text_only_files(d)
    assert "too large" in str(e.value)


def test_oversized_skill_in_total_is_refused(tmp_path, monkeypatch):
    from app.services import install_executor

    monkeypatch.setattr(install_executor, "MAX_SKILL_BYTES", 150)
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text("x" * 100)
    (d / "b.md").write_text("x" * 100)
    with pytest.raises(install_executor.SkillContentError):
        install_executor.text_only_files(d)


# ── 2. the chat line is held until it is localised (PR 2) ────────────────


@pytest.mark.asyncio
async def test_no_chat_line_is_written_while_the_line_is_held(client, auth_client, fake_redis, tmp_path, monkeypatch):
    from app.config import settings
    from app.models.chat_record import ChatEvent
    from sqlmodel import select

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    _lib(tmp_path, "notes")
    board = await _board()
    me, tok = await _agent("A", board)
    chat_id = await _chat(me.id)
    rid = (await _post(client, tok, _skill(me.id, "notes", "~/.mc/skills/notes",
                                           chat_id=str(chat_id)))).json()["approval_id"]
    with patch("app.routers.approvals.InstallExecutor") as cls:
        ex = MagicMock()
        ex.execute = AsyncMock(return_value=None)
        cls.return_value = ex
        r = await auth_client.patch(f"/api/v1/approvals/{rid}",
                                    json={"status": "rejected", "resolver_note": "n"})
    assert r.status_code == 200, r.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(ChatEvent).where(ChatEvent.chat_id == chat_id))).all()
    assert rows == []


def test_docs_do_not_promise_a_phantom_chat_env_or_a_chat_line():
    from app.services.tools_md_builder import generate_tools_md

    md = generate_tools_md(name="Cody", emoji="🤖", raw_token="tok", board_id="b1",
                           is_board_lead=False, scopes=["tasks:read"])
    assert "MC_CHAT_ID" not in md
    assert "system line" not in md
    assert "mc ext status" in md


def test_lead_docs_no_longer_offer_mcp_or_plugin_installs():
    from app.services.tools_md_builder import generate_tools_md

    md = generate_tools_md(name="Lead", emoji="🤖", raw_token="tok", board_id="b1",
                           is_board_lead=True, scopes=["agents:manage", "tasks:read"])
    assert '"type": "mcp",\n        "operation": "install"' not in md
    assert '\\"type\\": \\"mcp\\",\\n\\"operation\\": \\"install\\"' not in md
    assert "not offered" in md
