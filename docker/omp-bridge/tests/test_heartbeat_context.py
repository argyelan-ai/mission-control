#!/usr/bin/env python3
"""CTX-01 Nachzug Teil 2 (2026-08-10) — tests for bridge.py's
_build_heartbeat_payload() and NativeTuiController.capture_pane(): the
context_pct scrape that now rides along on the existing omp/Sparky heartbeat
(previously always POSTed `{"status": ...}` with no context info at all).

Run:  python3 test_heartbeat_context.py   (standalone)   OR   pytest -v
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import bridge.py (+ context_detect.py, same dir)

import bridge  # noqa: E402


def test_build_heartbeat_payload_includes_context_pct_from_omp_statusline():
    """Real omp/Sparky statusline (`◫ 8.3%/262K`, from the CTX-01 live
    capture) — openclaude harness is hardcoded since omp fahren denselben
    openclaude-Binary wie mc-agent-base."""
    pane = "╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 8.3%/262K ⟲ ▶───"
    payload = bridge._build_heartbeat_payload("working", lambda: pane)
    assert payload == {"status": "working", "context_pct": 8.0}


def test_build_heartbeat_payload_omits_context_pct_without_capture_pane():
    """No pane-capture callable supplied (e.g. injection seam not wired) ->
    body degrades exactly to the pre-fix status-only payload."""
    payload = bridge._build_heartbeat_payload("idle", None)
    assert payload == {"status": "idle"}


def test_build_heartbeat_payload_omits_context_pct_when_nothing_recognized():
    payload = bridge._build_heartbeat_payload("idle", lambda: "no useful info here at all")
    assert payload == {"status": "idle"}


def test_build_heartbeat_payload_survives_capture_pane_raising():
    """Best-effort contract: a broken pane-capture must never break the
    heartbeat — status is still sent, no exception escapes."""
    def _boom():
        raise RuntimeError("tmux capture-pane blew up")

    payload = bridge._build_heartbeat_payload("working", _boom)
    assert payload == {"status": "working"}


def test_native_tui_controller_capture_pane_returns_stdout_on_success():
    calls = []

    def fake_run(args):
        calls.append(args)
        return 0, "some pane text\n"

    tui = bridge.NativeTuiController(
        session="test-session", signal_file="/tmp/unused-signal", _run=fake_run
    )
    out = tui.capture_pane()
    assert out == "some pane text\n"
    assert calls == [["capture-pane", "-t", "test-session:0", "-p"]]


def test_native_tui_controller_capture_pane_returns_empty_on_failure():
    tui = bridge.NativeTuiController(
        session="test-session", signal_file="/tmp/unused-signal",
        _run=lambda args: (1, ""),
    )
    assert tui.capture_pane() == ""


# ---------------------------------------------------------------------------
# Standalone runner (matches test_native_tui.py's pattern)
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    print("=" * 70)
    print("omp-bridge HEARTBEAT-CONTEXT TEST (standalone runner)")
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
