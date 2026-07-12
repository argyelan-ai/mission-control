#!/usr/bin/env python3
"""Bug B (2026-07-12, live incident) — inject_file retry/verify/escalate.

Live trace: `tmux send-keys` hit a `subprocess.run(..., timeout=15)`
TimeoutExpired (swallowed inside `NativeTuiController._default_run`, which
returns rc=1) during the `@path` injection. The old `inject_file` fired the
three send-keys calls and returned unconditionally — no return-code check,
no verification the text actually left the composer. Result: the task
stayed `in_progress`, the agent "working", the TUI sitting at an empty
Welcome screen forever, with nobody retrying or escalating.

Covers:
  - a transient failed verification on attempt 1, succeeding on the retry
    (the "TimeoutExpired then success on 2nd attempt" scenario),
  - the swallowed-Enter case: composer not empty after the normal Escape+
    Enter sequence -> one extra bare Enter recovers it within the SAME
    attempt (no full retry/backoff needed),
  - exhausting all attempts -> inject_file returns False,
  - run_native_turn / run_native_continue escalate a False return straight
    to `_native_watchdog_kill` (relaunch + ABORT_HANG) instead of silently
    proceeding to observe and waiting out the multi-minute idle timeout —
    ABORT_HANG feeds drive_live_run's existing retry-then-blocker policy,
    which is the "blocked" fallback the brief asks to reuse.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402


def _controller(run_fn, *, sleep_log=None):
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    sig = os.path.join(tmp, "sig.ndjson")
    open(sig, "w", encoding="utf-8").close()

    def _sleep(dt):
        if sleep_log is not None:
            sleep_log.append(dt)

    return bridge.NativeTuiController(
        session="sparky", signal_file=sig, _run=run_fn,
        _pid_alive=lambda _pid: True, _sleep=_sleep,
    ), tmp


def _task_file_path(tmp: str) -> str:
    return os.path.join(tmp, "task.md")


def test_inject_file_retries_after_failed_verification_then_succeeds():
    """Attempt 1: capture-pane still shows the un-submitted @path (as if the
    Enter never registered because of the timeout) -> attempt 2 succeeds."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)
    calls = []
    attempt = {"n": 0}

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "capture-pane":
            attempt["n"] += 1
            # First 2 capture-pane checks (both within attempt 1: after the
            # normal Enter + after the swallowed-Enter fallback) still show
            # the unsubmitted mention; attempt 2 succeeds on its first check.
            if attempt["n"] <= 2:
                return 0, f"some prompt text\n@{tf}\n"
            return 0, "> \n"  # composer empty, submitted
        return 0, ""

    sleeps = []
    ctrl, _tmp2 = _controller(fake_run, sleep_log=sleeps)

    ok = ctrl.inject_file(tf, max_attempts=3, retry_backoff=5.0)

    assert ok is True
    # A full second attempt happened -> the retry-backoff sleep fired.
    assert 5.0 in sleeps
    sends = [c for c in calls if c and c[0] == "send-keys"]
    at_mentions = [c for c in sends if c[-1] == f"@{tf}"]
    assert len(at_mentions) == 2, "expected the @path mention retyped on the retry"


def test_inject_file_swallowed_enter_recovers_within_same_attempt():
    """Composer not empty after the normal Escape+Enter sequence -> ONE
    extra bare Enter (no retyping) recovers it, no full retry/backoff."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)
    calls = []
    capture_calls = {"n": 0}

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "capture-pane":
            capture_calls["n"] += 1
            if capture_calls["n"] == 1:
                return 0, f"@{tf}\n"  # not submitted yet
            return 0, "> \n"  # submitted after the extra Enter
        return 0, ""

    sleeps = []
    ctrl, _tmp2 = _controller(fake_run, sleep_log=sleeps)

    ok = ctrl.inject_file(tf, max_attempts=3, retry_backoff=5.0)

    assert ok is True
    assert 5.0 not in sleeps, "must not need a full retry for a single swallowed Enter"
    at_mentions = [c for c in calls if c and c[0] == "send-keys" and c[-1] == f"@{tf}"]
    assert len(at_mentions) == 1, "must not retype @path for the swallowed-Enter fallback"
    enters = [c for c in calls if c and c[0] == "send-keys" and c[-1] == "Enter"]
    assert len(enters) == 2, "normal Enter + one swallowed-Enter fallback Enter"


def test_inject_file_gives_up_after_max_attempts():
    """capture-pane NEVER clears -> inject_file must give up and report
    failure instead of returning True/None unconditionally."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)

    def fake_run(args):
        if args[0] == "capture-pane":
            return 0, f"@{tf}\n"  # always shows unsubmitted text
        return 0, ""

    ctrl, _tmp2 = _controller(fake_run)

    ok = ctrl.inject_file(tf, max_attempts=3, retry_backoff=0.0)

    assert ok is False


