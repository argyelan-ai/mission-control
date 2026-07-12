#!/usr/bin/env python3
"""Bug B (2026-07-12, live incident + same-day review) — inject_file
retry/verify/escalate.

Live trace #1: `tmux send-keys` hit a `subprocess.run(..., timeout=15)`
TimeoutExpired (swallowed inside `NativeTuiController._default_run`, which
returns rc=1) during the `@path` injection. The original `inject_file` fired
the three send-keys calls and returned unconditionally — no return-code
check, no verification the text actually left the composer. Result: the task
stayed `in_progress`, the agent "working", the TUI sitting at an empty
Welcome screen forever, with nobody retrying or escalating.

Live trace #2 (manual repro 2026-07-12 12:23, Sparky pane, review finding):
the first fix's verification scanned the last 400 chars of the WHOLE pane
for the literal `@path` string. Two failure modes: (1) FALSE NEGATIVE — omp
echoes the submitted message back into the transcript above the composer,
so a successful submit could still show `@path` in the tail, causing
repeated re-submissions of the SAME task (duplicate dispatch — worse than
the original hang); (2) FALSE POSITIVE — a long path wraps across composer
lines, so a literal match can miss a still-pending mention. The empirical
fix: the composer's own bottom-border line (`╰─...─╯`) shows the `@path`
fragment BEFORE submit and is blank (only border/space) AFTER — verification
now reads ONLY that line, matched against a short tail fragment of the path.

Covers:
  - composer still shows the pending fragment after paste 1 -> a full
    second paste (within the 2-paste hard cap) succeeds,
  - the swallowed-Enter case: composer still pending after the normal
    Escape+Enter -> one extra bare Enter (no retyping) recovers it,
  - exhausting the hard cap (2 pastes) -> inject_file returns False,
  - a transcript ECHO of `@path` elsewhere in the pane must NOT cause a
    false "pending" read (the false-negative / duplicate-dispatch risk) —
    only the composer's own bottom-border line is examined,
  - a blank/failed capture-pane read ("unclear", the TUI-redraw case seen
    live) is retried as a CAPTURE, never assumed "submitted",
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


def _pane_pending(tf: str, *, echo: bool = False) -> str:
    """Fake pane: composer bottom-border line still shows the mention.

    `echo=True` additionally puts an `@path` mention ABOVE the composer, as
    if omp already echoed a PRIOR submitted message back into the
    transcript — must not be mistaken for the composer's own state.
    """
    lines = []
    if echo:
        lines.append(f"> @{tf}")
        lines.append("(agent is working on the previous task...)")
    lines.append("╭──────────────────────────────╮")
    lines.append(f"╰─ @{tf} ─╯")
    return "\n".join(lines) + "\n"


def _pane_submitted(tf: str, *, echo: bool = False) -> str:
    """Fake pane: composer bottom-border line is blank (submitted)."""
    lines = []
    if echo:
        lines.append(f"> @{tf}")
        lines.append("(agent is working...)")
    lines.append("╭──────────────────────────────╮")
    lines.append("╰─                              ─╯")
    return "\n".join(lines) + "\n"


def test_inject_file_retries_full_paste_when_composer_stays_pending():
    """Composer bottom line still shows the pending mention after the
    normal sequence AND the swallowed-Enter fallback -> a full second paste
    (within the 2-paste hard cap) succeeds."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)
    calls = []
    capture_n = {"n": 0}

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "capture-pane":
            capture_n["n"] += 1
            # capture 1 (after Enter) and 2 (after swallowed-Enter fallback)
            # within paste attempt 1 both still show the pending mention;
            # capture 3 (after Enter in paste attempt 2) shows submitted.
            if capture_n["n"] <= 2:
                return 0, _pane_pending(tf)
            return 0, _pane_submitted(tf)
        return 0, ""

    sleeps = []
    ctrl, _tmp2 = _controller(fake_run, sleep_log=sleeps)

    ok = ctrl.inject_file(tf, max_paste_attempts=2, retry_backoff=5.0)

    assert ok is True
    assert 5.0 in sleeps, "the full-retry backoff must have fired before the 2nd paste"
    at_mentions = [
        c for c in calls if c and c[0] == "send-keys" and c[-1] == f"@{tf}"
    ]
    assert len(at_mentions) == 2, "expected the @path mention retyped exactly once (2 pastes total)"


