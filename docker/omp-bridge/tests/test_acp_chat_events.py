#!/usr/bin/env python3
"""Subtask 3/4 — tests for the ACP event -> ChatEvent mapping.

The unit under test is docker/omp-bridge/acp_chat_events.py (pure mapping)
plus its integration point bridge.run_acp_once(transcript_sink=...). The
backend half of the pipeline (omp_chat.OmpLineParser, read_history) is REAL:
every mapped line is fed through the actual parser the /sessions chat view
uses, so the tests assert the observable contract — what a browser consuming
GET /agents/{id}/chat/history and the SSE stream would render — not
implementation details.

Fixtures: rpc/acp-*.ndjson, golden transcripts captured from REAL omp 18.1.10
runs (same fixtures test_acp_replay.py replays against the live client).

Runs two ways:
  * pytest:      pytest test_acp_chat_events.py -v   (in docker/omp-bridge/tests/)
  * standalone:  python3 test_acp_chat_events.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT.parent.parent / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))
import acp_chat_events  # noqa: E402
from app.services.omp_chat import OmpLineParser, peek_entry_id  # noqa: E402
from app.services.transcript_chat import read_history  # noqa: E402
from app.services import transcript_adapters  # noqa: E402

RPC = ROOT / "rpc"
PERMISSION_FIXTURE = RPC / "acp-permission-tool.ndjson"
NORMAL_FIXTURE = RPC / "acp-normal-turn.ndjson"


# ── helpers ─────────────────────────────────────────────────────────────────


def load_fixture_updates(path: Path) -> list[dict]:
    """All `session/update` params dicts from a golden fixture."""
    updates = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("method") == "session/update":
            updates.append(msg["params"])
    return updates


def map_fixture(path: Path) -> tuple[acp_chat_events.ACPEventMapper, list[str]]:
    mapper = acp_chat_events.ACPEventMapper()
    lines: list[str] = []
    for params in load_fixture_updates(path):
        lines += mapper.dump(mapper.map_update(params))
    return mapper, lines


def parse_all(lines: list[str]) -> list[dict]:
    parser = OmpLineParser()
    events: list[dict] = []
    for line in lines:
        events += parser(line)
    return events


def as_history(lines: list[str], tmp: Path) -> dict:
    """Write mapped lines to a session file and read them back through the
    REAL read_history path (adapter = omp), like the chat history endpoint."""
    tdir = tmp / "omp-sessions" / "--workspace--"
    tdir.mkdir(parents=True)
    session = tdir / "2026-09-08T00-00-00-000Z_acptest0001.jsonl"
    session.write_text("\n".join(lines) + "\n", encoding="utf-8")

    agent = type("Agent", (), {})()
    agent.slug = "alpha"
    agent.agent_runtime = "cli-bridge"
    agent.harness = "omp"
    adapter = transcript_adapters.adapter_for(agent)

    import app.services.omp_chat as omp_chat

    original = omp_chat.resolve_transcript_dir
    omp_chat.resolve_transcript_dir = lambda a: tdir.parent
    try:
        history = read_history(session, adapter, limit=200)
    finally:
        omp_chat.resolve_transcript_dir = original
    return history


def chunk(text: str, words: list[str]) -> list[dict]:
    """agent_message_chunk params, one per word, same messageId — the shape
    the golden fixture shows for a real streamed sentence."""
    return [
        {
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": w},
                "messageId": "msg-1",
            }
        }
        for w in words
    ]


# ── tests: word-wise streaming ──────────────────────────────────────────────


def test_message_chunks_stream_word_by_word():
    """Every chunk must grow the visible text — the acceptance criterion
    'wortweises Streaming'. Chunk N's full text must CONTAIN chunk N-1's."""
    mapper = acp_chat_events.ACPEventMapper()
    words = ["Anna", " ", "verkauft", " ", " heute", " ", "Software", "."]
    previous = ""
    for params in chunk("ignored", words):
        lines = mapper.dump(mapper.map_update(params))
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "message"
        assert entry["message"]["role"] == "assistant"
        text = entry["message"]["content"][0]["text"]
        assert text.startswith(previous), f"{text!r} lost prefix {previous!r}"
        previous = text
    assert previous == "Anna verkauft  heute Software."


def test_streamed_message_renders_as_assistant_chat_events():
    mapper, lines = map_fixture(NORMAL_FIXTURE)
    events = parse_all(lines)
    messages = [e for e in events if e["kind"] == "message" and e["role"] == "assistant"]
    assert messages, "streamed text must render as assistant messages"
    # The last event carries the complete streamed sentence.
    assert "hello golden fixture" in messages[-1]["text"]


def test_each_chunk_line_has_fresh_entry_id_for_dedup():
    """The tailer dedups on the top-level id — repeat ids would swallow the
    growing snapshots; the reducer replaces on equal ids. Fresh ids per flush
    keep every growth step visible."""
    mapper = acp_chat_events.ACPEventMapper()
    ids = []
    for params in chunk("", ["a", "b", "c"]):
        (line,) = mapper.dump(mapper.map_update(params))
        ids.append(peek_entry_id(line))
    assert len(set(ids)) == 3
    assert all(i.startswith("acp") for i in ids)


# ── tests: tool call -> tool card + result ─────────────────────────────────


def test_tool_call_seeds_card_and_update_sets_result():
    mapper, lines = map_fixture(PERMISSION_FIXTURE)
    events = parse_all(lines)
    tool_events = [e for e in events if e["kind"] == "tool"]
    assert tool_events, "tool_call must produce a tool card"
    card = tool_events[0]
    assert card["title"].startswith("$ echo")
    assert card["toolUseId"] == "chatcmpl-tool-9d5637e4d16bf556"

    # The toolResult line merges onto the card via toolUseId (what
    # read_history does for the history page and the tailer for live events).
    results = [e for e in events if e["kind"] == "_tool_result"]
    assert results, "completed tool_call_update must produce a result"
    merged = dict(card)
    from app.services.transcript_chat import _merge_tool_result

    _merge_tool_result(merged, results[0])
    assert "fixture-proof" in merged["result"]
    assert merged["status"] == "done"


def test_failed_tool_call_marks_error():
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update(
        {"update": {"sessionUpdate": "tool_call", "toolCallId": "t1",
                    "title": "$ boom", "kind": "execute", "rawInput": {"command": "boom"}}}
    )
    lines = mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                    "status": "failed",
                    "rawOutput": {"content": [{"type": "text", "text": "exit 1"}]}}}
    ))
    events = parse_all(lines)
    results = [e for e in events if e["kind"] == "_tool_result"]
    assert results and results[0]["is_error"] is True


def test_interim_update_produces_no_result_line():
    """in_progress updates must not spam result lines — the card exists, the
    output would overwrite itself repeatedly."""
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update(
        {"update": {"sessionUpdate": "tool_call", "toolCallId": "t1",
                    "title": "$ work", "kind": "execute", "rawInput": {}}}
    )
    lines = mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                    "status": "in_progress",
                    "rawOutput": {"content": [{"type": "text", "text": "partial"}]}}}
    ))
    assert parse_all(lines) == []


def test_update_without_seed_is_fail_closed():
    """A settled update for an unknown toolCallId must not invent a card."""
    mapper = acp_chat_events.ACPEventMapper()
    lines = mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "ghost",
                    "status": "completed", "rawOutput": {"content": []}}}
    ))
    assert parse_all(lines) == []


# ── tests: permission request visibility ───────────────────────────────────


def test_permission_request_and_decision_visible():
    mapper = acp_chat_events.ACPEventMapper()
    params = {
        "toolCall": {"toolCallId": "t9", "title": "rm -rf /tmp/x", "kind": "execute"},
        "options": [
            {"optionId": "allow_once", "name": "Allow once"},
            {"optionId": "reject_once", "name": "Reject"},
        ],
    }
    lines = mapper.dump(mapper.map_permission_request(params))
    lines += mapper.dump(mapper.map_permission_outcome(params, "allow_once"))
    events = parse_all(lines)
    messages = [e for e in events if e["kind"] == "message"]
    assert len(messages) == 2
    # teammate role — the operator did not type this, and the chat must not
    # attribute it to them (same rule as omp's file mentions).
    assert all(m["role"] == "teammate" for m in messages)
    assert "rm -rf /tmp/x" in messages[0]["text"]
    assert "allow_once" in messages[1]["text"]


def test_bridge_run_transcribes_permission_roundtrip(tmp_path):
    """Integration: run_acp_once with a sink must write request + decision
    lines when a permission request crosses the wire, plus the streamed
    chat events (chunks/tool/usage) from the same run."""
    import time as _time

    import bridge
    from test_acp_adapter import InProcessFake

    writes: list[str] = []
    fake = InProcessFake(PERMISSION_FIXTURE, [])
    try:
        outcome = bridge.run_acp_once(
            "mach was",
            cwd=str(HERE),
            model="m",
            max_time=10,
            permission_policy="yolo",
            task_id="T1",
            client_factory=lambda: fake.client,
            ask_fn=lambda task_id, question: "yes",
            transcript_sink=writes.extend,
        )
        assert outcome.final_stop_reason == "end_turn"
        # The reader thread delivers the replayed session/updates
        # asynchronously — give it the same courtesy the other adapter
        # tests' wait_until uses.
        deadline = _time.time() + 5
        while _time.time() < deadline and not any("toolCall" in w for w in writes):
            _time.sleep(0.05)
    finally:
        fake.close()
    joined = "\n".join(writes)
    assert "acp-permission" in joined
    # yolo policy allows execute kind always — decision line says so
    assert "allow_always" in joined
    # the run's own chat events streamed too
    assert '"type": "toolCall"' in joined or '"type":"toolCall"' in joined
    assert "fixture-proof" in joined


def test_usage_update_stamps_context_window():
    """Review #465 mid 3 (Option b): usage rides ONLY on the final assistant
    message (never per chunk — 0/0 garbage fed the token counter #393).
    The window from usage_update lands in that final line's usage dict."""
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update({"update": {"sessionUpdate": "usage_update", "size": 500000, "used": 17395}})
    chunk_lines = mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "x"}, "messageId": "m"}}
    ))
    chunk_events = parse_all(chunk_lines)
    assert not [e for e in chunk_events if e["kind"] == "usage"], \
        "chunks must never carry usage (0/0 garbage)"
    final_lines = mapper.dump(mapper.map_final_assistant_message("done"))
    events = parse_all(final_lines)
    usage = [e for e in events if e["kind"] == "usage"]
    assert usage, "final message must surface a usage event"
    raw = json.loads(final_lines[-1])
    assert raw["message"]["usage"]["contextWindow"] == 500000
    assert raw["message"]["usage"]["usedTokens"] == 17395


