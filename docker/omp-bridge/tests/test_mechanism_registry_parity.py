"""Mechanism-registry parity test (registry/mechanisms.py, card f5f1bb9b).

Pattern: PR #521 dispatch-path parity. Structural checks, NOT text
tripwires (anti-#560): python anchors resolve via AST (def/assign-name
lookup, call-site and const deep checks), shell anchors via exact
constructed patterns (assignment forms, anchored function definitions).

Directions (both matter, review PR #521 blocker B1):
  forward : every SERVED entry's anchor must resolve in its branch source.
  reverse : every scripts/*-bridge.py that defines heartbeat_loop() must be
            onboarded in the registry — an unknown family member trips this
            test before it ships unserved.

Acceptance probes:
  retro  : the sabotage test rebuilds the PRE-#566 state (bridge.py native
           path without the `--model` pin) in a temp tree and proves the
           forward check reports exactly that cell.
  gegen  : DELIBERATE cells (consciously not served, with reason) must NOT
           trip the forward check — first-class registry state.

Run: pytest docker/omp-bridge/tests/test_mechanism_registry_parity.py -q
     (standalone: python3 test_mechanism_registry_parity.py)
"""

from __future__ import annotations

import ast
import glob
import importlib.util
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import the registry package

import registry.mechanisms as mech  # noqa: E402


# ── structural resolution helpers ─────────────────────────────────────────────
def _find_fn(tree: ast.AST, qualname: str):
    """FunctionDef/AsyncFunctionDef by (last part of) qualname."""
    want = qualname.split(".")[-1]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == want:
                return node
    return None


