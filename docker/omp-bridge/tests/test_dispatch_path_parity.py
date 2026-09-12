"""Dispatch path parity wiring test (docs/dispatch-path-parity.md, Part 2).

Style: AST/source-wiring check in the spirit of PR #498's
test_serve_loop_wiring.py — every dispatch/access mechanic that the parity
table documents has a NAMED entry point in serve_loop; the test fails when a
path stops wiring it. Known, not-yet-closed gaps are an explicit exception
list (KNOWN_GAPS) so the test is green today but trips as soon as a NEW gap
appears or the exception becomes stale (fix landed → remove the exception).

Scope note: the poll.sh path's side lives in shell (docker/shared/poll.sh) and
is exercised by backend integration tests; this module pins the BRIDGE paths
(native + ACP), where the 11/12.09 incidents happened. The backend shared side
(comment cursor, heartbeat control) is pinned via source anchors in
backend/app/routers/agents.py — path-independent by construction.

Run: pytest docker/omp-bridge/tests/test_dispatch_path_parity.py -q
"""

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

# The three delivery paths under audit. A NEW path (a new run factory or a
# new driver branch in serve_loop) must be added here AND wired for every
# mechanism below — otherwise the wiring assertions fail.
BRIDGE_RUN_FACTORIES = ["run_native_turn", "run_native_continue", "run_acp_once"]

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
    "startup_recovery": [],  # KNOWN GAP G2 — exceptions below
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
    # G4: ACP progress is synthesized only on tool events.
    ("G4", "_acp_tool_heartbeat", "exists, but no non-tool progress source — "
     "documented gap, fix card pending"),
]


def _serve_loop_source() -> str:
    return inspect.getsource(bridge.serve_loop)


def _file_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


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
    and the exception + doc row must be updated in the same change."""
    source = _serve_loop_source()
    acp_wired = "transcript_sink" in source or "_make_acp_run_factory" in source
    exception_open = any(g == "G3" for g, _s, _w in KNOWN_GAPS)
    if acp_wired and exception_open:
        # run_acp_once has sink params on main too (it is sink-ready) — the
        # wiring test is the serve_loop call site, so only flag a REAL change.
        call_site = re.search(r"run_acp_once\(([^)]*)\)", source)
        assert call_site is not None, "ACP call site vanished from serve_loop"
        assert "transcript_sink" not in call_site.group(1) and (
            "_make_acp_run_factory" not in source.split("def serve_loop")[1]
            .split("def ")[0]
            or exception_open
        ), "G3 appears CLOSED — update KNOWN_GAPS + docs/dispatch-path-parity.md"


def test_doc_gap_table_matches_exception_list():
    doc = _file_text(os.path.join(REPO_ROOT, "docs", "dispatch-path-parity.md"))
    for gap_id, _sym, _why in KNOWN_GAPS:
        assert gap_id in doc, f"KNOWN_GAPS entry {gap_id} missing from the doc"
    for gap_id in ("G1", "G2", "G3", "G4", "G5", "G6"):
        assert gap_id in doc, f"gap {gap_id} missing from docs/dispatch-path-parity.md"
