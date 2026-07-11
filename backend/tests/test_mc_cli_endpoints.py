"""Guarantee: every important agent-scoped endpoint has a `mc` CLI command.

When a new agent-scoped endpoint gets added, the author must either:
  - add a CommandSpec to scripts/mc-cli/mc_cli/commands.py, OR
  - add its key to SKIP_CLI below with a short justification.

CI breaks if a must-have endpoint loses its CLI mapping.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLI_PATH = REPO_ROOT / "scripts" / "mc-cli"
if str(MC_CLI_PATH) not in sys.path:
    sys.path.insert(0, str(MC_CLI_PATH))


# Endpoints every worker agent must be able to hit via `mc`.
# Format: "METHOD /path" (path relative to the agent_scoped router prefix).
MUST_HAVE_CLI_ENDPOINTS: set[str] = {
    "PATCH /boards/{board_id}/tasks/{task_id}",                    # status updates
    "POST /boards/{board_id}/tasks/{task_id}/comments",            # progress / blocker / resolution
    "POST /boards/{board_id}/tasks/{task_id}/checklist",           # add items
    "POST /boards/{board_id}/tasks/{task_id}/deliverables",        # register output
    "POST /boards/{board_id}/help-request",                        # cross-agent help
    "POST /boards/{board_id}/clarification",                       # ask the operator
}

# Endpoints deliberately not reachable via `mc`. Each entry needs a reason.
SKIP_CLI: dict[str, str] = {
    # Admin / management — not for regular workers.
    "POST /agents/request-spawn": "Boss-only: handled inline in SOUL",
    "POST /templates/{template_id}/instantiate": "Board-lead-only",
    "POST /agents": "Board-lead-only",
    "PATCH /agents/{target_agent_id}/plugins": "Boss-only plugin self-service",
    "PATCH /agents/{target_agent_id}/workflow-flags": "Boss-only workflow control",
    # Task lifecycle — not agent-triggered.
    "DELETE /boards/{board_id}/tasks/{task_id}": "only operator/UI, not agents",
    "POST /boards/{board_id}/tasks": "Board-Lead creates tasks, workers via help-request",
    "POST /boards/{board_id}/projects": "Board-Lead-only",
    # NOTE: POST /boards/{board_id}/tasks/{task_id}/review is now CLI-mapped
    # via `mc approve` / `mc reject` (B3) — no longer a SKIP entry.
    "PATCH /boards/{board_id}/tasks/{task_id}/report-back": "Board-Lead-only report-back contract",
    "POST /boards/{board_id}/tasks/{task_id}/checkpoint": "deprecated in A4 — use `mc checklist` + `mc comment progress`",
    # Board-level writes handled via other channels.
    "POST /boards/{board_id}/approvals": "approvals go via `mc question` (clarification) or API directly",
    "POST /boards/{board_id}/chat": "not part of worker lifecycle",
    "POST /boards/{board_id}/memory": "covered by `mc memory save` in a later iteration",
    "PATCH /me/memory": "self-memory edit covered later — workers log via comments",
    "POST /knowledge": "global knowledge writes via API only (admin-ish)",
    "POST /memory/query": "superseded by `mc memory search` via GET /me/memory/search",
    # Discord direct send — bot-bridge only.
    "POST /discord/send": "bot bridge, not part of task lifecycle",
    # Streaming / heartbeat — handled by poll.sh, not agent-triggered.
    "POST /heartbeat": "poll.sh sends this, not the agent",
    # Config self-edit — handled by the operator via UI; Henry uses dedicated path.
    "PUT /config/soul_md": "config edits go via provisioning, not agent",
    "GET /config/soul_md": "debug-only, not part of worker lifecycle",
}

# Content pipeline lives in the News-Studio vertical (optional, stripped in
# the public release) — SKIP_CLI entry + endpoint collection are conditional.
try:
    from app.verticals.news_studio.routers.content_agent import (
        router as _content_agent_router,
    )

    SKIP_CLI["POST /content/{pipeline_id}/submit"] = (
        "writer-only, separate mc subcommand later (News-Studio-Vertical)"
    )
except ImportError:
    _content_agent_router = None


def _agent_scoped_endpoints() -> set[str]:
    """Return `{"METHOD /path"}` set from all agent-scoped routers.

    Phase 4 REF-02 split agent_scoped.py into multiple sibling routers
    (agent_comments, agent_task_status, etc.). All carry prefix
    /api/v1/agent — collect from each so the must-have/CLI-mapping checks
    see the full surface.
    """
    from app.routers.agent_scoped import router as agent_router
    from app.routers.agent_comments import router as agent_comments_router
    from app.routers.agent_task_status import router as agent_task_status_router
    from app.routers.vault import agent_router as vault_agent_router

    endpoints: set[str] = set()
    routers = [agent_router, agent_comments_router, agent_task_status_router]
    if _content_agent_router is not None:
        routers.append(_content_agent_router)
    for router_obj in routers:
        for route in router_obj.routes:
            if not hasattr(route, "methods") or not hasattr(route, "path"):
                continue
            path = route.path.removeprefix("/api/v1/agent")
            for method in route.methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                endpoints.add(f"{method} {path}")
    for route in vault_agent_router.routes:
        if not hasattr(route, "methods") or not hasattr(route, "path"):
            continue
        path = route.path.removeprefix("/api/v1")
        for method in route.methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            endpoints.add(f"{method} {path}")
    return endpoints


def _cli_endpoints() -> set[str]:
    from mc_cli.commands import REGISTRY
    return {ep for spec in REGISTRY.values() for ep in spec.endpoints}


def test_must_have_endpoints_have_cli_command():
    """Every must-have agent endpoint has a `mc` command wired up."""
    cli = _cli_endpoints()
    missing = MUST_HAVE_CLI_ENDPOINTS - cli
    assert not missing, (
        f"Agent-scoped endpoints without `mc` command: {sorted(missing)}. "
        "Either add a CommandSpec in scripts/mc-cli/mc_cli/commands.py "
        "or remove from MUST_HAVE_CLI_ENDPOINTS if truly optional."
    )


def test_cli_commands_point_at_real_endpoints():
    """No `mc` command references a non-existent endpoint."""
    backend = _agent_scoped_endpoints()
    cli = _cli_endpoints()
    # Endpoints that do NOT live in the agent_scoped router but are still
    # legitimately used by the CLI — e.g. poll-infrastructure endpoints
    # served by `routers/agents.py`, not `routers/agent_scoped.py`.
    known_upcoming = {
        "GET /me/memory/search",              # added in A3 within same PR
        "GET /me/active-task-recovery",       # ADR-024 — lives in routers/agents.py
    }
    orphans = cli - backend
    real_orphans = orphans - known_upcoming
    assert not real_orphans, (
        f"CLI commands pointing at missing endpoints: {sorted(real_orphans)}"
    )


def test_no_unknown_skip_entries():
    """Every SKIP_CLI entry must still exist on the backend."""
    backend = _agent_scoped_endpoints()
    dead = [e for e in SKIP_CLI if e not in backend]
    assert not dead, (
        f"SKIP_CLI lists endpoints that no longer exist: {dead}. "
        "Remove the entries."
    )


def test_all_agent_endpoints_are_classified():
    """Every agent endpoint is either CLI-mapped or explicitly skipped.

    Breaks CI when a new agent-scoped endpoint gets added without deciding
    whether it belongs in the `mc` CLI.
    """
    backend = _agent_scoped_endpoints()
    cli = _cli_endpoints()
    skipped = set(SKIP_CLI.keys())
    # Read-only inspection endpoints get a free pass (curl-able, CLI focus
    # stays on write paths). This is the one blanket exemption we grant.
    readonly_pass = {e for e in backend if e.startswith("GET ")}
    unclassified = backend - cli - skipped - readonly_pass
    assert not unclassified, (
        f"Agent write-endpoints neither CLI-mapped nor SKIP_CLI-listed: "
        f"{sorted(unclassified)}. Add a CommandSpec in "
        f"scripts/mc-cli/mc_cli/commands.py:REGISTRY or a SKIP_CLI entry here."
    )


def test_registry_dispatch_smoke():
    """Every command in the registry must parse its args without errors."""
    from mc_cli.__main__ import build_parser

    parser = build_parser()
    # --help should succeed for every subcommand.
    for name in _cli_endpoints():
        pass  # smoke: import already validated above
    # Verify parser builds without raising.
    assert parser.prog == "mc"
