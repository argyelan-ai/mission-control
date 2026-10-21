#!/usr/bin/env python3
"""Incident 21.10.2026 — a chat reset leaves a card in the NEW conversation.

The operator's chat got a new, empty session (`new_session` job, `/new` or
`/clear` through the control socket) and nothing in the chat said so: the
history looked gone. The old session was untouched on disk.

Contract (docs/specs/chat-over-acp.md, "New conversation card"):

* Every conversation the daemon opens IN PLACE of another one starts with
  ONE `chat_notice` line, code `new_conversation` (frontend:
  chat.notice.new_conversation — "New conversation started · 17:07 · the
  previous one is saved"). `data.previous`/`data.current` are the two session
  ids (the slot the later "show previous" link reads), `data.via` says how:
  `new_session` (the control op — `/new`, `/clear`), `start` (a daemon
  start that could not resume: daemon restart or container recreate),
  `child_restart` (the omp child died and the respawn could not resume).
* The card is the FIRST line of the new conversation and never lands in the
  old one.
* It promises "the previous one is saved", so it is only written while the
  previous conversation's copy is on disk. A copy that is gone keeps its own
  red `session_reset`/`transcript_missing` card and gets no promise.
* The state mirror names the last reset (`lastReset`: via, reason, both
  session ids, time) — the backend reads the new session id from there for
  the activity event, and it survives a daemon restart.
* No reset, no card: a normal chat, a successful resume and a brand-new
  agent stay exactly as before; a `/new` that omp refuses keeps the current
  session and gets no card.

Every assertion reads what the backend reads: the JSONL transcript the chat
view tails and the state mirror next to it. The ACP side is the in-memory
fake of test_acp_chat (`FakeClient`), plus one run through the real control
socket and the `acp_chat_ctl.py` shim.

Run:  OPENAI_MODEL=ci-test-model python3 -m pytest tests/test_acp_chat_reset_card.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import (  # noqa: E402
    SESSION_RESULT,
    FakeClient,
    entries_of_type,
    make_session,
    read_lines,
    user_texts,
)
from test_acp_chat_pointer import (  # noqa: E402
    MISSING,
    S,
    client,
    container_session,
    layout,
    mc_copy,
    write_pointer,
)

NEW = "new_conversation"
TEXT = "New conversation started — the previous one is saved."


def notices(path) -> list[dict]:
    return entries_of_type(read_lines(path), "custom_message", "chat_notice")


def errors(path) -> list[dict]:
    return entries_of_type(read_lines(path), "custom_message", "chat_error")


def new_cards(path) -> list[dict]:
    return [n for n in notices(path) if (n.get("data") or {}).get("code") == NEW]


def body(path) -> list[dict]:
    """Every line after the session header."""
    return read_lines(path)[1:]


def mirror(sess) -> dict:
    return json.loads(sess.state_file.read_text(encoding="utf-8"))


def _wait_busy(sess, timeout=3.0):
    deadline = time.time() + timeout
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    assert sess.state()["busy"], "the first turn never started"


# ── 1. /new and /clear: the new_session op ──────────────────────────────────


def test_new_session_starts_the_new_conversation_with_the_card(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                   turns=[{"chunks": ["before"]}, {"chunks": ["after"]}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("old context")
        assert sess.wait_idle(timeout=5)
        old = sess.transcript_path

        assert sess.new_session()["ok"] is True
        assert sess.wait_idle(timeout=5)
        sess.prompt("fresh start")
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    new = sess.transcript_path
    assert new != old
    # The card is the first thing in the new conversation ...
    first = body(new)[0]
    assert first["type"] == "custom_message" and first["customType"] == "chat_notice"
    assert first["data"] == {"code": NEW, "previous": "sid-1", "current": "sid-2",
                             "via": "new_session"}
    assert first["content"] == TEXT
    assert first["display"] is True
    # ... exactly once, before the operator's next message ...
    assert len(new_cards(new)) == 1
    assert user_texts(read_lines(new)) == ["fresh start"]
    # ... and never in the old one, which keeps its history untouched.
    assert new_cards(old) == []
    assert user_texts(read_lines(old)) == ["old context"]


def test_a_new_session_queued_behind_a_turn_gets_the_card_when_it_runs(tmp_path):
    gate = threading.Event()
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"],
                   turns=[{"chunks": ["one"], "gate": gate}, {"chunks": ["two"]}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("first", mode="queue")
        _wait_busy(sess)
        assert sess.new_session(mode="queue")["queued"] is True
        sess.prompt("after the reset", mode="queue")
        first_transcript = sess.transcript_path
        gate.set()
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    assert new_cards(first_transcript) == []
    cards = new_cards(sess.transcript_path)
    assert [(n["data"]["previous"], n["data"]["current"]) for n in cards] == [("sid-1", "sid-2")]
    assert body(sess.transcript_path)[0]["data"]["code"] == NEW


def test_two_resets_in_a_row_each_start_with_their_own_card(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2", "sid-3"])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.new_session()
        assert sess.wait_idle(timeout=5)
        second = sess.transcript_path
        sess.new_session()
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()

    assert [n["data"]["previous"] for n in new_cards(second)] == ["sid-1"]
    assert [n["data"]["previous"] for n in new_cards(sess.transcript_path)] == ["sid-2"]


def test_the_state_mirror_names_the_last_reset(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        assert mirror(sess).get("lastReset") is None, "no reset yet, nothing to name"
        sess.new_session()
        assert sess.wait_idle(timeout=5)
        last = mirror(sess)["lastReset"]
    finally:
        sess.close()

    assert last["via"] == "new_session"
    assert last["previousSessionId"] == "sid-1"
    assert last["sessionId"] == "sid-2"
    assert last["at"].endswith("Z")
    assert "reason" not in last


def test_the_last_reset_survives_a_daemon_restart(tmp_path):
    """A restart rewrites the mirror; the reset it names stays readable (the
    backend may look a moment after the daemon came back)."""
    c1 = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c1)
    sess.start()
    sess.new_session()
    assert sess.wait_idle(timeout=5)
    sess.close()

    c2 = FakeClient(session_result=dict(SESSION_RESULT, sessionId="sid-2"))
    again = make_session(tmp_path, c2)
    again.start()
    try:
        assert ("load_session", "sid-2", str(tmp_path / "work")) in c2.calls
        last = mirror(again)["lastReset"]
        assert (last["previousSessionId"], last["sessionId"]) == ("sid-1", "sid-2")
        # The resume itself is no reset: no second card.
        assert len(new_cards(again.transcript_path)) == 1
    finally:
        again.close()


def test_a_refused_new_session_keeps_the_session_and_writes_no_card(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT,
                   new_raises=acp_client.ACPError("session/new failed: boom"))
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.new_session()
        assert sess.wait_idle(timeout=5)
        assert sess.state()["sessionId"] == "sid-1"
    finally:
        sess.close()

    assert new_cards(sess.transcript_path) == []
    assert [e["data"]["code"] for e in errors(sess.transcript_path)] == ["new_session_failed"]
    assert mirror(sess).get("lastReset") is None


def test_the_card_through_the_control_socket_and_the_ctl_shim(tmp_path):
    """The way the backend reaches the daemon: `acp_chat_ctl.py new_session`
    over the unix socket."""
    c = FakeClient(session_result=SESSION_RESULT, new_ids=["sid-1", "sid-2"])
    sess = make_session(tmp_path, c)
    sess.start()
    sock = Path(tempfile.mkdtemp(prefix="acpreset")) / "c.sock"
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
        code, answer = ctl("new_session", "--json", json.dumps({"mode": "queue"}))
        assert code == 0 and answer == {"ok": True, "previousSessionId": "sid-1"}
        assert sess.wait_idle(timeout=5)
        code, state = ctl("state")
        assert code == 0 and state["lastReset"]["sessionId"] == "sid-2"
    finally:
        server.shutdown()
        sess.close()

    assert [n["data"]["current"] for n in new_cards(sess.transcript_path)] == ["sid-2"]


# ── 2. the daemon has to start over on its own ──────────────────────────────


def test_a_start_that_cannot_load_the_conversation_starts_with_the_card(tmp_path):
    """Daemon restart or container recreate: the pointer names S, omp refuses
    the load. The new conversation says it began — and the red card says
    why."""
    _, folder = layout(tmp_path)
    old_copy = mc_copy(folder, S)
    write_pointer(folder, S, old_copy)
    c = client(new_ids=["sid-new"],
               load_raises=acp_client.ACPError("session/load failed: load was cancelled"))
    sess = container_session(folder, c)
    try:
        sess.start()
        new = sess.transcript_path
        assert new != old_copy
        first = body(new)[0]
        assert first["customType"] == "chat_notice"
        assert first["data"] == {"code": NEW, "previous": S, "current": "sid-new",
                                 "via": "start", "reason": "load_failed"}
        assert [e["data"]["code"] for e in errors(new)] == ["session_reset"]
        assert new_cards(old_copy) == []
        last = mirror(sess)["lastReset"]
        assert (last["via"], last["reason"]) == ("start", "load_failed")
        assert (last["previousSessionId"], last["sessionId"]) == (S, "sid-new")
    finally:
        sess.close()


def test_a_missing_model_at_start_names_the_reason_on_the_card(tmp_path):
    _, folder = layout(tmp_path)
    write_pointer(folder, S, mc_copy(folder, S))
    c = client(new_ids=["sid-new"], load_raises=MISSING)
    sess = container_session(folder, c)
    try:
        sess.start()
        cards = new_cards(sess.transcript_path)
        assert [(n["data"]["via"], n["data"]["reason"]) for n in cards] == [("start", "model_missing")]
    finally:
        sess.close()


def test_a_child_restart_that_cannot_load_starts_with_the_card(tmp_path):
    """The omp child died; the respawn could not resume the live session."""
    _, folder = layout(tmp_path)
    healthy = client(new_ids=["sid-live"])
    broken = client(new_ids=["sid-new"], load_raises=MISSING)
    sess = container_session(folder, healthy, broken)
    try:
        sess.start()
        live = sess.transcript_path
        sess._restart_child()
        new = sess.transcript_path
        assert new != live
        cards = new_cards(new)
        assert [(n["data"]["previous"], n["data"]["current"], n["data"]["via"])
                for n in cards] == [("sid-live", "sid-new", "child_restart")]
        assert body(new)[0]["data"]["code"] == NEW
        assert new_cards(live) == []
    finally:
        sess.close()


def test_a_copy_that_is_gone_gets_no_promise_only_its_red_card(tmp_path):
    """"The previous one is saved" would be false: MC's copy is gone. The
    existing red card (`transcript_missing`) stays the only card; the mirror
    still names the reset."""
    _, folder = layout(tmp_path)
    write_pointer(folder, S, folder / f"2026-10-20T03-36-53_{S}.jsonl")
    c = client(new_ids=["sid-new"])
    sess = container_session(folder, c)
    try:
        sess.start()
        assert new_cards(sess.transcript_path) == []
        assert [(e["data"]["code"], e["data"]["detail"]) for e in errors(sess.transcript_path)] \
            == [("session_reset", "transcript_missing")]
        last = mirror(sess)["lastReset"]
        assert (last["via"], last["reason"], last["sessionId"]) == ("start", "transcript_missing", "sid-new")
    finally:
        sess.close()


# ── 3. no reset, no card ────────────────────────────────────────────────────


def test_a_normal_chat_gets_no_card(tmp_path):
    c = FakeClient(session_result=SESSION_RESULT,
                   turns=[{"chunks": ["one"]}, {"chunks": ["two"]}])
    sess = make_session(tmp_path, c)
    sess.start()
    try:
        sess.prompt("hello")
        assert sess.wait_idle(timeout=5)
        sess.prompt("again")
        assert sess.wait_idle(timeout=5)
    finally:
        sess.close()
    assert notices(sess.transcript_path) == []
    assert mirror(sess).get("lastReset") is None


def test_a_successful_resume_gets_no_card(tmp_path):
    _, folder = layout(tmp_path)
    copy = mc_copy(folder, S)
    write_pointer(folder, S, copy)
    c = client(session_result=dict(SESSION_RESULT, sessionId=S))
    sess = container_session(folder, c)
    try:
        sess.start()
        assert sess.transcript_path == copy
    finally:
        sess.close()
    assert new_cards(copy) == []
    assert mirror(sess).get("lastReset") is None


def test_a_brand_new_agent_gets_no_card(tmp_path):
    _, folder = layout(tmp_path)
    c = client(new_ids=["sid-first"])
    sess = container_session(folder, c)
    try:
        sess.start()
    finally:
        sess.close()
    assert notices(sess.transcript_path) == []
    assert mirror(sess).get("lastReset") is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
