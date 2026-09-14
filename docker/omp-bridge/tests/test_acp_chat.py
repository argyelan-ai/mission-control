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
    """

    def __init__(self, *, turns=None, session_result=None, load_raises=None):
        self.turns = list(turns or [])
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
        self.last_session_result = dict(self._session_result)
        return self._session_result.get("sessionId", "sid-new")

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

    state_before = sess.state_file.read_text() if sess.state_file.exists() else None
    persist_before = sess.persist_file.read_text() if sess.persist_file.exists() else None

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
    persist_after = sess.persist_file.read_text() if sess.persist_file.exists() else None
    assert state_after == state_before, (
        "a closed session must not keep writing acp-chat-state.json — that "
        "path is shared with whatever session /restart put in its place"
    )
    assert persist_after == persist_before, (
        "a closed session must not keep writing the persisted sessionId — "
        "shared with the replacement session from /restart"
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