def test_inject_file_swallowed_enter_recovers_within_same_paste():
    """Composer pending after the normal Escape+Enter sequence -> ONE extra
    bare Enter (no retyping) recovers it, no full retry/re-paste."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)
    calls = []
    capture_n = {"n": 0}

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "capture-pane":
            capture_n["n"] += 1
            if capture_n["n"] == 1:
                return 0, _pane_pending(tf)
            return 0, _pane_submitted(tf)  # submitted after the extra Enter
        return 0, ""

    sleeps = []
    ctrl, _tmp2 = _controller(fake_run, sleep_log=sleeps)

    ok = ctrl.inject_file(tf, max_paste_attempts=2, retry_backoff=5.0)

    assert ok is True
    assert 5.0 not in sleeps, "must not need a full retry for a single swallowed Enter"
    at_mentions = [c for c in calls if c and c[0] == "send-keys" and c[-1] == f"@{tf}"]
    assert len(at_mentions) == 1, "must not retype @path for the swallowed-Enter fallback"
    enters = [c for c in calls if c and c[0] == "send-keys" and c[-1] == "Enter"]
    assert len(enters) == 2, "normal Enter + one swallowed-Enter fallback Enter"


def test_inject_file_hard_caps_paste_at_two_attempts():
    """Composer NEVER clears -> inject_file must give up after exactly 2
    @path sends (the hard cap — re-pasting more risks duplicate dispatch)
    and report failure instead of returning True/None unconditionally."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)
    calls = []

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "capture-pane":
            return 0, _pane_pending(tf)  # always pending
        return 0, ""

    ctrl, _tmp2 = _controller(fake_run)

    ok = ctrl.inject_file(tf, max_paste_attempts=2, retry_backoff=0.0)

    assert ok is False
    at_mentions = [c for c in calls if c and c[0] == "send-keys" and c[-1] == f"@{tf}"]
    assert len(at_mentions) == 2, "must hard-cap @path sends at max_paste_attempts, never more"


def test_inject_file_ignores_echoed_at_path_outside_composer():
    """False-negative regression (review finding): omp echoing the
    submitted message back into the transcript above the composer must NOT
    be mistaken for a still-pending composer — only the composer's own
    bottom-border line counts. A successful submit must resolve on the
    FIRST paste, never triggering a duplicate re-send."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)
    calls = []

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "capture-pane":
            # Composer itself is blank/submitted, but the transcript above
            # it still shows the echoed @path from the just-submitted turn.
            return 0, _pane_submitted(tf, echo=True)
        return 0, ""

    ctrl, _tmp2 = _controller(fake_run)

    ok = ctrl.inject_file(tf, max_paste_attempts=2, retry_backoff=5.0)

    assert ok is True
    at_mentions = [c for c in calls if c and c[0] == "send-keys" and c[-1] == f"@{tf}"]
    assert len(at_mentions) == 1, (
        "an echoed @path outside the composer must not trigger a duplicate "
        "re-send — this is the exact duplicate-dispatch regression"
    )


def test_inject_file_blank_capture_is_retried_not_assumed_submitted():
    """A blank/failed capture-pane read (TUI redraw — happened live on
    2026-07-12) is "unclear", not "submitted": retried as a CAPTURE (not a
    re-paste) until a clean read comes back."""
    tmp = tempfile.mkdtemp(prefix="omp-inject-")
    tf = _task_file_path(tmp)
    calls = []
    capture_n = {"n": 0}

    def fake_run(args):
        calls.append(list(args))
        if args[0] == "capture-pane":
            capture_n["n"] += 1
            if capture_n["n"] <= 2:
                return 0, ""  # blank capture (TUI redraw)
            return 0, _pane_submitted(tf)
        return 0, ""

    sleeps = []
    ctrl, _tmp2 = _controller(fake_run, sleep_log=sleeps)

    ok = ctrl.inject_file(
        tf, max_paste_attempts=2, verify_attempts=5, verify_wait=1.0,
    )

    assert ok is True
    at_mentions = [c for c in calls if c and c[0] == "send-keys" and c[-1] == f"@{tf}"]
    assert len(at_mentions) == 1, "a blank capture must trigger a re-capture, not a re-paste"


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
            return 0, _pane_pending(tf)  # never verified -> inject_file gives up
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
            return 0, _pane_pending(tf)
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
