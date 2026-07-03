"""Wave-0 stubs for MEM-01 — Two-Tier Kill-Switch (D-10 + D-11).

Bodies land in plan 03-02. Today these xfail because
get_effective_recycler_enabled does not exist yet.

Pattern: mock app.services.recycler_config.settings + FakeAgent class —
mirrors test_ack_timeout_per_runtime.py shape. Per-agent override wins
when not None; None falls back to settings.agent_recycler_enabled.

Lookup order:
  1. agent.recycler_enabled is False → False (per-agent disable)
  2. agent.recycler_enabled is True  → True  (per-agent explicit enable)
  3. agent.recycler_enabled is None  → settings.agent_recycler_enabled (global)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _import_helper():
    try:
        from app.services.recycler_config import get_effective_recycler_enabled
        return get_effective_recycler_enabled
    except ImportError as e:
        pytest.xfail(f"Plan 03-02 implements recycler_config.get_effective_recycler_enabled: {e}")


def test_per_agent_false_overrides_global():
    """Per-agent agent.recycler_enabled=False overrides global True."""
    helper = _import_helper()

    class FakeAgent:
        recycler_enabled = False  # per-agent override

    with patch("app.services.recycler_config.settings") as mock_settings:
        mock_settings.agent_recycler_enabled = True
        assert helper(FakeAgent()) is False


def test_per_agent_true_overrides_global_false():
    """Per-agent agent.recycler_enabled=True overrides global False (explicit opt-in)."""
    helper = _import_helper()

    class FakeAgent:
        recycler_enabled = True

    with patch("app.services.recycler_config.settings") as mock_settings:
        mock_settings.agent_recycler_enabled = False
        assert helper(FakeAgent()) is True


def test_null_falls_back_to_global():
    """agent.recycler_enabled=None → follow settings.agent_recycler_enabled.

    Parametrized over both global values to lock the fallback in both
    directions.
    """
    helper = _import_helper()

    class FakeAgent:
        recycler_enabled = None

    with patch("app.services.recycler_config.settings") as mock_settings:
        mock_settings.agent_recycler_enabled = True
        assert helper(FakeAgent()) is True
        mock_settings.agent_recycler_enabled = False
        assert helper(FakeAgent()) is False


@pytest.mark.parametrize("per_agent,global_enabled,expected", [
    (True,  True,  True),
    (True,  False, True),   # per-agent overrides
    (False, True,  False),  # per-agent overrides
    (False, False, False),
    (None,  True,  True),   # null falls through
    (None,  False, False),
])
def test_two_tier_truth_table(per_agent, global_enabled, expected):
    """Truth table for the two-tier kill-switch (D-10 + D-11).

    6 combinations: per-agent (True/False/None) × global (True/False).
    Per-agent always wins when not None; None falls through.
    """
    helper = _import_helper()

    class FakeAgent:
        recycler_enabled = per_agent

    with patch("app.services.recycler_config.settings") as mock_settings:
        mock_settings.agent_recycler_enabled = global_enabled
        assert helper(FakeAgent()) is expected
