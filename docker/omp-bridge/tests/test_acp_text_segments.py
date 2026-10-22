#!/usr/bin/env python3
"""The agent's text is kept where it was written (D7 + F1, spec A PR-1).

Before: the transcript held exactly ONE permanent assistant text line per
turn (Review #465 Option b), written at the turn's end. In a long turn the
agent's words between tool calls only lived in the preview slot and were
replaced by the next one, then all of them appeared as one block at the
end — below every tool call. The task path (``bridge.run_acp_once``) also
glued every THOUGHT into that one line: a finished bench task ended with a
514,640-character assistant line holding all 54 thoughts a second time.

Contract tested here (docs/specs/chat-over-acp.md, "Text segments"):

* one permanent assistant text line per text SEGMENT. A segment ends at a
  tool call, the start of a thought, a plan, a permission/elicitation card,
  a chat notice/error line, or the turn end;
* only the turn's last assistant line carries ``stopReason`` and ``usage``
  (``content: []`` when no text is left);
* a turn without boundaries still writes exactly one line (unchanged);
* the final line never repeats flushed text and never holds thought text;
  ``outcome.final_text`` (task classification) stays byte-identical;
* a preview snapshot of a flushed segment is never written after it.

All fixtures are synthetic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT.parent.parent / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

import acp_chat_events  # noqa: E402
from app.services.omp_chat import OmpLineParser, transcript_suggests_turn_ended  # noqa: E402

USAGE = {"inputTokens": 1200, "outputTokens": 85}


# ── helpers ─────────────────────────────────────────────────────────────────


def text(t: str, mid: str = "m-1") -> dict:
    return {"update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": t}, "messageId": mid}}


def thought(t: str, mid: str = "t-1") -> dict:
    return {"update": {"sessionUpdate": "agent_thought_chunk",
                       "content": {"type": "text", "text": t}, "messageId": mid}}


def tool_call(tid: str = "call-1", cmd: str = "ls") -> dict:
    return {"update": {"sessionUpdate": "tool_call", "toolCallId": tid,
                       "title": f"$ {cmd}", "kind": "execute", "status": "pending",
                       "rawInput": {"command": cmd}}}


def tool_done(tid: str = "call-1", out: str = "ok") -> dict:
    return {"update": {"sessionUpdate": "tool_call_update", "toolCallId": tid,
                       "status": "completed",
                       "rawOutput": {"content": [{"type": "text", "text": out}]}}}


def plan() -> dict:
    return {"update": {"sessionUpdate": "plan",
                       "entries": [{"content": "step one", "status": "pending"}]}}


def run_mapper(updates: list[dict], final_text: str = "", *, stop: str = "stop",
               usage: dict | None = USAGE, cut_forms=None
               ) -> tuple[acp_chat_events.ACPEventMapper, list[dict]]:
    """Map a turn the way both callers do: permanent lines only (previews
    go to their own channel), then the final line."""
    mapper = acp_chat_events.ACPEventMapper()
    mapper.begin_turn()
    lines: list[dict] = []
    for params in updates:
        lines += [e for e in mapper.map_update(params, stream=True)
                  if not acp_chat_events.is_volatile(e)]
    mapper.set_prompt_usage(usage)
    kw = {} if cut_forms is None else {"cut_forms": cut_forms}
    lines += mapper.map_final_assistant_message(final_text, stop_reason=stop, **kw)
    return mapper, lines


def assistant_lines(lines: list[dict]) -> list[dict]:
    return [ln for ln in lines if ln.get("type") == "message"
            and (ln.get("message") or {}).get("role") == "assistant"]


def kind_of(line: dict) -> str:
    blocks = (line.get("message") or {}).get("content") or []
    if not blocks:
        return "empty"
    return blocks[0].get("type")


def text_of(line: dict) -> str:
    return "".join(b.get("text", "") for b in (line.get("message") or {}).get("content") or []
                   if b.get("type") == "text")


def shape(lines: list[dict]) -> list[str]:
    """thinking / text:<words> / toolCall / empty, in file order."""
    out = []
    for ln in assistant_lines(lines):
        k = kind_of(ln)
        out.append(f"text:{text_of(ln)}" if k == "text" else k)
    return out


def parse(lines: list[dict]) -> list[dict]:
    parser = OmpLineParser()
    events: list[dict] = []
    for ln in lines:
        events += parser(json.dumps(ln))
    return events


def write_session(tmp: Path, lines: list[dict]) -> Path:
    path = tmp / "2026-10-22T00-00-00-000Z_sid-seg.jsonl"
    header = {"type": "session", "version": 3, "id": "sid-seg",
              "timestamp": "2026-10-22T00:00:00.000Z", "cwd": "/workspace"}
    path.write_text("\n".join(json.dumps(x) for x in [header, *lines]) + "\n")
    return path


# ── 1. a tool call ends a segment ───────────────────────────────────────────


def test_1_text_before_a_tool_call_is_its_own_line_only_the_last_carries_usage():
    _, lines = run_mapper(
        [text("A"), text("B"), tool_call(), tool_done(), text("C", mid="m-2")],
        final_text="ABC",
    )
    msgs = assistant_lines(lines)
    assert shape(lines) == ["text:AB", "toolCall", "text:C"]
    first, last = msgs[0]["message"], msgs[-1]["message"]
    assert first.get("stopReason") is None and "usage" not in first
    assert last["stopReason"] == "stop" and last["usage"]["input"] == 1200
    events = parse(lines)
    assert [e["text"] for e in events if e["kind"] == "message"] == ["AB", "C"]
    assert len([e for e in events if e["kind"] == "usage"]) == 1


# ── 2./3. the turn-ended probe stays correct ────────────────────────────────


def test_2_turn_ending_after_a_tool_writes_an_empty_final_line_the_probe_sees(tmp_path):
    _, lines = run_mapper([text("Checking."), tool_call(), tool_done()],
                          final_text="Checking.")
    last = assistant_lines(lines)[-1]
    assert last["message"]["content"] == []
    assert last["message"]["stopReason"] == "stop"
    assert "usage" in last["message"]
    assert shape(lines).count("text:Checking.") == 1, "flushed text never comes twice"
    assert transcript_suggests_turn_ended(write_session(tmp_path, lines)) is True
    # The empty final line is only usage for the chat: no bubble.
    assert [e["kind"] for e in parse([last])] == ["usage"]


def test_3_mid_turn_after_a_flushed_segment_the_probe_says_working(tmp_path):
    mapper = acp_chat_events.ACPEventMapper()
    lines: list[dict] = []
    for params in [text("Let me look."), thought("hmm")]:
        lines += [e for e in mapper.map_update(params, stream=True)
                  if not acp_chat_events.is_volatile(e)]
    assert shape(lines) == ["text:Let me look."]
    assert transcript_suggests_turn_ended(write_session(tmp_path, lines)) is False


# ── 4. a turn without boundaries: one line, unchanged ──────────────────────


def test_4_a_turn_without_boundaries_still_writes_exactly_one_line():
    _, lines = run_mapper([text("Hello"), text(" world.")], final_text="Hello world.")
    msgs = assistant_lines(lines)
    assert len(msgs) == 1
    assert text_of(msgs[0]) == "Hello world."
    assert msgs[0]["message"]["stopReason"] == "stop"


# ── 5. order is the order it happened ───────────────────────────────────────


def test_5_thought_text_thought_tool_text_keeps_its_order():
    _, lines = run_mapper([
        thought("plan it", mid="t-1"), text("First I read.", mid="m-1"),
        thought("now run", mid="t-2"), tool_call(), tool_done(),
        text("Done.", mid="m-2"),
    ], final_text="First I read.Done.")
    assert shape(lines) == ["thinking", "text:First I read.", "thinking", "toolCall",
                            "text:Done."]


def test_5b_a_plan_ends_a_segment():
    _, lines = run_mapper([text("Here is my plan."), plan(), text("Starting.")],
                          final_text="Here is my plan.Starting.")
    kinds = [ln.get("customType") or shape([ln])[0] for ln in lines]
    assert kinds == ["text:Here is my plan.", acp_chat_events.PLAN_CUSTOM_TYPE, "text:Starting."]


# ── 6. the next preview shows only the new segment ──────────────────────────


def test_6_after_a_flush_the_next_preview_holds_only_the_new_segment():
    mapper = acp_chat_events.ACPEventMapper()
    for params in [text("Old words. "), tool_call()]:
        mapper.map_update(params, stream=True)
    preview = [e for e in mapper.map_update(text("New."), stream=True)
               if acp_chat_events.is_volatile(e)]
    assert preview[-1]["content"] == "New."


# ── 10. permission / question / notice / error end a segment ───────────────


def test_10_permission_request_mid_text_lands_after_the_text():
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update(text("I will delete the build folder."), stream=True)
    lines = mapper.map_permission_request(
        {"toolCall": {"title": "rm -rf build", "kind": "execute"},
         "options": [{"name": "Allow"}]})
    assert [ln["type"] for ln in lines] == ["message", "custom_message"]
    assert text_of(lines[0]) == "I will delete the build folder."
    assert lines[1]["customType"] == "acp-permission"


@pytest.mark.parametrize("card", ["elicitation", "notice", "error"])
def test_10b_question_notice_and_error_cards_land_after_the_text(card):
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update(text("Before the card."), stream=True)
    if card == "elicitation":
        lines = mapper.map_elicitation("el-1", "Which branch?", {"type": "object"})
    elif card == "notice":
        lines = mapper.map_chat_notice("model_fallback", "Another model is used.")
    else:
        lines = mapper.map_chat_error("rpc_error", "boom")
    assert text_of(lines[0]) == "Before the card."
    assert lines[-1]["type"] == "custom_message"
    # The rest of the turn starts empty: the final line repeats nothing.
    final = mapper.map_final_assistant_message("Before the card.", stop_reason="stop")
    assert final[-1]["message"]["content"] == []


# ── 11. abort without a prompt result keeps the open text (N11) ────────────


@pytest.mark.parametrize("updates,expected", [
    ([thought("thinking hard"), text("Half an ans")], ["thinking", "text:Half an ans"]),
    ([text("Said this."), thought("then thinking")], ["text:Said this.", "thinking"]),
], ids=["open-text", "open-thought"])
def test_11_flush_on_abort_keeps_the_open_text_and_thought(updates, expected):
    mapper = acp_chat_events.ACPEventMapper()
    lines: list[dict] = []
    for params in updates:
        lines += [e for e in mapper.map_update(params, stream=True)
                  if not acp_chat_events.is_volatile(e)]
    lines += mapper.flush_on_abort()
    assert shape(lines) == expected
    assert all(ln["message"].get("stopReason") is None for ln in lines)
    assert mapper.flush_on_abort() == [], "nothing twice"


# ── 13. an empty chunk is no boundary (review #936 S1) ─────────────────────


def test_13_empty_or_non_text_thought_chunk_does_not_split_the_text():
    empty = {"update": {"sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": ""}, "messageId": "t-1"}}
    invalid = {"update": {"sessionUpdate": "agent_thought_chunk",
                          "content": {"type": "image", "data": "x"}, "messageId": "t-2"}}
    missing = {"update": {"sessionUpdate": "agent_thought_chunk", "messageId": "t-3"}}
    _, lines = run_mapper([text("Hello "), empty, text("world"), invalid, missing, text("!")],
                          final_text="Hello world!")
    assert shape(lines) == ["text:Hello world!"], shape(lines)
    assert assistant_lines(lines)[-1]["message"]["stopReason"] == "stop"


def test_13b_empty_text_chunk_does_not_split_the_thought():
    empty = {"update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": ""}, "messageId": "m-1"}}
    _, lines = run_mapper([thought("first half "), empty, thought("second half"),
                           text("Answer.")], final_text="Answer.")
    assert shape(lines) == ["thinking", "text:Answer."], shape(lines)
    think = assistant_lines(lines)[0]["message"]["content"][0]["thinking"]
    assert think == "first half second half"


def test_13c_a_real_thought_chunk_still_ends_the_text():
    """Guard against over-fixing: a thought WITH words is a boundary."""
    _, lines = run_mapper([text("Hello "), thought("hmm"), text("world")],
                          final_text="world")
    assert shape(lines) == ["text:Hello ", "thinking", "text:world"], shape(lines)


# ── 12. reasoning parsers that alternate every few tokens (N14) ─────────────


def test_12_alternating_thought_and_text_tokens_keep_order_and_count():
    """Synthetic worst case: thought/text chunks alternating every 1-3
    tokens. Every switch is a boundary, so the count is the number of runs —
    recorded here so a live file with >20 mini lines a minute is a known
    follow-up ("same messageId = same segment"), not a surprise."""
    runs = [("t", "a b"), ("m", "Hi"), ("t", "c"), ("m", " there"), ("t", "d e f"),
            ("m", " friend.")]
    updates = [thought(s) if k == "t" else text(s) for k, s in runs]
    _, lines = run_mapper(updates, final_text="Hi there friend.")
    assert shape(lines) == ["thinking", "text:Hi", "thinking", "text: there", "thinking",
                            "text: friend."]
    assert len(assistant_lines(lines)) == 6


# ── a turn that only thought still ends with its stop line ─────────────────


def test_thought_only_turn_still_writes_the_stop_line_with_usage():
    """Before, the task path glued the thoughts into the final line, so such
    a turn still ended with stopReason + usage. Without the thoughts there
    is no text left — the turn keeps its stop line (``content: []``)."""
    _, lines = run_mapper([thought("only thinking"), tool_call(), tool_done()],
                          final_text="")
    assert shape(lines) == ["thinking", "toolCall", "empty"]
    assert assistant_lines(lines)[-1]["message"]["stopReason"] == "stop"


def test_a_turn_with_nothing_at_all_still_writes_no_line():
    _, lines = run_mapper([], final_text="")
    assert lines == []


# ── 7. the task path: no thought text in the final line (F1) ───────────────


def _fixture(tmp_path: Path, updates: list[dict], *, stop: str = "end_turn",
             error: dict | None = None) -> Path:
    sid = "00000000-0000-0000-0000-00000000seg1"
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentInfo": {
            "name": "fake", "version": "0"}, "agentCapabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": sid, "configOptions": []}},
    ]
    for params in updates:
        msgs.append({"jsonrpc": "2.0", "method": "session/update",
                     "params": {"sessionId": sid, **params}})
    if error is not None:
        msgs.append({"jsonrpc": "2.0", "id": 3, "error": error})
    else:
        msgs.append({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": stop, "usage": USAGE}})
    path = tmp_path / "turn.ndjson"
    path.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
    return path


def _run_task(tmp_path: Path, updates: list[dict], **kw):
    import bridge
    from test_acp_adapter import InProcessFake

    no_result = kw.pop("no_result", False)
    order: list[tuple[str, dict]] = []
    fake = InProcessFake(_fixture(tmp_path, updates, **kw), [], fake_delay=0.0)
    if no_result:
        # A client whose prompt ends WITHOUT a result and without raising:
        # no error card flushes the open text, only the abort net can.
        real_prompt = fake.client.prompt

        def prompt_without_result(*a, **k):
            real_prompt(*a, **k)
            return None

        fake.client.prompt = prompt_without_result
    try:
        outcome = bridge.run_acp_once(
            "segment test", cwd=str(HERE), model=None, max_time=10,
            permission_policy="yolo", task_id="00000000-0000-0000-0000-000000000000",
            client_factory=lambda: fake.client,
            transcript_sink=lambda ls: order.extend(("t", json.loads(x)) for x in ls),
            preview_sink=lambda ls: order.extend(("p", json.loads(x)) for x in ls),
        )
    finally:
        fake.close()
    return outcome, order


def _transcript(order):
    return [e for ch, e in order if ch == "t"]


def test_7_task_final_line_holds_reply_text_only_final_text_unchanged(tmp_path):
    outcome, order = _run_task(tmp_path, [
        thought("SECRET-THOUGHT-1 about the task"), tool_call(), tool_done(),
        thought("SECRET-THOUGHT-2 checking"), text("All tests pass. "), text("Done."),
    ])
    lines = _transcript(order)
    last = assistant_lines(lines)[-1]["message"]
    assert last["stopReason"] == "stop"
    assert text_of(assistant_lines(lines)[-1]) == "All tests pass. Done."
    assert "SECRET-THOUGHT" not in json.dumps([ln for ln in assistant_lines(lines)
                                               if kind_of(ln) == "text"])
    # Classification input is untouched: thoughts + reply, arrival order.
    assert outcome.final_text == ("SECRET-THOUGHT-1 about the task"
                                  "SECRET-THOUGHT-2 checking"
                                  "All tests pass. Done.")


def test_7b_the_glued_giant_line_is_gone(tmp_path):
    """Synthetic replay of the measured shape (finished task: final line
    514,640 chars, all 54 thoughts inside it in order, and each thought
    ALSO stored as its own thinking line). Here: 12 thoughts of 4,000 chars
    between tool calls, then a short reply."""
    updates: list[dict] = []
    thoughts = []
    for i in range(12):
        body = f"[thought {i:02d}] " + ("x" * 3990)
        thoughts.append(body)
        updates += [thought(body, mid=f"t-{i}"), tool_call(f"c-{i}"), tool_done(f"c-{i}")]
    updates.append(text("Result: 3 files changed.", mid="m-end"))
    outcome, order = _run_task(tmp_path, updates)
    lines = _transcript(order)
    texts = [ln for ln in assistant_lines(lines) if kind_of(ln) == "text"]
    thinking = [ln for ln in assistant_lines(lines) if kind_of(ln) == "thinking"]
    assert len(thinking) == 12, "every thought is kept once, as its own line"
    assert [text_of(ln) for ln in texts] == ["Result: 3 files changed."]
    biggest = max(len(json.dumps(ln)) for ln in texts)
    assert biggest < 2_000, f"final line is {biggest} bytes — thoughts glued in again"
    assert sum(len(t) for t in thoughts) < len(outcome.final_text)


# ── 9. a withheld snapshot of a flushed segment never comes after it ────────


def test_9_task_path_never_writes_a_stale_snapshot_after_the_segment(tmp_path):
    _, order = _run_task(tmp_path, [
        text("First part"), text(" still the first."),  # 2nd is withheld (throttle)
        tool_call(), tool_done(),
    ])
    assert shape(_transcript(order))[:2] == ["text:First part still the first.", "toolCall"]
    seg_at = next(i for i, (ch, e) in enumerate(order)
                  if ch == "t" and text_of(e).startswith("First part"))
    later = [e for ch, e in order[seg_at + 1:] if ch == "p"
             and "First part" in str(e.get("content"))]
    assert later == [], f"stale preview after the segment: {later}"


def _chat_turn(tmp_path: Path, updates: list[dict], *, turn: dict | None = None,
               omp: list | None = None):
    """One chat-daemon turn firing ``updates`` (raw session/update params)."""
    from test_acp_chat import make_session, read_lines
    from test_acp_chat_model_unreachable import OmpClient

    class SegClient(OmpClient):
        def prompt(self, session_id, text_, timeout=600.0):
            for params in updates:
                self.fire({"sessionId": session_id, **params})
            return super().prompt(session_id, text_, timeout=timeout)

    spec = dict(turn or {"stopReason": "end_turn", "usage": USAGE})
    if omp is not None:
        spec["omp"] = omp
    client = SegClient(tmp_path / "sessions", turns=[spec])
    sess = make_session(tmp_path, client)
    real_preview = sess._emit_preview
    order: list[tuple[str, dict]] = []

    def spy_preview(entries):
        order.extend(("p", e) for e in entries)
        real_preview(entries)

    real_transcript = sess._emit_transcript

    def spy_transcript(entries):
        order.extend(("t", e) for e in entries)
        real_transcript(entries)

    sess._emit_preview = spy_preview
    sess._emit_transcript = spy_transcript
    sess.start()
    sess.prompt("go", origin="operator")
    assert sess.wait_idle(timeout=5)
    lines = read_lines(sess.transcript_path)
    sess.close()
    return lines, order


def test_9b_chat_daemon_never_writes_a_stale_snapshot_after_the_segment(tmp_path):
    _, order = _chat_turn(tmp_path, [
        text("First part"), text(" still the first."), tool_call(), tool_done(),
    ])
    assert shape(_transcript(order))[:2] == ["text:First part still the first.", "toolCall"]
    seg_at = next(i for i, (ch, e) in enumerate(order)
                  if ch == "t" and text_of(e).startswith("First part"))
    later = [e for ch, e in order[seg_at + 1:] if ch == "p"
             and "First part" in str(e.get("content"))]
    assert later == [], f"stale preview after the segment: {later}"


def test_9c_chat_daemon_writes_segments_and_one_stop_line(tmp_path):
    lines, _ = _chat_turn(tmp_path, [
        text("Looking."), tool_call(), tool_done(), text("Found it.", mid="m-2"),
    ])
    assert shape(lines) == ["text:Looking.", "toolCall", "text:Found it."]
    stops = [ln for ln in assistant_lines(lines) if ln["message"].get("stopReason")]
    assert len(stops) == 1 and stops[0] is assistant_lines(lines)[-1]


def test_11c_chat_daemon_abort_net_keeps_the_open_text_when_no_card_comes(tmp_path):
    """Review #936 S2: the prompt dies with an EMPTY message — no chat
    error card is written, so only ``flush_on_abort()`` keeps the open
    words. Without the net they were lost silently."""
    from test_acp_chat import make_session, read_lines
    from test_acp_chat_model_unreachable import OmpClient

    updates = [text("Before."), tool_call(), tool_done(), text("Open words", mid="m-2")]

    class DyingClient(OmpClient):
        def prompt(self, session_id, text_, timeout=600.0):
            for params in updates:
                self.fire({"sessionId": session_id, **params})
            raise RuntimeError()

    client = DyingClient(tmp_path / "sessions", turns=[{"stopReason": "end_turn"}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("go", origin="operator")
    assert sess.wait_idle(timeout=5)
    lines = read_lines(sess.transcript_path)
    sess.close()
    assert shape(lines) == ["text:Before.", "toolCall", "text:Open words"], shape(lines)


# ── 8. S1: the verdict cuts only the END of the mapper's own rest ──────────


UNABLE = "Unable to connect. Is the computer able to access the url?"


def test_8_flushed_segment_then_omps_error_line_nothing_lost_nothing_twice(tmp_path):
    from test_acp_chat_model_unreachable import omp_assistant, omp_tool_result, omp_tool_use

    lines, _ = _chat_turn(
        tmp_path,
        [text("Hello\n\n"), tool_call(), tool_done(), text(UNABLE, mid="m-2")],
        omp=[omp_tool_use(), omp_tool_result(), omp_assistant("error", error=UNABLE)],
    )
    texts = [text_of(ln) for ln in assistant_lines(lines) if kind_of(ln) == "text"]
    assert texts == ["Hello\n\n"], texts
    assert UNABLE not in json.dumps(assistant_lines(lines))
    last = assistant_lines(lines)[-1]["message"]
    assert last["content"] == [] and last["stopReason"] == "stop"
    cards = [ln for ln in lines if ln.get("customType") == acp_chat_events.CHAT_ERROR_CUSTOM_TYPE]
    assert [c["data"]["code"] for c in cards] == ["model_unreachable"]


def test_8b_rest_with_words_before_omps_line_keeps_the_words(tmp_path):
    from test_acp_chat_model_unreachable import omp_assistant, omp_tool_result, omp_tool_use

    lines, _ = _chat_turn(
        tmp_path,
        [text("Hello\n\n"), tool_call(), tool_done(), text("Done.\n\n" + UNABLE, mid="m-2")],
        omp=[omp_tool_use(), omp_tool_result(), omp_assistant("error", error=UNABLE)],
    )
    texts = [text_of(ln) for ln in assistant_lines(lines) if kind_of(ln) == "text"]
    assert texts == ["Hello\n\n", "Done."], texts


def test_8c_mapper_cut_forms_empty_tuple_hides_the_whole_rest():
    _, lines = run_mapper([text("Seen."), tool_call(), text("ECONNREFUSED", mid="m-2")],
                          final_text="", cut_forms=())
    assert shape(lines) == ["text:Seen.", "toolCall", "empty"]


def test_11d_task_path_abort_net_keeps_the_open_text_without_a_result(tmp_path):
    """Review #936 N1: the bridge's ``flush_on_abort()`` net, reached when
    the prompt yields no result and no error card was written."""
    _, order = _run_task(tmp_path, [thought("pondering"), text("Before."), tool_call(),
                                    tool_done(), text("Open words", mid="m-2")],
                         no_result=True)
    assert shape(_transcript(order)) == ["thinking", "text:Before.", "toolCall",
                                         "text:Open words"], shape(_transcript(order))


# ── 11b. the task path's error exit keeps the open text before the card ────


def test_11b_task_prompt_error_keeps_the_open_text_before_the_card(tmp_path):
    _, order = _run_task(tmp_path, [text("Partial answer")],
                         error={"code": -32603, "message": "Internal error"})
    lines = _transcript(order)
    kinds = [("text:" + text_of(ln)) if ln.get("type") == "message" else ln.get("customType")
             for ln in lines if ln.get("type") != "message"
             or (ln.get("message") or {}).get("role") == "assistant"]
    assert kinds == ["text:Partial answer", acp_chat_events.CHAT_ERROR_CUSTOM_TYPE]
