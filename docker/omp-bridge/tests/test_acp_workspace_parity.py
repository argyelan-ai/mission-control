#!/usr/bin/env python3
"""G8 (docs/dispatch-path-parity.md): ACP branch must use the backend-
prepared, per-task workspace as its `cwd` — not the container's bare
`os.getcwd()`.

Incident 2026-09-13: serve_loop already computed the correct, host-to-
container translated workspace (`cwd = container_workspace_path(task.get(
"workspace_path")) or "/workspace"`, bridge.py) and fed it to the NATIVE
branch (`run_native_turn(cwd=_cwd, ...)`), but the ACP branch's call to
`_make_acp_run_factory(...)` never passed `cwd` — the factory fell back to
`os.environ.get("OMP_ACP_CWD") or _acp_cwd_default()` (== `os.getcwd()`,
`/home/agent`, unprepared). The model then bootstrapped its own `gh repo
clone mission-control`, which resolved the short name against the logged-in
`gh` account (`marknx`) instead of `argyelan-ai` — two PRs (#148, #149)
landed on the wrong GitHub org and had to be ported by hand.

Style: reuses the AST/call-based conventions of test_serve_loop_wiring.py
and test_dispatch_path_parity.py (substring checks miss a deleted CALL that
leaves the `def` standing — see the sabotage note in those files) PLUS two
dynamic Durchstich tests that drive the REAL serve_loop production path
(test_acp_through_tests.py's `_drive_serve_loop_acp` harness) end to end
against the in-process fake ACP server, so the wiring claim is proven by
actual data flow, not just by a keyword name appearing on both sides of a
call.

Sabotage probes (run manually against the pre-fix tree, restored via Edit —
never `git checkout`, which would also wipe the fix — in the SAME session,
per the team's own "Sabotage-Mutation muss Schutzschicht durchbrechen" /
"Sabotage-Probe je Teil" lesson):

  1. Remove `cwd=cwd,` from the `_make_acp_run_factory(...)` call site in
     serve_loop -> test_acp_cwd_uses_prepared_workspace_not_env_var goes RED
     (captured cwd reverts to the OMP_ACP_CWD env value) AND
     test_acp_factory_call_sites_pass_cwd goes RED (AST: "cwd" missing from
     the call-site keyword set).
  2. Remove the `_require_prepared_acp_workspace(_cwd)` call from `run_once`
     (leaving the `def` standing per the "def survives the deleted call"
     trap) -> test_acp_workspace_missing_raises_loud_blocker goes RED (no
     blocker recorded, the fake run proceeds silently) AND
     test_native_and_acp_guard_wiring_is_call_based goes RED on its own
     "guard is actually CALLED" half.
  3. Delete the body of `_require_prepared_acp_workspace` (keep the `def`)
     -> test_require_prepared_acp_workspace_unit goes RED directly (no
     exception raised for a missing directory).

Run: pytest docker/omp-bridge/tests/test_acp_workspace_parity.py -v
     (needs OPENAI_MODEL set; the `omp-bridge` CI lane in
     .github/workflows/ci.yml runs the whole tests/ directory, this file
     included — PR #547 follow-up W3, see docs/dispatch-path-parity.md)
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import bridge  # noqa: E402
import acp_client  # noqa: E402
import test_acp_through_tests as tat  # noqa: E402


# ── AST helpers (mirrors test_serve_loop_wiring.py / test_dispatch_path_parity.py) ──


def _serve_loop_ast() -> ast.Module:
    return ast.parse(inspect.getsource(bridge.serve_loop))


def _find_fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _acp_factory_call_keyword_sets() -> list[set]:
    """Keyword-name set of every `_make_acp_run_factory(...)` call site in
    serve_loop, one set PER call site (a union across sites would let a
    second call site silently drop `cwd` while the first one's keyword
    keeps the check green — the exact M7 trap test_dispatch_path_parity.py
    already documents for cancel_state/heartbeat_fn/interrupt_state)."""
    calls = []
    for node in ast.walk(_serve_loop_ast()):
        if isinstance(node, ast.Call):
            name = (
                node.func.id if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name == "_make_acp_run_factory":
                calls.append({kw.arg for kw in node.keywords if kw.arg})
    assert calls, "no _make_acp_run_factory call site found in serve_loop"
    return calls


def _called_names(tree: ast.AST) -> set:
    """Every name actually INVOKED in this subtree — `f()` and `obj.f()`
    alike. Substring/`in source` checks cannot tell a live call from a
    leftover `def` (memory lesson: deleting a call left `def
    _stream_heartbeat` behind and a substring check stayed green)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.id if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name:
                out.add(name)
    return out