# ── tests: usage ────────────────────────────────────────────────────────────


def test_prompt_result_usage_flows_into_usage_event():
    mapper = acp_chat_events.ACPEventMapper()
    mapper.set_prompt_usage({"inputTokens": 34876, "outputTokens": 57})
    lines = mapper.dump(mapper.map_final_assistant_message("done"))
    usage = [e for e in parse_all(lines) if e["kind"] == "usage"]
    assert usage and usage[0]["inputTokens"] == 34876 and usage[0]["outputTokens"] == 57


# ── tests: user prompt + sink file layout ──────────────────────────────────


def test_user_prompt_renders_as_user_message():
    mapper = acp_chat_events.ACPEventMapper()
    lines = mapper.dump(mapper.map_user_prompt("mach was"))
    events = parse_all(lines)
    assert events == [
        {
            "kind": "message",
            "uuid": events[0]["uuid"],
            "ts": events[0]["ts"],
            "role": "user",
            "text": "mach was",
            "model": None,
            "sidechain": False,
        }
    ]


def test_sink_writes_session_file_in_omp_layout(tmp_path):
    agent_dir = tmp_path / "agent"
    directory = acp_chat_events.session_dir(
        agent_dir_env=str(agent_dir), cwd="/workspace/foo"
    )
    assert directory is not None
    assert directory.name == "--workspace-foo--"
    sink = acp_chat_events.ChatEventSink(directory, "sess-1")
    assert sink.path is not None and sink.path.name.endswith("_sess-1.jsonl")
    sink.write(['{"type":"message","id":"acp00001"}'])
    content = sink.path.read_text(encoding="utf-8").splitlines()
    assert len(content) == 2  # session header + one event
    assert json.loads(content[0])["type"] == "session"


