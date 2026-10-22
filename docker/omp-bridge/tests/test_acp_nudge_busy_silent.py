#!/usr/bin/env python3
"""Inbox/comment wake-ups never flood the chat with "busy" cards (22.10.2026).

Live finding: the omp bridge (``bridge.py --serve``) delivers a wake-up
("nudge") to the ACP chat daemon on every 5 s poll while an inbox message or
a task comment is pending. Under ACP the turn gate only looked at the task
lock, never at whether the chat daemon was answering, and the prompt carried
no ``mode`` and no ``origin`` — so the daemon refused it as ``busy`` AND
wrote a red ``chat_error`` card each time ("A reply is already running —
wait or press Stop."). The bridge did not record the nudge on a refusal, so
it came back every 5 s until the turn ended: 98 cards on 14.09., 206 on
15.09., 165 on 19.10. Each card also flushed the open thought and the open
text segment (``map_chat_error``), so a streaming reply was cut into pieces.

Contract tested here, through the REAL daemon (``acp_chat.ChatSession`` +
its Unix socket + ``dispatch``) and the REAL bridge sender:

* a prompt Mission Control wrote (``origin`` ``"nudge"`` / ``"task"``) that
  meets a running turn is refused quietly: ``{"ok": false, "error":
  "busy"}``, no card, no flush — its caller retries on its next poll;
* the operator's prompt (or one of unknown origin) still gets exactly ONE
  card — a typed message must never vanish silently;
* the bridge's sender says ``origin: "nudge"`` (a delivered wake-up is an MC
  event in the chat, not an operator bubble);
* under ACP the bridge's turn gate is closed while the daemon answers (the
  ``state`` op), so a busy daemon gets no prompt at all; an unreachable
  daemon does not hold the gate shut;
* nudges are NOT sent with ``mode: "queue"`` (a held nudge would come back
  into the operator's composer on Stop/withdraw and can be stale by then).

Run: pytest -q tests/test_acp_nudge_busy_silent.py
"""
from __future__ import annotations

import os
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
import bridge  # noqa: E402
from test_acp_chat import (  # noqa: E402
    SESSION_RESULT, FakeClient, assistant_texts, entries_of_type, make_session,
    read_lines, user_texts,
)
from test_comment_nudge import _comment, TID  # noqa: E402
from test_msg_nudge import _StubTui, _msg  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _wait_busy(sess, timeout=3.0):
    deadline = time.time() + timeout
    while not sess.state()["busy"] and time.time() < deadline:
        time.sleep(0.01)
    assert sess.state()["busy"], "the first turn never started"


def _thought(text: str) -> dict:
    return {"sessionId": "sid-1",
            "update": {"sessionUpdate": "agent_thought_chunk", "messageId": "th-1",
                       "content": {"type": "text", "text": text}}}


def _answer(text: str) -> dict:
    return {"sessionId": "sid-1",
            "update": {"sessionUpdate": "agent_message_chunk", "messageId": "m-1",
                       "content": {"type": "text", "text": text}}}


def _busy_cards(lines) -> list:
    return [l for l in entries_of_type(lines, "custom_message", "chat_error")
            if (l.get("data") or {}).get("code") == "busy"]


def _thinking_blocks(lines) -> list[str]:
    out = []
    for line in entries_of_type(lines, "message"):
        for block in (line.get("message") or {}).get("content") or []:
            if block.get("type") == "thinking":
                out.append(block.get("thinking") or "")
    return out


class _Daemon:
    """A real ChatSession behind its real Unix-socket server."""

    def __init__(self, tmp: Path, client: FakeClient):
        self.sess = make_session(tmp, client)
        self.sess.start()
        # AF_UNIX paths are capped at ~104 bytes — pytest's tmp_path is too long on macOS.
        self.sock = str(Path(tempfile.mkdtemp(prefix="acpnb")) / "c.sock")
        self.server = acp_chat.ChatSocketServer(self.sess, self.sock)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.time() + 3
        while not os.path.exists(self.sock) and time.time() < deadline:
            time.sleep(0.01)
        # Count every `prompt` op that reaches the session through the socket.
        self.prompts: list[tuple] = []
        real = self.sess.prompt

        def _counting(text, mode=acp_chat.MODE_NOW, origin=None):
            self.prompts.append((text, mode, origin))
            return real(text, mode, origin=origin)

        self.sess.prompt = _counting

    def close(self):
        self.server.shutdown()
        self.sess.close()


