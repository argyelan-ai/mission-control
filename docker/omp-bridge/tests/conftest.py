"""Shared pytest fixtures for the omp-bridge suite.

PR #555 follow-up B1: `tests/` only started running as a single pytest
process in CI with that PR (ci.yml's new omp-bridge lane) — before that,
each test file's module-level leakage into `bridge`'s process-global state
never met a sibling file in the same interpreter. `bridge._ACP_CONTEXT_PCT`
(G5, bridge.py:1631) is exactly such a global: a REAL turn through the
"normal" ACP fixture (used across test_acp_adapter.py, test_acp_chat.py,
test_acp_through_tests.py, and others) stamps it via the production
usage_update handler and nothing resets it, so whichever test runs last
leaves 3.5 sitting there for the next file — observed failing
test_heartbeat_context.py / test_heartbeat_turn_context.py, which assert
context_pct is ABSENT, purely because of file execution order, not because
of anything either file does wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import bridge  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_acp_context_pct():
    """Save/restore, not an unconditional None: mirrors
    test_acp_through_tests.py's `_with_serve_env` pattern — a test that
    legitimately starts with a pre-set holder (there are none today, but
    the fixture shouldn't assume that stays true) gets it back afterward."""
    saved = bridge._get_acp_context_pct()
    yield
    bridge._set_acp_context_pct(saved)
