#!/usr/bin/env python3
"""ADR-045 §4.3 — unit tests for bridge.py `--serve` poll loop + real lifecycle.

Exercises the injection seams (no network, no omp subprocess):
  - new_task → ack + finish (genuine completion contract)
  - ack-dedup: the same dispatch_attempt_id delivered twice spawns ONE run
  - idle between tasks clears the dedup cache
  - a retryable-then-exhausted abort resolves terminally as a blocker
  - container_workspace_path host→/workspace translation + null fallback
  - _default_model_selector always yields a provider-qualified selector
Run: python3 test_serve_loop.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py

import bridge  # noqa: E402


def _finish_outcome() -> "bridge.RunOutcome":
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


def _crash_outcome() -> "bridge.RunOutcome":
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_start = True
    o.saw_agent_end = False  # no agent_end -> ABORT_CRASH (retryable)
    return o


class RecordingLifecycle(bridge.MCLifecycle):
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


def _run(poll_states, run_factory, *, iterations, lifecycle=None):
    lc = lifecycle or RecordingLifecycle()
    it = iter(poll_states)

    def poll():
        try:
            return next(it)
        except StopIteration:
            return {"state": "idle"}

    bridge.serve_loop(
        poll_interval=0,
        max_iterations=iterations,
        _poll_fn=poll,
        _lifecycle_factory=lambda task: lc,
        _run_factory=run_factory,
        _sleep=lambda _s: None,
    )
    return lc


TASK = {
    "id": "task-1", "board_id": "board-1", "dispatch_attempt_id": "att-1",
    "workspace_path": "/Users/testuser/.mc/workspaces/sparky/proj/.worktrees/task-1",
    "prompt": "Do the thing.",
}


def test_new_task_ack_then_finish():
    seen_cwd = {}

    def rf(task, cwd):
        seen_cwd["cwd"] = cwd
        return _finish_outcome

    lc = _run([{"state": "new_task", "task": TASK}], rf, iterations=1)
    assert ("ack", "task-1") in lc.calls
    assert any(c[0] == "finish" for c in lc.calls)
    # cwd translated to the container view.
    assert seen_cwd["cwd"] == "/workspace/proj/.worktrees/task-1"
    print("PASS test_new_task_ack_then_finish")


def test_ack_dedup_same_attempt_runs_once():
    runs = {"n": 0}

    def rf(task, cwd):
        def _once():
            runs["n"] += 1
            return _finish_outcome()
        return _once

    # Same attempt delivered on two consecutive polls -> exactly one run.
    lc = _run(
        [{"state": "new_task", "task": TASK}, {"state": "new_task", "task": TASK}],
        rf, iterations=2,
    )
    assert runs["n"] == 1, runs
    assert sum(1 for c in lc.calls if c[0] == "ack") == 1
    print("PASS test_ack_dedup_same_attempt_runs_once")


def test_idle_clears_dedup_then_reruns():
    runs = {"n": 0}

    def rf(task, cwd):
        def _once():
            runs["n"] += 1
            return _finish_outcome()
        return _once

    lc = _run(
        [
            {"state": "new_task", "task": TASK},
            {"state": "idle"},
            {"state": "new_task", "task": TASK},
        ],
        rf, iterations=3,
    )
    assert runs["n"] == 2, runs
    print("PASS test_idle_clears_dedup_then_reruns")


def test_retryable_abort_exhausts_to_blocker():
    def rf(task, cwd):
        return _crash_outcome  # always crashes -> retry budget then blocker

    os.environ["OMP_MAX_RETRIES"] = "1"
    try:
        lc = _run([{"state": "new_task", "task": TASK}], rf, iterations=1)
    finally:
        del os.environ["OMP_MAX_RETRIES"]
    kinds = [c[0] for c in lc.calls]
    assert "ack" in kinds
    assert "blocker" in kinds  # always terminal, never left in_progress
    assert "finish" not in kinds
    print("PASS test_retryable_abort_exhausts_to_blocker")


def test_container_workspace_path():
    assert (
        bridge.container_workspace_path("/Users/testuser/.mc/workspaces/sparky/a/b")
        == "/workspace/a/b"
    )
    assert bridge.container_workspace_path(None) is None
    # ad-hoc / unknown shape passes through unchanged.
    assert bridge.container_workspace_path("/tmp/x") == "/tmp/x"
    print("PASS test_container_workspace_path")


def test_default_model_selector():
    assert bridge._default_model_selector("nvidia/Qwen3.6-35B-A3B-NVFP4") == (
        "mc-openai/nvidia/Qwen3.6-35B-A3B-NVFP4"
    )
    # No baked-in fallback (ADR-054): missing model is a boot error.
    try:
        bridge._default_model_selector("")
        raise AssertionError("expected RuntimeError for empty model")
    except RuntimeError:
        pass
    # already provider-qualified stays as-is.
    assert bridge._default_model_selector("mc-openai/foo") == "mc-openai/foo"
    print("PASS test_default_model_selector")


def test_ready_sentinel_printed(capfd=None):
    # OMP_BRIDGE_READY must be printed on the first completed poll.
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bridge.serve_loop(
            poll_interval=0, max_iterations=1,
            _poll_fn=lambda: {"state": "idle"},
            _sleep=lambda _s: None,
        )
    assert "OMP_BRIDGE_READY" in buf.getvalue()
    print("PASS test_ready_sentinel_printed")


def test_write_task_context_env_writes_three_keys():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "mc-context.env")
        ok = bridge.write_task_context_env(
            {"id": "task-1", "board_id": "board-1", "dispatch_attempt_id": "att-1"}, p
        )
        assert ok is True
        body = open(p, encoding="utf-8").read()
    assert body == "TASK_ID=task-1\nBOARD_ID=board-1\nX_DISPATCH_ATTEMPT_ID=att-1\n", body
    print("PASS test_write_task_context_env_writes_three_keys")


def test_write_task_context_env_missing_fields_empty():
    # board_id/attempt absent -> empty values, never a crash (mirrors poll.sh).
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "mc-context.env")
        bridge.write_task_context_env({"id": "task-9"}, p)
        body = open(p, encoding="utf-8").read()
    assert body == "TASK_ID=task-9\nBOARD_ID=\nX_DISPATCH_ATTEMPT_ID=\n", body
    print("PASS test_write_task_context_env_missing_fields_empty")


def test_write_task_context_env_unwritable_is_best_effort():
    # An unwritable path must return False, not raise (serve loop must survive).
    ok = bridge.write_task_context_env(
        {"id": "t"}, "/nonexistent-dir-xyz/mc-context.env"
    )
    assert ok is False
    print("PASS test_write_task_context_env_unwritable_is_best_effort")


def test_serve_writes_context_env_before_run():
    # The context file must exist with the dispatch's ids by the time the run
    # (and its ack) executes — the model's first `mc ack` depends on it.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        ctx = os.path.join(d, "mc-context.env")
        seen = {}

        def rf(task, cwd):
            # Snapshot the file as the run begins — proves write-before-run.
            seen["at_run"] = open(ctx, encoding="utf-8").read()
            return _finish_outcome

        it = iter([{"state": "new_task", "task": TASK}])
        bridge.serve_loop(
            poll_interval=0, max_iterations=1,
            _poll_fn=lambda: next(it, {"state": "idle"}),
            _lifecycle_factory=lambda task: RecordingLifecycle(),
            _run_factory=rf,
            _sleep=lambda _s: None,
            _context_env_path=ctx,
        )
    assert seen["at_run"] == (
        "TASK_ID=task-1\nBOARD_ID=board-1\nX_DISPATCH_ATTEMPT_ID=att-1\n"
    ), seen
    print("PASS test_serve_writes_context_env_before_run")


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