def test_sink_degrades_to_noop_on_unwritable_dir(tmp_path):
    # A FILE where the agent dir should be -> mkdir fails -> None (fail closed)
    (tmp_path / "nope").touch()
    directory = acp_chat_events.session_dir(agent_dir_env=str(tmp_path / "nope"), cwd="/w")
    assert directory is None
    sink = acp_chat_events.ChatEventSink(None, "s")
    assert sink.path is None
    sink.write(["{}"])  # must not raise


def test_history_roundtrip_through_read_history(tmp_path):
    """The full backend path: mapped lines on disk -> read_history -> events
    a browser would render, tool result merged onto its card."""
    mapper = acp_chat_events.ACPEventMapper()
    lines = mapper.dump(mapper.map_user_prompt("task"))
    lines += mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Starting"}, "messageId": "m1"}}))
    lines += mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "tool_call", "toolCallId": "t1",
                    "title": "$ echo proof", "kind": "execute",
                    "rawInput": {"command": "echo proof"}}}))
    lines += mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                    "status": "completed",
                    "rawOutput": {"content": [{"type": "text", "text": "proof"}]}}}))
    lines += mapper.dump(mapper.map_update(
        {"update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Done"}, "messageId": "m2"}}))

    history = as_history(lines, tmp_path)
    kinds = [(e["kind"], e.get("role") or e.get("name") or "") for e in history["events"]]
    assert ("message", "user") in kinds
    tools = [e for e in history["events"] if e["kind"] == "tool"]
    assert tools and tools[0]["result"] == "proof" and tools[0]["status"] == "done"
    texts = [e["text"] for e in history["events"] if e["kind"] == "message" and e["role"] == "assistant"]
    assert "Starting" in texts and "Done" in texts


