"""Recycler-Config Helpers — Two-Tier Kill-Switch for MEM-01 (Phase 3).

Mirrors the `_get_ack_timeout_minutes` shape (task_runner.py:131-149) but
scoped to the recycler boolean. Lookup order:
  1) agent.recycler_enabled is True   → True  (per-agent explicit enable)
  2) agent.recycler_enabled is False  → False (per-agent disable)
  3) agent.recycler_enabled is None   → settings.agent_recycler_enabled (global)

Callers (Plan 03-04):
  - docker_agent_sync.py: renders the result as the AGENT_RECYCLER_ENABLED line in agent.env
  - internal.py agent_bootstrap: returns the result as a bootstrap key

Phase-3 boundary: no class, no async ops, no DB queries. Pure
function over agent + settings. Tests in test_recycler_helper.py.

See also ADR-024 (Claude-process recycling).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.models.agent import Agent


def get_effective_recycler_enabled(agent: "Agent") -> bool:
    """Two-tier resolution for the recycler kill-switch (D-10 + D-11).

    Per-agent setting wins when not None. None means "follow global env var".
    Mirror of _get_ack_timeout_minutes in task_runner.py:131 — same shape,
    different field. Both keep the per-agent override as the most-specific
    signal so the operator can disable the recycler on a single problematic agent
    without redeploying the whole fleet.
    """
    per_agent = getattr(agent, "recycler_enabled", None)
    if per_agent is True:
        return True
    if per_agent is False:
        return False
    # None → follow global env-var
    return bool(settings.agent_recycler_enabled)