def _delivery(tmp: str, *, acp_prompt, acp_busy=None, logs=None):
    extra = {"acp_busy": acp_busy} if acp_busy is not None else {}
    return bridge._MsgDelivery(
        _StubTui(),
        signal_file=os.path.join(tmp, "sig.ndjson"),
        queue_dir=os.path.join(tmp, "queue"),
        ack_dir=os.path.join(tmp, "ack"),
        task_lock_path=os.path.join(tmp, "task.lock"),
        nudge_state_file=os.path.join(tmp, "nudge-state"),
        nudge_msg_file=os.path.join(tmp, "nudge.msg"),
        log=(logs.append if logs is not None else (lambda _m: None)),
        acp_prompt=acp_prompt,
        **extra,
    )


# ── 1. integration: bridge sender → real daemon, turn running, thought open ──


def test_nudges_into_a_busy_daemon_write_no_card_and_do_not_split_the_reply(tmp_path):
    """The live failure end to end, with the gate probe left OUT on purpose:
    even when a nudge does reach a busy daemon (the race between the gate's
    `state` read and the prompt), the daemon refuses it without a card and
    without cutting the open thought / text segment. The nudge stays pending
    (state file untouched) and is delivered once the turn is over."""
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"gate": gate}, {}])
    daemon = _Daemon(tmp_path, client)
    try:
        assert daemon.sess.prompt("operator question", origin="operator")["ok"] is True
        _wait_busy(daemon.sess)
        daemon.prompts.clear()
        client.fire(_thought("Let me think "))
        client.fire(_thought("about the "))

        with tempfile.TemporaryDirectory() as d:
            send = bridge._make_acp_chat_prompt(daemon.sock, timeout=5.0)
            dv = _delivery(d, acp_prompt=send)  # no busy probe: the race case
            for _ in range(3):
                dv.nudge([_msg(3, "t1")])
            assert len(daemon.prompts) == 3, "the nudges must really reach the daemon here"
            assert all(origin == "nudge" for _t, _m, origin in daemon.prompts), daemon.prompts
            assert all(mode == acp_chat.MODE_NOW for _t, mode, _o in daemon.prompts), (
                "a nudge must never be held in the queue (it would come back into the composer)"
            )
            assert not os.path.exists(dv.nudge_state_file), "a refused nudge is still pending"

            # The same for the task-comment wake-up.
            dv.note_comments([_comment()])
            dv.nudge_comments()
            assert dv._pending_comments == {TID: 1}

            client.fire(_thought("question."))
            client.fire(_answer("Here is "))
            dv.nudge([_msg(3, "t1")])
            client.fire(_answer("the answer."))

            gate.set()
            assert daemon.sess.wait_idle(timeout=5)

            lines = read_lines(daemon.sess.transcript_path)
            assert _busy_cards(lines) == [], "a nudge into a running turn must not write a card"
            assert entries_of_type(lines, "custom_message", "chat_error") == []
            assert _thinking_blocks(lines) == ["Let me think about the question."], (
                "the open thought must not be cut by a refused nudge"
            )
            # (the thinking line is an assistant message too — no text part)
            assert [t for t in assistant_texts(lines) if t] == ["Here is the answer."], (
                "the open text segment must not be cut by a refused nudge"
            )
            assert user_texts(lines) == ["operator question"]

            # Turn over → the next poll delivers it, as an MC event.
            dv.nudge([_msg(3, "t1")])
            assert os.path.exists(dv.nudge_state_file), "delivered → recorded"
            assert daemon.sess.wait_idle(timeout=5)
            lines = read_lines(daemon.sess.transcript_path)
            nudge_lines = [l for l in entries_of_type(lines, "message")
                           if (l.get("message") or {}).get("attribution") == "mc"]
            assert len(nudge_lines) == 1
            assert nudge_lines[0]["message"]["origin"]["kind"] == "nudge"
            assert "mc inbox" in nudge_lines[0]["message"]["content"][0]["text"]
    finally:
        gate.set()
        daemon.close()