def test_mapper_never_raises_on_garbage():
    mapper = acp_chat_events.ACPEventMapper()
    for params in (
        {},
        {"update": None},
        {"update": {"sessionUpdate": "unknown_future_thing"}},
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "image"}}},
        {"update": {"sessionUpdate": "tool_call"}},
        {"update": {"sessionUpdate": "tool_call", "toolCallId": 42}},
    ):
        assert mapper.map_update(params) == []
    # non-serializable objects in dump are skipped, not raised
    assert mapper.dump([{"bad": object()}]) == []


def test_preview_contract_own_channel_not_transcript(tmp_path):
    """Folge-PR zu #471: stream-mode chunks go to their OWN channel — the
    sibling previews/ file — and NEVER into the transcript JSONL. Contract:
    - mapper.map_update(stream=True) still emits acp-preview lines (the
      established custom_message shape), but the BACKEND tailer reads them
      from the preview file and broadcasts uuid-less `preview` events
      (source "acp"),
    - the transcript (read_history) holds exactly ONE final assistant line
      and ZERO preview-shaped lines,
    - the tailer's channel reader turns the file's lines into the volatile
      events and advances its offset."""
    mapper = acp_chat_events.ACPEventMapper()
    stream_lines: list[str] = []
    for params in chunk("ignored", ["Hallo", " ", "Welt", "."]):
        stream_lines += mapper.dump(mapper.map_update(params, stream=True))
    final_lines = mapper.dump(mapper.map_final_assistant_message("Hallo Welt."))

    # The preview lines keep the established custom_message shape (what the
    # backend's _read_preview_channel filters on and what legacy files carry).
    for line in stream_lines:
        entry = json.loads(line)
        assert entry["type"] == "custom_message"
        assert entry["customType"] == acp_chat_events.PREVIEW_CUSTOM_TYPE

    # The tailer's channel reader turns those lines into the volatile events.
    from app.services.transcript_chat import _read_preview_channel

    pfile = tmp_path / "previews" / "p1.jsonl"
    pfile.parent.mkdir()
    pfile.write_text("\n".join(stream_lines) + "\n", encoding="utf-8")
    state = {"path": pfile, "offset": 0, "buffer": b""}
    events = _read_preview_channel(state, None)
    assert events, "preview channel lines must become preview events"
    for ev in events:
        assert ev["kind"] == "preview", ev
        assert ev["uuid"] is None
        assert ev["source"] == "acp"
    assert [e["text"] for e in events][-1] == "Hallo Welt."
    assert state["offset"] == pfile.stat().st_size, "reader must advance its offset"

    # History: the transcript NEVER carries the snapshots — ONE final
    # assistant line, no preview lines, no teammate duplicates.
    history = as_history(stream_lines + final_lines, tmp_path)
    messages = [e for e in history["events"] if e["kind"] == "message"]
    assert len(messages) == 1, messages
    assert messages[0]["role"] == "assistant"
    assert messages[0]["text"] == "Hallo Welt."


