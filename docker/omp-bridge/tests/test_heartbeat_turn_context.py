#!/usr/bin/env python3
"""Fix 3b (2026-09-08) — bridge heartbeat turn-context tests.

While a turn (run_once/continue_once) is in flight, serve_loop sets the
turn context (_set_turn_context) and `_build_heartbeat_payload` carries
`task_id` + `attempt_id` in the heartbeat body. That is the backend's
window into WHAT is actually running — the precondition for the control
channel to see a stopped/blocked run (control=hard). After the turn the
context is cleared: the bridge reports NO ids again (legacy shape).

The Karte's acceptance criterion: "Payload enthaelt task_id waehrend
run_once, danach nicht mehr."

Run:  python3 test_heartbeat_turn_context.py   (standalone)   OR   pytest -v
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py (+ context_detect.py)

import bridge  # noqa: E402


def _fresh() -> None:
    """Reset module-level turn context (the standalone runner does NOT call
    pytest's setup_function hooks, so each test cleans up after itself)."""
    bridge._set_turn_context(None, None)


def test_payload_carries_task_id_during_turn():
    """Mid-turn the payload reports the running task's ids."""
    _fresh()
    bridge._set_turn_context("11111111-2222-3333-4444-555555555555", "attempt-7")
    payload = bridge._build_heartbeat_payload("working", None)
    assert payload["task_id"] == "11111111-2222-3333-4444-555555555555"
    assert payload["attempt_id"] == "attempt-7"
    assert payload["status"] == "working"
    _fresh()


def test_payload_carries_no_task_id_after_turn():
    """After the turn (context cleared) the payload must NOT report ids —
    a stale id would keep the backend steering a finished run."""
    _fresh()
    bridge._set_turn_context("11111111-2222-3333-4444-555555555555", "attempt-7")
    bridge._set_turn_context(None, None)
    payload = bridge._build_heartbeat_payload("idle", None)
    assert "task_id" not in payload
    assert "attempt_id" not in payload
    _fresh()


def test_payload_without_turn_context_is_legacy_shaped():
    """Fresh bridge, no turn ever started -> byte-identical legacy payload."""
    _fresh()
    payload = bridge._build_heartbeat_payload("idle", None)
    assert payload == {"status": "idle"}
    _fresh()


def test_attempt_id_omitted_when_empty():
    """A dispatch without a separate attempt_id (task id fallback) does not
    send an empty-string attempt_id field."""
    _fresh()
    bridge._set_turn_context("aaaabbbb-0000-0000-0000-ccccddddeeee", "")
    payload = bridge._build_heartbeat_payload("working", None)
    assert payload["task_id"] == "aaaabbbb-0000-0000-0000-ccccddddeeee"
    assert "attempt_id" not in payload
    _fresh()


def test_set_turn_context_toggles_via_getter():
    """Direct contract of the setter/getter pair serve_loop relies on."""
    _fresh()
    assert bridge._get_turn_context() is None
    bridge._set_turn_context("t-1", "a-1")
    ctx = bridge._get_turn_context()
    assert ctx == {"task_id": "t-1", "attempt_id": "a-1"}
    # Mutating the returned copy must not corrupt the stored context.
    ctx["task_id"] = "tampered"
    assert bridge._get_turn_context()["task_id"] == "t-1"
    bridge._set_turn_context(None, None)
    assert bridge._get_turn_context() is None
    _fresh()


# ---------------------------------------------------------------------------
# Standalone runner (matches test_heartbeat_context.py's pattern)
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    print("=" * 70)
    print("omp-bridge HEARTBEAT TURN-CONTEXT TEST (standalone runner)")
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
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
