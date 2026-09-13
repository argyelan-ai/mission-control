"""Dispatch path parity wiring test (docs/dispatch-path-parity.md, Part 2).

Style: AST/source-wiring check in the spirit of PR #498's
test_serve_loop_wiring.py — every dispatch/access mechanic that the parity
table documents has a NAMED entry point in serve_loop; the test fails when a
path stops wiring it. Still-open gaps are an explicit exception list
(KNOWN_GAPS) so the test is green today but trips as soon as a NEW gap
appears — and the path inventory is DERIVED FROM SOURCE (reverse direction),
so a new run factory or a new OMP_DRIVER branch trips the suite before it
ships unwired.

Both directions matter (review PR #521, blocker B1):
  forward : every registry entry still exists in the code
  reverse : every path-shaped thing in the code is in the registry

Refresh after #498/#522/#523 (card 5e7c8087) — three changes of substance:

  1. The CLOSED gaps became POSITIVE pins instead of absence-tripwires.
     G3's old tripwire asserted a bare `run_acp_once` call site inside
     serve_loop and went red the moment #498 removed exactly that (the
     fix landing is what broke the test). It now asserts the opposite: the
     sinks flow through _make_acp_run_factory and serve_loop never calls
     run_acp_once bare. G2 (startup recovery) and G4 (stream heartbeat)
     likewise moved out of the exception list into assertions, so an
     accidental revert is red rather than silently "known".
  2. KNOWN_GAPS entries carry a SCOPE. G1 lives in the backend and G6 in
     poll.sh; checking their symbols against serve_loop's source would have
     been permanently and meaninglessly green.
  3. M7 (row 21) is pinned per factory call site.
     test_serve_loop_wiring.py's `_kwargs_of_calls` merges every
     `_make_acp_run_factory` call into ONE dict, so a second call site that
     dropped cancel_state/heartbeat_fn/interrupt_state stays green there.
     test_every_acp_factory_call_site_is_fully_wired checks each site alone.

Scope note: the poll.sh path's side lives in shell (docker/shared/poll.sh) and
is exercised by backend integration tests; this module pins the BRIDGE paths
(native + ACP), where the 11/12.09 incidents happened, plus the two narrow
shell/backend anchors the exception list needs. The backend shared side
(comment cursor, heartbeat control) is pinned via source anchors in
backend/app/routers/agents.py — path-independent by construction.

Run: pytest docker/omp-bridge/tests/test_dispatch_path_parity.py -q
"""

import ast
import inspect
import os
import re
import sys
import textwrap

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
    # G2 CLOSED (card f5cc4cee point e): serve_loop now calls
    # GET /me/active-task-recovery on the first poll that actually returns a
    # payload with `working`/no task object — the same startup boundary
    # poll.sh's recover_task() heals on FIRST_POLL.
    "startup_recovery": ["_make_http_recovery", "recovery_fn"],
    "acp_cancel_flip": ["_acp_control_sink"],
    # G3 CLOSED (#498): the ACP branch reaches run_acp_once ONLY through the
    # sink-owning factory. The absence-tripwire this replaces went red the
    # moment #498 merged (it required a bare run_acp_once call site inside
    # serve_loop, which #498 deliberately removed).
    "acp_sinks_via_factory": ["_make_acp_run_factory"],
    # G4 CLOSED (#523): the tool heartbeat is the serve_loop half of the
    # watchdog liveness channel; the stream half lives in run_acp_once and is
    # pinned separately by test_acp_stream_feeds_the_watchdog.
    "acp_progress_heartbeat": ["_acp_tool_heartbeat", "heartbeat_fn"],
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


def _bridge_source() -> str:
    return inspect.getsource(bridge)


def _serve_loop_source() -> str:
    return inspect.getsource(bridge.serve_loop)


def _serve_loop_ast() -> ast.Module:
    return ast.parse(_serve_loop_source())


def _file_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