def test_preview_emit_throttled_own_sink_last_state_always_flushed():
    """Dritt-Review #471 (b) + Folge-PR: run_acp_once must NOT flush one
    preview line per chunk, and the flushes go to the PREVIEW sink (own
    channel), never the transcript sink. Throttle: >200 chars growth OR
    >250 ms since the last flush; the FINAL state always reaches the
    preview sink. End to end through run_acp_once + a fake ACP server
    replaying 200 chunk updates."""
    import bridge
    from test_acp_adapter import InProcessFake

    writes: list[str] = []
    preview_writes: list[str] = []
    fake = InProcessFake(RPC / "acp-many-chunks.ndjson", [])
    try:
        outcome = bridge.run_acp_once(
            "throttle test",
            cwd=str(HERE),
            model="m",
            max_time=10,
            permission_policy="yolo",
            task_id="T1",
            client_factory=lambda: fake.client,
            transcript_sink=writes.extend,
            preview_sink=preview_writes.extend,
        )
        assert outcome.final_stop_reason == "end_turn"
    finally:
        fake.close()

    # NOTHING preview-shaped may land in the transcript — the volatile
    # snapshots left the transcript JSONL entirely.
    for w in writes:
        assert json.loads(w).get("customType") != acp_chat_events.PREVIEW_CUSTOM_TYPE, w

    previews = [json.loads(w) for w in preview_writes]
    # Throttled: far fewer preview lines than the 200 streamed chunks.
    assert len(previews) < 50, \
        f"throttle failed: {len(previews)} preview lines for 200 chunks"
    assert len(previews) >= 2, "at least first + last snapshot must flush"
    # Snapshots grow monotonically; the LAST one carries the complete text.
    sizes = [len(p["content"]) for p in previews]
    assert sizes == sorted(sizes), sizes
    assert previews[-1]["content"] == outcome.final_text, (
        "the final preview flush must carry the COMPLETE turn text"
    )
    # The turn still ends with the ONE permanent assistant line (the user
    # prompt is also a `message` — filter by role).
    finals = [json.loads(w) for w in writes
              if json.loads(w).get("type") == "message"
              and json.loads(w).get("message", {}).get("role") == "assistant"]
    assert len(finals) == 1, len(finals)


# ── standalone runner ───────────────────────────────────────────────────────

if __name__ == "__main__":
    failures = 0

    def run(name, fn, *args):
        global failures
        try:
            fn(*args)
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        run("word_streaming", test_message_chunks_stream_word_by_word)
        run("assistant_events", test_streamed_message_renders_as_assistant_chat_events)
        run("fresh_ids", test_each_chunk_line_has_fresh_entry_id_for_dedup)
        run("tool_card_result", test_tool_call_seeds_card_and_update_sets_result)
        run("failed_tool", test_failed_tool_call_marks_error)
        run("interim_quiet", test_interim_update_produces_no_result_line)
        run("fail_closed_update", test_update_without_seed_is_fail_closed)
        run("permission_visible", test_permission_request_and_decision_visible)
        run("bridge_permission", test_bridge_run_transcribes_permission_roundtrip, tmp)
        run("usage_window", test_usage_update_stamps_context_window)
        run("prompt_usage", test_prompt_result_usage_flows_into_usage_event)
        run("user_prompt", test_user_prompt_renders_as_user_message)
        run("sink_layout", test_sink_writes_session_file_in_omp_layout, tmp)
        run("preview_contract", test_preview_contract_own_channel_not_transcript, tmp)
        run("preview_throttle", test_preview_emit_throttled_own_sink_last_state_always_flushed)
    sys.exit(1 if failures else 0)
