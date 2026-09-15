#!/usr/bin/env python3
"""Comment / thread-message wake-ups under OMP_DRIVER=acp (incident 14.09.2026).

Live finding: an ACP-driven agent's card was unblocked by the lead (comment
`UNBLOCKED`), and the bridge tried to wake the model with the native path —
`_MsgDelivery._nudge_comments` → `ctrl.inject_file()` → tmux paste into
Window 0. Under ACP Window 0 is a static banner shell (fix
omp-acp-no-tui-window), so every paste failed forever
(`[native] inject_file: FAILED after N paste attempt(s)`) and the comment
never reached the model; only the backend's stale-60min re-dispatch healed it.

Fix: when the serve loop runs under the ACP driver, `_MsgDelivery` gets an
`acp_prompt` callable and sends the SAME wake-up text as a prompt to the
chat daemon (`acp_chat.py --serve`, docs/specs/chat-over-acp.md) instead of
pasting. The native path (no `acp_prompt`) stays byte-for-byte on
`inject_file` — the existing test_comment_nudge / test_msg_nudge suites are
the regression guard for that, plus the source guard at the bottom.

Run: python3 test_acp_comment_nudge.py   OR   pytest -q
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402
from test_comment_nudge import _comment, TID  # noqa: E402
from test_msg_nudge import _StubTui, _delivery, _msg, _RecordingLifecycle, _finish_outcome  # noqa: E402


class _Recorder:
    """Stand-in for the chat-daemon prompt sender: records every text and
    answers from a queue of bools (True = daemon said ok)."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.sent: list[str] = []

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.results.pop(0) if self.results else True


def _acp_delivery(tmp, tui, recorder):
    dv = _delivery(tmp, tui)
    dv._acp_prompt = recorder  # what serve_loop wires under OMP_DRIVER=acp
    return dv


# ── comment wake-up ──────────────────────────────────────────────────────────

def test_comment_nudge_under_acp_goes_to_chat_daemon_not_inject_file():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        rec = _Recorder()
        dv = bridge._MsgDelivery(
            tui,
            signal_file=os.path.join(d, "sig.ndjson"),
            queue_dir=os.path.join(d, "queue"),
            ack_dir=os.path.join(d, "ack"),
            task_lock_path=os.path.join(d, "task.lock"),
            nudge_state_file=os.path.join(d, "nudge-state"),
            nudge_msg_file=os.path.join(d, "nudge.msg"),
            log=lambda _m: None,
            acp_prompt=rec,
        )
        dv.note_comments([_comment(), _comment()])
        dv.nudge_comments()
        assert tui.injected == [], "ACP driver must never paste into the (non-existent) TUI"
        assert len(rec.sent) == 1
        assert f"mc task-get {TID}" in rec.sent[0] and "2 neue(r)" in rec.sent[0]
        assert dv._pending_comments == {}
        # No signal-file await and no recycler lock: the chat daemon owns its
        # own turn; the gate must stay open for the next task dispatch.
        assert dv._awaiting_offset is None
        assert not os.path.exists(dv.task_lock_path)
        assert dv.gate_open()
        dv.nudge_comments()
        assert len(rec.sent) == 1, "nothing pending → no second prompt"
    print("PASS test_comment_nudge_under_acp_goes_to_chat_daemon_not_inject_file")


def test_comment_nudge_under_acp_busy_keeps_pending_for_retry():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui()
        rec = _Recorder([False, True])  # daemon busy, then ok
        dv = _acp_delivery(d, tui, rec)
        dv.note_comments([_comment()])
        dv.nudge_comments()
        assert dv._pending_comments == {TID: 1} and tui.injected == []
        dv.nudge_comments()
        assert dv._pending_comments == {} and len(rec.sent) == 2
    print("PASS test_comment_nudge_under_acp_busy_keeps_pending_for_retry")


def test_comment_nudge_under_acp_sender_exception_is_swallowed():
    def boom(_text):
        raise RuntimeError("socket gone")

    with tempfile.TemporaryDirectory() as d:
        dv = _acp_delivery(d, _StubTui(), boom)
        dv.note_comments([_comment()])
        dv.nudge_comments()  # must not raise
        assert dv._pending_comments == {TID: 1}
    print("PASS test_comment_nudge_under_acp_sender_exception_is_swallowed")


def test_withdrawn_notice_under_acp_travels_with_the_prompt():
    with tempfile.TemporaryDirectory() as d:
        rec = _Recorder()
        dv = _acp_delivery(d, _StubTui(), rec)
        dv.note_withdrawn(TID, "entzogen")
        dv.nudge_comments()
        assert len(rec.sent) == 1 and TID[:8] in rec.sent[0]
        assert dv._withdrawn_notice is None
        dv.nudge_comments()
        assert len(rec.sent) == 1
    print("PASS test_withdrawn_notice_under_acp_travels_with_the_prompt")


def test_comment_nudge_under_acp_still_respects_task_gate():
    """A running ACP task holds the recycler lock — the wake-up waits for the
    turn boundary exactly like native (the comment would otherwise land in
    the chat session while the task session is mid-work)."""
    with tempfile.TemporaryDirectory() as d:
        rec = _Recorder()
        dv = _acp_delivery(d, _StubTui(), rec)
        dv.note_comments([_comment()])
        open(dv.task_lock_path, "w").write("1")
        dv.nudge_comments()
        assert rec.sent == [] and dv._pending_comments == {TID: 1}
        os.remove(dv.task_lock_path)
        dv.nudge_comments()
        assert len(rec.sent) == 1 and dv._pending_comments == {}
    print("PASS test_comment_nudge_under_acp_still_respects_task_gate")


