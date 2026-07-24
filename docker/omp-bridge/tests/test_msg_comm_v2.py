#!/usr/bin/env python3
"""W2 Bridge-Parity — unit tests for the comm_v2 thread-message path in bridge.py.

Python port of docker/shared/poll.sh's message consumer. Covers:
  - build_acked_seq_param: empty when no acks; URL-encoded {tid: high_water}
  - queue_messages: seq-named files, idempotent redelivery, comm_v2=false no-op
  - _record_ack: per-thread high-water only advances
  - flush: delivers ONE per idle boundary, acks only after a verified inject,
    no ack on a failed inject, gate closed while a task lock is held, and the
    next message waits for the injected message's terminal turn_end
  - serve_loop with comm_v2=false is byte-identical (no queue/ack files touched)
  - _make_http_poll appends ?acked_seq= only when the ack-store is non-empty

Run: python3 test_msg_comm_v2.py   (standalone)   OR   pytest -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402


def _msg(seq, tid, body="hallo", sender="mark", mtype="dm"):
    return {"id": f"m{seq}", "thread_id": tid, "seq": seq, "sender": sender,
            "message_type": mtype, "body": body}


class _StubTui:
    """Minimal stand-in for NativeTuiController — _MsgDelivery only ever calls
    inject_file(path). `results` is a queue of bools it returns per call."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.injected: list[str] = []

    def inject_file(self, path, **kw):
        self.injected.append(path)
        return self.results.pop(0) if self.results else True


def _delivery(tmp, tui, *, signal="sig.ndjson"):
    return bridge._MsgDelivery(
        tui,
        signal_file=os.path.join(tmp, signal),
        queue_dir=os.path.join(tmp, "queue"),
        ack_dir=os.path.join(tmp, "ack"),
        task_lock_path=os.path.join(tmp, "task.lock"),
        log=lambda _m: None,
    )


# ── build_acked_seq_param ───────────────────────────────────────────────────

def test_acked_seq_empty_when_no_dir_or_empty():
    with tempfile.TemporaryDirectory() as d:
        assert bridge.build_acked_seq_param(os.path.join(d, "nope")) == ""
        empty = os.path.join(d, "ack")
        os.makedirs(empty)
        assert bridge.build_acked_seq_param(empty) == ""
    print("PASS test_acked_seq_empty_when_no_dir_or_empty")


def test_acked_seq_serializes_high_water():
    with tempfile.TemporaryDirectory() as d:
        ack = os.path.join(d, "ack")
        os.makedirs(ack)
        open(os.path.join(ack, "thread-a"), "w").write("7")
        open(os.path.join(ack, "thread-b"), "w").write("3")
        enc = bridge.build_acked_seq_param(ack)
        decoded = json.loads(urllib.parse.unquote(enc))
    assert decoded == {"thread-a": 7, "thread-b": 3}, decoded
    print("PASS test_acked_seq_serializes_high_water")


def test_acked_seq_skips_garbage_files():
    with tempfile.TemporaryDirectory() as d:
        ack = os.path.join(d, "ack")
        os.makedirs(ack)
        open(os.path.join(ack, "good"), "w").write("12")
        open(os.path.join(ack, "bad"), "w").write("notanint")
        decoded = json.loads(urllib.parse.unquote(bridge.build_acked_seq_param(ack)))
    assert decoded == {"good": 12}, decoded
    print("PASS test_acked_seq_skips_garbage_files")


# ── queue_messages ──────────────────────────────────────────────────────────

def test_queue_writes_seq_named_files_with_footer():
    with tempfile.TemporaryDirectory() as d:
        q = os.path.join(d, "queue")
        n = bridge.queue_messages({"new_messages": [_msg(12, "uuid-x", body="ping")]}, q)
        assert n == 1
        files = bridge.msg_queue_files(q)
        assert files == ["00000012__uuid-x.msg"], files
        content = open(os.path.join(q, files[0]), encoding="utf-8").read()
    assert "ping" in content
    assert "[thread uuid-x · seq 12 · von mark · typ dm]" in content, content
    print("PASS test_queue_writes_seq_named_files_with_footer")