# ── forward/reverse wiring: cwd reaches the ACP factory call site(s) ────────


def test_acp_factory_call_sites_pass_cwd():
    """G8: every `_make_acp_run_factory` call site in serve_loop must supply
    `cwd` — dropping it (even on a hypothetical second call site) silently
    reverts that site to the unprepared os.getcwd() fallback."""
    for i, kws in enumerate(_acp_factory_call_keyword_sets()):
        assert "cwd" in kws, (
            f"_make_acp_run_factory call site #{i + 1} in serve_loop omits "
            "'cwd' — that call site's ACP runs fall back to "
            "OMP_ACP_CWD/os.getcwd() instead of the backend-prepared "
            "workspace (G8, docs/dispatch-path-parity.md)"
        )


def test_acp_factory_forwards_cwd_to_run_acp_once():
    """Chain check (test_serve_loop_wiring.py style): the factory must not
    just ACCEPT `cwd`, it must forward it to run_acp_once under the same
    name — a factory that renamed it internally (`resolved = cwd or ...`)
    without also using that name in the run_acp_once call would swallow the
    parameter (the M1/M2 mutation class those tests already guard against
    for the other kwargs)."""
    factory = _find_fn(ast.parse(inspect.getsource(bridge._make_acp_run_factory)),
                        "_make_acp_run_factory")
    assert "cwd" in {a.arg for a in factory.args.kwonlyargs}, (
        "_make_acp_run_factory must accept a `cwd` kwonly parameter"
    )
    run_fn = next(
        n for n in ast.walk(factory)
        if isinstance(n, ast.FunctionDef) and n.name == "run"
    )
    call = next(
        n for n in ast.walk(run_fn)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "run_acp_once"
    )
    cwd_kw = next((k for k in call.keywords if k.arg == "cwd"), None)
    assert cwd_kw is not None, (
        "_make_acp_run_factory's run_acp_once call must pass cwd="
    )
    assert isinstance(cwd_kw.value, ast.Name) and cwd_kw.value.id == "cwd", (
        "run_acp_once must receive the factory's `cwd` — got "
        f"{ast.dump(cwd_kw.value)} (looks swallowed/renamed)"
    )


def _walk_body_only(nodes: list):
    """Like ast.walk, but over a list of statements — and critically does
    NOT descend into an ast.If's `.orelse` (the sibling `else:`/`elif`
    clause). Plain `ast.walk(if_node)` walks BOTH `.body` and `.orelse`,
    which is exactly wrong here: the ACP `elif` and the native `else` sit
    as siblings in the SAME If node's body/orelse, so a bare `ast.walk`
    over the ACP branch node silently pulls in the native branch's
    same-named `run_once`/`continue_once` too."""
    for stmt in nodes:
        yield stmt
        for field, value in ast.iter_fields(stmt):
            if field == "orelse":
                continue
            if isinstance(value, ast.AST):
                yield from _walk_body_only([value])
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        yield from _walk_body_only([item])


def _acp_branch_node() -> ast.If:
    """The `elif _acp_env_driver() == "acp":` branch, as its own AST node
    (an `ast.If` nested in the outer If's `orelse`) — scoping to exactly
    this subtree is what lets the per-closure check below tell the ACP
    branch's run_once/continue_once apart from the native branch's
    same-named closures."""
    for node in ast.walk(_serve_loop_ast()):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            left = node.test.left
            if (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "_acp_env_driver"
            ):
                return node
    raise AssertionError("could not locate the OMP_DRIVER == 'acp' branch in serve_loop")


