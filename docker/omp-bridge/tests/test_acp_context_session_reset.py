#!/usr/bin/env python3
"""G5 context-holder: reset at the SESSION change, not at the turn boundary.

Task 1556064c. Two failed intermediate states shaped this:
  - #554: the holder was NEVER reset — a new session kept reporting the
    previous session's context% (the leak).
  - #560: a reset at the TURN boundary — measured broken: usage_update lands
    only 2 events before turn end in all three fixtures, the only sender is
    the 30 s heartbeater, so the value would have been reported essentially
    by chance.

Verified against the real code (this is why on_session_id was REJECTED as
the reset point): run_acp_once opens a NEW ACP session for EVERY attempt
(`client = make_client()` + `client.new_session(cwd)`) — continue-nudges and
retries included. On_session_id therefore fires per ATTEMPT; a reset there is
the per-turn reset #560 measured as broken.

The fix: serve_loop stamps the holder with 0.0 ONCE per task pickup (the
session change in the display's sense). The value then survives every attempt
within the task, and a new task starts at a REPORTED 0% instead of leaking
the previous session's value — 0.0, not None, because the receiver's
semantics ("no context_pct = no news", agents.py, deliberately unchanged)
would otherwise keep the stale % on display until the new session's first
usage_update.

Tests here drive the REAL serve_loop dispatch (in-process fake ACP server,
same machinery as test_acp_through_tests.py) — no text anchors: the sabotage
probe removes the reset statement from serve_loop via AST on a copied tree
and re-drives the scenario in a subprocess; the leak must reproduce.

Run:  python3 test_acp_context_session_reset.py   OR   pytest -v
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # docker/omp-bridge
REPO_ROOT = os.path.dirname(os.path.dirname(ROOT))  # repo root

sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import bridge  # noqa: E402
import test_acp_adapter as taa  # noqa: E402  (fixtures + in-process fake)

BRIDGE_SRC = os.path.join(ROOT, "bridge.py")

# acp-normal-turn.ndjson: usage_update size=500000 used=17395 -> 3.5 %
EXPECTED_PCT = 3.5

_SERVE_ENV_KEYS = (
    "PI_CODING_AGENT_DIR", "OMP_DRIVER", "OMP_ACP_CWD", "OMP_ACP_PERMISSIONS",
    "OMP_TASK_DEADLINE", "MSG_DELIVERY_MODE", "OMP_HOME", "OMP_PROFILE",
    "OMP_TURN_SIGNAL_FILE",
    "OMP_TASK_LOCK_FILE", "OMP_MSG_QUEUE_DIR", "OMP_MSG_ACK_DIR",
    "OMP_MSG_NUDGE_STATE_FILE", "OMP_MSG_NUDGE_MSG_FILE",
    "OMP_MAX_RETRIES", "OMP_MAX_CONTINUES",
)

_SERVE_TASK = {"id": "task-1", "board_id": "board-1", "dispatch_attempt_id": "att-1",
               "workspace_path": "/workspace", "prompt": "Do the thing."}


class _RecordingLifecycle(bridge.MCLifecycle):
    def __init__(self):
        self.calls = []

    def ack(self, task_id):
        self.calls.append(("ack", task_id))

    def finish(self, task_id, reflection, *, review):
        self.calls.append(("finish", task_id, reflection, review))

    def set_blocker(self, task_id, *, blocker_type, question):
        self.calls.append(("blocker", task_id, blocker_type))

    def comment(self, task_id, text):
        self.calls.append(("comment", task_id))

    def task_is_active(self, task_id):
        return None


def drive_serve_loop_pickup(*, seed_pct: float, fixture_name: str) -> dict:
    """One REAL serve_loop dispatch on the ACP branch against the in-process
    fake server, with the holder pre-seeded to `seed_pct` (the previous
    session's last report). Returns the heartbeat payload the heartbeater
    would send AFTER the dispatch — the consumer-visible contract.

    `fixture_name` picks the replayed ACP transcript; "silent" contains NO
    usage_update, so nothing restamps the holder after the pickup: whatever
    the payload reports is exactly what the pickup reset produced.
    """
    saved = {k: os.environ.get(k) for k in _SERVE_ENV_KEYS}
    fakes: list = []
    try:
        with tempfile.TemporaryDirectory(prefix="g5-session-reset-") as td:
            tmp = Path(td)
            os.environ.update({
                "PI_CODING_AGENT_DIR": str(tmp / "agent"),
                "OMP_DRIVER": "acp",
                "OMP_ACP_CWD": str(Path(HERE)),
                "OMP_ACP_PERMISSIONS": "yolo",
                "MSG_DELIVERY_MODE": "nudge",
                "OMP_HOME": str(tmp / "home"),
                "OMP_TURN_SIGNAL_FILE": str(tmp / "turn-signal.ndjson"),
                "OMP_TASK_LOCK_FILE": str(tmp / "task.lock"),
                "OMP_MSG_QUEUE_DIR": str(tmp / "msg-q"),
                "OMP_MSG_ACK_DIR": str(tmp / "msg-ack"),
                "OMP_MSG_NUDGE_STATE_FILE": str(tmp / "nudge-state"),
                "OMP_MSG_NUDGE_MSG_FILE": str(tmp / "nudge-msg"),
                "OMP_MAX_RETRIES": "0",
                "OMP_MAX_CONTINUES": "0",
            })
            os.environ.pop("OMP_TASK_DEADLINE", None)
            # The container image pins OMP_PROFILE; with it set, session_dir() prefers
            # the profile tree over PI_CODING_AGENT_DIR and the transcript would land
            # somewhere this test never looks. Restored via _SERVE_ENV_KEYS.
            os.environ.pop("OMP_PROFILE", None)
            (tmp / "agent").mkdir()

            holder = bridge.ACPContextPct(seed_pct)

            orig_run = bridge.run_acp_once

            def spy_run(prompt, **kw):
                fake = taa.InProcessFake(taa.FIXTURES[fixture_name], [])
                fakes.append(fake)
                kw["client_factory"] = lambda: fake.client
                return orig_run(prompt, **kw)

            bridge.run_acp_once = spy_run
            poll_states = iter([{"state": "new_task", "task": dict(_SERVE_TASK)}])

            def poll():
                try:
                    return next(poll_states)
                except StopIteration:
                    return {"state": "idle"}

            try:
                bridge.serve_loop(
                    poll_interval=0, max_iterations=1, _poll_fn=poll,
                    _lifecycle_factory=lambda task: _RecordingLifecycle(),
                    _run_factory=None,
                    _sleep=lambda _s: None,
                    _context_env_path=str(tmp / "mc-context.env"),
                    context_pct=holder,
                )
            finally:
                bridge.run_acp_once = orig_run
            # The consumer-visible contract: what the 30 s heartbeater sends.
            return bridge._build_heartbeat_payload("working", None, holder.get)
    finally:
        for fake in fakes:
            try:
                fake.close()
            except Exception:
                pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_new_task_pickup_reports_zero_not_previous_session_pct():
    """DoD: a NEW session must not inherit the previous session's context%
    (#554). Seed the holder with the old session's last report (43.0), drive
    one REAL serve_loop pickup whose ACP transcript carries NO usage_update
    (nothing restamps the holder), and read the heartbeat payload: it must
    report the pickup's 0.0 — not the seeded 43.0, and not nothing."""
    payload = drive_serve_loop_pickup(seed_pct=43.0, fixture_name="silent")
    assert payload.get("context_pct") == 0.0, (
        f"new session must report the pickup reset (0.0), got {payload!r} — "
        "the previous session's value leaked"
    )
    print("PASS test_new_task_pickup_reports_zero_not_previous_session_pct")


def test_context_pct_survives_new_acp_session_within_task():
    """DoD Gegenprobe: WITHIN a task the value must survive the attempt
    boundary. Attempt 1 (normal fixture) stamps 3.5 via usage_update; the
    continue/retry attempt 2 opens a NEW ACP session (on_session_id fires)
    and carries NO usage_update — exactly where a per-attempt reset (the
    #560 mistake, via on_session_id or turn end) would blank the display.
    The payload after attempt 2 must still report 3.5."""
    holder = bridge.ACPContextPct()
    outcome1, _ = taa.run_adapter(taa.FIXTURES["normal"], context_pct=holder)
    assert outcome1.final_stop_reason == "end_turn"
    assert holder.get() == EXPECTED_PCT

    outcome2, _ = taa.run_adapter(taa.FIXTURES["silent"], context_pct=holder)
    assert outcome2.final_stop_reason == "cancelled"
    assert holder.get() == EXPECTED_PCT, (
        "the value must survive the attempt boundary within a task — "
        "a per-attempt reset would blank it here"
    )
    payload = bridge._build_heartbeat_payload("working", None, holder.get)
    assert payload.get("context_pct") == EXPECTED_PCT, payload
    print("PASS test_context_pct_survives_new_acp_session_within_task")


def _remove_pickup_reset_statement(src: str) -> str:
    """AST-anchored sabotage: delete the `acp_context_pct.set(0.0)`
    statement from serve_loop (located by AST — literal-0.0 call on the
    holder inside the serve_loop FunctionDef — NOT by a text anchor). No
    text search, so a renamed variable or reformatted comment cannot keep
    the probe green."""
    tree = ast.parse(src)
    hits: list[ast.Expr] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "serve_loop"):
            continue
        for sub in ast.walk(node):
            func = getattr(getattr(sub, "value", None), "func", None)
            if (isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Call)
                    and isinstance(func, ast.Attribute) and func.attr == "set"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "acp_context_pct"
                    and len(sub.value.args) == 1
                    and isinstance(sub.value.args[0], ast.Constant)
                    and sub.value.args[0].value == 0.0):
                hits.append(sub)
    assert len(hits) == 1, (
        f"expected exactly 1 pickup-reset statement in serve_loop, found {len(hits)}"
    )
    lines = src.splitlines(keepends=True)
    del lines[hits[0].lineno - 1:hits[0].end_lineno]
    return "".join(lines)