def test_queue_is_idempotent_and_seq_ordered():
    with tempfile.TemporaryDirectory() as d:
        q = os.path.join(d, "queue")
        payload = {"new_messages": [_msg(2, "t"), _msg(10, "t"), _msg(1, "t")]}
        bridge.queue_messages(payload, q)
        bridge.queue_messages(payload, q)  # redelivery overwrites identically
        files = bridge.msg_queue_files(q)
    # zero-padded prefix → lexical sort == numeric (1, 2, 10), no duplicates.
    assert files == ["00000001__t.msg", "00000002__t.msg", "00000010__t.msg"], files
    print("PASS test_queue_is_idempotent_and_seq_ordered")


def test_queue_no_key_is_noop_no_dir_created():
    with tempfile.TemporaryDirectory() as d:
        q = os.path.join(d, "queue")
        assert bridge.queue_messages({"state": "idle"}, q) == 0
        assert bridge.queue_messages(None, q) == 0
        assert bridge.queue_messages({"new_messages": []}, q) == 0
        # comm_v2=false must never even create the queue dir.
        assert not os.path.exists(q)
    print("PASS test_queue_no_key_is_noop_no_dir_created")


# ── _record_ack ─────────────────────────────────────────────────────────────

def test_record_ack_high_water_only_advances():
    with tempfile.TemporaryDirectory() as d:
        ack = os.path.join(d, "ack")
        bridge._record_ack(ack, "t", 5)
        bridge._record_ack(ack, "t", 3)  # lower → ignored
        bridge._record_ack(ack, "t", 9)  # higher → advances
        val = open(os.path.join(ack, "t"), encoding="utf-8").read().strip()
    assert val == "9", val
    print("PASS test_record_ack_high_water_only_advances")


# ── flush: delivery + ack + gate ────────────────────────────────────────────

def test_flush_delivers_one_and_acks_after_verify():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        bridge.queue_messages({"new_messages": [_msg(4, "th")]}, deliv.queue_dir)
        deliv.flush()
        # delivered exactly the queued file, acked, queue file removed.
        assert len(tui.injected) == 1
        assert bridge.msg_queue_files(deliv.queue_dir) == []
        acked = open(os.path.join(deliv.ack_dir, "th"), encoding="utf-8").read().strip()
    assert acked == "4", acked
    print("PASS test_flush_delivers_one_and_acks_after_verify")


def test_flush_no_ack_when_inject_fails():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([False])
        deliv = _delivery(d, tui)
        bridge.queue_messages({"new_messages": [_msg(4, "th")]}, deliv.queue_dir)
        deliv.flush()
        # inject failed → message stays queued, no ack file, no awaiting.
        assert bridge.msg_queue_files(deliv.queue_dir) == ["00000004__th.msg"]
        assert not os.path.exists(os.path.join(deliv.ack_dir, "th"))
    print("PASS test_flush_no_ack_when_inject_fails")


def test_gate_closed_while_task_lock_present():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        open(deliv.task_lock_path, "w").write("1")  # a dispatch is in flight
        bridge.queue_messages({"new_messages": [_msg(1, "th")]}, deliv.queue_dir)
        deliv.flush()
        # gate closed → nothing injected, message stays queued, no ack.
        assert tui.injected == []
        assert bridge.msg_queue_files(deliv.queue_dir) == ["00000001__th.msg"]
    print("PASS test_gate_closed_while_task_lock_present")