def test_the_gate_reads_the_daemon_state_and_sends_nothing_while_it_answers(tmp_path):
    """With the busy probe wired (what serve_loop does under ACP), a busy
    daemon gets NO prompt op at all, and the log says why."""
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"gate": gate}, {}])
    daemon = _Daemon(tmp_path, client)
    logs: list[str] = []
    try:
        assert daemon.sess.prompt("operator question", origin="operator")["ok"] is True
        _wait_busy(daemon.sess)
        daemon.prompts.clear()
        with tempfile.TemporaryDirectory() as d:
            dv = _delivery(
                d,
                acp_prompt=bridge._make_acp_chat_prompt(daemon.sock, timeout=5.0),
                acp_busy=bridge._make_acp_chat_busy(daemon.sock, timeout=5.0),
                logs=logs,
            )
            assert dv.gate_open() is False
            for _ in range(3):
                dv.nudge([_msg(3, "t1")])
            dv.note_comments([_comment()])
            dv.nudge_comments()
            assert daemon.prompts == [], "a busy daemon must get no prompt op"
            assert not os.path.exists(dv.nudge_state_file)
            assert dv._pending_comments == {TID: 1}
            assert any("Chat-Daemon" in m and "antwortet" in m for m in logs), logs

            gate.set()
            assert daemon.sess.wait_idle(timeout=5)
            assert dv.gate_open() is True
            dv.nudge([_msg(3, "t1")])
            assert len(daemon.prompts) == 1 and daemon.prompts[0][2] == "nudge"
            assert os.path.exists(dv.nudge_state_file)
        assert _busy_cards(read_lines(daemon.sess.transcript_path)) == []
    finally:
        gate.set()
        daemon.close()


# ── 2. daemon unit: who gets a card ──────────────────────────────────────────


def _busy_session(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"gate": gate, "chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt("first", origin="operator")["ok"] is True
    _wait_busy(sess)
    return sess, gate


def _finish(sess, gate):
    gate.set()
    assert sess.wait_idle(timeout=5)
    sess.close()
    return read_lines(sess.transcript_path)


def test_busy_mc_origins_are_refused_without_a_card(tmp_path):
    sess, gate = _busy_session(tmp_path)
    for origin in ("nudge", "task"):
        assert sess.prompt("wake up", origin=origin) == {"ok": False, "error": "busy"}
    assert sess.state()["lastError"] is None, "a quiet refusal is not the chat's last error"
    lines = _finish(sess, gate)
    assert _busy_cards(lines) == []
    assert user_texts(lines) == ["first"]


def test_busy_without_origin_still_gets_exactly_one_card(tmp_path):
    sess, gate = _busy_session(tmp_path)
    assert sess.prompt("typed by someone") == {"ok": False, "error": "busy"}
    lines = _finish(sess, gate)
    assert len(_busy_cards(lines)) == 1, "an unknown caller's message must not vanish silently"


def test_busy_operator_prompt_still_gets_exactly_one_card(tmp_path):
    sess, gate = _busy_session(tmp_path)
    assert sess.prompt("typed by the operator", origin="operator") == {"ok": False, "error": "busy"}
    lines = _finish(sess, gate)
    assert len(_busy_cards(lines)) == 1


def test_busy_unknown_origin_value_is_treated_like_no_origin(tmp_path):
    """Fail-closed: only the two MC names are quiet — a typo is not."""
    sess, gate = _busy_session(tmp_path)
    assert sess.prompt("x", origin="nudges") == {"ok": False, "error": "busy"}
    lines = _finish(sess, gate)
    assert len(_busy_cards(lines)) == 1


def test_busy_nudge_over_the_socket_dispatch_is_quiet(tmp_path):
    sess, gate = _busy_session(tmp_path)
    answer = acp_chat.dispatch(sess, {"op": "prompt", "text": "wake", "origin": "nudge"})
    assert answer == {"ok": False, "error": "busy"}
    lines = _finish(sess, gate)
    assert _busy_cards(lines) == []


# ── 3. bridge: sender + busy probe ───────────────────────────────────────────


def test_bridge_sender_says_origin_nudge_and_no_mode():
    from test_acp_comment_nudge import _fake_daemon

    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "s.sock")
        seen, t = _fake_daemon(sock, [{"ok": True, "turn": 1}])
        assert bridge._make_acp_chat_prompt(sock, timeout=5.0)("wake up") is True
        t.join(5)
    assert seen == [{"op": "prompt", "text": "wake up", "origin": "nudge"}]


