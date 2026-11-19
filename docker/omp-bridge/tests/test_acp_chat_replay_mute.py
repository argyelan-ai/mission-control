#!/usr/bin/env python3
"""S1-R2 — the replay of a `session/load` stays out of the chat.

omp answers `session/load` by replaying the WHOLE stored conversation as
`session/update` notifications, and only then replies. Measured live
(20.10.2026, omp 18.10.1, one real chat): 4 user, 7 agent, 33 `tool_call`,
33 `tool_call_update` and 19 thought chunks = 96 updates. None of them is
new. Before S1-R2 the chat daemon ran every one of them through the mapper:

* after a crash of the omp child (`_restart_child`) the reload APPENDED the
  old tool calls and results (66 lines) plus the old thoughts to the very
  transcript the chat shows, and the old answer text into the preview;
* at start-up the sink did not exist yet, so nothing was written — but the
  mapper kept the replay's last OPEN thought, and the first card written
  after the load (`queue_lost`, `session_reset`, later `turn_interrupted`)
  carried that old thought into the chat (#902 flushes a thought before
  every card).

Contract: `ChatSession._connect` loads inside `_muted_replay()`. While it
runs, only state updates (config options, commands, mode, session info) are
applied — to `acp-chat-state.json`. Nothing reaches the mapper, the
transcript, the preview or the turn text. The switch closes in `finally`,
also when the load fails, and the mapper's turn buffers are empty after it.
One log line counts what was replayed.

Every assertion reads what the backend reads (the JSONL files, the state
mirror), except the two that pin the mapper itself (no call, no open thought).
"""
from __future__ import annotations

import collections
import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import acp_client  # noqa: E402

SID = "sid-replay"
NEW_SID = "sid-fresh"

SESSION_RESULT = {
    "configOptions": [
        {"id": "thinking", "category": "thought_level", "currentValue": "medium",
         "options": [{"value": "medium", "name": "Medium"},
                     {"value": "high", "name": "High"}]},
    ],
}

#: The live-measured mix of one replay (see the module docstring).
LIVE_MIX = {
    "user_message_chunk": 4,
    "agent_message_chunk": 7,
    "tool_call": 33,
    "tool_call_update": 33,
    "agent_thought_chunk": 19,
}


# ── the replay omp sends on session/load ────────────────────────────────────


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def live_replay() -> list[dict]:
    """96 updates in the live mix, as four turns. The LAST turn was cut off
    while the model was thinking — so the replay ends on an open thought,
    the case where a leaked thought reaches the next card."""
    tools = [9, 8, 8, 8]
    thoughts = [5, 5, 5, 3]
    answers = [2, 2, 2, 1]
    out: list[dict] = []
    tool_no = 0
    for turn in range(4):
        out.append({"sessionUpdate": "user_message_chunk",
                    "content": _text(f"old question {turn}")})
        for i in range(tools[turn]):
            if i < thoughts[turn]:
                out.append({"sessionUpdate": "agent_thought_chunk",
                            "messageId": f"th-{turn}-{i}",
                            "content": _text(f"old thought {turn}.{i}")})
            tool_no += 1
            tid = f"call-{tool_no}"
            out.append({"sessionUpdate": "tool_call", "toolCallId": tid,
                        "title": f"$ step {tool_no}", "kind": "execute",
                        "status": "pending", "rawInput": {"command": "true"}})
            out.append({"sessionUpdate": "tool_call_update", "toolCallId": tid,
                        "status": "completed",
                        "rawOutput": {"content": [_text(f"old output {tool_no}")]}})
        for i in range(answers[turn]):
            out.append({"sessionUpdate": "agent_message_chunk",
                        "messageId": f"ans-{turn}",
                        "content": _text(f"old answer {turn}.{i} ")})
    out.append({"sessionUpdate": "agent_thought_chunk", "messageId": "th-cut",
                "content": _text("old thought that never ended")})
    return out