def test_second_message_waits_for_injected_turn_end():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True, True])
        deliv = _delivery(d, tui)
        bridge.queue_messages(
            {"new_messages": [_msg(1, "th"), _msg(2, "th")]}, deliv.queue_dir
        )
        # First flush delivers seq 1 and arms the "await turn_end" gate.
        deliv.flush()
        assert len(tui.injected) == 1
        assert bridge.msg_queue_files(deliv.queue_dir) == ["00000002__th.msg"]

        # Gate stays closed while the model is still processing (no turn_end yet).
        deliv.flush()
        assert len(tui.injected) == 1, "must not deliver seq 2 before turn_end"

        # Model finishes its turn → terminal turn_end appended to the signal.
        with open(deliv.signal_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")

        deliv.flush()
        assert len(tui.injected) == 2, "seq 2 must deliver after turn_end"
        assert bridge.msg_queue_files(deliv.queue_dir) == []
        acked = open(os.path.join(deliv.ack_dir, "th"), encoding="utf-8").read().strip()
    assert acked == "2", acked
    print("PASS test_second_message_waits_for_injected_turn_end")


def test_flush_empty_queue_is_noop():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui()
        deliv = _delivery(d, tui)
        deliv.flush()  # no queue dir at all
        assert tui.injected == []
    print("PASS test_flush_empty_queue_is_noop")


# ── Finding 1: signal truncate must not dead-lock the awaiting gate ──────────

def test_awaiting_offset_self_heals_when_signal_truncated():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True, True])
        deliv = _delivery(d, tui)
        # A non-empty signal so the first injection's offset is > 0.
        with open(deliv.signal_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")
        bridge.queue_messages(
            {"new_messages": [_msg(1, "th"), _msg(2, "th")]}, deliv.queue_dir
        )
        deliv.flush()  # delivers seq 1, arms awaiting at offset > 0
        assert len(tui.injected) == 1
        assert deliv._awaiting_offset is not None and deliv._awaiting_offset > 0

        # A task dispatch truncates the turn signal to 0 (run_native_turn does
        # this). The stored offset now points past EOF — must NOT dead-lock.
        open(deliv.signal_file, "w", encoding="utf-8").close()
        deliv.flush()  # gate self-heals (size < offset) → seq 2 delivers
        assert len(tui.injected) == 2, "truncate must not wedge the queue"
        assert bridge.msg_queue_files(deliv.queue_dir) == []
    print("PASS test_awaiting_offset_self_heals_when_signal_truncated")


def test_reset_awaiting_clears_window_and_releases_lock():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        bridge.queue_messages({"new_messages": [_msg(1, "th")]}, deliv.queue_dir)
        deliv.flush()  # arms awaiting + takes the recycler lock
        assert deliv._awaiting_offset is not None
        assert os.path.exists(deliv.task_lock_path)

        deliv.reset_awaiting()  # serve_loop calls this when a task dispatches
        assert deliv._awaiting_offset is None
        assert not os.path.exists(deliv.task_lock_path), "lock must be released"
    print("PASS test_reset_awaiting_clears_window_and_releases_lock")


# ── Finding 2: recycler task lock held across the message-processing turn ────

def test_lock_held_during_processing_and_released_on_turn_end():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([True])
        deliv = _delivery(d, tui)
        bridge.queue_messages({"new_messages": [_msg(1, "th")]}, deliv.queue_dir)
        deliv.flush()
        # Lock held while the model processes the injected message.
        assert os.path.exists(deliv.task_lock_path)
        assert deliv._holds_lock is True

        # Model finishes its turn → gate_open detects the terminal turn_end and
        # releases the recycler lock.
        with open(deliv.signal_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "turn_end", "stopReason": "stop"}) + "\n")
        assert deliv.gate_open() is True
        assert not os.path.exists(deliv.task_lock_path)
        assert deliv._holds_lock is False
    print("PASS test_lock_held_during_processing_and_released_on_turn_end")


def test_no_lock_left_when_inject_fails():
    with tempfile.TemporaryDirectory() as d:
        tui = _StubTui([False])
        deliv = _delivery(d, tui)
        bridge.queue_messages({"new_messages": [_msg(1, "th")]}, deliv.queue_dir)
        deliv.flush()
        # Nothing submitted → no processing turn → lock must not linger.
        assert not os.path.exists(deliv.task_lock_path)
        assert deliv._holds_lock is False
    print("PASS test_no_lock_left_when_inject_fails")


# ── Finding 3: a message-path exception must never crash the poll loop ───────

class _RaisingTui:
    def inject_file(self, path, **kw):
        raise RuntimeError("tmux exploded")


def test_flush_swallows_exceptions():
    with tempfile.TemporaryDirectory() as d:
        deliv = _delivery(d, _RaisingTui())
        bridge.queue_messages({"new_messages": [_msg(1, "th")]}, deliv.queue_dir)
        deliv.flush()  # must not raise
        # Message stays queued (never acked), loop can retry later.
        assert bridge.msg_queue_files(deliv.queue_dir) == ["00000001__th.msg"]
        assert not os.path.exists(os.path.join(deliv.ack_dir, "th"))
    print("PASS test_flush_swallows_exceptions")