def test_busy_probe_maps_the_state_op():
    from test_acp_comment_nudge import _fake_daemon

    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "s.sock")
        seen, t = _fake_daemon(sock, [
            {"ok": True, "busy": True}, {"ok": True, "busy": False},
            {"ok": False, "error": "internal"}, {"ok": True},
        ])
        probe = bridge._make_acp_chat_busy(sock, timeout=5.0)
        assert probe() is True
        assert probe() is False
        assert probe() is None, "a refusal says nothing about busy"
        assert probe() is None, "no busy field says nothing about busy"
        t.join(5)
    assert seen == [{"op": "state"}] * 4


def test_busy_probe_unreachable_is_none_and_keeps_the_gate_open():
    """A dead socket must not hold the gate shut (the prompt then fails with
    its own honest 'unreachable' log and is retried)."""
    with tempfile.TemporaryDirectory() as d:
        probe = bridge._make_acp_chat_busy(os.path.join(d, "missing.sock"), timeout=1.0)
        assert probe() is None
        sent: list[str] = []
        dv = _delivery(d, acp_prompt=lambda text: (sent.append(text), True)[1], acp_busy=probe)
        assert dv.gate_open() is True
        dv.nudge([_msg(3, "t1")])
        assert len(sent) == 1


def test_busy_probe_exception_is_swallowed_and_gate_stays_open():
    def boom():
        raise RuntimeError("probe broke")

    with tempfile.TemporaryDirectory() as d:
        dv = _delivery(d, acp_prompt=lambda _t: True, acp_busy=boom)
        assert dv.gate_open() is True


def test_busy_probe_is_ignored_on_the_native_path():
    """No ACP sender → native delivery; a probe handed in must change nothing."""
    with tempfile.TemporaryDirectory() as d:
        dv = bridge._MsgDelivery(
            _StubTui([True]),
            signal_file=os.path.join(d, "sig.ndjson"),
            queue_dir=os.path.join(d, "queue"), ack_dir=os.path.join(d, "ack"),
            task_lock_path=os.path.join(d, "task.lock"),
            nudge_state_file=os.path.join(d, "nudge-state"),
            nudge_msg_file=os.path.join(d, "nudge.msg"),
            log=lambda _m: None, acp_busy=lambda: True,
        )
        assert dv.gate_open() is True


def test_task_lock_still_closes_the_gate_before_the_probe_is_asked():
    calls: list[int] = []

    def probe():
        calls.append(1)
        return False

    with tempfile.TemporaryDirectory() as d:
        dv = _delivery(d, acp_prompt=lambda _t: True, acp_busy=probe)
        open(dv.task_lock_path, "w").write("1")
        assert dv.gate_open() is False
        assert calls == [], "a task lock answers first — no socket round trip"


def test_serve_loop_under_acp_wires_the_busy_probe():
    """serve_loop hands the probe in under OMP_DRIVER=acp: a busy daemon
    holds the comment wake-up back, nothing reaches the sender."""
    from test_acp_comment_nudge import _Recorder
    from test_msg_nudge import _RecordingLifecycle, _finish_outcome

    rec = _Recorder()
    saved = os.environ.get("OMP_DRIVER")
    os.environ["OMP_DRIVER"] = "acp"
    try:
        with tempfile.TemporaryDirectory() as d:
            payloads = iter([{"state": "idle", "new_comments": [_comment()]}, {"state": "idle"}])
            bridge.serve_loop(
                poll_interval=0, max_iterations=2,
                _poll_fn=lambda: next(payloads, {"state": "idle"}),
                _lifecycle_factory=lambda t: _RecordingLifecycle(),
                _run_factory=lambda t, cwd: _finish_outcome,
                _sleep=lambda _s: None,
                _context_env_path=os.path.join(d, "ctx.env"),
                _msg_queue_dir=os.path.join(d, "queue"), _msg_ack_dir=os.path.join(d, "ack"),
                _task_lock_path=os.path.join(d, "task.lock"),
                _nudge_state_file=os.path.join(d, "nudge-state"),
                _nudge_msg_file=os.path.join(d, "nudge.msg"),
                _acp_prompt=rec,
                _acp_busy=lambda: True,
            )
    finally:
        if saved is None:
            os.environ.pop("OMP_DRIVER", None)
        else:
            os.environ["OMP_DRIVER"] = saved
    assert rec.sent == [], "busy daemon → no wake-up sent"
