"""Dispatch path parity wiring test (docs/dispatch-path-parity.md, Part 2).

Style: AST/source-wiring check in the spirit of PR #498's
test_serve_loop_wiring.py — every dispatch/access mechanic that the parity
table documents has a NAMED entry point in serve_loop; the test fails when a
path stops wiring it. Known, not-yet-closed gaps are an explicit exception
list (KNOWN_GAPS) so the test is green today but trips as soon as a NEW gap
appears — and the path inventory is DERIVED FROM SOURCE (reverse direction),
so a new run factory or a new OMP_DRIVER branch trips the suite before it
ships unwired.

Both directions matter (review PR #521, blocker B1):
  forward : every registry entry still exists in the code
  reverse : every path-shaped thing in the code is in the registry

Scope note: the poll.sh path's side lives in shell (docker/shared/poll.sh) and
is exercised by backend integration tests; this module pins the BRIDGE paths
(native + ACP), where the 11/12.09 incidents happened. The backend shared side
(comment cursor, heartbeat control) is pinned via source anchors in
backend/app/routers/agents.py — path-independent by construction.

Run: pytest docker/omp-bridge/tests/test_dispatch_path_parity.py -q
"""

import ast
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

AGENTS_ROUTER = os.path.join(
    REPO_ROOT, "backend", "app", "routers", "agents.py"
)

# The audited bridge paths. This list is pinned by BOTH directions below:
# test_all_bridge_run_factories_exist (forward: entry still exists) and
# test_new_paths_must_join_the_registry (reverse: a path-shaped function that
# is NOT listed here fails the suite).
BRIDGE_RUN_FACTORIES = ["run_native_turn", "run_native_continue", "run_acp_once"]

# A module-level function whose name looks like a run driver is treated as a
# dispatch path. Deliberately narrow suffix set so helpers don't false-trip:
# `run_omp_subprocess` (the legacy ADR-045 headless one-shot behind --run/
# --replay) does NOT match and is not one of the three audited serve_loop
# paths — add it here with a reason if the audit ever covers it.
PATH_DEF_RE = re.compile(r"^run_[a-z_]*(_once|_turn|_continue)$")

# OMP_DRIVER values serve_loop branches on (main: only the ACP driver).
# A new value means a new driver branch → must join the table + wiring.
KNOWN_DRIVER_VALUES = {"acp"}

# mechanism -> named entry points that must appear in serve_loop's source.
# Each entry is the *symbol* the parity table cites as evidence.
SERVE_LOOP_WIRING = {
    "dispatch_dedup": ["last_attempt_id"],
    "comment_delivery": ["note_comments", "nudge_comments"],
    "thread_cursor_nudge": ["delivery.nudge", "delivery.flush", "reset_awaiting"],
    "withdrawn_notice": ["withdrawn_notes", "note_withdrawn"],
    "soft_hard_interrupt": ["interrupt_state", "_on_control", "start_heartbeater"],
    "task_context_env": ["write_task_context_env"],
    "finish_guard": ["drive_live_run", "set_blocker"],
    # Intentionally EMPTY: G2 (startup recovery) has NO wired symbol on any
    # bridge path today — there is nothing to assert. The gap itself is
    # covered by the KNOWN_GAPS exception machinery below, and the reverse
    # direction is covered by test_serve_loop_driver_branches_are_known.
    "startup_recovery": [],
    "acp_cancel_flip": ["_acp_control_sink"],
}

# Backend anchors: symbol -> expected to appear in routers/agents.py.
# These are shared by all three paths; the test pins them so a rename trips
# the parity audit instead of silently orphaning the table.
BACKEND_WIRING = {
    "comment_cursor_upsert": ["_upsert_cursor", "_collect_and_ack_new_comments"],
    "heartbeat_control": ["_heartbeat_control", "_collect_heartbeat_control"],
    "withdrawn_guard": ["_withdrawn_task_reason", "_WITHDRAWN_TASK_STATUSES"],
    "orphan_redispatch": ["_maybe_redispatch_orphaned_run"],
    "startup_recovery_endpoint": ["active-task-recovery"],
}

