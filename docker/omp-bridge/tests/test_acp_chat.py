#!/usr/bin/env python3
"""PR A — tests for the long-lived ACP chat daemon (acp_chat.py).

The unit under test is `acp_chat.ChatSession` (pure core, injected client
factory) plus the Unix-socket server and the `acp_chat_ctl.py` shim. The
mapping half (acp_chat_events) is REAL: every assertion reads the JSONL the
backend's chat reader would read, not an internal buffer.

Runs two ways:
  * pytest:      pytest test_acp_chat.py -v   (in docker/omp-bridge/tests/)
  * standalone:  python3 test_acp_chat.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import acp_client  # noqa: E402


# ── in-memory fake ACP client ───────────────────────────────────────────────


class FakeClient:
    """Scripted stand-in for ACPClient — no process, no pipes.

    `turns` is consumed one entry per prompt():
      {"chunks": [...]}            streamed agent_message_chunks
      {"stopReason": "end_turn"}   terminal reason (default end_turn)
      {"usage": {...}}             session/prompt result usage
      {"raise": exc}               raise instead of returning
      {"block": True}              block until cancel() (then stopReason)
      {"gate": threading.Event()}  block until the test sets the event
    """

    def __init__(self, *, turns=None, session_result=None, load_raises=None,
                 new_ids=None, new_raises=None):
        self.turns = list(turns or [])
        # session/new ids handed out in order (a fresh session per call);
        # empty = always session_result's id, the single-session default.
        self._new_ids = list(new_ids or [])
        self._new_raises = new_raises
        self.last_session_result = session_result or {}
        self._session_result = session_result or {}
        self._load_raises = load_raises
        self.calls: list[tuple] = []
        self.closed = False
        self._events: list = []
        self._permission = None
        self._cancelled = threading.Event()

    # -- ACPClient surface used by ChatSession ----------------------------
    def on_event(self, cb):
        self._events.append(cb)

    def on_permission(self, cb):
        self._permission = cb

    def _ensure_process(self):
        self.calls.append(("_ensure_process",))

    def initialize(self, timeout=30.0):
        self.calls.append(("initialize",))
        return {}

    def new_session(self, cwd, mcp_servers=None, timeout=60.0):
        self.calls.append(("new_session", cwd))
        if self._new_raises is not None and any(c[0] == "new_session" for c in self.calls[:-1]):
            raise self._new_raises
        self.last_session_result = dict(self._session_result)
        if self._new_ids:
            sid = self._new_ids.pop(0)
            self.last_session_result["sessionId"] = sid
            return sid
        return self._session_result.get("sessionId", "sid-new")

    def close_session(self, session_id, timeout=30.0):
        self.calls.append(("close_session", session_id))
        return {}

    def load_session(self, session_id, cwd, mcp_servers=None, timeout=60.0):
        self.calls.append(("load_session", session_id, cwd))
        if self._load_raises is not None:
            raise self._load_raises
        self.last_session_result = dict(self._session_result)
        return self.last_session_result

    def set_config_option(self, session_id, key, value, timeout=30.0):
        self.calls.append(("set_config_option", session_id, key, value))
        if isinstance(value, str) and value.startswith("boom"):
            raise acp_client.ACPError("session/set_config_option failed: no such option")
        return {}

    def prompt(self, session_id, text, timeout=600.0):
        self.calls.append(("prompt", session_id, text))
        turn = self.turns.pop(0) if self.turns else {}
        for chunk in turn.get("chunks") or []:
            self.fire({
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": turn.get("messageId", "m1"),
                    "content": {"type": "text", "text": chunk},
                },
            })
        if turn.get("block"):
            self._cancelled.wait(timeout=5)
        if turn.get("gate") is not None:
            turn["gate"].wait(timeout=5)
        exc = turn.get("raise")
        if exc is not None:
            raise exc
        return acp_client.PromptResult(
            stopReason=turn.get("stopReason", "end_turn"), usage=turn.get("usage")
        )

    def cancel(self, session_id=None):
        self.calls.append(("cancel", session_id))
        self._cancelled.set()

    def close(self, timeout=5.0):
        self.closed = True

    # -- test helpers -----------------------------------------------------
    def fire(self, params):
        for cb in list(self._events):
            cb(params)


SESSION_RESULT = {
    "sessionId": "sid-1",
    "configOptions": [
        {
            "id": "thinking",
            "category": "thought_level",
            "currentValue": "medium",
            "options": [{"value": "off", "name": "Off"}, {"value": "high", "name": "High"}],
        },
        {"id": "model", "category": "model", "currentValue": "model-a",
         "options": [{"value": "model-a", "name": "A"}, {"value": "model-b", "name": "B"}]},
    ],
    "availableCommands": [{"name": "usage", "description": "show usage", "input": None}],
}


def make_session(tmp: Path, client: FakeClient, **kw) -> "acp_chat.ChatSession":
    state_dir = tmp / "state"
    sessions_dir = tmp / "sessions"
    state_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return acp_chat.ChatSession(
        client_factory=lambda: client,
        cwd=str(tmp / "work"),
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        **kw,
    )


def read_lines(path) -> list[dict]:
    if path is None or not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def entries_of_type(lines, type_, custom=None) -> list[dict]:
    out = []
    for line in lines:
        if line.get("type") != type_:
            continue
        if custom is not None and line.get("customType") != custom:
            continue
        out.append(line)
    return out


def assistant_texts(lines) -> list[str]:
    out = []
    for line in entries_of_type(lines, "message"):
        msg = line.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        out.append("".join(c.get("text", "") for c in msg.get("content") or []))
    return out


def user_texts(lines) -> list[str]:
    out = []
    for line in entries_of_type(lines, "message"):
        msg = line.get("message") or {}
        if msg.get("role") != "user":
            continue
        out.append("".join(c.get("text", "") for c in msg.get("content") or []))
    return out


# ── 1. a prompt produces user line + previews + exactly ONE final line ──────


def test_prompt_writes_user_line_previews_and_one_final_assistant_line(tmp_path):
    client = FakeClient(
        session_result=SESSION_RESULT,
        turns=[{"chunks": ["Hel", "lo ", "world"], "usage": {"totalTokens": 42}}],
    )
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt("hi there") == {"ok": True, "turn": 1}
    assert sess.wait_idle(timeout=5)
    sess.close()

    lines = read_lines(sess.transcript_path)
    assert user_texts(lines) == ["hi there"]
    assert assistant_texts(lines) == ["Hello world"]
    # previews live in the SIBLING channel, never in the transcript
    assert entries_of_type(lines, "custom_message", acp_chat_events.PREVIEW_CUSTOM_TYPE) == []
    previews = read_lines(sess.preview_path)
    assert entries_of_type(previews, "custom_message", acp_chat_events.PREVIEW_CUSTOM_TYPE)
    print("PASS test_prompt_writes_user_line_previews_and_one_final_assistant_line")


# ── 2. one turn at a time ───────────────────────────────────────────────────


def test_second_prompt_while_busy_is_rejected_as_busy(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"block": True, "stopReason": "cancelled"}, {}])
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt("first")["ok"] is True
    deadline = time.time() + 3
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    second = sess.prompt("second")
    assert second == {"ok": False, "error": "busy"}
    sess.cancel()
    assert sess.wait_idle(timeout=5)
    sess.close()
    lines = read_lines(sess.transcript_path)
    busy = [l for l in entries_of_type(lines, "custom_message", "chat_error")
            if (l.get("data") or {}).get("code") == "busy"]
    assert len(busy) == 1, "the rejected prompt must be visible in the chat"
    assert user_texts(lines) == ["first"], "a rejected prompt must not land as a user line"
    print("PASS test_second_prompt_while_busy_is_rejected_as_busy")


# ── 2b. close() mid-turn (hermes-bridge POST /restart, dcaf687a) ────────────
#
# Live 14.09.2026: `/restart` under HERMES_DRIVER=acp is
# `ChatDaemon.restart()` = stop() (closes the OLD session) + start() (builds
# a brand new one). ACPClient.close() force-wakes the pending `session/prompt`
# RPC wait (`_wake_all()`) BEFORE the real child is confirmed dead — so
# `client.prompt()` on the worker thread returns a fake "successful" empty
# result instead of raising. Nothing in `_run_turn()`'s tail checked
# `self._closed`, so the ORPHANED session (the daemon already swapped in a
# different one) carried on as if its turn had genuinely finished: if
# `_client_alive()` happened to see the process as dead by then, it called
# `_restart_child()` — spawning yet ANOTHER real child process nobody will
# ever close — and then `_write_state()` / `_write_persisted()` wrote to the
# EXACT SAME workspace-scoped files (`sessions_dir`/`state_dir` are keyed by
# cwd, not by session object) the new, active session was writing to,
# clobbering its sessionId. That is "restart said 200 but the old turn is
# somehow still shaping what happens" — not a literal immortal process, but
# an immortal SESSION OBJECT whose worker thread keeps acting after close().


class _DeadAfterCloseProc:
    """Stand-in for `subprocess.Popen`: alive until close() marks it dead —
    mirrors a real child that only actually exits once ACPClient.close()
    finishes killing it."""

    def __init__(self):
        self.dead = False

    def poll(self):
        return None if not self.dead else 1


class _RestartRaceClient:
    """FakeClient variant that models ACPClient.close()'s real race: a
    blocked prompt() is force-released by close() (like `_wake_all()`) with
    an empty, non-error result — NOT a raised exception — and `_proc.poll()`
    reports the child as dead once close() has run (the realistic case: the
    close() call's terminate()/kill() sequence wins the race easily against
    a worker thread that was blocked for a while)."""

    def __init__(self, session_result):
        self.last_session_result = dict(session_result)
        self._session_result = session_result
        self._proc = _DeadAfterCloseProc()
        self._released = threading.Event()
        self.closed = False
        self.calls: list[tuple] = []
        self._events: list = []

    def on_event(self, cb):
        self._events.append(cb)

    def on_permission(self, cb):
        pass

    def _ensure_process(self):
        self.calls.append(("_ensure_process",))

    def initialize(self, timeout=30.0):
        return {}

    def new_session(self, cwd, mcp_servers=None, timeout=60.0):
        self.calls.append(("new_session", cwd))
        self.last_session_result = dict(self._session_result)
        return self._session_result.get("sessionId", "sid-new")

    def load_session(self, session_id, cwd, mcp_servers=None, timeout=60.0):
        self.calls.append(("load_session", session_id, cwd))
        self.last_session_result = dict(self._session_result)
        return self.last_session_result

    def set_config_option(self, session_id, key, value, timeout=30.0):
        return {}

    def prompt(self, session_id, text, timeout=600.0):
        self.calls.append(("prompt", session_id, text))
        # Stream SOME real text before hanging — otherwise `_full_text` is
        # empty and `map_final_assistant_message("")` returns `[]` no matter
        # what a sabotaged guard does (empty text never becomes a line), so
        # a transcript-pinning assertion would pass for the wrong reason.
        for cb in list(self._events):
            cb({
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "m1",
                    "content": {"type": "text", "text": "partial reply"},
                },
            })
        # Blocks until close() force-releases it — exactly like a real
        # `_request()` parked on `pend.event.wait()` when `_wake_all()` fires.
        self._released.wait(timeout=10)
        return acp_client.PromptResult(stopReason="", usage=None)

    def cancel(self, session_id=None):
        pass

    def close(self, timeout=5.0):
        self.closed = True
        self._proc.dead = True
        self._released.set()


def test_close_mid_turn_does_not_resurrect_a_child_or_clobber_shared_state(tmp_path):
    """A `/restart` that closes a session WHILE its turn is stuck must retire
    that turn outright — no self-heal respawn, no state-file write from the
    now-irrelevant session (dcaf687a-6d40-4bcf-b494-bdfed37208d6)."""
    made_clients: list[_RestartRaceClient] = []

    def factory():
        c = _RestartRaceClient(SESSION_RESULT)
        made_clients.append(c)
        return c

    state_dir = tmp_path / "state"
    sessions_dir = tmp_path / "sessions"
    state_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sess = acp_chat.ChatSession(
        client_factory=factory,
        cwd=str(tmp_path / "work"),
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        driver="hermes",
    )
    sess.start()
    assert len(made_clients) == 1

    assert sess.prompt("hang me")["ok"] is True
    deadline = time.time() + 3
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    assert sess.state()["busy"] is True

    # The worker thread writes the user-prompt transcript line asynchronously
    # too — wait for it explicitly so `transcript_before` is a stable
    # snapshot, not a coin flip against that write.
    deadline = time.time() + 3
    while time.time() < deadline and not (
        sess.transcript_path and sess.transcript_path.exists()
        and "hang me" in sess.transcript_path.read_text()
    ):
        time.sleep(0.01)

    state_before = sess.state_file.read_text() if sess.state_file.exists() else None
    transcript_before = (
        sess.transcript_path.read_text()
        if sess.transcript_path and sess.transcript_path.exists() else None
    )
    assert transcript_before and "hang me" in transcript_before

    # `ChatDaemon.restart()`'s other half: a BRAND NEW session (a different
    # object, a different sessionId) takes over the same workspace-scoped
    # persist file. Written here — while THIS turn's worker thread is
    # still verifiably parked in `client.prompt()` (`_RestartRaceClient`
    # only unblocks it via `close()` below) — so the ordering versus
    # whatever the zombie does afterwards is deterministic, not a race
    # against thread scheduling. A stale rewrite by the zombie is now
    # actually observable as a clobber, not just "the file happens to
    # still look the same" (that's all the old before/after
    # string-equality check could ever show, since a same-session
    # reconnect always reloads and re-writes the SAME id).
    replacement_session_id = "sid-REPLACEMENT"
    sess.persist_file.write_text(json.dumps({
        "sessionId": replacement_session_id,
        "transcript": str(sess.transcript_path) if sess.transcript_path else "",
    }))

    # This is the `/restart` moment: the daemon closes THIS session (and, in
    # production, immediately starts a brand new one — irrelevant here, the
    # defect lives entirely in what the OLD session's worker thread does to
    # itself and to shared files after being told it is retired).
    sess.close()

    assert sess.wait_idle(timeout=5), "close() must release a turn stuck on the dead client"
    assert sess.state()["busy"] is False

    # Give the worker thread a moment past close() — the bug window.
    time.sleep(0.3)

    assert len(made_clients) == 1, (
        "close() must not let the turn's worker thread self-heal via "
        "_restart_child() and spawn a second, permanently orphaned client/process"
    )
    state_after = sess.state_file.read_text() if sess.state_file.exists() else None
    assert state_after == state_before, (
        "a closed session must not keep writing acp-chat-state.json — that "
        "path is shared with whatever session /restart put in its place"
    )
    transcript_after = (
        sess.transcript_path.read_text()
        if sess.transcript_path and sess.transcript_path.exists() else None
    )
    assert transcript_after == transcript_before, (
        "close() mid-turn must not append a transcript line for the retired "
        "turn — the transcript file is shared with the replacement session "
        "/restart just started"
    )
    persisted_final = json.loads(sess.persist_file.read_text())
    assert persisted_final["sessionId"] == replacement_session_id, (
        "closed session's worker thread clobbered the replacement session's "
        "persisted sessionId with its own stale id"
    )
    print("PASS test_close_mid_turn_does_not_resurrect_a_child_or_clobber_shared_state")


# ── 3. cancel is not an error ───────────────────────────────────────────────


def test_cancel_ends_turn_without_chat_error(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"chunks": ["par"], "block": True, "stopReason": "cancelled"}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("long one")
    deadline = time.time() + 3
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    assert sess.cancel() == {"ok": True}
    assert sess.wait_idle(timeout=5)
    sess.close()
    lines = read_lines(sess.transcript_path)
    assert entries_of_type(lines, "custom_message", "chat_error") == []
    assert ("cancel", "sid-1") in client.calls
    assert sess.state()["busy"] is False
    print("PASS test_cancel_ends_turn_without_chat_error")


def test_cancel_before_any_text_is_still_not_an_error(tmp_path):
    # Stop pressed while the agent is still thinking: zero agent text AND a
    # cancelled stopReason. Without the explicit cancelled branch this lands
    # in the empty_turn case and the operator gets a red card for their own
    # Stop click.
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"chunks": [], "block": True, "stopReason": "cancelled"}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("long one")
    deadline = time.time() + 3
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    sess.cancel()
    assert sess.wait_idle(timeout=5)
    sess.close()
    lines = read_lines(sess.transcript_path)
    assert entries_of_type(lines, "custom_message", "chat_error") == []
    assert assistant_texts(lines) == []
    print("PASS test_cancel_before_any_text_is_still_not_an_error")


# ── 3b. a second turn starts clean ──────────────────────────────────────────


def test_second_turn_does_not_inherit_the_first_turns_text_or_usage(tmp_path):
    # ONE mapper serves the whole daemon lifetime. Its chunk accumulator is
    # keyed by messageId, and its prompt usage sticks until replaced — so
    # without a per-turn reset turn 2 previews "onetwo" and inherits turn 1's
    # token counts.
    client = FakeClient(session_result=SESSION_RESULT, turns=[
        {"chunks": ["one"], "messageId": "m1", "usage": {"totalTokens": 11}},
        {"chunks": ["two"], "messageId": "m1"},
    ])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("first")
    assert sess.wait_idle(timeout=5)
    sess.prompt("second")
    assert sess.wait_idle(timeout=5)
    sess.close()

    lines = read_lines(sess.transcript_path)
    assert assistant_texts(lines) == ["one", "two"]
    finals = [l for l in entries_of_type(lines, "message")
              if (l.get("message") or {}).get("role") == "assistant"]
    assert "usage" in finals[0]["message"]
    assert "usage" not in finals[1]["message"], "turn 2 inherited turn 1's token counts"
    preview_texts = [p.get("content") for p in read_lines(sess.preview_path)]
    assert "two" in preview_texts
    assert "onetwo" not in preview_texts, "turn 2 preview carries turn 1's text"
    print("PASS test_second_turn_does_not_inherit_the_first_turns_text_or_usage")


# ── 4. config: thinking level ───────────────────────────────────────────────


def test_config_thinking_calls_set_config_option_and_updates_state_file(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)
    sess.start()
    res = sess.config("thinking", "high")
    assert res["ok"] is True
    assert ("set_config_option", "sid-1", "thinking", "high") in client.calls
    state = json.loads((sess.state_file).read_text())
    thinking = [o for o in state["configOptions"] if o["id"] == "thinking"][0]
    assert thinking["currentValue"] == "high"
    sess.close()
    print("PASS test_config_thinking_calls_set_config_option_and_updates_state_file")


def test_config_rpc_error_emits_chat_error_and_returns_not_ok(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)
    sess.start()
    res = sess.config("model", "boom-does-not-exist")
    assert res["ok"] is False and res["error"] == "rpc_error"
    lines = read_lines(sess.transcript_path)
    errors = entries_of_type(lines, "custom_message", "chat_error")
    assert [(e.get("data") or {}).get("code") for e in errors] == ["rpc_error"]
    sess.close()
    print("PASS test_config_rpc_error_emits_chat_error_and_returns_not_ok")


# ── 5. state file schema ────────────────────────────────────────────────────


def test_state_file_carries_the_documented_schema(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("hi")
    assert sess.wait_idle(timeout=5)
    state = json.loads(sess.state_file.read_text())
    assert state["version"] == 1
    assert state["driver"] == "omp"
    assert state["sessionId"] == "sid-1"
    assert state["busy"] is False
    assert state["turn"] == 1
    assert state["transcript"] == str(sess.transcript_path)
    assert [o["id"] for o in state["configOptions"]] == ["thinking", "model"]
    assert state["commands"] == [{"name": "usage", "description": "show usage", "input": None}]
    assert state["updatedAt"].endswith("Z")
    assert state["lastError"] is None
    sess.close()
    print("PASS test_state_file_carries_the_documented_schema")


def test_notifications_update_config_options_and_commands(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)
    sess.start()
    client.fire({"sessionId": "sid-1", "update": {
        "sessionUpdate": "config_option_update",
        "configOptions": [{"id": "thinking", "currentValue": "off", "options": []}],
    }})
    client.fire({"sessionId": "sid-1", "update": {
        "sessionUpdate": "available_commands_update",
        "availableCommands": [{"name": "compact", "description": "compact", "input": None}],
    }})
    state = json.loads(sess.state_file.read_text())
    assert [o["id"] for o in state["configOptions"]] == ["thinking"]
    assert [c["name"] for c in state["commands"]] == ["compact"]
    sess.close()
    print("PASS test_notifications_update_config_options_and_commands")


# ── 6. session/load appends to the stored transcript ────────────────────────


def test_session_load_appends_to_the_existing_transcript_file(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["again"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    first_path = sess.transcript_path
    sess.prompt("first run")
    assert sess.wait_idle(timeout=5)
    sess.close()
    before = len(read_lines(first_path))

    client2 = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["resumed"]}])
    sess2 = make_session(tmp_path, client2)
    sess2.start()
    assert ("load_session", "sid-1", str(tmp_path / "work")) in client2.calls
    assert ("new_session", str(tmp_path / "work")) not in client2.calls
    assert sess2.transcript_path == first_path
    sess2.prompt("second run")
    assert sess2.wait_idle(timeout=5)
    sess2.close()
    lines = read_lines(first_path)
    assert len(lines) > before
    assert user_texts(lines) == ["first run", "second run"]
    assert entries_of_type(lines, "custom_message", "chat_error") == []
    print("PASS test_session_load_appends_to_the_existing_transcript_file")


def test_session_load_failure_resets_with_chat_error_and_new_file(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["x"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    first_path = sess.transcript_path
    sess.close()

    client2 = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-2"},
                         load_raises=acp_client.ACPError("session/load failed: unknown session"))
    sess2 = make_session(tmp_path, client2)
    sess2.start()
    assert sess2.transcript_path != first_path
    lines = read_lines(sess2.transcript_path)
    errors = entries_of_type(lines, "custom_message", "chat_error")
    assert [(e.get("data") or {}).get("code") for e in errors] == ["session_reset"]
    assert sess2.state()["sessionId"] == "sid-2"
    sess2.close()
    print("PASS test_session_load_failure_resets_with_chat_error_and_new_file")


# ── 7. error codes on the turn ──────────────────────────────────────────────


def test_prompt_rpc_error_emits_chat_error_rpc_error(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"raise": acp_client.ACPError("session/prompt failed: boom")}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("hi")
    assert sess.wait_idle(timeout=5)
    lines = read_lines(sess.transcript_path)
    errors = entries_of_type(lines, "custom_message", "chat_error")
    assert [(e.get("data") or {}).get("code") for e in errors] == ["rpc_error"]
    assert "boom" in (errors[0].get("data") or {}).get("detail", "")
    assert sess.state()["lastError"]["code"] == "rpc_error"
    sess.close()
    print("PASS test_prompt_rpc_error_emits_chat_error_rpc_error")


def test_provider_error_is_classified_from_the_error_text(tmp_path):
    client = FakeClient(
        session_result=SESSION_RESULT,
        turns=[{"raise": acp_client.ACPError("session/prompt failed: HTTP 429 quota exceeded")}],
    )
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("hi")
    assert sess.wait_idle(timeout=5)
    errors = entries_of_type(read_lines(sess.transcript_path), "custom_message", "chat_error")
    assert [(e.get("data") or {}).get("code") for e in errors] == ["provider_error"]
    sess.close()
    print("PASS test_provider_error_is_classified_from_the_error_text")


def test_empty_turn_emits_chat_error_empty_turn(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"chunks": [], "stopReason": "end_turn"}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("say nothing")
    assert sess.wait_idle(timeout=5)
    lines = read_lines(sess.transcript_path)
    assert assistant_texts(lines) == []
    errors = entries_of_type(lines, "custom_message", "chat_error")
    assert [(e.get("data") or {}).get("code") for e in errors] == ["empty_turn"]
    sess.close()
    print("PASS test_empty_turn_emits_chat_error_empty_turn")


# ── 8. socket round-trip through acp_chat_ctl.py ────────────────────────────


def test_socket_round_trip_through_the_ctl_shim(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["pong"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    # AF_UNIX paths are capped at ~104 bytes — pytest's tmp_path is already
    # longer than that on macOS, so the socket gets its own short directory.
    import tempfile

    sock = Path(tempfile.mkdtemp(prefix="acpchat")) / "c.sock"
    server = acp_chat.ChatSocketServer(sess, str(sock))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 3
    while not sock.exists() and time.time() < deadline:
        time.sleep(0.01)

    def ctl(*args):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "acp_chat_ctl.py"), *args, "--socket", str(sock)],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, json.loads(proc.stdout or "{}")

    try:
        code, body = ctl("state")
        assert code == 0 and body["ok"] is True and body["sessionId"] == "sid-1"

        code, body = ctl("prompt", "--json", json.dumps({"text": "ping"}))
        assert code == 0 and body == {"ok": True, "turn": 1}
        assert sess.wait_idle(timeout=5)
        assert assistant_texts(read_lines(sess.transcript_path)) == ["pong"]

        code, body = ctl("config", "--json", json.dumps({"id": "model", "value": "boom-x"}))
        assert code == 2 and body["ok"] is False and body["error"] == "rpc_error"

        code, body = ctl("cancel")
        assert code == 0 and body["ok"] is True
    finally:
        server.shutdown()
        sess.close()

    code, body = ctl("state")
    assert code == 3 and body == {"ok": False, "error": "unreachable"}
    print("PASS test_socket_round_trip_through_the_ctl_shim")


# ── model pinning (live incident 14.09.2026, twin of #483) ──────────────────
#
# `omp acp` inherits the bridge environment; OPENAI_API_KEY activates omp's
# BUILT-IN openai provider, and without an explicit selector every session
# opens on openai/gpt-5.5 with the shim key — the first chat turn after the
# container recreate answered "401 Incorrect API key provided: sk-noauth".
# bridge.py (task path) pins the model since #483; the chat daemon did not.


def test_new_session_pins_the_configured_model(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client, model="model-b")
    sess.start()
    assert ("set_config_option", "sid-1", "model", "model-b") in client.calls
    model = [o for o in sess.state()["configOptions"] if o["id"] == "model"][0]
    assert model["currentValue"] == "model-b"
    sess.close()
    print("PASS test_new_session_pins_the_configured_model")


def test_session_load_re_pins_the_model(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client, model="model-b")
    sess.start()
    sess.close()

    client2 = FakeClient(session_result=SESSION_RESULT)
    sess2 = make_session(tmp_path, client2, model="model-b")
    sess2.start()
    assert ("load_session", "sid-1", str(tmp_path / "work")) in client2.calls
    assert ("set_config_option", "sid-1", "model", "model-b") in client2.calls
    sess2.close()
    print("PASS test_session_load_re_pins_the_model")


def test_no_model_means_no_pin_call(tmp_path):
    """Hermes builds the same ChatSession without a selector (`hermes acp`
    picks its own model) — the daemon must not send an empty pin there."""
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)
    sess.start()
    assert not [c for c in client.calls if c[0] == "set_config_option"]
    sess.close()
    print("PASS test_no_model_means_no_pin_call")


def test_pin_failure_is_a_chat_error_not_a_crash(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client, model="boom-model")
    sess.start()  # must not raise — the daemon still serves state/cancel
    lines = read_lines(sess.transcript_path)
    errs = entries_of_type(lines, "custom_message", "chat_error")
    assert errs and errs[0]["data"]["code"] == "rpc_error"
    sess.close()
    print("PASS test_pin_failure_is_a_chat_error_not_a_crash")


def test_model_selector_precedence_matches_bridge(tmp_path):
    """OMP_ACP_MODEL > OMP_MODEL_SELECTOR > mc-openai/<OPENAI_MODEL> — the
    same order launch-omp.sh and bridge._acp_model_selector use."""
    sel = acp_chat.model_selector
    assert sel({"OMP_ACP_MODEL": "x/y", "OMP_MODEL_SELECTOR": "a/b", "OPENAI_MODEL": "m"}) == "x/y"
    assert sel({"OMP_MODEL_SELECTOR": "a/b", "OPENAI_MODEL": "m"}) == "a/b"
    assert sel({"OPENAI_MODEL": "m"}) == "mc-openai/m"
    assert sel({"OMP_ACP_MODEL": "  ", "OMP_MODEL_SELECTOR": ""}) is None
    print("PASS test_model_selector_precedence_matches_bridge")


def test_build_session_refuses_to_start_without_a_model(tmp_path, monkeypatch=None):
    """No selector = boot error, not a silent fallback to omp's built-in
    catalog (ADR-054, same rule as bridge._default_model_selector)."""
    saved = {k: os.environ.pop(k, None) for k in
             ("OMP_ACP_MODEL", "OMP_MODEL_SELECTOR", "OPENAI_MODEL")}
    os.environ["PI_CODING_AGENT_DIR"] = str(tmp_path / "agent")
    os.environ["OMP_HOME"] = str(tmp_path / "omp")
    try:
        try:
            acp_chat.build_session()
        except RuntimeError as exc:
            assert "OMP_MODEL_SELECTOR" in str(exc)
        else:
            raise AssertionError("build_session started without a model selector")
        os.environ["OMP_MODEL_SELECTOR"] = "mc-openai/pinned"
        sess = acp_chat.build_session()
        assert sess._model == "mc-openai/pinned"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS test_build_session_refuses_to_start_without_a_model")


# ── follow-up queue (operator finding 09.10.2026) ───────────────────────────
#
# A message sent while a reply runs used to be REFUSED by this daemon ("a turn
# is already running") while the composer showed it as queued — the second
# message was simply gone. `mode: "queue"` holds it in a FIFO and delivers it
# the moment the running turn ends; the default mode keeps the old refusal for
# machine callers (bridge wake-ups retry on their own).


def _wait_busy(sess, timeout=3.0):
    deadline = time.time() + timeout
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    assert sess.state()["busy"], "the first turn never started"


def test_queued_prompts_run_in_order_after_the_running_turn(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[
        {"chunks": ["one"], "gate": gate}, {"chunks": ["two"]}, {"chunks": ["three"]},
    ])
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt("first", mode="queue") == {"ok": True, "turn": 1}
    _wait_busy(sess)

    second = sess.prompt("second", mode="queue")
    third = sess.prompt("third", mode="queue")
    assert second["ok"] is True and second["queued"] is True and second["position"] == 1
    assert third["ok"] is True and third["queued"] is True and third["position"] == 2
    # visible as queued: the state mirror (what the backend reads) lists both
    mirrored = json.loads(sess.state_file.read_text())
    assert [q["text"] for q in mirrored["queue"]] == ["second", "third"]
    assert [q["id"] for q in mirrored["queue"]] == [second["queueId"], third["queueId"]]
    # held, not sent: nothing reached the agent or the transcript yet
    assert [c[2] for c in client.calls if c[0] == "prompt"] == ["first"]
    assert user_texts(read_lines(sess.transcript_path)) == ["first"]

    gate.set()
    assert sess.wait_idle(timeout=5)
    sess.close()

    lines = read_lines(sess.transcript_path)
    assert user_texts(lines) == ["first", "second", "third"]
    assert assistant_texts(lines) == ["one", "two", "three"]
    assert [c[2] for c in client.calls if c[0] == "prompt"] == ["first", "second", "third"]
    assert not [l for l in entries_of_type(lines, "custom_message", "chat_error")
                if (l.get("data") or {}).get("code") == "busy"], \
        "a queued message is not a refusal — no busy card"
    final = json.loads(sess.state_file.read_text())
    assert final["busy"] is False and final["queue"] == [] and final["turn"] == 3
    print("PASS test_queued_prompts_run_in_order_after_the_running_turn")


def test_queue_on_an_idle_session_starts_the_turn_right_away(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt("now", mode="queue") == {"ok": True, "turn": 1}
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert user_texts(read_lines(sess.transcript_path)) == ["now"]
    print("PASS test_queue_on_an_idle_session_starts_the_turn_right_away")


def test_queue_clear_hands_back_the_held_messages_and_they_never_run(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"chunks": ["one"], "gate": gate}, {"chunks": ["never"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("first", mode="queue")
    _wait_busy(sess)
    held = sess.prompt("take me back", mode="queue")

    cleared = sess.queue_clear()
    assert cleared["ok"] is True
    assert cleared["dropped"] == [{"id": held["queueId"], "kind": "prompt",
                                   "text": "take me back", "at": held["at"]}]
    assert json.loads(sess.state_file.read_text())["queue"] == []

    gate.set()
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert user_texts(read_lines(sess.transcript_path)) == ["first"]
    assert [c[2] for c in client.calls if c[0] == "prompt"] == ["first"]
    assert sess.queue_clear() == {"ok": True, "dropped": []}
    print("PASS test_queue_clear_hands_back_the_held_messages_and_they_never_run")


def test_a_full_queue_refuses_visibly_instead_of_dropping(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"gate": gate, "chunks": ["x"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("first", mode="queue")
    _wait_busy(sess)
    for i in range(acp_chat.MAX_QUEUE):
        assert sess.prompt(f"q{i}", mode="queue")["queued"] is True
    over = sess.prompt("one too many", mode="queue")
    assert over == {"ok": False, "error": "queue_full"}
    sess.queue_clear()
    gate.set()
    assert sess.wait_idle(timeout=5)
    sess.close()
    codes = [(l.get("data") or {}).get("code")
             for l in entries_of_type(read_lines(sess.transcript_path), "custom_message", "chat_error")]
    assert codes == ["queue_full"]
    print("PASS test_a_full_queue_refuses_visibly_instead_of_dropping")


def test_an_unknown_mode_is_refused_not_guessed(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt("hi", mode="steer") == {"ok": False, "error": "bad_mode", "detail": "steer"}
    sess.close()
    print("PASS test_an_unknown_mode_is_refused_not_guessed")


# ── /new and /clear: a REAL new ACP session ─────────────────────────────────
#
# omp advertises 45 commands over ACP; `new` and `clear` are not among them.
# Typed into the chat they reached the MODEL, which answered "session
# cleared" while session and context stayed the same. `new_session` opens a
# fresh `session/new`, writes a new transcript file (the chat view follows it
# as a rollover) and leaves the old file untouched.


def test_new_session_opens_a_fresh_session_and_keeps_the_old_transcript(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                        turns=[{"chunks": ["before"]}, {"chunks": ["after"]}])
    sess = make_session(tmp_path, client, model="model-b")
    sess.start()
    sess.prompt("old context")
    assert sess.wait_idle(timeout=5)
    sess.config("thinking", "high")
    old_transcript = sess.transcript_path

    answer = sess.new_session()
    assert answer == {"ok": True, "previousSessionId": "sid-1"}
    assert sess.wait_idle(timeout=5)

    state = sess.state()
    assert state["sessionId"] == "sid-2"
    assert sess.transcript_path != old_transcript
    assert ("close_session", "sid-1") in client.calls
    # same model and thinking level as before — /clear forgets the
    # conversation, not the operator's settings
    assert ("set_config_option", "sid-2", "model", "model-b") in client.calls
    assert ("set_config_option", "sid-2", "thinking", "high") in client.calls
    # restart-safe: a daemon restart re-loads the NEW session
    persisted = json.loads(sess.persist_file.read_text())
    assert persisted == {"sessionId": "sid-2", "transcript": str(sess.transcript_path)}

    sess.prompt("fresh start")
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert ("prompt", "sid-2", "fresh start") in client.calls
    old_lines = read_lines(old_transcript)
    assert user_texts(old_lines) == ["old context"], "old history must stay readable"
    new_lines = read_lines(sess.transcript_path)
    assert new_lines[0]["type"] == "session" and new_lines[0]["id"] == "sid-2"
    assert user_texts(new_lines) == ["fresh start"]
    print("PASS test_new_session_opens_a_fresh_session_and_keeps_the_old_transcript")


def test_new_session_closes_the_old_session_before_the_new_file_exists(tmp_path):
    """The chat view follows the file whose LAST entry is newest. omp may
    persist into the old session's file while disposing it, so the close must
    come first — otherwise the old session could out-rank the new one."""
    files_at_close: list[int] = []

    class _Client(FakeClient):
        def close_session(self, session_id, timeout=30.0):
            files_at_close.append(len(list((tmp_path / "sessions").glob("*.jsonl"))))
            return super().close_session(session_id, timeout)

    client = _Client(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.new_session()
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert files_at_close == [1], "new transcript was created before session/close"
    assert len(list((tmp_path / "sessions").glob("*.jsonl"))) == 2
    print("PASS test_new_session_closes_the_old_session_before_the_new_file_exists")


def test_new_session_while_busy_refuses_by_default_and_queues_on_request(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                        turns=[{"chunks": ["one"], "gate": gate}, {"chunks": ["two"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("first", mode="queue")
    _wait_busy(sess)

    assert sess.new_session() == {"ok": False, "error": "busy"}
    queued_new = sess.new_session(mode="queue")
    assert queued_new["ok"] is True and queued_new["queued"] is True
    sess.prompt("after the reset", mode="queue")
    assert [q["kind"] for q in sess.state()["queue"]] == ["new_session", "prompt"]
    first_transcript = sess.transcript_path

    gate.set()
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert sess.state()["sessionId"] == "sid-2"
    assert user_texts(read_lines(first_transcript)) == ["first"]
    assert user_texts(read_lines(sess.transcript_path)) == ["after the reset"]
    assert ("prompt", "sid-2", "after the reset") in client.calls
    print("PASS test_new_session_while_busy_refuses_by_default_and_queues_on_request")


def test_new_session_failure_keeps_the_current_session_and_says_so(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT,
                        new_raises=acp_client.ACPError("session/new failed: boom"),
                        turns=[{"chunks": ["still here"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.new_session()["ok"] is True
    assert sess.wait_idle(timeout=5)
    assert sess.state()["sessionId"] == "sid-1"
    assert ("close_session", "sid-1") not in client.calls
    sess.prompt("hello")
    assert sess.wait_idle(timeout=5)
    sess.close()
    lines = read_lines(sess.transcript_path)
    codes = [(l.get("data") or {}).get("code")
             for l in entries_of_type(lines, "custom_message", "chat_error")]
    assert codes == ["new_session_failed"]
    assert user_texts(lines) == ["hello"]
    print("PASS test_new_session_failure_keeps_the_current_session_and_says_so")


def test_socket_queue_and_new_session_ops_through_the_ctl_shim(tmp_path):
    import tempfile

    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                        turns=[{"chunks": ["one"], "gate": gate}, {"chunks": ["two"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sock = Path(tempfile.mkdtemp(prefix="acpchat")) / "c.sock"
    server = acp_chat.ChatSocketServer(sess, str(sock))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.time() + 3
    while not sock.exists() and time.time() < deadline:
        time.sleep(0.01)

    def ctl(*args):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "acp_chat_ctl.py"), *args, "--socket", str(sock)],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, json.loads(proc.stdout or "{}")

    try:
        code, body = ctl("prompt", "--json", json.dumps({"text": "first", "mode": "queue"}))
        assert code == 0 and body == {"ok": True, "turn": 1}
        _wait_busy(sess)
        code, body = ctl("prompt", "--json", json.dumps({"text": "held", "mode": "queue"}))
        assert code == 0 and body["queued"] is True
        code, body = ctl("queue_clear")
        assert code == 0 and [d["text"] for d in body["dropped"]] == ["held"]
        code, body = ctl("new_session")
        assert code == 2 and body == {"ok": False, "error": "busy"}
        gate.set()
        assert sess.wait_idle(timeout=5)
        code, body = ctl("new_session")
        assert code == 0 and body == {"ok": True, "previousSessionId": "sid-1"}
        assert sess.wait_idle(timeout=5)
        code, body = ctl("state")
        assert code == 0 and body["sessionId"] == "sid-2" and body["queue"] == []
    finally:
        server.shutdown()
        sess.close()
    print("PASS test_socket_queue_and_new_session_ops_through_the_ctl_shim")


# ── review #777: state mirror under concurrent writers, queue across restart ─


def test_state_mirror_is_never_torn_under_concurrent_writers(tmp_path):
    """The worker writes the mirror outside the lock while socket handlers
    write it under the lock. With ONE shared tmp file two writers truncate
    each other's tmp and rename half a file into place — the backend reads
    torn JSON (review: 20–97 parse errors per run)."""
    client = FakeClient(session_result=SESSION_RESULT)
    sess = make_session(tmp_path, client)
    sess.start()
    # A big payload makes each write slow enough to overlap.
    sess._set_commands([{"name": f"cmd{i}", "description": "x" * 400} for i in range(200)])
    stop = threading.Event()
    errors: list[str] = []

    def writer():
        while not stop.is_set():
            sess._write_state()

    def reader():
        while not stop.is_set():
            try:
                raw = sess.state_file.read_text()
            except OSError:
                continue
            try:
                json.loads(raw)
            except ValueError as exc:
                errors.append(str(exc))

    threads = [threading.Thread(target=writer) for _ in range(6)] + [threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join()
    sess.close()
    assert errors == [], f"{len(errors)} torn reads of acp-chat-state.json"
    assert not list(sess.state_file.parent.glob("*.tmp")), "tmp files left behind"
    print("PASS test_state_mirror_is_never_torn_under_concurrent_writers")


def test_messages_held_when_the_daemon_restarts_are_reported_not_lost_silently(tmp_path):
    """The queue lives in memory. A daemon restart (container recreate,
    bridge reload) drops it — the restarted daemon finds the old queue in the
    state mirror and says so in the chat, with the texts."""
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["x"], "gate": gate}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("first", mode="queue")
    _wait_busy(sess)
    sess.prompt("held one", mode="queue")
    sess.prompt("held two", mode="queue")
    transcript = sess.transcript_path
    sess.close()          # the daemon dies with two messages held
    gate.set()

    again = make_session(tmp_path, FakeClient(session_result=SESSION_RESULT))
    again.start()         # session/load -> same transcript file
    again.close()
    lost = [l for l in entries_of_type(read_lines(transcript), "custom_message", "chat_error")
            if (l.get("data") or {}).get("code") == "queue_lost"]
    assert len(lost) == 1
    assert "held one" in lost[0]["content"] and "held two" in lost[0]["content"]
    assert again.state()["queue"] == []

    third = make_session(tmp_path, FakeClient(session_result=SESSION_RESULT))
    third.start()         # nothing held this time -> no second card
    third.close()
    lost = [l for l in entries_of_type(read_lines(transcript), "custom_message", "chat_error")
            if (l.get("data") or {}).get("code") == "queue_lost"]
    assert len(lost) == 1
    print("PASS test_messages_held_when_the_daemon_restarts_are_reported_not_lost_silently")


if __name__ == "__main__":  # standalone runner
    import tempfile

    failures = 0
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
