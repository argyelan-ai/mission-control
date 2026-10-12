"""omp 18.10.1 over ACP: live tool output, per-edit diffs, file locations,
session cost (chat package P1 "what is it doing right now").

Fixture rpc/acp-tool-live-diff-cost-18.10.1.ndjson is the verbatim wire
stream of a REAL `omp acp` 18.10.1 run (throwaway container without
network, 12.10.2026) driven by the bridge's own ACPClient. The model
endpoint was a local OpenAI-compatible stub that answered

  1. a `bash` call (`for i in 1..5; echo step $i; sleep 1`) -> omp streams
     six `tool_call_update` frames with status `in_progress`, each carrying
     the WHOLE output so far in rawOutput.content (a snapshot, not a delta),
  2. an `edit` call (PI_EDIT_VARIANT=replace) -> the settled update carries
     ACP `content` type `diff` (path/oldText/newText, whole file) and
     `locations` (also on the `tool_call` seed),
  3. text -> the end-of-turn `usage_update` carries `cost`
     ({amount, currency}) because the stub model was given a price.

Every assertion reads what the backend reads: transcript lines (permanent)
and preview-channel lines (volatile).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat_events  # noqa: E402
from test_acp_adapter import InProcessFake  # noqa: E402

FIXTURE = ROOT / "rpc" / "acp-tool-live-diff-cost-18.10.1.ndjson"
TOOL_OUTPUT = acp_chat_events.TOOL_OUTPUT_CUSTOM_TYPE


class StepClock:
    """Deterministic monotonic clock: every read advances by `step` seconds."""

    def __init__(self, step: float = 1.0):
        self.t = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def _wire() -> list[dict]:
    return [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]


def _updates() -> list[dict]:
    return [m["params"] for m in _wire() if m.get("method") == "session/update"]


def _run_turn(clock=None):
    fake = InProcessFake(FIXTURE, [], fake_delay=0.0)
    client = fake.client
    mapper = acp_chat_events.ACPEventMapper(clock=clock or StepClock())
    mapped: list[dict] = []

    client.on_event(lambda params: mapped.extend(mapper.map_update(params, stream=True)))
    client.on_permission(lambda _req: "allow_once")
    try:
        client.initialize(timeout=10)
        sid = client.new_session(cwd=str(HERE), timeout=10)
        client.set_config_option(sid, "model", "mc-openai/mock-model", timeout=10)
        result = client.prompt(sid, "run the loop, then edit", timeout=20)
    finally:
        fake.close()
    return mapper, mapped, result


def _bash_progress(text: str, tool_id: str = "call_x") -> dict:
    return {
        "sessionId": "s",
        "update": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_id,
            "status": "in_progress",
            "rawOutput": {"content": [{"type": "text", "text": text}], "details": {}},
        },
    }


def _seed(mapper, tool_id: str = "call_x") -> None:
    mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "tool_call", "toolCallId": tool_id, "title": "$ make",
        "kind": "execute", "status": "pending", "rawInput": {"command": "make"},
    }}, stream=True)


# ── the fixture itself ──────────────────────────────────────────────────────


def test_fixture_is_a_real_omp_18_10_1_run_with_all_four_shapes():
    wire = _wire()
    assert wire[0]["result"]["agentInfo"]["version"] == "18.10.1"
    ups = [p["update"] for p in _updates()]
    progress = [u for u in ups if u.get("sessionUpdate") == "tool_call_update"
                and u.get("status") == "in_progress"]
    assert len(progress) >= 5
    # Snapshot semantics: every frame carries the whole output so far.
    texts = [u["rawOutput"]["content"][0]["text"] for u in progress]
    assert texts[0] == "step 1\n" and texts[3].startswith("step 1\nstep 2\nstep 3\nstep 4")
    diffs = [c for u in ups if u.get("sessionUpdate") == "tool_call_update"
             for c in (u.get("content") or []) if c.get("type") == "diff"]
    assert diffs and diffs[0]["path"].endswith("demo.py")
    assert any(u.get("locations") for u in ups if u.get("sessionUpdate") == "tool_call")
    cost = [u["cost"] for u in ups if u.get("sessionUpdate") == "usage_update"]
    assert cost == [{"amount": 0.012855, "currency": "USD"}]


# ── 4. live tool output ─────────────────────────────────────────────────────


def test_in_progress_updates_become_volatile_tool_output_lines():
    _mapper, mapped, _result = _run_turn()
    outs = [m for m in mapped if m.get("customType") == TOOL_OUTPUT]
    assert len(outs) >= 5, "omp's running-command frames were dropped"
    for o in outs:
        assert o["type"] == "custom_message"
        assert o["data"]["toolCallId"] == "call_bash_1"
        assert acp_chat_events.is_volatile(o), "tool output must ride the preview channel"
    assert outs[0]["content"] == "step 1"
    assert outs[-1]["content"].startswith("step 1\nstep 2\nstep 3\nstep 4\nstep 5")
    # Nothing volatile is mistaken for a permanent line, and vice versa.
    perm = [m for m in mapped if not acp_chat_events.is_volatile(m)]
    assert all(m.get("customType") != TOOL_OUTPUT for m in perm)


def test_tool_output_strips_ansi_and_control_sequences():
    mapper = acp_chat_events.ACPEventMapper(clock=StepClock())
    _seed(mapper)
    out = mapper.map_update(_bash_progress("\x1b[31mred\x1b[0m ok\r\n\x1b]0;title\x07done\n"), stream=True)
    assert [o["content"] for o in out] == ["red ok\ndone"]


def test_tool_output_is_tail_capped_at_a_line_boundary():
    mapper = acp_chat_events.ACPEventMapper(clock=StepClock())
    _seed(mapper)
    big = "".join(f"line {i:05d}\n" for i in range(5000))
    out = mapper.map_update(_bash_progress(big), stream=True)
    text = out[0]["content"]
    assert len(text) <= acp_chat_events._TOOL_OUTPUT_TAIL_CHARS + 2
    assert text.endswith("line 04999"), "the newest output must survive the cap"
    assert text.startswith("…\nline "), "cut at a whole line, marked as cut"


def test_tool_output_is_throttled_and_the_newest_snapshot_flushes_later():
    clock = StepClock(step=0.0)
    mapper = acp_chat_events.ACPEventMapper(clock=clock)
    _seed(mapper)
    first = mapper.map_update(_bash_progress("a\n"), stream=True)
    second = mapper.map_update(_bash_progress("a\nb\n"), stream=True)  # same instant
    assert [o["content"] for o in first] == ["a"]
    assert second == [], "a burst inside the interval must coalesce"
    clock.t += 1.0
    # ANY later update (here: an unrelated one) flushes the withheld snapshot.
    later = mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "session_info_update", "updatedAt": "x"}}, stream=True)
    assert [o["content"] for o in later] == ["a\nb"]


def test_identical_snapshot_is_not_rewritten():
    mapper = acp_chat_events.ACPEventMapper(clock=StepClock())
    _seed(mapper)
    assert mapper.map_update(_bash_progress("same\n"), stream=True)
    assert mapper.map_update(_bash_progress("same\n"), stream=True) == []


def test_settled_tool_drops_its_withheld_snapshot():
    clock = StepClock(step=0.0)
    mapper = acp_chat_events.ACPEventMapper(clock=clock)
    _seed(mapper)
    mapper.map_update(_bash_progress("a\n"), stream=True)
    mapper.map_update(_bash_progress("a\nb\n"), stream=True)  # withheld
    done = mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "tool_call_update", "toolCallId": "call_x", "status": "completed",
        "rawOutput": {"content": [{"type": "text", "text": "a\nb\n"}]}}}, stream=True)
    clock.t += 5
    after = mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "session_info_update"}}, stream=True)
    assert [d["message"]["role"] for d in done] == ["toolResult"]
    assert [a for a in after if a.get("customType") == TOOL_OUTPUT] == [], \
        "a finished tool must not get a stale live snapshot after its result"


def test_unknown_tool_progress_and_empty_output_stay_quiet():
    mapper = acp_chat_events.ACPEventMapper(clock=StepClock())
    assert mapper.map_update(_bash_progress("x", tool_id="never-seeded"), stream=True) == []
    _seed(mapper)
    assert mapper.map_update(_bash_progress("   \n"), stream=True) == []


def test_final_tool_result_text_is_ansi_free_too():
    mapper = acp_chat_events.ACPEventMapper(clock=StepClock())
    _seed(mapper)
    done = mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "tool_call_update", "toolCallId": "call_x", "status": "completed",
        "rawOutput": {"content": [{"type": "text", "text": "\x1b[32mPASS\x1b[0m"}]}}}, stream=True)
    assert done[0]["message"]["content"][0]["text"] == "PASS"


# ── 5. diff per edit + 8. locations ─────────────────────────────────────────


def test_edit_result_carries_the_acp_diff_and_locations():
    _mapper, mapped, _result = _run_turn()
    results = [m for m in mapped if m.get("type") == "message"
               and m["message"].get("role") == "toolResult"
               and m["message"]["toolCallId"] == "call_edit_1"]
    assert len(results) == 1
    details = results[0]["message"]["details"]
    assert details["diffs"] == [{
        "path": "/tmp/ws/demo.py",
        "oldText": "def value():\n    return 1\n",
        "newText": "def value():\n    # doubled\n    return 2\n",
    }]
    assert details["locations"] == ["/tmp/ws/demo.py"]
    # The bash result has neither — no empty keys (K3: nothing empty).
    bash = [m for m in mapped if m.get("type") == "message"
            and m["message"].get("toolCallId") == "call_bash_1"]
    assert "diffs" not in bash[0]["message"]["details"]
    assert "locations" not in bash[0]["message"]["details"]


def test_tool_seed_carries_locations_on_the_tool_call_block():
    _mapper, mapped, _result = _run_turn()
    seeds = [b for m in mapped if m.get("type") == "message"
             for b in m["message"].get("content", []) if b.get("type") == "toolCall"]
    edit = next(b for b in seeds if b["id"] == "call_edit_1")
    assert edit["locations"] == ["/tmp/ws/demo.py"]
    bash = next(b for b in seeds if b["id"] == "call_bash_1")
    assert "locations" not in bash


def test_diff_text_is_capped_and_marked():
    mapper = acp_chat_events.ACPEventMapper(clock=StepClock())
    mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "tool_call", "toolCallId": "e", "kind": "edit", "title": "edit"}}, stream=True)
    huge = "x\n" * 100_000
    done = mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "tool_call_update", "toolCallId": "e", "status": "completed",
        "content": [{"type": "diff", "path": "/w/a.txt", "oldText": None, "newText": huge},
                    {"type": "diff", "path": 7, "newText": "bad path"}]}}, stream=True)
    diffs = done[0]["message"]["details"]["diffs"]
    assert len(diffs) == 1, "a diff without a string path is dropped (fail closed)"
    assert diffs[0]["oldText"] is None, "a created file keeps oldText null"
    assert len(diffs[0]["newText"]) <= acp_chat_events._DIFF_TEXT_LIMIT
    assert diffs[0]["truncated"] is True


# ── 12. cost ────────────────────────────────────────────────────────────────


def test_usage_update_cost_lands_on_the_final_line_as_session_cost():
    mapper, _mapped, result = _run_turn()
    mapper.set_prompt_usage(result.usage)
    final = mapper.map_final_assistant_message("MOCK-OK P1 probe")
    usage = final[0]["message"]["usage"]
    assert usage["sessionCost"] == {"amount": 0.012855, "currency": "USD"}
    assert usage["contextWindow"] == 131072


def test_no_or_zero_cost_means_no_session_cost_key():
    for cost in (None, {"amount": 0, "currency": "USD"}, {"amount": "1"}, {"amount": -2},
                 {"amount": float("nan"), "currency": "USD"}):
        mapper = acp_chat_events.ACPEventMapper(clock=StepClock())
        update = {"sessionUpdate": "usage_update", "size": 1000, "used": 10}
        if cost is not None:
            update["cost"] = cost
        mapper.map_update({"sessionId": "s", "update": update}, stream=True)
        final = mapper.map_final_assistant_message("ok")
        assert "sessionCost" not in final[0]["message"]["usage"], cost


# ── routing through both callers ────────────────────────────────────────────


def test_run_acp_once_routes_tool_output_to_the_preview_sink_only():
    import bridge

    writes: list[str] = []
    preview_writes: list[str] = []
    fake = InProcessFake(FIXTURE, [], fake_delay=0.0)
    try:
        bridge.run_acp_once(
            "p1 routing", cwd=str(HERE), model="mc-openai/mock-model", max_time=20,
            permission_policy="yolo", task_id="T1",
            client_factory=lambda: fake.client,
            transcript_sink=writes.extend, preview_sink=preview_writes.extend,
        )
    finally:
        fake.close()
    perm = [json.loads(w) for w in writes]
    vol = [json.loads(w) for w in preview_writes]
    assert all(p.get("customType") != TOOL_OUTPUT for p in perm)
    tool_out = [v for v in vol if v.get("customType") == TOOL_OUTPUT]
    assert tool_out, "live tool output never reached the preview channel"
    assert "step 1" in tool_out[0]["content"]
    # The permanent side still settles the card with the final output.
    results = [p for p in perm if p.get("message", {}).get("role") == "toolResult"]
    assert any("Wall time" in r["message"]["content"][0]["text"] for r in results)


def test_chat_daemon_routes_tool_output_to_the_preview_sink(tmp_path):
    from test_acp_chat import FakeClient, SESSION_RESULT, make_session, read_lines

    sess = make_session(tmp_path, FakeClient(session_result=SESSION_RESULT, turns=[]))
    sess.start()
    try:
        for params in _updates():
            sess._on_event(params)
    finally:
        sess.close()
    perm = read_lines(sess.transcript_path)
    vol = read_lines(sess.preview_path)
    assert not [p for p in perm if p.get("customType") == TOOL_OUTPUT]
    assert [v for v in vol if v.get("customType") == TOOL_OUTPUT]


# ── end to end: bridge lines -> the backend's own readers ───────────────────


def test_end_to_end_backend_sees_live_output_diff_locations_and_cost(tmp_path):
    """The real fixture through run_acp_once's sinks, then through the
    backend's REAL readers: the preview-channel reader (live tool output)
    and read_history (diff, locations, true +/- stats, session cost)."""
    import bridge
    from test_acp_chat_events import as_history
    from app.services.transcript_chat import _read_preview_channel

    writes: list[str] = []
    preview_writes: list[str] = []
    fake = InProcessFake(FIXTURE, [], fake_delay=0.0)
    try:
        bridge.run_acp_once(
            "p1 e2e", cwd=str(HERE), model="mc-openai/mock-model", max_time=20,
            permission_policy="yolo", task_id="T1",
            client_factory=lambda: fake.client,
            transcript_sink=writes.extend, preview_sink=preview_writes.extend,
        )
    finally:
        fake.close()

    pfile = tmp_path / "previews" / "p.jsonl"
    pfile.parent.mkdir()
    pfile.write_text("\n".join(preview_writes) + "\n", encoding="utf-8")
    live = [e for e in _read_preview_channel({"path": pfile, "offset": 0, "buffer": b""})
            if e["kind"] == "tool_output"]
    # The replay fires all six frames within milliseconds, so the throttle
    # lets the first through and the settled result supersedes the rest
    # (the unit tests above pin the coalescing itself).
    assert live and all(e["toolUseId"] == "call_bash_1" and e["uuid"] is None for e in live)
    assert live[0]["text"].startswith("step 1")

    history = as_history(writes, tmp_path)
    tools = {e["toolUseId"]: e for e in history["events"] if e["kind"] == "tool"}
    edit = tools["call_edit_1"]
    assert edit["locations"] == ["/tmp/ws/demo.py"]
    assert edit["stats"] == {"additions": 2, "deletions": 1}
    (diff,) = edit["diffs"]
    assert [(ln["t"], ln["text"]) for ln in diff["lines"] if ln["t"] != "hunk"] == [
        ("ctx", "def value():"), ("del", "    return 1"),
        ("add", "    # doubled"), ("add", "    return 2"),
    ]
    assert "Wall time" in tools["call_bash_1"]["result"]
    assert "diffs" not in tools["call_bash_1"]
    assert not [e for e in history["events"] if e["kind"] == "tool_output"]
    usage = [e for e in history["events"] if e["kind"] == "usage"]
    assert usage and usage[-1]["sessionCost"] == {"amount": 0.012855, "currency": "USD"}


def test_pump_flushes_a_withheld_snapshot_when_the_command_goes_quiet():
    """A burst then silence: no further update arrives, yet the newest
    snapshot must reach the preview channel within about one interval."""
    import threading
    import time as _time

    mapper = acp_chat_events.ACPEventMapper()
    _seed(mapper)
    got: list[dict] = []
    arrived = threading.Event()

    def emit(entries):
        got.extend(entries)
        arrived.set()

    assert [o["content"] for o in mapper.map_update(_bash_progress("1\n"), stream=True)] == ["1"]
    assert mapper.map_update(_bash_progress("1\n2\n3\n"), stream=True) == []  # withheld
    pump = acp_chat_events.ToolOutputPump(mapper, emit, interval=0.05).start()
    try:
        assert arrived.wait(acp_chat_events._TOOL_OUTPUT_INTERVAL_S + 1.0)
    finally:
        pump.stop()
    assert [g["content"] for g in got] == ["1\n2\n3"]
    # ids stay unique across the reader thread and the pump.
    later = mapper.map_update(_bash_progress("1\n2\n3\n4\n"), stream=True)
    _time.sleep(0)
    ids = [g["id"] for g in got] + [entry["id"] for entry in later]
    assert len(ids) == len(set(ids))
