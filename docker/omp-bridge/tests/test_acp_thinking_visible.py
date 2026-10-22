#!/usr/bin/env python3
"""The thinking phase is visible in the chat (operator finding 20.10.2026).

Live: an omp agent over ACP with a reasoning model thought for minutes —
~5 900 tokens of reasoning — and the chat showed nothing that said so. omp
DID stream ``agent_thought_chunk``; the mapper wrote each chunk as an
``acp-preview`` line that looked exactly like answer text (so the chat showed
the thought as a "live preview" of the answer, frozen after 4 000 chars),
carried the WHOLE thought on every line (33 MB of preview file in 5 min) and
never kept a finished thought in the transcript.

Contract tested here, end to end through the REAL backend readers:

* a thought preview line says it is one (``data.block == "thinking"``) and
  carries ``chars`` + ``startedAt``; its ``content`` is only the END of the
  thought (bounded), so the preview file grows linearly;
* the backend's preview reader turns it into a ``preview`` event with
  ``block: "thinking"`` (one event type, any harness with a preview channel);
* a finished thought lands ONCE in the transcript as an assistant
  ``thinking`` block with ``durationMs`` — before the tool call / answer
  that ended it — and ``read_history`` serves it as a ``thinking`` event.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT.parent.parent / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))
import acp_chat_events  # noqa: E402
from app.services.transcript_chat import _read_preview_channel  # noqa: E402
from test_acp_chat_events import as_history  # noqa: E402


def thought(text: str, mid: str = "m-1") -> dict:
    return {"update": {"sessionUpdate": "agent_thought_chunk",
                       "content": {"type": "text", "text": text}, "messageId": mid}}


def answer(text: str, mid: str = "m-2") -> dict:
    return {"update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": text}, "messageId": mid}}


def tool_call(tid: str = "t-1") -> dict:
    return {"update": {"sessionUpdate": "tool_call", "toolCallId": tid,
                       "title": "$ ls", "kind": "execute", "status": "pending",
                       "rawInput": {"command": "ls"}}}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_thought_preview_is_marked_and_carries_size_and_start():
    mapper = acp_chat_events.ACPEventMapper()
    lines = mapper.map_update(thought("Let me "), stream=True)
    lines += mapper.map_update(thought("check the task."), stream=True)
    assert lines, "a thought must reach the preview channel"
    last = lines[-1]
    assert last["customType"] == acp_chat_events.PREVIEW_CUSTOM_TYPE
    assert acp_chat_events.is_volatile(last)
    assert last["data"]["block"] == "thinking"
    assert last["data"]["chars"] == len("Let me check the task.")
    assert isinstance(last["data"]["startedAt"], str) and last["data"]["startedAt"]
    # Both lines of one thought name the SAME start.
    assert lines[0]["data"]["startedAt"] == last["data"]["startedAt"]
    assert last["content"] == "Let me check the task."


def test_answer_preview_stays_unmarked():
    mapper = acp_chat_events.ACPEventMapper()
    lines = mapper.map_update(answer("Hallo"), stream=True)
    assert lines and "block" not in (lines[-1].get("data") or {})


def test_long_thought_preview_carries_only_the_end_bounded():
    mapper = acp_chat_events.ACPEventMapper()
    word = "reasoning step number forty two, "
    lines: list[dict] = []
    for _ in range(600):  # ~20 000 chars of thought
        lines += mapper.map_update(thought(word), stream=True)
    last = lines[-1]
    total = len(word) * 600
    assert last["data"]["chars"] == total
    assert len(last["content"]) <= acp_chat_events.THINKING_PREVIEW_TAIL_CHARS
    assert last["content"].strip().endswith("forty two,")
    # Linear, not quadratic: no line ever carries the whole thought.
    assert max(len(json.dumps(line)) for line in lines) < acp_chat_events.THINKING_PREVIEW_TAIL_CHARS + 600


def test_the_preview_tail_never_opens_mid_word():
    """Review R3: the cut took max() of the first newline and the first
    space — a paragraph longer than the 200-char window (first newline far
    in) meant NO cut at all, and the opened thought began mid-word."""
    text = "x" * 5000 + "abcdefgh word" + "y" * 900 + "\n" + "z" * 1086
    tail = acp_chat_events._tail_on_boundary(text, 2000)
    assert tail.startswith("word"), tail[:20]
    # A newline near the start still wins over a later space.
    text = "x" * 5000 + "ab\ncd ef" + "y" * 1991
    assert acp_chat_events._tail_on_boundary(text, 2000).startswith("cd ef")
    # No boundary in reach: the plain tail, never empty.
    text = "x" * 5000
    assert acp_chat_events._tail_on_boundary(text, 2000) == "x" * 2000


def test_finished_thought_lands_once_in_transcript_before_the_tool_call():
    clock = Clock()
    mapper = acp_chat_events.ACPEventMapper(clock=clock)
    out: list[dict] = []
    out += mapper.map_update(thought("Plan: read "), stream=True)
    clock.now += 105.0  # 1:45 of thinking
    out += mapper.map_update(thought("the file."), stream=True)
    out += mapper.map_update(tool_call(), stream=True)
    permanent = [e for e in out if not acp_chat_events.is_volatile(e)]
    kinds = [
        (e["message"]["content"][0]["type"] if e.get("type") == "message" else e.get("customType"))
        for e in permanent
    ]
    assert kinds[0] == "thinking", kinds
    assert kinds.count("thinking") == 1, kinds
    block = permanent[0]["message"]["content"][0]
    assert block["thinking"] == "Plan: read the file."
    assert block["durationMs"] == 105_000
    assert permanent[0]["message"]["role"] == "assistant"
    # A second update after that never repeats the thought.
    again = mapper.map_update(answer("Done."), stream=True)
    assert not any(e.get("type") == "message" and e["message"]["content"][0]["type"] == "thinking"
                   for e in again)


def test_thought_open_at_turn_end_is_kept_with_the_final_answer():
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update(thought("only thinking, then the turn ends"), stream=True)
    final = mapper.map_final_assistant_message("")
    assert final[0]["message"]["content"][0]["type"] == "thinking"
    # D7: the turn still ends with its stop line — no text left, so
    # ``content: []`` (the thought is never glued into it).
    assert [e["message"]["content"] for e in final[1:]] == [[]]
    assert final[-1]["message"]["stopReason"] == "stop"


def test_thought_open_at_chat_error_is_kept():
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update(thought("thinking before the engine died"), stream=True)
    lines = mapper.map_chat_error("rpc_error", "ECONNREFUSED")
    assert lines[0]["message"]["content"][0]["type"] == "thinking"
    assert lines[-1]["customType"] == acp_chat_events.CHAT_ERROR_CUSTOM_TYPE


def test_usage_update_mid_thought_does_not_end_it():
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update(thought("part one "), stream=True)
    mid = mapper.map_update({"update": {"sessionUpdate": "usage_update", "size": 100, "used": 10}},
                            stream=True)
    assert not [e for e in mid if not acp_chat_events.is_volatile(e)]
    mapper.map_update(thought("part two"), stream=True)
    final = mapper.map_final_assistant_message("")
    assert final[0]["message"]["content"][0]["thinking"] == "part one part two"


def test_backend_reader_turns_thought_preview_into_thinking_preview(tmp_path):
    mapper = acp_chat_events.ACPEventMapper()
    lines = []
    for w in ["Let ", "me ", "think."]:
        lines += mapper.dump(mapper.map_update(thought(w), stream=True))
    lines += mapper.dump(mapper.map_update(answer("Hi"), stream=True))
    pfile = tmp_path / "previews" / "p.jsonl"
    pfile.parent.mkdir()
    pfile.write_text("\n".join(
        line for line in lines if json.loads(line).get("customType") == acp_chat_events.PREVIEW_CUSTOM_TYPE
    ) + "\n", encoding="utf-8")
    events = _read_preview_channel({"path": pfile, "offset": 0, "buffer": b""})
    thinking = [e for e in events if e.get("block") == "thinking"]
    assert thinking, events
    assert thinking[-1]["kind"] == "preview"
    assert thinking[-1]["text"] == "Let me think."
    assert thinking[-1]["chars"] == len("Let me think.")
    assert thinking[-1]["startedAt"]
    # The answer preview stays a plain preview.
    assert events[-1]["text"] == "Hi" and events[-1].get("block") is None


def test_history_serves_the_finished_thought_with_its_duration(tmp_path):
    clock = Clock()
    mapper = acp_chat_events.ACPEventMapper(clock=clock)
    lines: list[str] = []
    lines += mapper.dump(mapper.map_update(thought("Weighing A against B."), stream=True))
    clock.now += 12.5
    lines += mapper.dump(mapper.map_update(answer("B."), stream=True))
    lines += mapper.dump(mapper.map_final_assistant_message("B."))
    permanent = [line for line in lines if not acp_chat_events.is_volatile(json.loads(line))]
    history = as_history(permanent, tmp_path)
    kinds = [e["kind"] for e in history["events"]]
    assert kinds == ["thinking", "message"], kinds
    assert history["events"][0]["text"] == "Weighing A against B."
    assert history["events"][0]["durationMs"] == 12_500