def test_native_and_acp_guard_wiring_is_call_based():
    """G8 loud-failure guard: `_require_prepared_acp_workspace` must be
    CALLED inside EACH of the ACP branch's run_once/continue_once closures
    — per-closure, not a union over the whole function (M7 lesson from
    test_dispatch_path_parity.py: a union check stays green when only ONE
    of two closures drops the call) — and NEVER inside the native (`else:`)
    branch, so poll.sh/native dispatch stays byte-identical (DoD:
    "poll.sh-Agenten verhalten sich unveraendert"). Call-based per the
    def-survives-the-deleted-call lesson: a `def
    _require_prepared_acp_workspace` sitting unused would satisfy a
    substring check while the guard is dead."""
    acp_branch = _acp_branch_node()
    # `.body` only — `ast.walk(acp_branch)` would also pull in `.orelse`
    # (the native `else:` block), which defines same-named closures.
    acp_closures = {
        n.name: n for n in _walk_body_only(acp_branch.body)
        if isinstance(n, ast.FunctionDef) and n.name in ("run_once", "continue_once")
    }
    assert set(acp_closures) == {"run_once", "continue_once"}, (
        f"expected both run_once and continue_once inside the ACP branch, "
        f"found {sorted(acp_closures)} — re-anchor this test"
    )
    missing = [
        name for name, fn in acp_closures.items()
        if "_require_prepared_acp_workspace" not in _called_names(fn)
    ]
    assert not missing, (
        f"G8 REOPENED — ACP closure(s) {missing} never CALL "
        "_require_prepared_acp_workspace: an unprepared ACP workspace is "
        "silently accepted again for that closure"
    )

    # The guard belongs to the ACP branch only — the native (`else:`)
    # branch's own run_once/continue_once must never call it (DoD:
    # poll.sh-Agenten verhalten sich unveraendert).
    native_closures = {
        n.name: n for n in _walk_body_only(acp_branch.orelse)
        if isinstance(n, ast.FunctionDef) and n.name in ("run_once", "continue_once")
    }
    assert set(native_closures) == {"run_once", "continue_once"}, (
        f"expected both run_once and continue_once inside the native branch, "
        f"found {sorted(native_closures)} — re-anchor this test"
    )
    for name, fn in native_closures.items():
        assert "_require_prepared_acp_workspace" not in _called_names(fn), (
            f"G8 scope violation — the native/poll.sh branch's {name} must "
            "not call the ACP-only workspace guard (DoD: poll.sh-Agenten "
            "verhalten sich unveraendert)"
        )


# ── direct unit test of the guard itself ────────────────────────────────────


def test_require_prepared_acp_workspace_unit():
    """Direct behavioural test (not AST): a real, existing directory passes
    silently; a missing one raises loudly with a message an operator can
    act on."""
    with tempfile.TemporaryDirectory(prefix="acp-ws-ok-") as td:
        bridge._require_prepared_acp_workspace(td)  # must not raise

    missing = os.path.join(tempfile.gettempdir(), "acp-ws-does-not-exist-xyz")
    assert not os.path.isdir(missing)
    try:
        bridge._require_prepared_acp_workspace(missing)
        raise AssertionError("expected RuntimeError for a missing workspace dir")
    except RuntimeError as e:
        assert "nicht vorbereitet" in str(e)
        assert missing in str(e)


# ── dynamic Durchstich: the REAL serve_loop production path, fake ACP server ─