# ── thread-message nudge (MSG_DELIVERY_MODE=nudge, the live default) ─────────

def test_thread_nudge_under_acp_goes_to_chat_daemon():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        rec = _Recorder()
        dv = _acp_delivery(d, tui, rec)
        dv.nudge([_msg(3, "t1")])
        assert tui.injected == []
        assert len(rec.sent) == 1 and "mc inbox" in rec.sent[0]
        assert os.path.exists(dv.nudge_state_file), "high-water state must be written on success"
        assert dv._awaiting_offset is None and not os.path.exists(dv.task_lock_path)
        # dedup: same seq again → no second prompt
        dv.nudge([_msg(3, "t1")])
        assert len(rec.sent) == 1
    print("PASS test_thread_nudge_under_acp_goes_to_chat_daemon")


def test_thread_nudge_under_acp_busy_leaves_state_untouched():
    with tempfile.TemporaryDirectory() as d:
        rec = _Recorder([False])
        dv = _acp_delivery(d, _StubTui(), rec)
        dv.nudge([_msg(3, "t1")])
        assert not os.path.exists(dv.nudge_state_file)
        dv.nudge([_msg(3, "t1")])  # retried on the next poll
        assert len(rec.sent) == 2 and os.path.exists(dv.nudge_state_file)
    print("PASS test_thread_nudge_under_acp_busy_leaves_state_untouched")


# ── serve_loop wiring ────────────────────────────────────────────────────────

def _run_idle_loop(d, seam=None):
    payloads = iter([{"state": "idle", "new_comments": [_comment()]}, {"state": "idle"}])
    kw = {}
    if seam is not None:
        kw["_acp_prompt"] = seam
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
        **kw,
    )


def test_serve_loop_under_acp_driver_routes_comment_nudge_over_acp():
    rec = _Recorder()
    saved = os.environ.get("OMP_DRIVER")
    os.environ["OMP_DRIVER"] = "acp"
    orig_inject = bridge.NativeTuiController.inject_file
    pasted = []
    bridge.NativeTuiController.inject_file = lambda self, path, **kw: (pasted.append(path), False)[1]
    try:
        with tempfile.TemporaryDirectory() as d:
            _run_idle_loop(d, seam=rec)
    finally:
        bridge.NativeTuiController.inject_file = orig_inject
        if saved is None:
            os.environ.pop("OMP_DRIVER", None)
        else:
            os.environ["OMP_DRIVER"] = saved
    assert pasted == [], "no tmux paste under the ACP driver"
    assert len(rec.sent) == 1 and f"mc task-get {TID}" in rec.sent[0]
    print("PASS test_serve_loop_under_acp_driver_routes_comment_nudge_over_acp")


def test_serve_loop_native_driver_ignores_acp_seam_and_pastes():
    """Sabotage guard: WITHOUT OMP_DRIVER=acp the loop must keep using
    inject_file even if an ACP sender were handed in — the native fleet's
    behaviour must not depend on this fix at all."""
    rec = _Recorder()
    saved = os.environ.pop("OMP_DRIVER", None)
    orig_inject = bridge.NativeTuiController.inject_file
    pasted = []
    bridge.NativeTuiController.inject_file = lambda self, path, **kw: (pasted.append(path), True)[1]
    try:
        with tempfile.TemporaryDirectory() as d:
            _run_idle_loop(d, seam=rec)
    finally:
        bridge.NativeTuiController.inject_file = orig_inject
        if saved is not None:
            os.environ["OMP_DRIVER"] = saved
    assert len(pasted) == 1 and rec.sent == []
    print("PASS test_serve_loop_native_driver_ignores_acp_seam_and_pastes")


# ── the real sender: Unix socket → chat daemon ───────────────────────────────

def _fake_daemon(sock_path, answers):
    """One-shot JSON-lines server, same wire format as acp_chat.py --serve."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(4)
    seen = []

    def _serve():
        for ans in answers:
            conn, _ = srv.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                seen.append(json.loads(buf.decode("utf-8").strip()))
                conn.sendall((json.dumps(ans) + "\n").encode("utf-8"))
        srv.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return seen, t


def test_make_acp_chat_prompt_sends_prompt_op_and_maps_answers():
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "acp-chat.sock")
        seen, t = _fake_daemon(sock, [{"ok": True, "turn": 7}, {"ok": False, "error": "busy"}])
        send = bridge._make_acp_chat_prompt(sock, timeout=5.0)
        assert send("wake up") is True
        assert send("wake up again") is False
        t.join(5)
        assert seen == [{"op": "prompt", "text": "wake up"}, {"op": "prompt", "text": "wake up again"}]
    print("PASS test_make_acp_chat_prompt_sends_prompt_op_and_maps_answers")


def test_make_acp_chat_prompt_unreachable_socket_is_false_not_raise():
    with tempfile.TemporaryDirectory() as d:
        send = bridge._make_acp_chat_prompt(os.path.join(d, "missing.sock"), timeout=1.0)
        assert send("hello") is False
    print("PASS test_make_acp_chat_prompt_unreachable_socket_is_false_not_raise")


# ── source guard: native lines untouched ─────────────────────────────────────

def test_native_inject_lines_still_present_in_both_nudge_paths():
    src = open(os.path.join(os.path.dirname(HERE), "bridge.py"), encoding="utf-8").read()
    assert src.count("if self.ctrl.inject_file(self.nudge_msg_file):") == 2, (
        "native comment + thread nudge must still paste via inject_file"
    )
    assert "if self.ctrl.inject_file(path):" in src, "paste-mode flush untouched"
    print("PASS test_native_inject_lines_still_present_in_both_nudge_paths")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
