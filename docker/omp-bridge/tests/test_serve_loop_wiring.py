#!/usr/bin/env python3
"""Static wiring test: every optional callback/sink parameter of
run_acp_once must actually be SET by the production path.

Why AST, not grep: `transcript_sink=` as a text search also matches the
_factory_ and the tests — exactly the false sense of coverage that let the
production gap ship (sinks built in _make_acp_run_factory, which nothing in
production called). This test parses docker/omp-bridge/bridge.py with the
ast module and walks the REAL call graph:

  serve_loop --(ACP branch)--> _make_acp_run_factory(...) --> run(prompt)
      run(prompt) --> run_acp_once(...)

Every keyword-only parameter of run_acp_once must appear as a keyword at
one of those production call sites, either directly in serve_loop's
run_acp_once calls (if the branch calls it bare) or in the factory's
internal run_acp_once call. Deliberately test-only parameters live in the
ALLOWLIST below, each with a reason.

Run:  python3 test_serve_loop_wiring.py   (or pytest test_serve_loop_wiring.py)
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
BRIDGE_SRC = os.path.join(BRIDGE_DIR, "bridge.py")

# Parameters that are deliberately NOT wired in production, with reasons.
ALLOWLIST: dict[str, str] = {
    # Pure test seams: run_acp_once's own tests inject the in-process fake
    # server and canned permission answers through these; production uses
    # the default (real subprocess spawn, real ask round-trip).
    "client_factory": "test seam — in-process fake server in tests only",
    "ask_fn": "test seam — canned permission answers in tests only",
    # Explicit tuning knobs with correct defaults: the retry poll interval
    # is a constant the production path never needs to change.
    "cancel_poll_interval": "tuning knob — default 1.0s poll is the prod value",
}


def _parse() -> ast.Module:
    with open(BRIDGE_SRC, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _find_fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in bridge.py")


def _kwonly(fn: ast.FunctionDef) -> list[str]:
    return [a.arg for a in fn.args.kwonlyargs]


def _call_names(node: ast.AST) -> set[str]:
    """All Call sites in this subtree as `name` (func.id or func.attr)."""
    return {
        (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
    }


def _kwargs_of_calls(node: ast.AST, callee: str) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if name != callee:
            continue
        out.update(k.arg for k in n.keywords if k.arg is not None)
    return out


def _serve_loop_acp_kwargs(tree: ast.Module) -> set[str]:
    """Kwargs reach run_acp_once from serve_loop either directly or through
    the factory. The factory is the ONE place the ACP branch builds its
    run callable — so the union of (factory call kwargs in serve_loop) and
    (run_acp_once kwargs inside the factory's run closure) is the set of
    parameters production actually sets."""
    serve = _find_fn(tree, "serve_loop")
    factory = _find_fn(tree, "_make_acp_run_factory")
    serve_factory_kwargs = _kwargs_of_calls(serve, "_make_acp_run_factory")
    factory_passes = _kwargs_of_calls(factory, "run_acp_once")
    return serve_factory_kwargs | factory_passes


def test_every_run_acp_once_param_is_production_wired():
    tree = _parse()
    params = set(_kwonly(_find_fn(tree, "run_acp_once")))
    wired = _serve_loop_acp_kwargs(tree)
    allowlisted = set(ALLOWLIST)

    missing = params - wired - allowlisted
    assert not missing, (
        "run_acp_once parameter(s) never set on the production path "
        f"(serve_loop ACP branch -> factory -> run_acp_once): "
        f"{sorted(missing)}. Either wire them at the real call sites or "
        "add them to ALLOWLIST with a reason."
    )
    # The allowlist must stay honest: an entry for a parameter that IS wired
    # somewhere would rot silently.
    stale = allowlisted - params
    assert not stale, f"ALLOWLIST entries no longer parameters: {sorted(stale)}"
    wired_allowlisted = allowlisted & wired
    assert not wired_allowlisted, (
        f"ALLOWLIST entries that ARE wired in production (remove them): "
        f"{sorted(wired_allowlisted)}"
    )
    print("PASS test_every_run_acp_once_param_is_production_wired")


def test_factory_does_not_create_private_cancel_state_when_serve_wires_one():
    """The Stop-Knopf regression this whole wiring was about: serve_loop must
    pass cancel_state into the factory (ladder Stufe 1: the heartbeater's
    _on_control flips THAT object via _acp_control_sink). If serve_loop
    ever stops passing it, the factory falls back to a private
    ACPCancelState no control channel knows — the stop knob dies silently."""
    tree = _parse()
    serve = _find_fn(tree, "serve_loop")
    serve_factory_kwargs = _kwargs_of_calls(serve, "_make_acp_run_factory")
    assert "cancel_state" in serve_factory_kwargs, (
        "serve_loop must pass cancel_state to _make_acp_run_factory — "
        "otherwise the factory creates a private ACPCancelState and the "
        "heartbeat control channel (stop knob) stops working"
    )
    assert "heartbeat_fn" in serve_factory_kwargs, (
        "serve_loop must pass heartbeat_fn to _make_acp_run_factory — "
        "otherwise ACP tool runs look idle to the watchdog (#410/#411)"
    )
    print("PASS test_factory_does_not_create_private_cancel_state_when_serve_wires_one")


def test_interrupt_state_flows_to_run_acp_once():
    """#492 interplay: run_acp_once stamps interrupt kind/reason from an
    InterruptState (fix/acp-interrupt-stamp). Whatever callable serve_loop's
    ACP branch ends up calling must forward interrupt_state — otherwise the
    merge of that PR silently drops the stamp and the abort log is back to
    'interrupted (None: None)'. The parameter must be passed somewhere on
    the production path: serve_loop directly, or via the factory."""
    tree = _parse()
    run_fn = _find_fn(tree, "run_acp_once")
    params = set(_kwonly(run_fn))
    if "interrupt_state" not in params:
        # PR #492 not merged into this base yet — the assert becomes active
        # the moment run_acp_once gains the parameter (this test file must
        # be on the branch that wires it).
        print("SKIP test_interrupt_state_flows_to_run_acp_once "
              "(run_acp_once has no interrupt_state param on this base)")
        return
    wired = _serve_loop_acp_kwargs(tree)
    assert "interrupt_state" in wired, (
        "interrupt_state must reach run_acp_once on the production path "
        "(serve_loop ACP branch) — otherwise PR #492's interrupt stamp is "
        "silently lost for the ACP path after the merge"
    )
    print("PASS test_interrupt_state_flows_to_run_acp_once")


def test_task_id_reaches_run_acp_once_from_serve_loop():
    """task_id is a REAL parameter (lifecycle calls need it) — it must not
    be dropped between serve_loop and run_acp_once. It may arrive via the
    factory (which binds it) — the wiring test above checks the union, this
    one pins the parameter as non-allowlisted."""
    tree = _parse()
    params = set(_kwonly(_find_fn(tree, "run_acp_once")))
    assert "task_id" in params
    assert "task_id" not in ALLOWLIST, (
        "task_id is load-bearing for the MC lifecycle — never allowlist it"
    )
    print("PASS test_task_id_reaches_run_acp_once_from_serve_loop")


if __name__ == "__main__":
    test_every_run_acp_once_param_is_production_wired()
    test_factory_does_not_create_private_cancel_state_when_serve_wires_one()
    test_interrupt_state_flows_to_run_acp_once()
    test_task_id_reaches_run_acp_once_from_serve_loop()
    print("ALL WIRING TESTS PASS")