def _find_assign_name(tree: ast.AST, name: str):
    """Module-level-ish assignment target (Assign or AnnAssign)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node
    return None


def resolve_anchor(rel_path: str, spec: str, repo_root: str):
    """Resolve one SERVED anchor structurally. Returns (ok, detail)."""
    path = os.path.join(repo_root, rel_path)
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        return False, f"source unreadable: {exc}"

    if path.endswith(".sh"):
        # shell anchors are exact regex patterns (assignment form / anchored
        # def); MULTILINE because they are line-anchored by construction
        return bool(re.search(spec, src, re.M)), f"shell regex {spec!r}"

    # python anchors: "qualname" plus optional &-separated deep checks
    # ("...&const:--model" = def exists AND the const appears inside it;
    #  "...&call:xyz()" = def exists AND the call-site text appears inside it)
    parts = spec.split("&")
    tree = ast.parse(src)  # syntax error -> raises -> surfaced, not swallowed
    node = _find_fn(tree, parts[0]) or _find_assign_name(tree, parts[0])
    if node is None:
        return False, f"ast def/assign {parts[0]!r}"
    node_src = ast.get_source_segment(src, node) or ""
    for mod_ in parts[1:]:
        if mod_.startswith("const:"):
            needle = mod_[len("const:"):]
            if needle not in node_src:
                return False, f"const {needle!r} missing inside {parts[0]}"
        elif mod_.startswith("call:"):
            # call-SITES live in the caller, not inside the def -> whole-file
            needle = mod_[len("call:"):]
            if needle not in src:
                return False, f"call-site {needle!r} missing from file"
        else:
            return False, f"unknown anchor modifier {mod_!r}"
    return True, f"ast def/assign {parts[0]!r} + deep checks"


def forward_failures(module=mech, repo_root: str | None = None):
    """Run the forward direction over a mechanisms module; return the list
    of failing (branch, mechanism, reason) triples. SERVED-only: DELIBERATE
    cells are consciously exempt (the Gegenprobe)."""
    root = repo_root or module.REPO_ROOT
    failures = []
    for (branch, mechanism), entry in module.REGISTRY.items():
        if entry.state != module.SERVED:
            continue
        rel, _, spec = entry.anchor.partition("::")
        ok, detail = resolve_anchor(rel, spec, root)
        if not ok:
            failures.append(
                (branch, mechanism.key,
                 f"SERVED anchor unresolved ({detail}): {entry.anchor}")
            )
    return failures


# ── forward direction ─────────────────────────────────────────────────────────
def test_forward_anchors_resolve():
    """Every SERVED registry entry must resolve structurally in its branch
    source. A branch that stops serving a required mechanism (function
    removed, pin dropped, constant gone) lands here with a named cell."""
    failures = forward_failures()
    assert not failures, (
        "mechanism registry parity BROKEN — these cells claim SERVED but "
        "their structural anchor is gone:\n  "
        + "\n  ".join(f"{b} :: {m}: {r}" for b, m, r in failures)
    )
    print(f"PASS test_forward_anchors_resolve "
          f"({sum(1 for e in mech.REGISTRY.values() if e.state == mech.SERVED)} served cells)")


def test_deliberate_cells_carry_reasons_and_stay_green():
    """Gegenprobe: DELIBERATE is a first-class state. Every such cell must
    carry a real reason (>= 40 chars), and the forward check must not
    require anchors for them — a branch that consciously lacks a mechanism
    never turns the suite red by itself."""
    problems = []
    for (branch, mechanism), entry in mech.REGISTRY.items():
        if entry.state == mech.DELIBERATE:
            if not entry.reason or len(entry.reason) < 40:
                problems.append(f"{branch} :: {mechanism.key}: reason missing/too short")
            if entry.anchor:
                problems.append(f"{branch} :: {mechanism.key}: DELIBERATE must not carry an anchor")
    assert not problems, "DELIBERATE cells malformed:\n  " + "\n  ".join(problems)
    # and explicitly: forward_failures ignores them
    served_only = [k for k, v in mech.REGISTRY.items() if v.state == mech.SERVED]
    all_keys = list(mech.REGISTRY.keys())
    assert len(served_only) < len(all_keys), "registry lost its DELIBERATE cells"
    print(f"PASS test_deliberate_cells_carry_reasons_and_stay_green "
          f"({len(all_keys) - len(served_only)} deliberate cells exempt)")


def test_no_open_required_missing_gaps_ship_silently():
    """REQUIRED_MISSING is a to-do in code form: if a cell declares it, this
    test is red until the gap is fixed or re-classified with a reason."""
    open_gaps = [
        (b, m.key) for (b, m), e in mech.REGISTRY.items()
        if e.state == mech.REQUIRED_MISSING
    ]
    assert not open_gaps, (
        "registry declares OPEN gaps that must be fixed or re-classified: "
        + ", ".join(f"{b}::{m}" for b, m in open_gaps)
    )
    print("PASS test_no_open_required_missing_gaps_ship_silently")


def test_registry_is_complete_over_family_and_mechanisms():
    """Every family branch must have a cell for every mechanism — a new
    mechanism added to the registry without covering all branches (or a new
    branch missing cells) trips here."""
    branches = set(mech.BRANCH_FILES)
    mechanism_keys = {k[1].key for k in mech.REGISTRY}
    for b in branches:
        have = {k[1].key for k in mech.REGISTRY if k[0] == b}
        missing = mechanism_keys - have
        assert not missing, f"branch {b!r} lacks registry cells for: {sorted(missing)}"
    # and the mechanism set must be exactly the declared Mechanism objects
    declared = {
        v.key for v in vars(mech).values() if isinstance(v, mech.Mechanism)
    }
    assert declared == mechanism_keys, (
        f"registry/declaration drift: declared={sorted(declared)} used={sorted(mechanism_keys)}"
    )
    print(f"PASS test_registry_is_complete_over_family_and_mechanisms "
          f"({len(branches)} branches x {len(mechanism_keys)} mechanisms)")


# ── reverse direction ─────────────────────────────────────────────────────────
def test_reverse_unknown_family_members_onboarded():
    """Any scripts/*-bridge.py that defines heartbeat_loop() is a family
    member and must appear in BRANCH_FILES (or be onboarded with a reason).
    A NEW bridge file added to scripts/ trips this before it ships
    unserved — the #566 class, caught at review time instead of in
    operation."""
    known = {os.path.basename(v): v for v in mech.BRANCH_FILES.values()}
    exclusions = {}  # filename -> reason (none today; keep for deliberate omissions)
    unknown = []
    for path in sorted(glob.glob(os.path.join(mech.REPO_ROOT, "scripts", "*-bridge.py"))):
        name = os.path.basename(path)
        if name in known or name in exclusions:
            continue
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        if _find_fn(tree, "heartbeat_loop") is not None:
            unknown.append(name)
    assert not unknown, (
        "unonboarded family member(s) in scripts/ (define heartbeat_loop "
        f"but are not in the mechanism registry): {unknown} — onboard them "
        "into registry/mechanisms.py or add an exclusion with a reason"
    )
    print("PASS test_reverse_unknown_family_members_onboarded")


# ── retro-probe: would this have caught #566? ────────────────────────────────
def test_sabotage_pre566_dropped_model_pin_trips_forward():
    """Abnahmebedingung: rebuild the PRE-#566 state — bridge.py's native
    path WITHOUT the `--model` pin (the state that died 401 sk-noauth in
    operation) — in a temp tree and prove the forward check reports exactly
    that cell and nothing else."""
    assert "--model" in open(mech.BRANCH_FILES[mech.BRIDGE], encoding="utf-8").read()

    tmp = tempfile.mkdtemp(prefix="mech-registry-sab-")
    try:
        # tmp-root layout that mechanisms.py's REPO_ROOT math expects:
        # tmproot/<l1>/<l2>/<l3>/registry/mechanisms.py
        reg_dir = os.path.join(tmp, "l1", "l2", "l3", "registry")
        os.makedirs(reg_dir)
        shutil.copy(os.path.join(os.path.dirname(mech.__file__), "mechanisms.py"),
                    os.path.join(reg_dir, "mechanisms.py"))
        shutil.copy(os.path.join(os.path.dirname(mech.__file__), "__init__.py"),
                    os.path.join(reg_dir, "__init__.py"))

        # ALL files the SERVED anchors point into (branch files AND
        # anchor-only files like docker/hermes/entrypoint.sh)
        rel_paths = {b: os.path.relpath(p, mech.REPO_ROOT)
                     for b, p in mech.BRANCH_FILES.items()}
        for (b, m), e in mech.REGISTRY.items():
            if e.state == mech.SERVED and e.anchor:
                rel_paths.setdefault(f"anchor:{b}:{m.key}",
                                     e.anchor.partition("::")[0])
        for rel in set(rel_paths.values()):
            dst = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(os.path.join(mech.REPO_ROOT, rel), dst)

        # THE mutation: drop the native `--model` pin from bridge.py
        bpath = os.path.join(tmp, rel_paths[mech.BRIDGE])
        src = open(bpath, encoding="utf-8").read()
        needle = '        "--model", model,\n'
        assert src.count(needle) == 1, "sabotage anchor (--model line) not unique"
        open(bpath, "w", encoding="utf-8").write(src.replace(needle, "", 1))

        spec = importlib.util.spec_from_file_location(
            "mech_sabotage", os.path.join(reg_dir, "mechanisms.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        failures = forward_failures(mod, repo_root=tmp)
        broken_cells = {(b, m) for b, m, _ in failures}
        assert (mech.BRIDGE, "model_pinning") in broken_cells, (
            f"retro-probe FAILED its purpose: pre-#566 state not detected; "
            f"got {sorted(broken_cells)}"
        )
        unexpected = broken_cells - {(mech.BRIDGE, "model_pinning")}
        assert not unexpected, (
            f"sabotage too broad, also flagged: {sorted(unexpected)}"
        )
        print("PASS test_sabotage_pre566_dropped_model_pin_trips_forward "
              "(pre-#566 state detected as bridge.py::model_pinning)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── standalone runner (house style) ───────────────────────────────────────────
if __name__ == "__main__":
    fns = [
        test_forward_anchors_resolve,
        test_deliberate_cells_carry_reasons_and_stay_green,
        test_no_open_required_missing_gaps_ship_silently,
        test_registry_is_complete_over_family_and_mechanisms,
        test_reverse_unknown_family_members_onboarded,
        test_sabotage_pre566_dropped_model_pin_trips_forward,
    ]
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        raise SystemExit(1)
    print("ALL PASS (mechanism registry parity)")
