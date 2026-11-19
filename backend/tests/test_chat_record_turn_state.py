"""Turn state per chat in MC's chat record (follow-up of #1051).

The state is derived ON READ from the chat's current session file — never
from a writer that only runs while somebody watches the chat (critic
finding on #1052: the live recorder lives inside the SSE tailer, which is
cancelled with the last viewer, and follows only the agent's newest file).

* ``busy``  — the last content line of the file is part of a running turn;
* ``idle``  — it ends the turn: the harness's turn end (Claude Code
  ``end_turn`` with text, omp ``stopReason`` stop/error/aborted), an
  interrupt (``[Request interrupted by user…]``), an API error line, a
  ``max_tokens`` / ``stop_sequence`` / ``refusal`` stop — or there is no
  content line at all (fresh session, slash-command echoes only);
* ``stale`` — not ended, but the file has been quiet longer than
  ``TURN_STALE_SECONDS`` (process gone, or one very long tool call): the
  UI says "no sign of life since …" instead of a "working" for ever;
* ``None``  — no session file found for the chat (unknown, never guessed).

Fixture lines are synthetic copies of the Claude Code / omp JSONL schema
(structure kept, content neutral).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services import chat_record, chat_turn_state
from tests.conftest import test_engine

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def host_home(tmp_path, monkeypatch):
    from app.services import omp_chat, transcript_chat

    monkeypatch.setattr(transcript_chat, "_host_home", lambda: tmp_path)
    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    chat_turn_state._memo.clear()
    return tmp_path


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


def _claude_dir(home: Path, slug: str) -> Path:
    return home / ".mc" / "agents" / slug / "claude-config" / "projects" / "-home-agent"


def _c_user(uid, ts, text, sidechain=False, **extra):
    return {"type": "user", "uuid": uid, "timestamp": ts, "isSidechain": sidechain, **extra,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _c_tool(uid, ts, tool_id, name="Bash"):
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": False,
            "message": {"role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{uid}",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                     "input": {"command": "ls"}}]}}


def _c_result(uid, ts, tool_id, content="a.py"):
    return {"type": "user", "uuid": uid, "timestamp": ts, "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False}]}}


def _c_answer(uid, ts, text, sidechain=False, stop="end_turn", **extra):
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": sidechain, **extra,
            "message": {"role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{uid}",
                        "stop_reason": stop, "content": [{"type": "text", "text": text}]}}


def _c_thinking(uid, ts, stop="end_turn"):
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": False,
            "message": {"role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{uid}",
                        "stop_reason": stop, "content": [{"type": "thinking", "thinking": "hm"}]}}


def _c_system(uid, ts):
    return {"type": "system", "uuid": uid, "timestamp": ts, "subtype": "turn_duration", "durationMs": 9}


def _write(path: Path, lines: list[dict], *, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


T = "2026-11-19T10:00:0{}.000Z"
TURN = [_c_user("u1", T.format(0), "rename the loader")]


def _state(path: Path, harness: str = "claude") -> str | None:
    return chat_turn_state.file_turn_state(path, harness)["state"]


# ── Claude Code: classification of one file ────────────────────────────────


def test_claude_normal_turn(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, TURN)
    assert _state(p) == "busy"
    _write(p, [_c_tool("a1", T.format(1), "t-1")])
    assert _state(p) == "busy"
    _write(p, [_c_result("r1", T.format(2), "t-1")])
    assert _state(p) == "busy"
    # A text line whose turn goes on is not the end (the 182-of-245 trap).
    _write(p, [_c_answer("a2", T.format(3), "Looking.", stop="tool_use")])
    assert _state(p) == "busy"
    # end_turn on the THINKING line: the text follows, still busy.
    _write(p, [_c_thinking("a3", T.format(4))])
    assert _state(p) == "busy"
    _write(p, [_c_answer("a4", T.format(5), "Done."), _c_system("s1", T.format(6))])
    assert _state(p) == "idle"
    _write(p, [_c_user("u2", T.format(7), "and the tests?")])
    assert _state(p) == "busy"


def test_claude_interrupt_ends_the_turn(tmp_path):
    p = _write(tmp_path / "s.jsonl", [*TURN, _c_tool("a1", T.format(1), "t-1")])
    _write(p, [_c_user("u2", T.format(2), "[Request interrupted by user for tool use]")])
    assert _state(p) == "idle"


def test_claude_interrupt_inside_the_tool_result_ends_the_turn(tmp_path):
    p = _write(tmp_path / "s.jsonl", [*TURN, _c_tool("a1", T.format(1), "t-1")])
    _write(p, [_c_result("r1", T.format(2), "t-1", content="[Request interrupted by user for tool use]")])
    assert _state(p) == "idle"


def test_claude_interrupt_while_thinking_ends_the_turn(tmp_path):
    p = _write(tmp_path / "s.jsonl", TURN)
    _write(p, [_c_user("u2", T.format(2), "[Request interrupted by user]")])
    assert _state(p) == "idle"


def test_claude_api_error_ends_the_turn(tmp_path):
    p = _write(tmp_path / "s.jsonl", TURN)
    _write(p, [_c_answer("e1", T.format(2), "API Error: 529 overloaded", stop=None,
                         isApiErrorMessage=True)])
    assert _state(p) == "idle"


@pytest.mark.parametrize("stop", ["max_tokens", "stop_sequence", "refusal"])
def test_claude_other_final_stop_reasons_end_the_turn(tmp_path, stop):
    p = _write(tmp_path / "s.jsonl", [*TURN, _c_answer("a1", T.format(2), "cut", stop=stop)])
    assert _state(p) == "idle"


def test_claude_compact_summary_after_an_idle_turn_stays_idle(tmp_path):
    p = _write(tmp_path / "s.jsonl", [*TURN, _c_answer("a1", T.format(1), "Done.")])
    _write(p, [
        {"type": "system", "uuid": "b1", "timestamp": T.format(2), "subtype": "compact_boundary"},
        _c_user("c1", T.format(3), "This session is being continued ...", isCompactSummary=True),
        _c_user("c2", T.format(4), "<local-command-stdout>Compacted</local-command-stdout>"),
    ])
    assert _state(p) == "idle"


def test_claude_auto_compact_inside_a_turn_stays_busy(tmp_path):
    p = _write(tmp_path / "s.jsonl", [*TURN, _c_tool("a1", T.format(1), "t-1"), _c_result("r1", T.format(2), "t-1")])
    _write(p, [_c_user("c1", T.format(3), "This session is being continued ...", isCompactSummary=True)])
    assert _state(p) == "busy"


def test_claude_meta_and_sidechain_lines_do_not_count(tmp_path):
    p = _write(tmp_path / "s.jsonl", [*TURN, _c_answer("a1", T.format(1), "Done.")])
    _write(p, [
        _c_user("m1", T.format(2), "caveat", isMeta=True),
        _c_user("s1", T.format(3), "helper prompt", sidechain=True),
        _c_answer("s2", T.format(4), "helper", sidechain=True, stop="tool_use"),
    ])
    assert _state(p) == "idle"


def test_claude_session_without_a_conversation_is_idle(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        {"type": "mode", "mode": "default"},
        _c_user("c1", T.format(1), "<command-name>/model</command-name>"),
    ])
    assert _state(p) == "idle"


def test_unreadable_last_line_is_never_called_idle(tmp_path):
    p = _write(tmp_path / "s.jsonl", TURN)
    with p.open("a") as fh:
        fh.write('{"type": "user", "message": {"content": "cut in the mid\n')
    assert _state(p) == "busy"


def test_a_quiet_running_turn_turns_stale_not_busy_for_ever(tmp_path):
    p = _write(tmp_path / "s.jsonl", [*TURN, _c_tool("a1", T.format(1), "t-1")],
               age=chat_turn_state.TURN_STALE_SECONDS + 60)
    out = chat_turn_state.file_turn_state(p, "claude")
    assert out["state"] == "stale"
    assert out["at"] is not None
    # An ended turn never turns stale.
    q = _write(tmp_path / "t.jsonl", [*TURN, _c_answer("a1", T.format(1), "Done.")],
               age=chat_turn_state.TURN_STALE_SECONDS + 60)
    assert _state(q) == "idle"


def test_at_is_the_last_write_time(tmp_path):
    p = _write(tmp_path / "s.jsonl", TURN, age=120)
    at = datetime.fromisoformat(chat_turn_state.file_turn_state(p, "claude")["at"])
    assert abs(at.timestamp() - (time.time() - 120)) < 5
    assert at.tzinfo is not None


def test_missing_file_is_unknown(tmp_path):
    assert chat_turn_state.file_turn_state(tmp_path / "nope.jsonl", "claude") == {"state": None, "at": None}


# ── omp ─────────────────────────────────────────────────────────────────────


def _o_user(mid, ts, text):
    return {"type": "message", "id": mid, "parentId": None, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _o_answer(mid, ts, text, stop):
    return {"type": "message", "id": mid, "parentId": None, "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                        "model": "m-1", "stopReason": stop}}


def _o_head():
    return {"type": "session", "version": 3, "id": "s-1", "timestamp": T.format(0), "cwd": "/workspace"}


@pytest.mark.parametrize("stop", ["stop", "aborted", "error"])
def test_omp_turn(tmp_path, stop):
    p = _write(tmp_path / "s.jsonl", [_o_head()])
    assert _state(p, "omp") == "idle"
    _write(p, [_o_user("m1", T.format(1), "hello")])
    assert _state(p, "omp") == "busy"
    _write(p, [_o_answer("m2", T.format(2), "hi", "toolUse")])
    assert _state(p, "omp") == "busy"
    _write(p, [_o_answer("m3", T.format(3), "done", stop)])
    assert _state(p, "omp") == "idle"


# ── read side: every chat, nobody watching ──────────────────────────────────


async def _chats(make_agent, host_home, *, further: bool = False):
    agent = await make_agent(name="Alpha", slug="alpha")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.ensure_agent_chat(s, agent)
        other = await chat_record.create_chat(s, None, agent, title="Second") if further else None
        await s.commit()
    return agent, chat, other


async def test_list_derives_the_state_without_any_recorder_or_import(
    host_home, flag_on, make_agent, auth_client
):
    """No tailer, no recorder, no import sweep: the file alone decides —
    the chat nobody has open shows its state, and it follows the file."""
    _agent, chat, _ = await _chats(make_agent, host_home)
    path = _write(_claude_dir(host_home, "alpha") / "n-1.jsonl", TURN)

    r = await auth_client.get("/api/v1/chat-record/chats")
    assert r.status_code == 200
    row = {c["id"]: c for c in r.json()["chats"]}[str(chat.id)]
    assert row["turnState"] == "busy"
    assert row["turnStateAt"] is not None

    _write(path, [_c_answer("a1", T.format(5), "Done.")])
    r = await auth_client.get("/api/v1/chat-record/chats")
    assert {c["id"]: c for c in r.json()["chats"]}[str(chat.id)]["turnState"] == "idle"

    r = await auth_client.get(f"/api/v1/chat-record/chats/{chat.id}/history")
    assert r.status_code == 200
    assert r.json()["chat"]["turnState"] == "idle"
    assert r.json()["chat"]["turnStateAt"] is not None


async def test_each_chat_of_one_agent_reads_its_own_session(host_home, flag_on, make_agent, auth_client):
    """A further chat (own native session) and the default chat: each gets
    the state of ITS file — the default chat never takes the pinned file of
    a further chat, even when that one is the newest on disk."""
    _agent, chat, other = await _chats(make_agent, host_home, further=True)
    tdir = _claude_dir(host_home, "alpha")
    _write(tdir / "default-1.jsonl", [*TURN, _c_answer("a1", T.format(1), "Done.")], age=60)
    # Newest on disk, with an answer line, so the agent's own pick
    # (find_active_session) is this pinned file.
    _write(tdir / f"{other.native_session_id}.jsonl", [*TURN, _c_tool("a1", T.format(1), "t-1")])

    r = await auth_client.get("/api/v1/chat-record/chats")
    rows = {c["id"]: c for c in r.json()["chats"]}
    assert rows[str(other.id)]["turnState"] == "busy"
    assert rows[str(chat.id)]["turnState"] == "idle"


async def test_recorded_session_path_is_used(host_home, flag_on, make_agent, auth_client):
    """A chat whose session the record already knows (e.g. after a switch
    to another harness) reads that file."""
    _agent, chat, other = await _chats(make_agent, host_home, further=True)
    elsewhere = _write(host_home / "elsewhere" / "x.jsonl", TURN)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await chat_record.ensure_session(
            s, other, native_session_id="x", harness="claude", runtime_id=None, model=None,
            place="container", transcript_path=str(elsewhere),
        )
        await s.commit()
    r = await auth_client.get("/api/v1/chat-record/chats")
    assert {c["id"]: c for c in r.json()["chats"]}[str(other.id)]["turnState"] == "busy"


async def test_chat_without_a_session_file_is_unknown(host_home, flag_on, make_agent, auth_client):
    _agent, chat, other = await _chats(make_agent, host_home, further=True)
    r = await auth_client.get("/api/v1/chat-record/chats")
    rows = {c["id"]: c for c in r.json()["chats"]}
    for cid in (chat.id, other.id):
        assert rows[str(cid)]["turnState"] is None and rows[str(cid)]["turnStateAt"] is None


async def test_turn_state_reads_are_cached_per_file_version(tmp_path, monkeypatch):
    p = _write(tmp_path / "s.jsonl", TURN)
    chat_turn_state._memo.clear()
    calls = []
    real = chat_turn_state._claude_turn_ended
    monkeypatch.setattr(chat_turn_state, "_claude_turn_ended", lambda path: calls.append(1) or real(path))
    chat_turn_state.file_turn_state(p, "claude")
    chat_turn_state.file_turn_state(p, "claude")
    assert len(calls) == 1
    _write(p, [_c_answer("a1", T.format(5), "Done.")])
    assert chat_turn_state.file_turn_state(p, "claude")["state"] == "idle"
    assert len(calls) == 2