def test_serve_loop_survives_message_queue_error():
    task = {"id": "task-1", "board_id": "b1", "dispatch_attempt_id": "att-1",
            "prompt": "Do the thing."}
    with tempfile.TemporaryDirectory() as d:
        # queue dir whose parent is a FILE → os.makedirs raises NotADirectoryError.
        blocker = os.path.join(d, "afile")
        open(blocker, "w").close()
        bad_queue = os.path.join(blocker, "queue")
        lc = _RecordingLifecycle()
        it = iter([{"state": "new_task", "task": task, "new_messages": [_msg(1, "th")]}])
        # Must not raise despite the unwritable queue dir on a real message.
        bridge.serve_loop(
            poll_interval=0, max_iterations=1,
            _poll_fn=lambda: next(it, {"state": "idle"}),
            _lifecycle_factory=lambda t: lc,
            _run_factory=lambda t, cwd: _finish_outcome,
            _sleep=lambda _s: None,
            _context_env_path=os.path.join(d, "ctx.env"),
            _msg_queue_dir=bad_queue, _msg_ack_dir=os.path.join(d, "ack"),
            _task_lock_path=os.path.join(d, "task.lock"),
        )
        # The task still resolved normally — the queue error was swallowed.
        assert ("ack", "task-1") in lc.calls
        assert any(c[0] == "finish" for c in lc.calls)
    print("PASS test_serve_loop_survives_message_queue_error")


# ── serve_loop comm_v2=false parity ─────────────────────────────────────────

def _finish_outcome():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_start = True
    o.saw_agent_end = True
    o.final_stop_reason = "stop"
    o.final_text = (
        "## Was wurde gemacht\nDatei erstellt und getestet, alles laeuft.\n"
        "## Was hat funktioniert\nDer deterministische Fix, zweiter Lauf gruen.\n"
        "## Was war unklar\nNichts Wesentliches, Aufgabe war eindeutig.\n"
        "## Lesson fuer Agent-Memory\nImmer erst reproduzieren, dann fixen.\n"
        "TASK_COMPLETE"
    )
    return o


class _RecordingLifecycle(bridge.MCLifecycle):
    def __init__(self):
        self.calls = []

    def ack(self, task_id):
        self.calls.append(("ack", task_id))

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish", task_id, review))

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", task_id, blocker_type))

    def comment(self, task_id, text):
        self.calls.append(("comment", task_id))


def test_serve_loop_commv2_off_touches_no_msg_state():
    task = {"id": "task-1", "board_id": "b1", "dispatch_attempt_id": "att-1",
            "prompt": "Do the thing."}
    with tempfile.TemporaryDirectory() as d:
        q = os.path.join(d, "queue")
        ack = os.path.join(d, "ack")
        lc = _RecordingLifecycle()
        it = iter([{"state": "new_task", "task": task}])
        bridge.serve_loop(
            poll_interval=0, max_iterations=1,
            _poll_fn=lambda: next(it, {"state": "idle"}),
            _lifecycle_factory=lambda t: lc,
            _run_factory=lambda t, cwd: _finish_outcome,
            _sleep=lambda _s: None,
            _context_env_path=os.path.join(d, "ctx.env"),
            _msg_queue_dir=q, _msg_ack_dir=ack,
            _task_lock_path=os.path.join(d, "task.lock"),
        )
        # Normal dispatch still resolves ack+finish …
        assert ("ack", "task-1") in lc.calls
        assert any(c[0] == "finish" for c in lc.calls)
        # … and NO comm_v2 state was ever created (byte-identical behaviour).
        assert not os.path.exists(q)
        assert not os.path.exists(ack)
        assert bridge.build_acked_seq_param(ack) == ""
    print("PASS test_serve_loop_commv2_off_touches_no_msg_state")


# ── _make_http_poll ?acked_seq= wiring ──────────────────────────────────────

def test_make_http_poll_appends_acked_seq_only_when_present():
    import urllib.request

    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"state":"idle"}'

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        return _FakeResp()

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        with tempfile.TemporaryDirectory() as d:
            ack = os.path.join(d, "ack")
            poll = bridge._make_http_poll("http://backend:8000", "tok", ack_dir=ack)
            # No acks yet → plain URL, byte-identical to pre-comm_v2.
            poll()
            assert captured["url"] == "http://backend:8000/api/v1/agent/me/poll", captured
            # Once an ack exists → ?acked_seq= is appended.
            os.makedirs(ack)
            open(os.path.join(ack, "th"), "w").write("5")
            poll()
            assert "?acked_seq=" in captured["url"], captured
            enc = captured["url"].split("?acked_seq=", 1)[1]
            assert json.loads(urllib.parse.unquote(enc)) == {"th": 5}
    finally:
        urllib.request.urlopen = orig
    print("PASS test_make_http_poll_appends_acked_seq_only_when_present")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