def test_run_native_turn_escalates_on_inject_failure_without_waiting_idle():
    """A definitively-failed injection must not fall through to
    _observe_native_turn and wait out idle_timeout — it escalates
    immediately via the same watchdog-kill path as a dead/wedged TUI."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-run-")
    tf = _task_file_path(tmp)
    calls = []

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "list-panes":
            return 0, "4242\n"
        if args[0] == "capture-pane":
            return 0, f"@{tf}\n"  # never verified -> inject_file gives up
        return 0, ""

    sig = os.path.join(tmp, "sig.ndjson")
    open(sig, "w", encoding="utf-8").close()

    ctrl = bridge.NativeTuiController(
        session="sparky", signal_file=sig, _run=fake_run,
        _pid_alive=lambda _pid: True, _sleep=lambda _s: None,
    )

    class _Clock:
        t = 0.0
        def now(self):
            return self.t
        def sleep(self, dt):
            self.t += dt or 0.001
            # Deliver the ready-hook right after the first poll tick, so the
            # ready-wait resolves fast — this test is about the INJECT step,
            # not readiness. Emulates the hook firing while the driver polls.
            if self.t <= 2 and os.path.getsize(sig) == 0:
                with open(sig, "a", encoding="utf-8") as fh:
                    fh.write('{"kind": "session_start"}\n')

    clock = _Clock()
    outcome = bridge.run_native_turn(
        ctrl, cwd="/workspace/proj", prompt="Do the thing.",
        task_file_path=tf, isolate=True,
        ready_timeout=1000, turn_deadline=1000, idle_timeout=1000,
        poll_interval=1.0, now=clock.now, sleep=clock.sleep,
    )

    assert outcome.watchdog_killed is True
    assert bridge.classify(outcome).kind is bridge.Kind.ABORT_HANG
    respawns = [c for c in calls if c and c[0] == "respawn-window"]
    assert respawns, "escalation must relaunch the wedged/failed-injection window"
    # No time was burned waiting on the multi-minute idle-timeout observe
    # loop (1000s) — the escalation happens right after inject_file gives up
    # (only the small ready-wait tick + inject_file's own zero-delay retries).
    assert clock.t < 50


def test_run_native_continue_escalates_on_inject_failure():
    tmp = tempfile.mkdtemp(prefix="omp-inject-continue-")
    tf = os.path.join(tmp, "nudge.md")

    def fake_run(args):
        if args[0] == "list-panes":
            return 0, "4242\n"
        if args[0] == "capture-pane":
            return 0, f"@{tf}\n"
        return 0, ""

    sig = os.path.join(tmp, "sig.ndjson")
    open(sig, "w", encoding="utf-8").close()

    ctrl = bridge.NativeTuiController(
        session="sparky", signal_file=sig, _run=fake_run,
        _pid_alive=lambda _pid: True, _sleep=lambda _s: None,
    )

    class _Clock:
        t = 0.0
        def now(self):
            return self.t
        def sleep(self, dt):
            self.t += dt or 0.001

    clock = _Clock()
    outcome = bridge.run_native_continue(
        ctrl, cwd="/workspace/proj", nudge_prompt="Keep going.",
        task_file_path=tf,
        turn_deadline=1000, idle_timeout=1000, poll_interval=1.0,
        now=clock.now, sleep=clock.sleep,
    )

    assert outcome.watchdog_killed is True
    assert bridge.classify(outcome).kind is bridge.Kind.ABORT_HANG


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-v"]))