def test_fixture_replays_the_live_measured_mix():
    """Guard on the fixture itself: the numbers the plan measured live."""
    mix = collections.Counter(u["sessionUpdate"] for u in live_replay())
    assert dict(mix) == LIVE_MIX
    assert sum(mix.values()) == 96
    assert live_replay()[-1]["sessionUpdate"] == "agent_thought_chunk"


# ── in-memory ACP client ────────────────────────────────────────────────────


class _Proc:
    def __init__(self) -> None:
        self.dead = False

    def poll(self):
        return 1 if self.dead else None


class ReplayClient:
    """Stand-in for `acp_client.ACPClient`. `load_session` fires `replay`
    through the event callbacks BEFORE it returns or raises — the order the
    real client guarantees (one reader thread, callbacks run synchronously,
    the load reply is read after the last replayed update).

    `turns` is consumed one per `prompt()`: `{"updates": [...]}` fired
    during the turn, `{"die": True}` = the child exits mid-turn."""

    def __init__(self, *, replay=(), load_result=None, load_raises=None, turns=()):
        self.replay = list(replay)
        self.load_result = dict(SESSION_RESULT if load_result is None else load_result)
        self.load_raises = load_raises
        self.turns = list(turns)
        self.last_session_result: dict = {}
        self._proc = _Proc()
        self._events: list = []
        self.calls: list[tuple] = []
        self.loading = False  # True while load_session replays

    def on_event(self, cb):
        self._events.append(cb)

    def on_permission(self, cb):
        pass

    def _ensure_process(self):
        pass

    def initialize(self, timeout=30.0):
        return {}

    def new_session(self, cwd, mcp_servers=None, timeout=60.0):
        self.calls.append(("new_session",))
        self.last_session_result = dict(SESSION_RESULT)
        return NEW_SID

    def load_session(self, session_id, cwd, mcp_servers=None, timeout=60.0):
        self.calls.append(("load_session", session_id))
        self.loading = True
        try:
            for update in self.replay:
                self.fire({"sessionId": session_id, "update": update})
        finally:
            self.loading = False
        if self.load_raises is not None:
            raise self.load_raises
        self.last_session_result = dict(self.load_result)
        return self.last_session_result

    def set_config_option(self, session_id, key, value, timeout=30.0):
        return {}

    def prompt(self, session_id, text, timeout=600.0):
        self.calls.append(("prompt", text))
        turn = self.turns.pop(0) if self.turns else {}
        for update in turn.get("updates") or []:
            self.fire({"sessionId": session_id, "update": update})
        if turn.get("die"):
            self._proc.dead = True
            raise acp_client.ACPError("omp acp stdout closed unexpectedly")
        return acp_client.PromptResult(stopReason="end_turn", usage=None)

    def cancel(self, session_id=None):
        pass

    def close(self, timeout=5.0):
        pass

    def fire(self, params):
        for cb in list(self._events):
            cb(params)


def make_session(tmp_path: Path, *clients: ReplayClient) -> acp_chat.ChatSession:
    """One ChatSession; each `_connect` (start, child restart) takes the next
    client — a respawned child is a NEW client, as in production."""
    queue = list(clients)
    return acp_chat.ChatSession(
        client_factory=lambda: queue.pop(0),
        cwd=str(tmp_path / "work"),
        state_dir=tmp_path / "state",
        sessions_dir=tmp_path / "sessions",
    )


def seed_pointer(tmp_path: Path, sid: str = SID) -> Path:
    """What a previous daemon leaves behind: its transcript file (one header
    line, as ChatEventSink writes it) and the pointer `acp-chat.json`."""
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    sink = acp_chat_events.ChatEventSink(tmp_path / "sessions", sid)
    assert sink.path is not None
    sink.write(acp_chat_events.ACPEventMapper.dump(
        acp_chat_events.ACPEventMapper().map_user_prompt("a question from before")))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "acp-chat.json").write_text(
        json.dumps({"sessionId": sid, "transcript": str(sink.path)}), encoding="utf-8")
    return sink.path