def test_sabotage_pickup_reset_removed_reproduces_the_leak():
    """Sabotage probe (AST mutation on a COPIED tree, replayed in a
    SUBPROCESS — the live process stays untouched): with serve_loop's pickup
    reset removed, the new-task dispatch must report the SEEDED 43.0 again —
    the #554 leak reproduces. Proves the green test above really depends on
    the reset statement."""
    tmp = tempfile.mkdtemp(prefix="g5-reset-sabotage-")
    broot = os.path.join(tmp, "bridge-root")
    shutil.copytree(ROOT, broot)
    btests = os.path.join(broot, "tests")
    target = os.path.join(broot, "bridge.py")
    with open(target, encoding="utf-8") as fh:
        src = fh.read()
    mutated = _remove_pickup_reset_statement(src)
    assert mutated != src, "sabotage mutation is a no-op"
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(mutated)
    script = (
        "import json, sys\n"
        "broot, btests = sys.argv[1], sys.argv[2]\n"
        "sys.path[:0] = [btests, broot]\n"
        "import test_acp_context_session_reset as m\n"
        "payload = m.drive_serve_loop_pickup(seed_pct=43.0, fixture_name='silent')\n"
        "print(json.dumps({'context_pct': payload.get('context_pct')}))\n"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    res = subprocess.run(
        [sys.executable, "-c", script, broot, btests],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert res.returncode == 0, (
        f"sabotage probe did not behave as expected:\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["context_pct"] == 43.0, (
        f"probe did not bite: without the pickup reset the seeded 43.0 must "
        f"leak into the new session's payload, got {out!r}"
    )
    shutil.rmtree(tmp, ignore_errors=True)
    print("PASS test_sabotage_pickup_reset_removed_reproduces_the_leak")


if __name__ == "__main__":
    test_new_task_pickup_reports_zero_not_previous_session_pct()
    test_context_pct_survives_new_acp_session_within_task()
    test_sabotage_pickup_reset_removed_reproduces_the_leak()
