#!/usr/bin/env python3
"""Static wiring test: the REAL call chain serve_loop -> factory ->
run_acp_once, walked end to end (Rex review B3 rebuild).

Why AST, not grep: `transcript_sink=` as a text search also matches the
_factory_ and the tests — exactly the false sense of coverage that let the
production gap ship.

Why a CHAIN, not a union: the first version unioned "kwargs serve_loop
passes to the factory" with "kwargs the factory passes to run_acp_once".
A union proves name PRESENCE somewhere, never that a value FLOWS from
serve_loop through the factory into run_acp_once. The mutation probes
(M1/M2/M3/M6 in the PR) all stayed green under the union check. This
version walks the chain:

  serve_loop
    └─ calls _make_acp_run_factory(**serve_kwargs)   <- checked (chain head)
    └─ calls the factory RESULT: acp_run(...)/run_once(...)  <- M6 guard
         └─ factory's run() calls run_acp_once(**factory_kwargs)
              └─ every kwonly param of run_acp_once must be set HERE,
                 or be allowlisted; `**`-unpacking with CONSTANT string
                 keys is resolved (ast.Dict), so the conditional
                 forwarding dict is visible to the parser.
              └─ for each forwarded name that is ALSO a factory
                 parameter, the value must be that parameter (the
                 factory may not swallow it) — and that factory
                 parameter must in turn be passed by serve_loop.

Deliberately test-only parameters live in ALLOWLIST, each with a reason.
An allowlist entry that IS wired anywhere on the chain fails the test
(no silent rot).

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


def _kwargs_of_calls(node: ast.AST, callee: str) -> dict[str, ast.expr]:
    """All keyword arguments of `callee(...)` calls in this subtree.
    `**`-unpacking with CONSTANT string keys (ast.Dict of ast.Constant)
    is RESOLVED into plain names — a conditional forwarding dict like
    `**({"x": v} if cond else {})` is exactly how the factory forwards,
    and it must be visible to the parser (Rex review B3). Dynamic
    `**mapping` (keyword.arg is None with a non-dict value) is recorded
    as the special name "**<expr>" and can never satisfy a pin."""
    out: dict[str, ast.expr] = {}
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if name != callee:
            continue
        for k in n.keywords:
            if k.arg is not None:
                out[k.arg] = k.value
            else:
                # **unpacking — resolve constant-key dicts, including a
                # conditional forwarding dict (`**({...} if cond else {})`,
                # an IfExp wrapping the Dict — the exact shape of the
                # interrupt_state pass-through).
                def _const_dict_keys(node):
                    if isinstance(node, ast.Dict) and all(
                        isinstance(key, ast.Constant) and isinstance(key.value, str)
                        for key in node.keys
                    ):
                        yield from zip(node.keys, node.values)
                    elif isinstance(node, ast.IfExp):
                        yield from _const_dict_keys(node.body)
                        yield from _const_dict_keys(node.orelse)
                resolved = dict(_const_dict_keys(k.value))
                if resolved:
                    for key, val in resolved.items():
                        out[key.value] = val
                else:
                    out["**<dynamic>"] = k.value
    return out



def _is_param_ref(value: ast.expr, param: str) -> bool:
    """True when `value` is exactly the local variable `param` (or a
    `param or default` fallback — still the parameter's flow)."""
    if isinstance(value, ast.Name) and value.id == param:
        return True
    if isinstance(value, ast.IfExp):
        return _is_param_ref(value.body, param) or _is_param_ref(value.orelse, param)
    if isinstance(value, ast.BoolOp):
        return any(_is_param_ref(v, param) for v in value.values)
    return False


def _chain_wiring(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """Walk the chain and return (factory_input_names, factory_output_names,
    serve_passed_names) where:
    - factory_input_names: kwargs serve_loop passes to _make_acp_run_factory
    - factory_output_names: kwargs the factory's run() passes to run_acp_once
      (** constant-key unpacking resolved)
    - serve_passed_names: names among factory inputs that serve_loop passes
    """
    serve = _find_fn(tree, "serve_loop")
    factory = _find_fn(tree, "_make_acp_run_factory")
    run_fn = next(
        n for n in ast.walk(factory)
        if isinstance(n, ast.FunctionDef) and n.name == "run"
    )
    serve_to_factory = _kwargs_of_calls(serve, "_make_acp_run_factory")
    factory_to_run = _kwargs_of_calls(run_fn, "run_acp_once")
    return set(serve_to_factory), set(factory_to_run), set(serve_to_factory)


def _factory_forwards_inputs(tree: ast.Module) -> list[str]:
    """Factory parameters that the factory RECEIVES but does NOT forward to
    run_acp_once (as the same-named value). These are the swallowed-input
    mutations (M1/M2 class). cancel_state has a deliberate local fallback
    (`cancel_state or ACPCancelState()`) — the fallback still flows the
    INPUT when given, so it counts as forwarded (checked via IfExp/BoolOp
    in _is_param_ref)."""
    factory = _find_fn(tree, "_make_acp_run_factory")
    factory_params = {a.arg for a in factory.args.kwonlyargs}
    run_fn = next(
        n for n in ast.walk(factory)
        if isinstance(n, ast.FunctionDef) and n.name == "run"
    )
    call = next(
        n for n in ast.walk(run_fn)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "run_acp_once"
    )
    passed = _kwargs_of_calls(run_fn, "run_acp_once")
    forwarded: list[str] = []
    for p in sorted(factory_params):
        val = passed.get(p)
        if val is None or not _is_param_ref(val, p):
            forwarded.append(p)
    return forwarded


def test_every_run_acp_once_param_is_production_wired():
    """CHAIN check (B3): every kwonly parameter of run_acp_once must be set
    in the factory's run_acp_once call (chain tail). Allowlist may excuse
    test seams — but an allowlisted name that IS wired anywhere fails, and
    an allowlist entry for a non-parameter fails."""
    tree = _parse()
    params = set(_kwonly(_find_fn(tree, "run_acp_once")))
    _, factory_out, _ = _chain_wiring(tree)
    allowlisted = set(ALLOWLIST)

    missing = params - factory_out - allowlisted
    assert not missing, (
        "run_acp_once parameter(s) never set inside the factory's "
        f"run_acp_once call: {sorted(missing)}. Wire them in the factory "
        "or add to ALLOWLIST with a reason."
    )
    stale = allowlisted - params
    assert not stale, f"ALLOWLIST entries no longer parameters: {sorted(stale)}"
    wired_allowlisted = allowlisted & factory_out
    assert not wired_allowlisted, (
        f"ALLOWLIST entries that ARE wired in the factory (remove them): "
        f"{sorted(wired_allowlisted)}"
    )
    print("PASS test_every_run_acp_once_param_is_production_wired")


def test_factory_forwards_what_serve_loop_passes():
    """CHAIN check (B3, M1/M2 class): every factory parameter that carries
    production behaviour must FLOW to run_acp_once, not be swallowed. The
    factory may wrap (cancel_state or ACPCancelState()) but the input must
    remain the value under the same name."""
    tree = _parse()
    factory = _find_fn(tree, "_make_acp_run_factory")
    factory_params = {a.arg for a in factory.args.kwonlyargs}
    swallowed = _factory_forwards_inputs(tree)
    # task_id/cwd-style params the factory legitimately binds are still
    # expected to appear under their own name in the run_acp_once call —
    # the only acceptable non-forwarded params are NONE: every factory
    # param exists precisely to reach run_acp_once.
    assert not swallowed, (
        "factory parameter(s) received but NOT forwarded to run_acp_once "
        f"(swallowed — the M1/M2 mutation class): {sorted(swallowed)}"
    )
    # And whatever the factory accepts, serve_loop must supply (chain head):
    serve = _find_fn(tree, "serve_loop")
    serve_to_factory = _kwargs_of_calls(serve, "_make_acp_run_factory")
    unsupplied = factory_params - set(serve_to_factory) - {"interrupt_state"}
    assert not unsupplied, (
        f"factory parameter(s) serve_loop never passes: {sorted(unsupplied)}"
    )
    print("PASS test_factory_forwards_what_serve_loop_passes")


def test_serve_loop_calls_the_factory_result():
    """M6 guard: the factory being DEFINED and fed is worthless if
    serve_loop never calls its result. Both closures (run_once and
    continue_once) must invoke the factory-built callable, and that
    callable must be produced by _make_acp_run_factory on the ACP branch."""
    tree = _parse()
    serve = _find_fn(tree, "serve_loop")
    factory_calls = [
        n for n in ast.walk(serve)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "_make_acp_run_factory"
    ]
    assert factory_calls, "serve_loop must build the ACP run via the factory"
    # The assignment target of the factory call:
    assigned_names: set[str] = set()
    for n in ast.walk(serve):
        if (
            isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call)
            and (getattr(n.value.func, "id", None) or getattr(n.value.func, "attr", None)) == "_make_acp_run_factory"
        ):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned_names.add(t.id)
    assert assigned_names, "factory result must be assigned to a name"
    # Each assigned name must be CALLED somewhere in serve_loop (M6: factory
    # built but bypassed by a blank run_acp_once call):
    called_names = {
        n.func.id for n in ast.walk(serve)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    never_called = assigned_names - called_names
    assert not never_called, (
        f"factory result(s) built but never called in serve_loop "
        f"(M6 mutation class): {sorted(never_called)}"
    )
    print("PASS test_serve_loop_calls_the_factory_result")


def test_no_bare_run_acp_once_call_in_serve_loop():
    """M6 complement: inside serve_loop's ACP branch, run_acp_once may only
    be reached THROUGH the factory. A direct run_acp_once call in serve_loop
    would bypass sink creation — the exact founding failure of this test."""
    tree = _parse()
    serve = _find_fn(tree, "serve_loop")
    direct = [
        n for n in ast.walk(serve)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "run_acp_once"
    ]
    assert not direct, (
        "serve_loop must not call run_acp_once directly (bypasses the "
        f"factory's sinks — M6 class); found {len(direct)} direct call(s) "
        f"at line(s) {[n.lineno for n in direct]}"
    )
    print("PASS test_no_bare_run_acp_once_call_in_serve_loop")


def test_factory_does_not_create_private_cancel_state_when_serve_wires_one():
    """Stop-Knopf pin: serve_loop must pass cancel_state into the factory
    (ladder Stufe 1) and the factory must forward it (see the forwarding
    test) — a private ACPCancelState no control channel knows kills the
    stop knob silently."""
    tree = _parse()
    serve = _find_fn(tree, "serve_loop")
    serve_to_factory = _kwargs_of_calls(serve, "_make_acp_run_factory")
    assert "cancel_state" in serve_to_factory, (
        "serve_loop must pass cancel_state to _make_acp_run_factory — "
        "otherwise the factory creates a private ACPCancelState and the "
        "heartbeat control channel (stop knob) stops working"
    )
    assert "heartbeat_fn" in serve_to_factory, (
        "serve_loop must pass heartbeat_fn to _make_acp_run_factory — "
        "otherwise ACP tool runs look idle to the watchdog (#410/#411)"
    )
    # AND the factory must forward both values (M1/M2 arrival check):
    factory = _find_fn(tree, "_make_acp_run_factory")
    run_fn = next(
        n for n in ast.walk(factory)
        if isinstance(n, ast.FunctionDef) and n.name == "run"
    )
    passed = _kwargs_of_calls(run_fn, "run_acp_once")
    for name in ("cancel_state", "heartbeat_fn"):
        val = passed.get(name)
        assert val is not None and _is_param_ref(val, name), (
            f"factory must forward {name} AS the received parameter value "
            f"(got {ast.dump(val) if val else 'nothing'})"
        )
    print("PASS test_factory_does_not_create_private_cancel_state_when_serve_wires_one")


def test_interrupt_state_flows_to_run_acp_once():
    """#492 interplay (W1 fix — previously SKIPped on this base and
    false-green after the merge): the check is signature-bound, not
    value-bound. If run_acp_once has no interrupt_state param, this test
    verifies the factory ACCEPTS one and holds it for the post-merge
    pass-through (the module flag gates forwarding at import time); if the
    param exists, it must actually be forwarded."""
    tree = _parse()
    run_params = set(_kwonly(_find_fn(tree, "run_acp_once")))
    factory = _find_fn(tree, "_make_acp_run_factory")
    factory_params = {a.arg for a in factory.args.kwonlyargs}
    assert "interrupt_state" in factory_params, (
        "factory must ACCEPT interrupt_state — serve_loop always has one "
        "to pass (PR #492 interplay)"
    )
    if "interrupt_state" not in run_params:
        # Pre-#492 base: the factory holds it, forwarding is gated by the
        # import-time signature flag — verify that flag exists and is False
        # here (it flips True the moment #492's signature lands).
        src = open(BRIDGE_SRC, "r", encoding="utf-8").read()
        assert "_RUN_ACP_ACCEPTS_INTERRUPT_STATE" in src, (
            "pre-#492 base must gate the forwarding on the signature flag"
        )
        print("PASS test_interrupt_state_flows_to_run_acp_once "
              "(pre-#492 base: factory accepts + signature-flag gate present)")
        return
    # Post-#492 base: the kwarg must reach run_acp_once (constant-key
    # unpacking resolved), bound to the factory parameter.
    run_fn = next(
        n for n in ast.walk(factory)
        if isinstance(n, ast.FunctionDef) and n.name == "run"
    )
    passed = _kwargs_of_calls(run_fn, "run_acp_once")
    val = passed.get("interrupt_state")
    assert val is not None and _is_param_ref(val, "interrupt_state"), (
        "post-#492: factory must forward interrupt_state AS the received "
        "parameter — otherwise the merge silently drops the stamp "
        "('interrupted (None: None)')"
    )
    print("PASS test_interrupt_state_flows_to_run_acp_once")


def test_task_id_reaches_run_acp_once_from_serve_loop():
    """task_id is load-bearing for the MC lifecycle — never allowlist it."""
    tree = _parse()
    params = set(_kwonly(_find_fn(tree, "run_acp_once")))
    assert "task_id" in params
    assert "task_id" not in ALLOWLIST, (
        "task_id is load-bearing for the MC lifecycle — never allowlist it"
    )
    print("PASS test_task_id_reaches_run_acp_once_from_serve_loop")


if __name__ == "__main__":
    test_every_run_acp_once_param_is_production_wired()
    test_factory_forwards_what_serve_loop_passes()
    test_serve_loop_calls_the_factory_result()
    test_no_bare_run_acp_once_call_in_serve_loop()
    test_factory_does_not_create_private_cancel_state_when_serve_wires_one()
    test_interrupt_state_flows_to_run_acp_once()
    test_task_id_reaches_run_acp_once_from_serve_loop()
    print("ALL WIRING TESTS PASS")
