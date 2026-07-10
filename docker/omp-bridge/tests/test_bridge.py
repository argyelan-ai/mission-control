#!/usr/bin/env python3
"""GOLDEN tests for the omp-bridge lifecycle reducer.

Feeds captured/synthetic NDJSON streams through the bridge and asserts the
terminal MC lifecycle decision:
  - genuine finish (stopReason=stop + sentinel + valid reflection) -> FINISH
  - every abort / silent-abort / malformed case                    -> SET-BLOCKER

Runs two ways:
  * pytest:      pytest -v            (in docker/omp-bridge/tests/)
  * standalone:  python3 test_bridge.py   (no pytest needed — prints a report)

Real captured streams (../rpc/*.ndjson) are used directly where present, so the
golden assertions run against GROUND TRUTH, not only synthetic data.
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
sys.path.insert(0, BRIDGE_DIR)

import bridge  # noqa: E402
from bridge import Kind, LoggingLifecycle, drive_run  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
RPC = os.path.join(BRIDGE_DIR, "rpc")


def _decide(path: str, *, review: bool = True, retries: int = 0):
    """Run one stream file through the driver, return (action, silent_calls)."""
    calls: list[str] = []
    lc = LoggingLifecycle(sink=calls.append)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        action = drive_run(fh, lc, task_id="T1",
                           board_requires_review=review, retries_left=retries)
    return action, lc


def _classify(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        outcome, cls = bridge.classify_stream(fh)
    return outcome, cls


# ---------------------------------------------------------------------------
# The FINISH case (the completion oracle)
# ---------------------------------------------------------------------------

def test_finish_with_sentinel_decides_finish():
    action, lc = _decide(os.path.join(FIX, "finish-with-sentinel.ndjson"))
    assert action.action == "finish", action
    assert action.classification.kind is Kind.FINISH
    assert action.review is True                      # MC-Dev board -> review
    assert action.reflection and "## Lesson fuer Agent-Memory" in action.reflection
    kinds = [c[0] for c in lc.calls]
    assert "ack" in kinds and "finish" in kinds
    assert "blocker" not in kinds


def test_finish_without_review_goes_done():
    action, _ = _decide(os.path.join(FIX, "finish-with-sentinel.ndjson"), review=False)
    assert action.action == "finish"
    assert action.review is False                     # -> done


# ---------------------------------------------------------------------------
# The SILENT-ABORT gap the whole design closes
# ---------------------------------------------------------------------------

def test_incomplete_abort_crash_sets_blocker():
    # A turn ends without completion (no agent_end) -> NEVER left in_progress.
    action, lc = _decide(os.path.join(FIX, "incomplete-abort-crash.ndjson"))
    assert action.action == "blocker", action
    assert action.classification.kind is Kind.ABORT_CRASH
    assert action.blocker_type == "technical_problem"
    assert ("ack",) not in []  # sanity
    kinds = [c[0] for c in lc.calls]
    assert "blocker" in kinds and "finish" not in kinds


def test_transient_api_error_is_first_class_and_blocks_after_retries():
    # The ORIGINAL openclaude failure. Retryable, but with retries exhausted
    # the terminal decision is a human-visible blocker (never `mc failed`).
    action, _ = _decide(os.path.join(FIX, "transient-api-error.ndjson"), retries=0)
    assert action.action == "blocker"
    assert action.classification.kind is Kind.ABORT_TRANSIENT_API
    assert action.classification.retryable is True


def _spawn_factory(path: str):
    """Return a spawn() callable that yields a FRESH open stream of `path` each
    time the driver re-runs omp (each retry re-reads from the top)."""
    def spawn():
        return open(path, "r", encoding="utf-8", errors="replace")
    return spawn


def test_transient_api_error_blocks_after_exhausting_real_retries():
    # Issue #2: with a positive budget AND a real executor, drive_run must OWN the
    # retry loop and STILL resolve terminally. Every retry re-hits the transient
    # error; once the budget is spent the terminal decision is a human-visible
    # blocker (never a dangling 'retry', never 'failed').
    p = os.path.join(FIX, "transient-api-error.ndjson")
    calls: list = []
    lc = LoggingLifecycle(sink=lambda s: None)
    lc.calls = calls
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        action = drive_run(fh, lc, task_id="T1", board_requires_review=True,
                           retries_left=2, spawn=_spawn_factory(p))
    assert action.action == "blocker", action              # TERMINAL, not 'retry'
    assert action.classification.kind is Kind.ABORT_TRANSIENT_API
    assert action.classification.retryable is True
    kinds = [c[0] for c in calls]
    assert kinds.count("ack") == 1                          # claimed once, not per retry
    assert kinds.count("comment") == 2                      # exactly OMP_MAX_RETRIES re-spawns
    assert kinds.count("blocker") == 1 and "finish" not in kinds


def test_retry_then_finish_recovers_within_budget():
    # A transient abort followed by a clean finish stream -> the retry loop
    # recovers and the terminal decision is FINISH (proves retry is executed,
    # not just logged).
    err = os.path.join(FIX, "transient-api-error.ndjson")
    ok = os.path.join(FIX, "finish-with-sentinel.ndjson")
    lc = LoggingLifecycle(sink=lambda s: None)
    with open(err, "r", encoding="utf-8", errors="replace") as fh:
        action = drive_run(fh, lc, task_id="T1", board_requires_review=True,
                           retries_left=2, spawn=_spawn_factory(ok))
    assert action.action == "finish", action
    assert action.classification.kind is Kind.FINISH
    kinds = [c[0] for c in lc.calls]
    assert kinds.count("ack") == 1 and kinds.count("comment") == 1
    assert kinds.count("finish") == 1 and "blocker" not in kinds


def test_retry_without_executor_still_resolves_to_blocker():
    # No spawn executor wired (pure replay) + budget left: the retryable class must
    # NOT strand as 'retry' — it collapses to a terminal blocker.
    action, _ = _decide(os.path.join(FIX, "transient-api-error.ndjson"), retries=2)
    assert action.action == "blocker"
    assert action.classification.kind is Kind.ABORT_TRANSIENT_API


def test_malformed_reflection_sets_blocker():
    _, cls = _classify(os.path.join(FIX, "malformed-reflection.ndjson"))
    assert cls.kind is Kind.MALFORMED_REFLECTION
    action, _ = _decide(os.path.join(FIX, "malformed-reflection.ndjson"))
    assert action.action == "blocker"


def test_anti_echo_giveup_sets_blocker():
    # TASK_COMPLETE echoed mid-text, real last line is a give-up -> not finished.
    _, cls = _classify(os.path.join(FIX, "anti-echo-giveup.ndjson"))
    assert cls.kind is Kind.SILENT_ABORT_NO_SENTINEL
    action, _ = _decide(os.path.join(FIX, "anti-echo-giveup.ndjson"))
    assert action.action == "blocker"


# ---------------------------------------------------------------------------
# REAL captured streams (ground truth) — all are non-finishes under the contract
# ---------------------------------------------------------------------------

def test_real_json_stream_is_silent_abort_without_sentinel():
    # The real multi-step run: stopReason=stop + "Done." text but NO sentinel/
    # reflection (bare prompt). Under the completion contract -> blocker, which
    # is exactly the semantic silent-abort the design catches.
    p = os.path.join(RPC, "json-stream.ndjson")
    if not os.path.exists(p):
        return
    outcome, cls = _classify(p)
    assert outcome.saw_agent_end is True
    assert outcome.final_stop_reason == "stop"
    assert cls.kind is Kind.SILENT_ABORT_NO_SENTINEL
    action, _ = _decide(p)
    assert action.action == "blocker"


def test_real_err2_stream_is_model_error():
    p = os.path.join(RPC, "err2-stream.ndjson")
    if not os.path.exists(p):
        return
    _, cls = _classify(p)
    # Azure preflight config error -> stopReason=error, non-transient message.
    assert cls.kind in (Kind.ABORT_ERROR, Kind.ABORT_TRANSIENT_API)
    action, _ = _decide(p)
    assert action.action == "blocker"


def test_real_maxtime_stream_is_maxtime_cutoff():
    p = os.path.join(RPC, "maxtime-stream.ndjson")
    if not os.path.exists(p):
        return
    outcome, cls = _classify(p)
    assert cls.kind is Kind.ABORT_MAXTIME
    assert outcome.tool_cancelled is True
    action, _ = _decide(p)
    assert action.action == "blocker"


def test_real_trivial_stream_is_silent_abort_without_sentinel():
    p = os.path.join(RPC, "trivial-json.ndjson")
    if not os.path.exists(p):
        return
    _, cls = _classify(p)
    assert cls.kind is Kind.SILENT_ABORT_NO_SENTINEL


# ---------------------------------------------------------------------------
# The HANG case (design §2 case 3 / §3.3 / §3.4 "hang fixture") — the same
# failure family as the original openclaude bug, closed by the out-of-band
# wall-clock / no-progress watchdog.
# ---------------------------------------------------------------------------

def test_hang_watchdog_flag_classifies_abort_hang_and_blocks():
    # Reducer level: a watchdog-killed run -> ABORT_HANG -> terminal blocker.
    outcome = bridge.RunOutcome(saw_session=True, watchdog_killed=True)
    cls = bridge.classify(outcome)
    assert cls.kind is Kind.ABORT_HANG
    action = bridge.decide_lifecycle(cls, board_requires_review=True, retries_left=0)
    assert action.action == "blocker"


def test_hang_live_watchdog_fires_over_blocking_pipe():
    # LIVE-PATH proof (issue #1/#3): replay the synthetic hang fixture through
    # supervise_stream over a REAL blocking pipe. The stream advances (4 lines)
    # then wedges with the write end held open, exactly like a deadlocked provider
    # read. The OUT-OF-BAND no-progress watchdog must fire WHILE the reader is
    # blocked on readline() — the branch that was dead code before the fix.
    fixture = os.path.join(FIX, "hang-truncated.ndjson")
    with open(fixture, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    rfd, wfd = os.pipe()
    rf = os.fdopen(rfd, "r", encoding="utf-8")
    wf = os.fdopen(wfd, "w", encoding="utf-8")
    for ln in lines:
        wf.write(ln)
    wf.flush()
    # wfd intentionally left OPEN -> the reader blocks after these 4 lines.

    outcome = bridge.RunOutcome()
    killed = {"n": 0}

    def _kill():
        killed["n"] += 1
        try:
            wf.close()      # closing the write end gives the reader EOF -> unblocks
        except Exception:
            pass

    import time as _t
    start = _t.monotonic()
    bridge.supervise_stream(
        rf, outcome,
        kill=_kill,
        deadline=_t.monotonic() + 100,   # not the wall-clock branch — isolate idle
        stream_idle_timeout=0.3,         # no-progress deadline fires fast
        poll_interval=0.05,
    )
    elapsed = _t.monotonic() - start

    assert outcome.watchdog_killed is True          # the watchdog actually fired
    assert killed["n"] >= 1                          # SIGKILL path was taken
    assert outcome.saw_session is True               # it DID advance before wedging
    assert outcome.saw_agent_end is False            # never terminated cleanly
    assert elapsed < 5.0                             # fired promptly, no infinite block
    assert bridge.classify(outcome).kind is Kind.ABORT_HANG
    try:
        rf.close()
    except Exception:
        pass


def test_wall_clock_watchdog_fires_even_while_stream_advances():
    # The hard wall-clock cap must fire independently of idle: feed a stream that
    # keeps advancing but set the deadline in the past -> immediate kill.
    fixture = os.path.join(FIX, "hang-truncated.ndjson")
    with open(fixture, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    rfd, wfd = os.pipe()
    rf = os.fdopen(rfd, "r", encoding="utf-8")
    wf = os.fdopen(wfd, "w", encoding="utf-8")
    for ln in lines:
        wf.write(ln)
    wf.flush()
    outcome = bridge.RunOutcome()

    def _kill():
        try:
            wf.close()
        except Exception:
            pass

    import time as _t
    bridge.supervise_stream(
        rf, outcome,
        kill=_kill,
        deadline=_t.monotonic() - 1.0,   # ALREADY past -> wall-clock branch
        stream_idle_timeout=999.0,       # idle branch cannot be the cause
        poll_interval=0.05,
    )
    assert outcome.watchdog_killed is True
    assert bridge.classify(outcome).kind is Kind.ABORT_HANG
    try:
        rf.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Determinism guarantee: every run resolves to finish | blocker | retry
# ---------------------------------------------------------------------------

def test_no_run_is_ever_left_in_progress():
    streams = []
    for d in (FIX, RPC):
        if os.path.isdir(d):
            streams += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".ndjson")]
    assert streams
    for p in streams:
        action, _ = _decide(p, retries=0)
        assert action.action in ("finish", "blocker"), (p, action.action)


# ---------------------------------------------------------------------------
# McCliLifecycle terminal guarantee: a failed `mc finish` must fall back to
# `mc blocked` (never leave the task silently in_progress — the exact hang this
# runtime exists to close).
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, rc, stderr=None):
        self.returncode = rc
        self.stdout = ""
        self.stderr = stderr if stderr is not None else ("rejected" if rc else "")


def _mc_cli_with_fake_run(run_fn):
    lc = bridge.McCliLifecycle(
        api_url="http://x", token="t", task_id="T1",
        board_id="B", attempt_id="A", mc_bin="mc",
    )
    orig = bridge.subprocess.run
    bridge.subprocess.run = run_fn
    try:
        lc.finish("T1", "REFLECTION-TEXT", review=True)
    finally:
        bridge.subprocess.run = orig


def test_mc_cli_finish_falls_back_to_blocked_on_nonzero():
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        return _FakeProc(1 if sub == "finish" else 0)  # finish fails, blocked ok

    _mc_cli_with_fake_run(fake_run)
    subs = [c[1] for c in calls if len(c) > 1]
    assert "finish" in subs, subs
    assert "blocked" in subs, ("failed finish MUST fall back to blocked", subs)


def test_mc_cli_finish_no_fallback_on_success():
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _FakeProc(0)

    _mc_cli_with_fake_run(fake_run)
    subs = [c[1] for c in calls if len(c) > 1]
    assert subs == ["finish"], ("clean finish must NOT spuriously block", subs)


# ---------------------------------------------------------------------------
# McCliLifecycle checklist-open handoff: a `mc finish` failure caused by open
# checklist items (including out-of-role items an omp agent physically cannot
# do, e.g. a live Vercel deploy) must route to review with a handoff comment
# listing the pending items — NOT the generic `blocked`/technical_problem
# fallback, which mislabels "work is genuinely done except for an out-of-role
# item" as a technical failure. Real errors (5xx, network, unparseable) must
# still fall back to blocked — the terminal no-silent-hang guarantee holds.
# ---------------------------------------------------------------------------

_CHECKLIST_OPEN_STDERR = (
    "mc finish: 2 Checklist-Item(s) noch offen: ab12cd34 (Vercel Deploy), "
    "ef56ab78 (DNS Cutover). Erst alle mit `mc checklist done <id>` schliessen, "
    "dann `mc finish` erneut."
)


def test_mc_cli_finish_checklist_open_routes_to_review_not_blocked():
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "finish":
            return _FakeProc(1, stderr=_CHECKLIST_OPEN_STDERR)
        return _FakeProc(0)

    _mc_cli_with_fake_run(fake_run)
    subs = [c[1] for c in calls if len(c) > 1]
    assert "finish" in subs, subs
    assert "review" in subs, ("checklist-open must route to review", subs)
    assert "blocked" not in subs, ("checklist-open must NOT fall back to blocked", subs)
    assert "comment" in subs, ("checklist-open must post a handoff comment", subs)
    comment_call = next(c for c in calls if len(c) > 1 and c[1] == "comment")
    # The handoff comment must carry the specific pending items, not a generic
    # message, so the Board Lead/Mark sees exactly what's outstanding.
    joined = " ".join(comment_call)
    assert "Vercel Deploy" in joined or "ab12cd34" in joined, comment_call


def test_mc_cli_finish_real_error_still_falls_back_to_blocked():
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "finish":
            return _FakeProc(2, stderr="mc finish: 502 Bad Gateway from backend")
        return _FakeProc(0)

    _mc_cli_with_fake_run(fake_run)
    subs = [c[1] for c in calls if len(c) > 1]
    assert "finish" in subs, subs
    assert "blocked" in subs, ("genuine errors must still fall back to blocked", subs)
    assert "review" not in subs, ("genuine errors must NOT be routed to review", subs)


def test_mc_cli_finish_checklist_open_falls_back_to_blocked_when_review_also_fails():
    # Checklist-open triggers the review-handoff branch, but the `mc review`
    # rescue itself fails (network/5xx/concurrent status change). The task must
    # NOT be left silently in_progress — fall back to `blocked` so it still
    # reaches a terminal, Mark-visible state (the no-silent-hang guarantee).
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "finish":
            return _FakeProc(1, stderr=_CHECKLIST_OPEN_STDERR)
        if sub == "review":
            return _FakeProc(2, stderr="mc review: 503 Service Unavailable")
        return _FakeProc(0)  # comment + blocked succeed

    _mc_cli_with_fake_run(fake_run)
    subs = [c[1] for c in calls if len(c) > 1]
    assert "finish" in subs, subs
    assert "comment" in subs, ("handoff comment must still be posted first", subs)
    assert "review" in subs, ("review rescue must be attempted", subs)
    assert "blocked" in subs, (
        "when the review rescue fails, MUST fall back to blocked, "
        "never a silent in_progress return", subs,
    )


# ---------------------------------------------------------------------------
# Standalone runner (no pytest)
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    print("=" * 70)
    print("omp-bridge GOLDEN TEST (standalone runner)")
    print("=" * 70)
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("-" * 70)
    print(f"  {passed} passed, {failed} failed")
    print("=" * 70)

    # Also print the human decision table for the key streams.
    print("\nDECISION TABLE (stream -> kind -> MC action):")
    demo = [
        ("fixtures/finish-with-sentinel.ndjson", FIX),
        ("fixtures/incomplete-abort-crash.ndjson", FIX),
        ("fixtures/transient-api-error.ndjson", FIX),
        ("fixtures/malformed-reflection.ndjson", FIX),
        ("fixtures/anti-echo-giveup.ndjson", FIX),
        ("json-stream.ndjson", RPC),
        ("err2-stream.ndjson", RPC),
        ("maxtime-stream.ndjson", RPC),
        ("trivial-json.ndjson", RPC),
    ]
    for name, base in demo:
        p = os.path.join(base, os.path.basename(name))
        if not os.path.exists(p):
            print(f"  (skip {name} — not present)")
            continue
        action, _ = _decide(p)
        cls = action.classification
        tag = "FINISH " if action.action == "finish" else "BLOCKER"
        print(f"  {tag}  {name:38s} kind={cls.kind.value}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