# Explicit exception list — known gaps from docs/dispatch-path-parity.md
# (gap summary G1..G6). Format: (gap_id, missing_symbol, why_ok_today).
# `missing_symbol` must be ABSENT from serve_loop's source — pick a symbol
# that only a real wiring would introduce (not prose words from comments).
# When a fix lands, the symbol appears, this test fails, and the exception
# row + doc row go in the same change.
KNOWN_GAPS = [
    # G2: serve_loop has no startup recovery call (poll.sh does, poll.sh:454).
    ("G2", "active_task_recovery", "serve_loop never calls the "
     "active-task-recovery endpoint; backend orphan redispatch is the only "
     "safety net (agents.py:2835)"),
    # G3: ACP runs on main carry no transcript/preview sinks (PR #498 open).
    ("G3", "transcript_sink", "serve_loop's ACP run_once calls run_acp_once "
     "without sinks; _make_acp_run_factory owns them but is unused on main"),
    ("G3", "preview_sink", "same as transcript_sink — PR #498 open"),
    # G4: ACP progress is synthesized only on tool events. The symbol
    # _acp_tool_heartbeat EXISTS (that is the gap: it fires on tool calls
    # only), so a stale-check on absence cannot work — G4 deliberately has
    # no absence-tripwire. A G4 fix (non-tool progress source) should replace
    # this comment with a positive assertion on the new symbol.
    ("G4", "_acp_tool_heartbeat", "exists, but no non-tool progress source — "
     "documented gap, fix card pending"),
]


def _bridge_source() -> str:
    return inspect.getsource(bridge)


def _serve_loop_source() -> str:
    return inspect.getsource(bridge.serve_loop)


def _serve_loop_ast() -> ast.Module:
    return ast.parse(_serve_loop_source())


def _file_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _module_run_functions() -> set:
    """Every module-level function whose name is path-shaped (reverse
    direction of the registry check)."""
    tree = ast.parse(_bridge_source())
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and PATH_DEF_RE.match(node.name)
    }


