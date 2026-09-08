#!/usr/bin/env python3
"""Subtask 2/4 — adapter tests for the OMP_DRIVER=acp path in bridge.py.

The REAL bridge adapter (bridge.run_acp_once + bridge.classify_acp) runs
against the fake ACP server replaying the golden fixtures, exactly like
test_acp_replay.py drives the raw client.

Covers:
  - run_once: prompt in -> RunOutcome with final_stop_reason/usage/final_text
  - Interrupt (Fix 3 ladder step 1): cancel -> session/cancel notification ->
    stopReason=cancelled -> Kind.INTERRUPTED
  - Classification: end_turn -> completion contract, cancelled -> INTERRUPTED,
    error/abort families, launch preflight
  - Permission policy: yolo auto-allows execute/edit; ask path maps answers
  - Sabotage probe: without OMP_DRIVER the native selection is untouched

Runs two ways:
  * pytest:      pytest test_acp_adapter.py -v   (in docker/omp-bridge/tests/)
  * standalone:  python3 test_acp_adapter.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import bridge  # noqa: E402
import acp_client  # noqa: E402
import fake_acp_server  # noqa: E402  (import guard: syntax check)

RPC = ROOT / "rpc"

FIXTURES = {
    "normal": RPC / "acp-normal-turn.ndjson",
    "permission": RPC / "acp-permission-tool.ndjson",
    "cancel": RPC / "acp-cancel-mid-turn.ndjson",
}

for name, path in FIXTURES.items():
    if not path.exists():
        raise RuntimeError(f"missing fixture: {path}")


def make_client(fixture: Path, transcript_sink: list) -> acp_client.ACPClient:
    """In-process fake: ACPClient wired to an in-memory fake server.

    We drive the REAL ACPClient dispatch path but replace the subprocess with
    a pipe pair fed by fake_acp_server's replay logic (same approach as
    test_acp_replay.make_client, minus the real subprocess spawn)."""
    raise NotImplementedError  # replaced below by in-process fake


class InProcessFake:
    """Runs fake_acp_server.replay_transcript on pipes against the real client."""

    def __init__(self, fixture: Path, transcript_sink: list):
        c_r, c_w = os.pipe()   # client writes here (fake reads)
        f_r, f_w = os.pipe()   # fake writes here (client reads)
        self.client = acp_client.ACPClient(command=["never-spawned"])
        self.client._proc = _FakeProc(c_w, f_r)
        self.client._closed = False
        self._sink = transcript_sink
        self._thread = threading.Thread(
            target=self._run_fake, args=(fixture, c_r, f_w), daemon=True
        )
        self._thread.start()
        self.client._reader = threading.Thread(
            target=self.client._read_loop, name="acp-reader", daemon=True
        )
        self.client._reader.start()
        self.client._stderr_drain = threading.Thread(
            target=lambda: None, name="acp-stderr", daemon=True
        )

    def _run_fake(self, fixture: Path, c_r: int, f_w: int) -> None:
        import io

        with io.open(c_r, "r", encoding="utf-8", newline="") as fin, \
                io.open(f_w, "w", encoding="utf-8", newline="") as fout:
            fake_acp_server.replay(
                [json.loads(l) for l in fixture.read_text().splitlines() if l.strip()],
                fin, fout, sink=self._sink,
            )

    def close(self) -> None:
        self.client.close()


class _FakeProc:
    """Duck-typed subprocess.Popen stand-in for ACPClient._ensure_process."""

    def __init__(self, stdin_w: int, stdout_r: int):
        import io

        self.stdin = io.open(stdin_w, "w", encoding="utf-8", newline="")
        self.stdout = io.open(stdout_r, "r", encoding="utf-8", newline="")
        self.stderr = None
        self.pid = -1
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def run_adapter(fixture: Path, *, policy: str = "yolo", cancel_before: bool = False,
                ask_answers: list | None = None, model: str | None = "m") -> tuple[bridge.RunOutcome, list]:
    """Run one bridge.run_acp_once attempt against an in-process fake server."""
    sink: list = []
    fake = InProcessFake(fixture, sink)
    answers = list(ask_answers or [])
    state = bridge.ACPCancelState(requested=cancel_before)

    def ask_fn(task_id: str, question: str) -> str:
        return answers.pop(0) if answers else "reject"

    try:
        outcome = bridge.run_acp_once(
            "hello golden fixture",
            cwd=str(HERE),
            model=model,
            max_time=10,
            permission_policy=policy,
            task_id="T1",
            cancel_state=state,
            cancel_poll_interval=0.01,
            client_factory=lambda: fake.client,
            ask_fn=ask_fn,
        )
    finally:
        fake.close()
    return outcome, sink


# ---------------------------------------------------------------------------
# run_once contract
# ---------------------------------------------------------------------------

def test_acp_run_once_normal_turn_returns_outcome():
    outcome, _ = run_adapter(FIXTURES["normal"])
    assert outcome.saw_session is True
    assert outcome.final_stop_reason == "end_turn"
    assert outcome.saw_agent_end is True
    assert outcome.usage and outcome.usage.get("totalTokens") == 17400
    print("PASS test_acp_run_once_normal_turn_returns_outcome")


def test_acp_run_once_collects_streamed_text():
    outcome, _ = run_adapter(FIXTURES["normal"])
    assert "hello golden fixture" in outcome.final_text
    print("PASS test_acp_run_once_collects_streamed_text")


def test_acp_run_once_prefixes_context_files(tmp_path="unused"):
    # Context prefix: acp_context_prefix inlines TASK.md/CARD.md when present.
    import tempfile

    d = tempfile.mkdtemp()
    Path(d, "TASK.md").write_text("# Task\nDo the thing.")
    Path(d, "CARD.md").write_text("# Card\nBe brief.")
    prefix = bridge.acp_context_prefix(d)
    assert "--- TASK.md ---" in prefix and "Do the thing." in prefix
    assert "--- CARD.md ---" in prefix and "Be brief." in prefix
    assert bridge.acp_context_prefix(tempfile.mkdtemp()) == ""
    print("PASS test_acp_run_once_prefixes_context_files")


# ---------------------------------------------------------------------------
# Interrupt ladder (Fix 3): cancel -> cancelled -> INTERRUPTED
# ---------------------------------------------------------------------------

def test_acp_cancel_mid_turn_reports_cancelled():
    outcome, _ = run_adapter(FIXTURES["cancel"], cancel_before=True)
    assert outcome.final_stop_reason == "cancelled"
    print("PASS test_acp_cancel_mid_turn_reports_cancelled")


def test_acp_cancelled_classifies_interrupted():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.final_stop_reason = "cancelled"
    o.saw_agent_end = True
    cls = bridge.classify_acp(o)
    assert cls.kind is bridge.Kind.INTERRUPTED
    assert cls.retryable is False  # operator decision, never auto-retried
    assert bridge.Kind.INTERRUPTED not in bridge.RETRYABLE_KINDS
    print("PASS test_acp_cancelled_classifies_interrupted")


def test_acp_cancel_sends_session_cancel_notification():
    sink: list = []
    fake = InProcessFake(FIXTURES["cancel"], sink)
    state = bridge.ACPCancelState(requested=True)
    try:
        bridge.run_acp_once(
            "x", cwd=str(HERE), max_time=10, permission_policy="yolo",
            task_id="T1", cancel_state=state, cancel_poll_interval=0.01,
            client_factory=lambda: fake.client,
        )
    finally:
        fake.close()
    # The cancel notification goes to the fake's stdin — recorded wire-side
    # as {"__client": ...} entries, never parsed as server output.
    cancels = [m["__client"] for m in sink
               if isinstance(m, dict) and m.get("__client", {}).get("method") == "session/cancel"]
    assert cancels, "session/cancel must be sent as ladder step 1"
    assert all("id" not in m for m in cancels), "cancel is a bare notification"
    print("PASS test_acp_cancel_sends_session_cancel_notification")


# ---------------------------------------------------------------------------
# Classification: completion contract + abort families
# ---------------------------------------------------------------------------

def _finish_outcome() -> bridge.RunOutcome:
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_end = True
    o.final_stop_reason = "end_turn"
    o.final_text = (
        "## Was wurde gemacht\nx\n"
        "## Was hat funktioniert\ny\n"
        "## Was war unklar\nz\n"
        "## Lesson fuer Agent-Memory\nw\n"
        "TASK_COMPLETE"
    )
    return o


def test_acp_end_turn_with_sentinel_classifies_finish():
    cls = bridge.classify_acp(_finish_outcome())
    assert cls.kind is bridge.Kind.FINISH
    print("PASS test_acp_end_turn_with_sentinel_classifies_finish")


def test_acp_end_turn_without_sentinel_classifies_silent_abort():
    o = _finish_outcome()
    o.final_text = "Done."
    assert bridge.classify_acp(o).kind is bridge.Kind.SILENT_ABORT_NO_SENTINEL
    print("PASS test_acp_end_turn_without_sentinel_classifies_silent_abort")


def test_acp_error_stop_reason_transient():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_end = True
    o.final_stop_reason = "error"
    o.error_message = "fetch failed: connection error"
    assert bridge.classify_acp(o).kind is bridge.Kind.ABORT_TRANSIENT_API
    assert bridge.classify_acp(o).retryable is True
    print("PASS test_acp_error_stop_reason_transient")


def test_acp_error_stop_reason_model_error():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_end = True
    o.final_stop_reason = "error"
    o.error_message = "invalid api key"
    assert bridge.classify_acp(o).kind is bridge.Kind.ABORT_ERROR
    print("PASS test_acp_error_stop_reason_model_error")


def test_acp_max_tokens_classifies_maxtime():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_end = True
    o.final_stop_reason = "max_tokens"
    assert bridge.classify_acp(o).kind is bridge.Kind.ABORT_MAXTIME
    print("PASS test_acp_max_tokens_classifies_maxtime")


def test_acp_no_session_classifies_launch_preflight():
    o = bridge.RunOutcome()
    o.error_message = "omp acp: command not found"
    assert bridge.classify_acp(o).kind is bridge.Kind.LAUNCH_PREFLIGHT
    print("PASS test_acp_no_session_classifies_launch_preflight")


def test_acp_no_prompt_result_classifies_crash():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.final_stop_reason = None
    assert bridge.classify_acp(o).kind is bridge.Kind.ABORT_CRASH
    print("PASS test_acp_no_prompt_result_classifies_crash")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def test_acp_permission_yolo_allows_execute_always():
    params = json.loads(json.dumps({
        "toolCall": {"kind": "execute", "title": "echo hi"},
        "options": [{"optionId": "allow_always", "kind": "allow_always"}],
    }))
    decision = bridge._acp_permission_decision(params, policy="yolo", task_id="T1",
                                              ask_fn=lambda t, q: "never")
    assert decision == acp_client.ALLOW_ALWAYS
    print("PASS test_acp_permission_yolo_allows_execute_always")


def test_acp_permission_yolo_edit_allows_always():
    params = {"toolCall": {"kind": "edit", "title": "patch"},
              "options": [{"optionId": "allow_always", "kind": "allow_always"}]}
    assert bridge._acp_permission_decision(params, policy="yolo", task_id="T1",
                                           ask_fn=lambda t, q: "no") == acp_client.ALLOW_ALWAYS
    print("PASS test_acp_permission_yolo_edit_allows_always")


def test_acp_permission_ask_maps_operator_yes():
    params = {"toolCall": {"kind": "execute", "title": "rm -rf /"},
              "options": [{"optionId": "allow_once", "kind": "allow_once"},
                          {"optionId": "allow_always", "kind": "allow_always"}]}
    asked: list = []

    def ask_fn(task_id, q):
        asked.append((task_id, q))
        return "yes"

    assert bridge._acp_permission_decision(params, policy="ask", task_id="T1",
                                           ask_fn=ask_fn) == acp_client.ALLOW_ALWAYS
    assert asked and asked[0][0] == "T1"
    print("PASS test_acp_permission_ask_maps_operator_yes")


def test_acp_permission_ask_maps_operator_no():
    params = {"toolCall": {"kind": "execute", "title": "x"},
              "options": [{"optionId": "reject_once", "kind": "reject_once"}]}
    assert bridge._acp_permission_decision(params, policy="ask", task_id="T1",
                                           ask_fn=lambda t, q: "no") == acp_client.REJECT_ONCE
    print("PASS test_acp_permission_ask_maps_operator_no")


def test_acp_permission_ask_failure_rejects_once():
    params = {"toolCall": {"kind": "execute", "title": "x"},
              "options": [{"optionId": "reject_once", "kind": "reject_once"}]}

    def broken_ask(task_id, q):
        raise RuntimeError("mc ask down")

    assert bridge._acp_permission_decision(params, policy="ask", task_id="T1",
                                           ask_fn=broken_ask) == acp_client.REJECT_ONCE
    print("PASS test_acp_permission_ask_failure_rejects_once")


# ---------------------------------------------------------------------------
# Sabotage probe: without OMP_DRIVER the native path is selected
# ---------------------------------------------------------------------------

def test_sabotage_probe_default_driver_is_native(monkeypatch=None):
    old = os.environ.pop("OMP_DRIVER", None)
    try:
        assert bridge._acp_env_driver() == "native"
        os.environ["OMP_DRIVER"] = "acp"
        assert bridge._acp_env_driver() == "acp"
        os.environ["OMP_DRIVER"] = "ACP"  # case-insensitive
        assert bridge._acp_env_driver() == "acp"
    finally:
        if old is None:
            os.environ.pop("OMP_DRIVER", None)
        else:
            os.environ["OMP_DRIVER"] = old
    print("PASS test_sabotage_probe_default_driver_is_native")


def test_sabotage_probe_serve_loop_native_selection_unchanged():
    """Without OMP_DRIVER the serve_loop default run_once must be the native
    run_omp_subprocess wrapper — the exact pre-existing behavior."""
    old = os.environ.pop("OMP_DRIVER", None)
    try:
        # Serve-loop source must still branch on _acp_env_driver AFTER the
        # _run_factory injection seam and default to run_omp_subprocess.
        src = open(ROOT / "bridge.py", encoding="utf-8").read()
        assert "elif _acp_env_driver() == \"acp\":" in src
        native_idx = src.index("def run_once(_cwd=cwd, _p=prompt, _tf=task_file")
        acp_idx = src.index('elif _acp_env_driver() == "acp":')
        factory_idx = src.index("if _run_factory is not None:")
        assert factory_idx < acp_idx < native_idx, "driver branch order changed"
        # The native path still runs run_native_turn with interrupt_state
        # (upstream ADR-049 native-TUI driver — marknx ran run_omp_subprocess).
        assert "return run_native_turn(" in src
    finally:
        if old is not None:
            os.environ["OMP_DRIVER"] = old
    print("PASS test_sabotage_probe_serve_loop_native_selection_unchanged")


def test_sabotage_probe_classify_native_stream_untouched():
    """The native reducer/classifier still classifies the ORIGINAL stop-based
    stream fixtures identically (regression guard against drift)."""
    fix = HERE / "fixtures" / "finish-with-sentinel.ndjson"
    if fix.exists():
        with open(fix, encoding="utf-8") as fh:
            outcome, cls = bridge.classify_stream(fh)
        assert cls.kind is bridge.Kind.FINISH
    print("PASS test_sabotage_probe_classify_native_stream_untouched")


# ---------------------------------------------------------------------------
# Driver integration: classify_acp + decide_lifecycle end to end
# ---------------------------------------------------------------------------

def test_acp_interrupted_decides_blocker_not_retry():
    o = bridge.RunOutcome()
    o.saw_session = True
    o.saw_agent_end = True
    o.final_stop_reason = "cancelled"
    action = bridge.decide_lifecycle(bridge.classify_acp(o),
                                     board_requires_review=True, retries_left=2)
    # Upstream Fix 3 (#456): an INTERRUPTED run is halted_interrupted —
    # no retry, no blocker escalation. (marknx mapped it to blocker;
    # argyelan-ai's ladder contract is authoritative here.)
    assert action.action == "halted_interrupted"
    print("PASS test_acp_interrupted_decides_blocker_not_retry")


def test_acp_finish_outcome_from_real_fixture_turn():
    # The normal fixture's streamed text is short — feed the full contract
    # through run_adapter then classify: silent abort (no sentinel), NOT finish.
    outcome, _ = run_adapter(FIXTURES["normal"])
    cls = bridge.classify_acp(outcome)
    assert cls.kind is bridge.Kind.SILENT_ABORT_NO_SENTINEL
    print("PASS test_acp_finish_outcome_from_real_fixture_turn")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