@tat._with_serve_env
def test_acp_cwd_uses_prepared_workspace_not_env_var():
    """Durchstich (production path, no _run_factory injection): a task whose
    `workspace_path` differs from the OMP_ACP_CWD env value must reach
    run_acp_once with the TASK's path, not the env fallback.
    `_drive_serve_loop_acp` sets OMP_ACP_CWD=str(HERE) for its own baseline
    plumbing — pre-fix, that env value is exactly what leaked into every
    ACP run regardless of the task, which is the incident this test pins."""
    acp_client.ACPClient._fire_event_cbs = tat._enrich_final_chunk
    orig_task = tat._SERVE_TASK
    try:
        with tempfile.TemporaryDirectory(prefix="acp-serve-") as td, \
             tempfile.TemporaryDirectory(prefix="acp-task-ws-") as task_ws:
            agent_dir = Path(td) / "agent"
            agent_dir.mkdir()
            tat._SERVE_TASK = {**orig_task, "workspace_path": task_ws}
            calls, captured, _written, _cancel = tat._drive_serve_loop_acp(agent_dir)

            assert captured.get("cwd") == task_ws, (
                f"expected run_acp_once cwd={task_ws!r} (the task's prepared "
                f"workspace), got {captured.get('cwd')!r} — the ACP branch is "
                "ignoring the backend-prepared workspace again (G8 REOPENED)"
            )
            assert captured.get("cwd") != str(HERE), (
                "run_acp_once cwd equals the OMP_ACP_CWD env fallback, not "
                "the task's workspace — the factory call dropped `cwd`"
            )
            finishes = [c for c in calls if c[0] == "finish"]
            assert finishes, calls
    finally:
        tat._SERVE_TASK = orig_task
        acp_client.ACPClient._fire_event_cbs = tat._orig_fire_event_cbs


@tat._with_serve_env
def test_acp_workspace_missing_raises_loud_blocker():
    """Durchstich: a task whose translated workspace path does not exist on
    disk must BLOCK the card with a comprehensible reason, never silently
    run the model somewhere else (DoD: "Fehlschlag erzeugt einen sichtbaren
    Hinweis in der Karte, keinen stillen leeren Ordner")."""
    acp_client.ACPClient._fire_event_cbs = tat._enrich_final_chunk
    orig_task = tat._SERVE_TASK
    try:
        with tempfile.TemporaryDirectory(prefix="acp-serve-") as td:
            agent_dir = Path(td) / "agent"
            agent_dir.mkdir()
            missing_ws = str(Path(td) / "never-created-task-workspace")
            assert not os.path.isdir(missing_ws)
            tat._SERVE_TASK = {**orig_task, "workspace_path": missing_ws}
            calls, captured, written, _cancel = tat._drive_serve_loop_acp(agent_dir)

            blockers = [c for c in calls if c[0] == "blocker"]
            assert blockers, (
                "expected a blocker call when the ACP workspace does not "
                f"exist — got calls={calls} (silent failure — G8 REOPENED)"
            )
            finishes = [c for c in calls if c[0] == "finish"]
            assert not finishes, (
                "a missing workspace must never resolve as a finished task: "
                f"{calls}"
            )
            # No transcript/preview either — the run never reached omp acp.
            assert not written, [str(f) for f in written]
            # run_acp_once was never invoked for this turn — proves the
            # guard fires BEFORE the model runs, not after (no silent
            # partial run in the wrong directory).
            assert not captured, captured
    finally:
        tat._SERVE_TASK = orig_task
        acp_client.ACPClient._fire_event_cbs = tat._orig_fire_event_cbs


if __name__ == "__main__":
    test_acp_factory_call_sites_pass_cwd()
    print("PASS test_acp_factory_call_sites_pass_cwd")
    test_acp_factory_forwards_cwd_to_run_acp_once()
    print("PASS test_acp_factory_forwards_cwd_to_run_acp_once")
    test_native_and_acp_guard_wiring_is_call_based()
    print("PASS test_native_and_acp_guard_wiring_is_call_based")
    test_require_prepared_acp_workspace_unit()
    print("PASS test_require_prepared_acp_workspace_unit")
    test_acp_cwd_uses_prepared_workspace_not_env_var()
    print("PASS test_acp_cwd_uses_prepared_workspace_not_env_var")
    test_acp_workspace_missing_raises_loud_blocker()
    print("PASS test_acp_workspace_missing_raises_loud_blocker")
    print("ALL G8 WORKSPACE PARITY TESTS PASS")