def _mentions_driver(node: ast.AST) -> bool:
    """True if this expression subtree references the OMP_DRIVER selection:
    either the literal env key or a *_driver helper call."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value == "OMP_DRIVER":
            return True
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id.endswith("driver")
        ):
            return True
    return False


def _driver_cmp_values(tree: ast.AST) -> set:
    """String values compared against an OMP_DRIVER selection anywhere in the
    given AST — e.g. `_acp_env_driver() == "acp"` or
    `os.environ.get("OMP_DRIVER") == "gemini"`."""
    values = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for i, op in enumerate(operands):
            if isinstance(op, ast.Constant) and isinstance(op.value, str):
                others = [o for j, o in enumerate(operands) if j != i]
                if any(_mentions_driver(o) for o in others):
                    values.add(op.value)
    return values


def _run_acp_once_call_keywords() -> list:
    """Keyword names of EVERY run_acp_once call site inside serve_loop
    (AST-based — survives nested parens like max_time=int(turn_deadline),
    which a `[^)]*` regex cannot)."""
    calls = []
    for node in ast.walk(_serve_loop_ast()):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name == "run_acp_once":
                calls.append(sorted(kw.arg for kw in node.keywords if kw.arg))
    assert calls, "no run_acp_once call site found in serve_loop"
    return calls


# ── forward direction: registry entries exist and are wired ────────────────


def test_all_bridge_run_factories_exist():
    """Every path in the audit must be a named, callable entry point."""
    for name in BRIDGE_RUN_FACTORIES:
        assert hasattr(bridge, name), f"path entry point missing: {name}"
        assert callable(getattr(bridge, name))


def test_serve_loop_wires_every_mechanism():
    source = _serve_loop_source()
    missing = []
    for mechanism, symbols in SERVE_LOOP_WIRING.items():
        for sym in symbols:
            if sym not in source:
                missing.append((mechanism, sym))
    assert not missing, (
        "serve_loop stopped wiring documented mechanics "
        "(update docs/dispatch-path-parity.md if intentional): "
        + ", ".join(f"{m}:{s}" for m, s in missing)
    )


def test_backend_anchors_present():
    text = _file_text(AGENTS_ROUTER)
    missing = []
    for mechanism, symbols in BACKEND_WIRING.items():
        for sym in symbols:
            if sym not in text:
                missing.append((mechanism, sym))
    assert not missing, (
        "backend parity anchors missing (table evidence rotted): "
        + ", ".join(f"{m}:{s}" for m, s in missing)
    )


# ── reverse direction: new paths must join the registry ────────────────────


def test_new_paths_must_join_the_registry():
    """A path-shaped run function in bridge.py that is NOT in
    BRIDGE_RUN_FACTORIES fails here — a new path must enter the parity table
    AND wire every mechanism in serve_loop before it ships."""
    unknown = _module_run_functions() - set(BRIDGE_RUN_FACTORIES)
    assert not unknown, (
        "NEW dispatch path detected in bridge.py — add it to "
        "docs/dispatch-path-parity.md and wire every mechanism in serve_loop "
        "(or extend PATH_DEF_RE with a reason if this is not a path): "
        + ", ".join(sorted(unknown))
    )


def test_serve_loop_driver_branches_are_known():
    """A new OMP_DRIVER branch in serve_loop (even without its own run
    factory) fails here — same reverse direction for driver selection."""
    unknown = _driver_cmp_values(_serve_loop_ast()) - KNOWN_DRIVER_VALUES
    assert not unknown, (
        "NEW OMP_DRIVER branch in serve_loop — add it to "
        "docs/dispatch-path-parity.md and audit its wiring: "
        + ", ".join(sorted(unknown))
    )


# ── exception list hygiene ──────────────────────────────────────────────────


def test_known_gaps_reference_real_symbols():
    """Every exception must cite a symbol that is REALLY absent today —
    the exception goes stale (fails here) the moment someone wires it,
    prompting the doc + exception cleanup."""
    source = _serve_loop_source()
    stale = []
    for gap_id, sym, _why in KNOWN_GAPS:
        if sym in source:
            stale.append((gap_id, sym))
    # G4's symbol exists but its GAP is the missing non-tool source; exclude
    # symbols documented as present-but-partial in the gap table.
    partial_ok = {"_acp_tool_heartbeat"}
    real_stale = [(g, s) for g, s in stale if s not in partial_ok]
    assert not real_stale, (
        "exception list stale — gap closed? remove from KNOWN_GAPS + docs: "
        + ", ".join(f"{g}:{s}" for g, s in real_stale)
    )


def test_acp_branch_does_not_silently_gain_sinks():
    """G3 tripwire: when the sinks wiring lands (PR #498 merge), this fails
    and the exception + doc row must be updated in the same change.

    AST-based on every run_acp_once call site in serve_loop (a keyword regex
    is not viable: `max_time=int(turn_deadline)` closes the group early)."""
    sinks = {"transcript_sink", "preview_sink"}
    for call_keywords in _run_acp_once_call_keywords():
        wired = sinks & set(call_keywords)
        assert not wired, (
            "G3 appears CLOSED — run_acp_once call site now passes "
            f"{sorted(wired)}: remove the G3 rows from KNOWN_GAPS and update "
            "rows 13/14 in docs/dispatch-path-parity.md in the same change"
        )


def test_doc_gap_table_matches_exception_list():
    doc = _file_text(os.path.join(REPO_ROOT, "docs", "dispatch-path-parity.md"))
    for gap_id, _sym, _why in KNOWN_GAPS:
        assert gap_id in doc, f"KNOWN_GAPS entry {gap_id} missing from the doc"
    for gap_id in ("G1", "G2", "G3", "G4", "G5", "G6"):
        assert gap_id in doc, f"gap {gap_id} missing from docs/dispatch-path-parity.md"