KNOWN_GAPS = [
    # G1 (PR #519, OPEN): the heartbeat soft-check has no author filter and
    # no cursor seed, so a missing cursor row makes the agent's OWN
    # blocker/handoff comments read as unread. Backend-side, so the symbol is
    # pinned against agents.py, not serve_loop. `last_signalled_comment_id`
    # is the column #519 introduces — when that branch merges this entry goes
    # stale and this test fails, prompting the doc + exception cleanup.
    ("G1", "last_signalled_comment_id", "backend",
     "no separate signal watermark and no author filter: _upsert_cursor has "
     "exactly one caller (the poll handler), so a low-polling path runs "
     "without a row and every historic comment reads as unread"),
    # G6: poll.sh's heartbeat payload carries status(+context_pct) only. A
    # fix has to put the task id into that payload before the backend can
    # resolve a control task for it, so `task_id` is the arriving symbol.
    ("G6", "task_id", "poll_heartbeat",
     "poll.sh's heartbeat sends neither task_id nor a control read, so "
     "operator interrupts arrive only via the next poll state"),
    ("G6", "control", "poll_heartbeat",
     "same payload: nothing reads a `control` directive back out of the "
     "heartbeat response on the poll.sh path"),
]

POLL_SH = os.path.join(REPO_ROOT, "docker", "shared", "poll.sh")


def _poll_heartbeat_body() -> str:
    """poll.sh's `heartbeat()` function body with full-line shell comments
    stripped — G6's absence check must not be satisfied by prose (the
    function's own comments contain the word "controlled")."""
    text = _file_text(POLL_SH)
    lines = text.splitlines()
    start = next(
        i for i, ln in enumerate(lines) if ln.startswith("heartbeat() {")
    )
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    body = lines[start + 1:end]
    return "\n".join(ln for ln in body if not ln.lstrip().startswith("#"))


def _absence_scope(scope: str) -> str:
    """Source text a KNOWN_GAPS symbol must be ABSENT from."""
    if scope == "serve_loop":
        return _serve_loop_source()
    if scope == "backend":
        return _file_text(AGENTS_ROUTER)
    if scope == "poll_heartbeat":
        return _poll_heartbeat_body()
    raise AssertionError(f"unknown KNOWN_GAPS scope: {scope}")


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



