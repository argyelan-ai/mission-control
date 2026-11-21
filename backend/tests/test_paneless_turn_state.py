"""A host agent (no pane to read) that works silently is still "working".

Critic finding 21.11.2026: the pane-less branch of ``_compute_pane_state``
said "working" only while the transcript was written in the last 20 s. A
host agent inside a long tool call or a long think wrote nothing for longer,
so the chat read "Bereit" and — since the composer follows the shown state —
offered no Stop. The state must come from the turn itself: a turn that has
not ended (last line = operator request, tool use or tool result) is
"working" until ``chat_turn_state.TURN_STALE_SECONDS`` of silence.
"""
from __future__ import annotations

import json
import os
import time

import pytest

pytestmark = pytest.mark.asyncio


class _HostAgent:
    slug = "boss"
    name = "Boss"
    agent_runtime = "host"
    harness = "claude"
    headless_chat = False


def _line(entry: dict) -> str:
    return json.dumps(entry) + "\n"


def _user(text: str) -> str:
    return _line({"type": "user", "message": {"role": "user", "content": text}})


def _assistant(block_type: str, stop_reason: str) -> str:
    block = {"type": block_type}
    if block_type == "text":
        block["text"] = "Fertig."
    if block_type == "tool_use":
        block.update({"id": "tu1", "name": "Bash", "input": {"command": "sleep 60"}})
    return _line({"type": "assistant", "message": {"role": "assistant", "content": [block], "stop_reason": stop_reason}})


def _tool_result() -> str:
    return _line({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}})


def _age(path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def mgr(monkeypatch):
    from app.services import transcript_chat as tc

    async def _no_pane(agent):
        return None

    async def _aliveness(agent, path, adapter=None):
        return "idle"

    monkeypatch.setattr(tc, "capture_pane", _no_pane)
    monkeypatch.setattr(tc, "resolve_aliveness", _aliveness)
    return tc.ChatTailerManager()


async def test_silent_tool_call_past_the_mtime_window_is_still_working(tmp_path, mgr):
    f = tmp_path / "s.jsonl"
    f.write_text(_user("Bau das") + _assistant("tool_use", "tool_use"))
    _age(f, 60)  # a 60 s Bash call: no transcript write since it started
    state = await mgr._compute_pane_state(_HostAgent(), f)
    assert state["status"] == "working"


async def test_operator_request_waiting_for_the_first_answer_is_working(tmp_path, mgr):
    f = tmp_path / "s.jsonl"
    f.write_text(_user("Denk lange nach"))
    _age(f, 45)  # a long think before the first line of the answer
    state = await mgr._compute_pane_state(_HostAgent(), f)
    assert state["status"] == "working"


async def test_tool_result_then_long_think_is_working(tmp_path, mgr):
    f = tmp_path / "s.jsonl"
    f.write_text(_user("x") + _assistant("tool_use", "tool_use") + _tool_result())
    _age(f, 90)
    state = await mgr._compute_pane_state(_HostAgent(), f)
    assert state["status"] == "working"


async def test_an_ended_turn_reads_idle(tmp_path, mgr):
    f = tmp_path / "s.jsonl"
    f.write_text(_user("x") + _assistant("text", "end_turn"))
    _age(f, 60)
    state = await mgr._compute_pane_state(_HostAgent(), f)
    assert state["status"] == "idle"


async def test_a_turn_silent_past_the_stale_limit_is_not_working_for_ever(tmp_path, mgr):
    from app.services.chat_turn_state import TURN_STALE_SECONDS

    f = tmp_path / "s.jsonl"
    f.write_text(_user("x") + _assistant("tool_use", "tool_use"))
    _age(f, TURN_STALE_SECONDS + 60)  # process likely gone: no "working" for the evening
    state = await mgr._compute_pane_state(_HostAgent(), f)
    assert state["status"] == "idle"
