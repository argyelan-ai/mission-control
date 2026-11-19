"""Turn state per chat in MC's chat record (follow-up of #1051).

The live recorder writes ``busy`` when a turn starts (the operator's
message) and ``idle`` when the harness's own turn-end judgement
(``adapter.transcript_suggests_turn_ended``) says the turn is over — for
Claude Code (``stop_reason: end_turn`` with a text block) and omp
(``stopReason`` stop/error/aborted). The chat list and the history answer
carry it, so a further chat can show "working" / "ready" honestly.

Fixture lines are synthetic copies of the Claude Code / omp JSONL schema
(structure kept, content neutral).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.chat_record import Chat
from app.services import chat_record
from tests.conftest import test_engine

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def host_home(tmp_path, monkeypatch):
    from app.services import omp_chat, transcript_chat

    monkeypatch.setattr(transcript_chat, "_host_home", lambda: tmp_path)
    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


def _factory():
    return AsyncSession(test_engine, expire_on_commit=False)


def _claude_dir(home: Path, slug: str) -> Path:
    return home / ".mc" / "agents" / slug / "claude-config" / "projects" / "-home-agent"


def _c_user(uid, ts, text, sidechain=False):
    return {"type": "user", "uuid": uid, "timestamp": ts, "isSidechain": sidechain,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _c_tool(uid, ts, tool_id):
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": False,
            "message": {"role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{uid}",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                                     "input": {"command": "ls"}}]}}


def _c_result(uid, ts, tool_id):
    return {"type": "user", "uuid": uid, "timestamp": ts, "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "a.py", "is_error": False}]}}


def _c_answer(uid, ts, text, sidechain=False):
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": sidechain,
            "message": {"role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{uid}",
                        "stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}}


def _append(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


async def _feed(rec, adapter, parse, path: Path, lines: list[dict]) -> None:
    """What the tailer does: the lines land in the file, the parser turns
    them into events, every event goes to the recorder."""
    _append(path, lines)
    for line in lines:
        for ev in parse(json.dumps(line), None):
            rec.record(path, ev)
    await rec.wait_idle()


async def _chat(agent_id) -> Chat:
    async with AsyncSession(test_engine) as s:
        return (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent_id))).one()


def _at(value) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ── Claude Code ─────────────────────────────────────────────────────────────


async def test_claude_turn_goes_busy_then_idle_with_timestamps(host_home, flag_on, make_agent):
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha")
    adapter = adapter_for(agent)
    parse = adapter.new_parser()
    path = _claude_dir(host_home, "alpha") / "live.jsonl"
    rec = chat_record.LiveRecorder(agent, adapter, None, session_factory=_factory)
    await rec.backfilled.wait()

    await _feed(rec, adapter, parse, path, [_c_user("u1", "2026-11-19T10:00:00.000Z", "rename the loader")])
    chat = await _chat(agent.id)
    assert chat.turn_state == "busy"
    assert _at(chat.turn_state_at) == datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)

    # A tool call and its result: still the same turn.
    await _feed(rec, adapter, parse, path, [
        _c_tool("a1", "2026-11-19T10:00:03.000Z", "t-1"),
        _c_result("r1", "2026-11-19T10:00:04.000Z", "t-1"),
    ])
    chat = await _chat(agent.id)
    assert chat.turn_state == "busy"
    # The turn started at the operator's message, not at the last tool.
    assert _at(chat.turn_state_at) == datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)

    await _feed(rec, adapter, parse, path, [_c_answer("a2", "2026-11-19T10:00:09.000Z", "Done.")])
    chat = await _chat(agent.id)
    assert chat.turn_state == "idle"
    assert _at(chat.turn_state_at) == datetime(2026, 11, 19, 10, 0, 9, tzinfo=timezone.utc)

    # The next message starts the next turn.
    await _feed(rec, adapter, parse, path, [_c_user("u2", "2026-11-19T10:05:00.000Z", "and the tests?")])
    chat = await _chat(agent.id)
    assert chat.turn_state == "busy"
    assert _at(chat.turn_state_at) == datetime(2026, 11, 19, 10, 5, 0, tzinfo=timezone.utc)
    rec.close()
    assert rec.errors == 0


async def test_claude_text_between_tools_is_not_a_turn_end(host_home, flag_on, make_agent):
    """A text line whose turn goes on (``stop_reason: tool_use``) must not
    flip the chat to idle — the measured 182-of-245 trap of the old rule."""
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha")
    adapter = adapter_for(agent)
    parse = adapter.new_parser()
    path = _claude_dir(host_home, "alpha") / "live.jsonl"
    rec = chat_record.LiveRecorder(agent, adapter, None, session_factory=_factory)
    await rec.backfilled.wait()
    between = _c_answer("a1", "2026-11-19T10:00:02.000Z", "Looking at it.")
    between["message"]["stop_reason"] = "tool_use"
    await _feed(rec, adapter, parse, path, [_c_user("u1", "2026-11-19T10:00:00.000Z", "go"), between])
    assert (await _chat(agent.id)).turn_state == "busy"
    rec.close()


async def test_subagent_lines_never_change_the_turn_state(host_home, flag_on, make_agent):
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha")
    adapter = adapter_for(agent)
    parse = adapter.new_parser()
    path = _claude_dir(host_home, "alpha") / "live.jsonl"
    rec = chat_record.LiveRecorder(agent, adapter, None, session_factory=_factory)
    await rec.backfilled.wait()
    await _feed(rec, adapter, parse, path, [
        _c_user("u1", "2026-11-19T10:00:00.000Z", "go"),
        _c_answer("a1", "2026-11-19T10:00:05.000Z", "Done."),
    ])
    assert (await _chat(agent.id)).turn_state == "idle"
    # A helper's prompt is a "user" line with isSidechain — not a new turn.
    await _feed(rec, adapter, parse, path, [_c_user("s1", "2026-11-19T10:01:00.000Z", "sub", sidechain=True)])
    chat = await _chat(agent.id)
    assert chat.turn_state == "idle"
    assert _at(chat.turn_state_at) == datetime(2026, 11, 19, 10, 0, 5, tzinfo=timezone.utc)
    rec.close()


# ── omp ─────────────────────────────────────────────────────────────────────


def _omp_path(home: Path, slug: str) -> Path:
    return home / ".mc" / "agents" / slug / "omp-sessions" / "--workspace--" / "2026-11-19T10-00-00_s-1.jsonl"


def _o_head():
    return {"type": "session", "version": 3, "id": "s-1", "timestamp": "2026-11-19T10:00:00.000Z",
            "cwd": "/workspace"}


def _o_user(mid, ts, text):
    return {"type": "message", "id": mid, "parentId": None, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _o_answer(mid, ts, text, stop):
    return {"type": "message", "id": mid, "parentId": None, "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                        "model": "m-1", "stopReason": stop,
                        "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0}}}


@pytest.mark.parametrize("stop", ["stop", "aborted"])
async def test_omp_turn_goes_busy_then_idle(host_home, flag_on, make_agent, stop):
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    adapter = adapter_for(agent)
    path = _omp_path(host_home, "omp-a")
    _append(path, [_o_head()])
    parse = adapter.new_parser(path)
    rec = chat_record.LiveRecorder(agent, adapter, path, session_factory=_factory)
    await rec.backfilled.wait()

    await _feed(rec, adapter, parse, path, [_o_user("m1", "2026-11-19T10:00:01.000Z", "hello")])
    chat = await _chat(agent.id)
    assert chat.turn_state == "busy"

    await _feed(rec, adapter, parse, path, [_o_answer("m2", "2026-11-19T10:00:04.000Z", "hi", "toolUse")])
    assert (await _chat(agent.id)).turn_state == "busy"

    await _feed(rec, adapter, parse, path, [_o_answer("m3", "2026-11-19T10:00:07.000Z", "done", stop)])
    chat = await _chat(agent.id)
    assert chat.turn_state == "idle"
    assert _at(chat.turn_state_at) == datetime(2026, 11, 19, 10, 0, 7, tzinfo=timezone.utc)
    rec.close()
    assert rec.errors == 0


# ── write rule ──────────────────────────────────────────────────────────────


async def test_older_turn_state_never_overwrites_a_newer_one(flag_on, make_agent):
    agent = await make_agent(name="Alpha", slug="alpha")
    async with _factory() as s:
        chat = await chat_record.ensure_agent_chat(s, agent)
        newer = datetime(2026, 11, 19, 10, 5, tzinfo=timezone.utc)
        older = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
        assert await chat_record.set_turn_state(s, chat.id, "idle", newer) is True
        assert await chat_record.set_turn_state(s, chat.id, "busy", older) is False
        await s.commit()
    chat = await _chat(agent.id)
    assert chat.turn_state == "idle" and _at(chat.turn_state_at) == newer


def test_turn_signal_by_event():
    sig = chat_record.turn_signal
    assert sig({"kind": "message", "role": "user", "uuid": "u", "sidechain": False}) == "start"
    assert sig({"kind": "message", "role": "assistant", "uuid": "a"}) == "check_end"
    assert sig({"kind": "usage", "uuid": "a"}) == "check_end"
    assert sig({"kind": "tool", "uuid": "a", "toolUseId": "t"}) == "working"
    assert sig({"kind": "thinking", "uuid": "a"}) == "working"
    # Not part of the operator's turn: helpers, team mail, slash commands.
    assert sig({"kind": "message", "role": "user", "uuid": "u", "sidechain": True}) is None
    assert sig({"kind": "message", "role": "teammate", "uuid": "u"}) is None
    assert sig({"kind": "command", "uuid": "c", "command": "/clear"}) is None
    assert sig({"kind": "notification", "uuid": "n"}) is None


# ── read side ───────────────────────────────────────────────────────────────


async def test_api_list_and_history_carry_the_turn_state(host_home, flag_on, make_agent, auth_client):
    agent = await make_agent(name="Alpha", slug="alpha")
    at = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
    async with _factory() as s:
        chat = await chat_record.ensure_agent_chat(s, agent)
        other = await chat_record.create_chat(s, None, agent, title="Second")
        await chat_record.set_turn_state(s, chat.id, "busy", at)
        await s.commit()
        chat_id, other_id = chat.id, other.id
    r = await auth_client.get("/api/v1/chat-record/chats")
    assert r.status_code == 200
    rows = {c["id"]: c for c in r.json()["chats"]}
    assert rows[str(chat_id)]["turnState"] == "busy"
    assert datetime.fromisoformat(rows[str(chat_id)]["turnStateAt"]).replace(tzinfo=timezone.utc) == at
    # A chat no recorder saw yet says so — no guessed "ready".
    assert rows[str(other_id)]["turnState"] is None and rows[str(other_id)]["turnStateAt"] is None

    r = await auth_client.get(f"/api/v1/chat-record/chats/{chat_id}/history")
    assert r.status_code == 200
    assert r.json()["chat"]["turnState"] == "busy"
    assert r.json()["chat"]["turnStateAt"] is not None


# ── migration ───────────────────────────────────────────────────────────────


def test_migration_adds_and_drops_the_two_columns():
    from tests.test_chat_record_migration_0229 import _load, _run

    rev27 = _load("0227_chat_record")
    rev29 = _load("0229_chat_record_many_chats")
    rev30 = _load("0230_chat_turn_state")
    assert rev30.down_revision == "0229_chat_record_many_chats"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.connection.driver_connection.create_function("NOW", 0, lambda: "2026-11-19 00:00:00")
        for t in ("users", "projects", "runtimes", "agents"):
            conn.execute(sa.text(f"CREATE TABLE {t} (id CHAR(32) PRIMARY KEY)"))
        _run(conn, rev27, "upgrade")
        _run(conn, rev29, "upgrade")
        conn.execute(sa.text("INSERT INTO chats (id, title, next_seq, pinned) VALUES (:a, 'Alpha', 1, 0)"),
                     {"a": uuid.uuid4().hex})
        _run(conn, rev30, "upgrade")
        cols = {c["name"] for c in sa.inspect(conn).get_columns("chats")}
        assert {"turn_state", "turn_state_at"} <= cols
        # Existing chats start without a state (unknown, not "ready").
        assert conn.execute(sa.text("SELECT turn_state, turn_state_at FROM chats")).all() == [(None, None)]
        _run(conn, rev30, "downgrade")
        cols = {c["name"] for c in sa.inspect(conn).get_columns("chats")}
        assert "turn_state" not in cols and "turn_state_at" not in cols
        assert conn.execute(sa.text("SELECT title FROM chats")).all() == [("Alpha",)]