def _called_names(tree: ast.AST) -> set:
    """Every name that is actually INVOKED in this subtree — `f()` and
    `obj.f()` alike.

    Substring checks cannot tell a live call from a leftover definition or a
    mention in a comment: the sabotage probe for this card removed the
    `_stream_heartbeat()` CALL and the suite stayed green, because
    `def _stream_heartbeat` kept the substring alive. Closed-gap pins are
    call-based for that reason."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name:
                out.add(name)
    return out


def _acp_factory_call_keywords() -> list:
    """Keyword names of EVERY _make_acp_run_factory call site inside
    serve_loop, ONE LIST PER CALL SITE (AST-based — survives nested parens
    like max_time=int(turn_deadline), which a `[^)]*` regex cannot).

    Per call site, deliberately: test_serve_loop_wiring.py's `_kwargs_of_calls`
    merges all call sites into one dict, so a second factory call that dropped
    a control kwarg would be covered by the first one's keywords (gap M7).
    """
    calls = []
    for node in ast.walk(_serve_loop_ast()):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name == "_make_acp_run_factory":
                calls.append(sorted(kw.arg for kw in node.keywords if kw.arg))
    assert calls, (
        "no _make_acp_run_factory call site found in serve_loop — since "
        "#498 the ACP branch must reach run_acp_once THROUGH the factory "
        "(rows 13/14 in docs/dispatch-path-parity.md)"
    )
    return calls


def _factory_run_keywords() -> list:
    """Keyword names of every run_acp_once call inside _make_acp_run_factory
    — the second leg of the sink chain (factory -> driver)."""
    src = inspect.getsource(bridge._make_acp_run_factory)
    calls = []
    for node in ast.walk(ast.parse(textwrap.dedent(src))):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name == "run_acp_once":
                calls.append(sorted(kw.arg for kw in node.keywords if kw.arg))
    assert calls, "no run_acp_once call site inside _make_acp_run_factory"
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
    """Every exception must cite a symbol that is REALLY absent from ITS OWN
    scope today — the exception goes stale (fails here) the moment someone
    wires it, prompting the doc + exception cleanup.

    Scoped since the #498/#522/#523 refresh: G1 is a backend gap and G6 a
    poll.sh one, so checking them against serve_loop's source would have made
    them permanently, meaninglessly green."""
    stale = []
    for gap_id, sym, scope, _why in KNOWN_GAPS:
        if sym in _absence_scope(scope):
            stale.append((gap_id, sym, scope))
    assert not stale, (
        "exception list stale — gap closed? remove from KNOWN_GAPS + docs: "
        + ", ".join(f"{g}:{s} (in {sc})" for g, s, sc in stale)
    )


def test_acp_sinks_stay_wired_through_the_factory():
    """G3 CLOSED (#498) — turned from an absence-tripwire into a positive
    pin. Rows 13/14: an ACP run must carry BOTH sinks, and it may only get
    them via the factory. This fails if someone unwires a sink or reverts
    serve_loop to a bare run_acp_once call."""
    sinks = {"transcript_sink", "preview_sink"}
    for call_keywords in _factory_run_keywords():
        missing = sinks - set(call_keywords)
        assert not missing, (
            "G3 REOPENED — _make_acp_run_factory's run_acp_once call no "
            f"longer passes {sorted(missing)}: ACP runs produce no session "
            "transcript/preview. Update rows 13/14 in "
            "docs/dispatch-path-parity.md in the same change."
        )
    bare = [
        n for n in ast.walk(_serve_loop_ast())
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        == "run_acp_once"
    ]
    assert not bare, (
        "G3 REOPENED — serve_loop calls run_acp_once directly, bypassing "
        "the factory that owns the sinks (the exact main state before #498)"
    )


def test_every_acp_factory_call_site_is_fully_wired():
    """M7 (row 21): the control wiring is pinned PER CALL SITE.

    test_serve_loop_wiring.py checks the union over all
    `_make_acp_run_factory` calls, so a second call site that dropped
    `cancel_state` (stop knob), `heartbeat_fn` (watchdog liveness) or
    `interrupt_state` would still be green there. Here every call site must
    carry all three on its own."""
    required = {"cancel_state", "heartbeat_fn", "interrupt_state"}
    for i, call_keywords in enumerate(_acp_factory_call_keywords()):
        missing = required - set(call_keywords)
        assert not missing, (
            f"_make_acp_run_factory call site #{i + 1} in serve_loop omits "
            f"{sorted(missing)} — that call site's runs lose the stop "
            "knob / watchdog liveness / interrupt stamp (gap M7, row 21 in "
            "docs/dispatch-path-parity.md)"
        )


def test_acp_stream_feeds_the_watchdog():
    """G4 CLOSED (#523), ACP half: streaming assistant/thought chunks must
    feed the liveness channel, so a long pure-reasoning turn counts as
    progress. Row 11. The serve_loop half (`_acp_tool_heartbeat` passed as
    `heartbeat_fn`) is pinned by SERVE_LOOP_WIRING["acp_progress_heartbeat"].

    Call-based, not substring-based: the sabotage probe for this card
    deleted the `_stream_heartbeat()` call and left `def _stream_heartbeat`
    standing, which a substring check happily accepts."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(bridge.run_acp_once)))
    src = inspect.getsource(bridge.run_acp_once)
    for chunk in ("agent_message_chunk", "agent_thought_chunk"):
        assert chunk in src, (
            f"G4 REOPENED — run_acp_once no longer reacts to {chunk}: a "
            "pure-reasoning ACP turn produces no progress records and the "
            "idle watchdog kills a healthy run. Update row 11 in "
            "docs/dispatch-path-parity.md in the same change."
        )
    called = _called_names(tree)
    assert "_stream_heartbeat" in called, (
        "G4 REOPENED — run_acp_once defines but never CALLS "
        "_stream_heartbeat: streaming tokens stop feeding the watchdog's "
        "liveness channel. Update row 11 in docs/dispatch-path-parity.md."
    )
    # And the heartbeat must actually reach the injected callback.
    hb = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_stream_heartbeat"
    )
    assert "_heartbeat" in _called_names(hb), (
        "G4 REOPENED — _stream_heartbeat no longer calls the heartbeat "
        "callback, so the throttle swallows every stamp"
    )


def test_closed_gap_wiring_is_called_not_merely_mentioned():
    """Closed-gap pins that SERVE_LOOP_WIRING can only check as substrings
    get a call-based second leg here.

    Same sabotage lesson as above: renaming `recovery_fn` to `recovery_fn2`
    and dropping `_make_http_recovery(...)` left the substring registry green
    (`recovery_fn` is a prefix of `recovery_fn2`), while the startup recovery
    G2 closed was in fact unwired."""
    serve = _serve_loop_ast()
    called = _called_names(serve)
    required = {
        # G2 (#522, row 7): the endpoint client must be BUILT and the
        # resulting callable must be INVOKED.
        "_make_http_recovery": "startup recovery never builds its HTTP client",
        "recovery_fn": "startup recovery client is built but never called",
        # G3 (#498, rows 13/14): the sink-owning factory.
        "_make_acp_run_factory": "ACP runs bypass the sink-owning factory",
        # Control channel (row 8) and task context (row 17).
        "start_heartbeater": "no heartbeater — control channel is dead",
        "write_task_context_env": "mc CLI loses its task context",
    }
    missing = {sym: why for sym, why in required.items() if sym not in called}
    assert not missing, (
        "serve_loop no longer CALLS documented wiring (a mention in a "
        "comment or a leftover definition does not count): "
        + "; ".join(f"{s} — {w}" for s, w in sorted(missing.items()))
    )


def test_acp_context_pct_gap_is_still_shaped_as_documented():
    """G5 (row 16), open: serve_loop hands the heartbeater the NATIVE TUI's
    capture_pane on both driver branches, which is why ACP never reports a
    context%. An ACP-aware source would change this call — and then row 16
    plus the G5 summary row must be updated in the same change."""
    calls = [
        n for n in ast.walk(_serve_loop_ast())
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        == "start_heartbeater"
    ]
    assert len(calls) == 1, (
        f"expected exactly one start_heartbeater call in serve_loop, "
        f"found {len(calls)} — re-audit row 16 (G5)"
    )
    kw = {k.arg: k.value for k in calls[0].keywords if k.arg}
    src = kw.get("_capture_pane")
    assert src is not None, "start_heartbeater must receive _capture_pane"
    rendered = ast.unparse(src)
    assert rendered == "tui.capture_pane", (
        "G5 may be CLOSED — serve_loop now passes "
        f"_capture_pane={rendered} instead of the unconditional native "
        "tui.capture_pane. Update row 16 + the G5 summary row in "
        "docs/dispatch-path-parity.md and replace this test with a "
        "positive assertion on the new source."
    )


def test_doc_gap_table_matches_exception_list():
    doc = _file_text(os.path.join(REPO_ROOT, "docs", "dispatch-path-parity.md"))
    for gap_id, _sym, _scope, _why in KNOWN_GAPS:
        assert gap_id in doc, f"KNOWN_GAPS entry {gap_id} missing from the doc"
    for gap_id in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "M7"):
        assert gap_id in doc, f"gap {gap_id} missing from docs/dispatch-path-parity.md"

    # A gap this test suite now pins POSITIVELY must not still be listed as
    # an open exception, and vice versa: the doc's gap-summary status and
    # KNOWN_GAPS have to tell the same story.
    open_ids = {g for g, _s, _sc, _w in KNOWN_GAPS}
    for gap_id in ("G2", "G3", "G4", "G7"):
        assert gap_id not in open_ids, (
            f"{gap_id} is wired positively in this suite but still sits in "
            "KNOWN_GAPS — pick one"
        )
    for line in doc.splitlines():
        for gap_id in ("G2", "G3", "G4", "G7"):
            if line.startswith(f"| {gap_id}:"):
                assert "CLOSED" in line, (
                    f"{gap_id} is pinned as closed by this suite but the doc "
                    f"gap-summary row does not say CLOSED: {line[:120]}"
                )