def seed_lost_queue(tmp_path: Path) -> None:
    """The previous daemon's state mirror still held one queued message — so
    start() writes a `queue_lost` card right after the load."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "acp-chat-state.json").write_text(json.dumps({
        "queue": [{"id": "q1", "kind": "prompt", "text": "held back", "at": "x"}],
    }), encoding="utf-8")


def read_lines(path) -> list[dict]:
    if path is None or not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def tool_and_thought_lines(lines: list[dict]) -> list[dict]:
    """Tool calls, tool results and thinking blocks — what a replay leaks."""
    out = []
    for line in lines:
        msg = line.get("message") or {}
        if msg.get("role") == "toolResult":
            out.append(line)
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("toolCall", "thinking"):
                out.append(line)
                break
    return out


def card_codes(lines: list[dict]) -> list[str]:
    return [(line.get("data") or {}).get("code") for line in lines
            if line.get("customType") == acp_chat_events.CHAT_ERROR_CUSTOM_TYPE]


def snapshot_previews(tmp_path: Path) -> dict[str, bytes]:
    pdir = tmp_path / "sessions" / acp_chat_events.MC_SINK_DIRNAME / "previews"
    if not pdir.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in pdir.glob("*.jsonl")}


class CountingMapper:
    """Counts `map_update` calls made while the client replays a load —
    'no mapper call' is the contract, so the test checks it directly."""

    def __init__(self, session: acp_chat.ChatSession, client: ReplayClient) -> None:
        self.during_load = 0
        real = session._mapper.map_update

        def counting(params, *, stream=False):
            if client.loading:
                self.during_load += 1
            return real(params, stream=stream)

        session._mapper.map_update = counting


# ── 1. a crashed child is reloaded without repeating anything ──────────────


def test_1_child_restart_writes_only_the_process_exit_card(tmp_path, caplog):
    first = ReplayClient(turns=[
        {"updates": [{"sessionUpdate": "agent_message_chunk", "messageId": "a1",
                      "content": _text("first answer")}]},
        {"die": True},
    ])
    respawned = ReplayClient(replay=live_replay())
    sess = make_session(tmp_path, first, respawned)
    sess.start()
    sess.prompt("first")
    assert sess.wait_idle(timeout=5)
    before = read_lines(sess.transcript_path)
    previews_before = snapshot_previews(tmp_path)
    assert previews_before, "the first turn must have written a preview"

    caplog.set_level(logging.INFO, logger="acp_chat")
    sess.prompt("second")  # the child dies in this turn -> _restart_child
    assert sess.wait_idle(timeout=5)
    try:
        assert ("load_session", NEW_SID) in respawned.calls
        added = read_lines(sess.transcript_path)[len(before):]
        assert tool_and_thought_lines(added) == [], (
            f"{len(tool_and_thought_lines(added))} replayed tool/thought lines "
            "landed in the chat")
        # The dying turn's own user line, then its card — nothing else.
        assert [line.get("customType") or (line.get("message") or {}).get("role")
                for line in added] == ["user", acp_chat_events.CHAT_ERROR_CUSTOM_TYPE]
        assert card_codes(added) == ["process_exit"]
        after = snapshot_previews(tmp_path)
        for name, body in previews_before.items():
            assert after.get(name) == body, f"preview {name} changed during the reload"
        assert all(not body for name, body in after.items() if name not in previews_before)
        assert sess._full_text == [], "replayed answer text reached the turn buffer"
        logged = [r.getMessage() for r in caplog.records
                  if r.getMessage().startswith("load replayed")]
        assert len(logged) == 1, logged
        assert logged[0].startswith("load replayed 96 updates (muted)")
        for kind, count in LIVE_MIX.items():
            assert f"{kind}={count}" in logged[0], logged[0]
    finally:
        sess.close()


# ── 2. start() with a stored pointer: nothing old reaches the chat ─────────


def test_2a_start_with_pointer_writes_no_old_line_and_keeps_no_open_thought(tmp_path):
    transcript = seed_pointer(tmp_path)
    seed_lost_queue(tmp_path)
    before = read_lines(transcript)
    client = ReplayClient(replay=live_replay())
    sess = make_session(tmp_path, client)
    counter = CountingMapper(sess, client)
    sess.start()
    try:
        assert ("load_session", SID) in client.calls
        assert sess.transcript_path == transcript
        assert counter.during_load == 0, (
            f"{counter.during_load} replayed updates went through the mapper")
        assert sess._mapper._open_thought is None
        added = read_lines(transcript)[len(before):]
        # The first card after the load must not carry the replay's open
        # thought: the ONLY new line is the queue_lost card itself.
        assert tool_and_thought_lines(added) == []
        assert card_codes(added) == ["queue_lost"]
        assert len(added) == 1, added
    finally:
        sess.close()


def test_2b_after_a_load_no_earlier_open_thought_rides_out_with_the_next_card(tmp_path):
    """The thought buffer is empty after EVERY load, whatever the mapper
    held before it. Today the only caller that reloads mid-life
    (`_run_turn` -> `_restart_child`) flushes the dying turn's thought with
    its `process_exit` card first; S1-R4/R5 and P4 add callers of
    `_connect`, so the guarantee sits in the load, not in that caller."""
    first = ReplayClient()
    respawned = ReplayClient(replay=live_replay())
    sess = make_session(tmp_path, first, respawned)
    sess.start()
    try:
        first.fire({"sessionId": NEW_SID, "update": {
            "sessionUpdate": "agent_thought_chunk", "messageId": "th-open",
            "content": _text("a thought nobody closed")}})
        assert sess._mapper._open_thought is not None  # precondition
        before = read_lines(sess.transcript_path)
        sess._restart_child()
        assert sess._mapper._open_thought is None
        sess._report_lost_queue([{"kind": "prompt", "text": "held back"}])
        added = read_lines(sess.transcript_path)[len(before):]
        assert tool_and_thought_lines(added) == [], added
        assert card_codes(added) == ["queue_lost"]
    finally:
        sess.close()


# ── 3. state updates during the replay still reach the state mirror ───────


def test_3_state_updates_during_the_replay_land_in_the_state_file(tmp_path):
    """Muting must not swallow state. The load reply here carries no
    options (ACP allows a server to send them only as notifications), so
    the mirror can only know them from the replayed updates."""
    transcript = seed_pointer(tmp_path)
    before = read_lines(transcript)
    replay = live_replay()
    replay.insert(10, {"sessionUpdate": "config_option_update", "configOptions": [
        {"id": "thinking", "currentValue": "high", "options": []},
        {"id": "mode", "currentValue": "default", "options": []},
    ]})
    replay.insert(20, {"sessionUpdate": "current_mode_update", "currentModeId": "plan"})
    replay.insert(30, {"sessionUpdate": "available_commands_update",
                       "availableCommands": [{"name": "compact", "description": "c"}]})
    replay.insert(40, {"sessionUpdate": "session_info_update", "title": "Replayed title"})
    client = ReplayClient(replay=replay, load_result={})
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        state = json.loads(sess.state_file.read_text(encoding="utf-8"))
        options = {o["id"]: o["currentValue"] for o in state["configOptions"]}
        assert options == {"thinking": "high", "mode": "plan"}, options
        assert [c["name"] for c in state["commands"]] == ["compact"]
        assert state["sessionInfo"].get("title") == "Replayed title"
        # current_mode_update is state only while replaying: no mode line.
        added = read_lines(transcript)[len(before):]
        assert [line for line in added
                if line.get("customType") == acp_chat_events.MODE_CUSTOM_TYPE] == []
    finally:
        sess.close()


# ── 4. after the load, a normal turn is mapped again ───────────────────────


NEXT_TURN = {"updates": [
    {"sessionUpdate": "agent_thought_chunk", "messageId": "th-new",
     "content": _text("new thought")},
    {"sessionUpdate": "tool_call", "toolCallId": "call-new", "title": "$ ls",
     "kind": "execute", "status": "pending", "rawInput": {"command": "ls"}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "call-new",
     "status": "completed", "rawOutput": {"content": [_text("file.txt")]}},
    {"sessionUpdate": "agent_message_chunk", "messageId": "ans-new",
     "content": _text("new answer")},
]}


def _assert_next_turn_mapped(sess: acp_chat.ChatSession, before: int) -> None:
    sess.prompt("next question")
    assert sess.wait_idle(timeout=5)
    added = read_lines(sess.transcript_path)[before:]
    kinds = []
    for line in added:
        msg = line.get("message") or {}
        if msg.get("role") == "toolResult":
            kinds.append("toolResult")
        elif msg:
            blocks = [b.get("type") for b in msg.get("content") or []]
            kinds.append(f"{msg.get('role')}:{'+'.join(blocks)}")
        else:
            kinds.append(line.get("customType"))
    assert kinds == ["user:text", "assistant:thinking", "assistant:toolCall",
                     "toolResult", "assistant:text"], kinds
    assert added[-1]["message"]["content"][0]["text"] == "new answer"
    assert card_codes(added) == []
    assert read_lines(sess.preview_path), "the new turn must reach the preview again"


def test_4a_after_a_load_the_next_turn_is_mapped_again(tmp_path):
    seed_pointer(tmp_path)
    client = ReplayClient(replay=live_replay(), turns=[NEXT_TURN])
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        _assert_next_turn_mapped(sess, len(read_lines(sess.transcript_path)))
    finally:
        sess.close()


def test_4b_after_a_failed_load_the_next_turn_is_mapped_again(tmp_path):
    """Defensive case: a load can fail AFTER (part of) its replay — the
    agent dies while it replays (broken pipe), the reply times out, or an
    agent errors late. Then the daemon opens a new session and says so in
    a card, and the switch must still be off: no replayed line and no
    replayed thought in front of that card, and the next turn is mapped."""
    seed_pointer(tmp_path)
    client = ReplayClient(
        replay=live_replay(),
        load_raises=acp_client.ACPError("omp acp stdout closed unexpectedly"),
        turns=[NEXT_TURN],
    )
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        assert sess.state()["sessionId"] == NEW_SID
        lines = read_lines(sess.transcript_path)
        # The reset card is the new transcript's only entry: no replayed
        # line and no replayed thought in front of it.
        assert card_codes(lines) == ["session_reset"]
        assert tool_and_thought_lines(lines) == []
        assert sess._mapper._open_thought is None
        _assert_next_turn_mapped(sess, len(lines))
    finally:
        sess.close()


# ── 5. a malformed update is skipped quietly ───────────────────────────────


#: `sessionUpdate` values an agent should never send. A list or a dict is
#: unhashable, so a plain `su in <frozenset>` would raise on it.
MALFORMED = [["tool_call"], {"kind": "tool_call"}, None, 7]


def test_5_a_non_string_session_update_never_raises(tmp_path, caplog):
    """`sessionUpdate` comes from the agent. A value that is not a string
    is skipped like any unknown update — during a replay (counted as
    `unknown`) and outside it — and never logs an `event handling failed`
    traceback. A normal turn still maps afterwards."""
    seed_pointer(tmp_path)
    replay = live_replay()
    replay[5:5] = [{"sessionUpdate": su} for su in MALFORMED]
    client = ReplayClient(replay=replay, turns=[NEXT_TURN])
    sess = make_session(tmp_path, client)
    caplog.set_level(logging.INFO, logger="acp_chat")
    sess.start()
    try:
        for su in MALFORMED:
            client.fire({"sessionId": SID, "update": {"sessionUpdate": su}})
        failed = [r.getMessage() for r in caplog.records
                  if r.getMessage() == "event handling failed"]
        assert failed == [], f"{len(failed)} malformed updates raised"
        logged = [r.getMessage() for r in caplog.records
                  if r.getMessage().startswith("load replayed")]
        assert len(logged) == 1, logged
        assert logged[0].startswith("load replayed 100 updates (muted)"), logged[0]
        assert "unknown=4" in logged[0], logged[0]
        _assert_next_turn_mapped(sess, len(read_lines(sess.transcript_path)))
    finally:
        sess.close()
