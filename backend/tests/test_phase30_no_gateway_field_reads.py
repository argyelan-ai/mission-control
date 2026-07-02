"""Phase 30 invariants: backend code no longer reads agent.gateway_id /
agent.gateway_agent_id / Gateway model.

Post Plan 30-02: SQLModel fields are GONE (Agent.gateway_*, Board.gateway_id),
Gateway model file is DELETED (git rm), DiscordConfig replaces gateways-as-
config-table. EXEMPT list is empty — no file legitimately still references
these symbols. Plan 30-03 will drop the DB columns.

Companion to `test_no_gateway_imports.py` (Phase 29 import-safety net):
- That test catches re-introduction of openclaw_rpc.* call patterns.
- This test catches re-introduction of `agent.gateway_*` *field reads*.
"""
from __future__ import annotations

import pathlib
import re

BACKEND_APP = pathlib.Path(__file__).parents[1] / "app"

# Each forbidden pattern is a regex against backend/app/**/*.py source.
FORBIDDEN = [
    # Active code reads of Agent.gateway_id / Agent.gateway_agent_id
    (r"\bAgent\.gateway_id\b", "Agent.gateway_id is being dropped in Plan 30-03"),
    (
        r"\bAgent\.gateway_agent_id\b",
        "Agent.gateway_agent_id is being dropped in Plan 30-03",
    ),
    (
        r"\bagent\.gateway_id\b",
        "agent.gateway_id field reads break post-30-02",
    ),
    (
        r"\bagent\.gateway_agent_id\b",
        "agent.gateway_agent_id field reads break post-30-02",
    ),
    (
        r"\.gateway_agent_id\b",
        "any *.gateway_agent_id read breaks post-30-02",
    ),
    # Imports of Gateway model
    (
        r"from app\.models\.gateway import",
        "Gateway model is deleted in Plan 30-02",
    ),
    (
        r"from app\.models import .*\bGateway\b",
        "Gateway model is deleted in Plan 30-02",
    ),
]

# Plan 30-02 cleaned the model declarations + deleted gateway.py. No
# exemptions remain — any source file containing these patterns is a
# regression.
EXEMPT: set[str] = set()


def _iter_py_files():
    for p in BACKEND_APP.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(BACKEND_APP).as_posix()
        if rel in EXEMPT:
            continue
        yield p, rel


def test_no_gateway_field_reads():
    violations: list[str] = []
    for path, rel in _iter_py_files():
        src = path.read_text(encoding="utf-8")
        for pattern, reason in FORBIDDEN:
            for m in re.finditer(pattern, src):
                line_no = src[: m.start()].count("\n") + 1
                violations.append(
                    f"{rel}:{line_no} {m.group(0)}  ({reason})"
                )
    assert not violations, (
        "Phase 30 invariant violation — these reads must be removed "
        "before Plan 30-02/03 lands:\n  " + "\n  ".join(violations)
    )
