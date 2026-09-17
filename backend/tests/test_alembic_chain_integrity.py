"""The migration chain must stay deployable: one head, valid parents, ids ≤ 32.

Both failure modes here broke real deploys on 2026-07-28:

1. **Two heads.** #183 and #184 each added a migration on top of the same
   parent. Every branch was green on its own — the collision only existed once
   both were on main, where ``alembic upgrade head`` refuses to run:
   ``Multiple head revisions are present``. Resolved back then with the merge
   revision ``0169_merge_heads``; these tests catch the NEXT one in the PR that
   creates it instead of at deploy time. Merge revisions (tuple
   ``down_revision``) are the legitimate fix and parse as multiple parents.

2. **Revision id longer than 32 chars.** ``alembic_version.version_num`` is
   ``varchar(32)``. ``0167_runtime_display_name_derived`` (33 chars) passed
   every local run — a local database already holds a row — and only a FRESH
   database exploded: ``StringDataRightTruncationError``. The fresh-boot E2E
   caught it; this catches it before CI even builds an image.

No database needed: the files are parsed directly, so the failure lands in the
offending PR.

**Destructive-migration detection (task 40c484c6) — closed and open gaps.**
Three ways were found to walk a column drop past the original AST scan
(which only looked for `op.drop_column(...)` inside `upgrade()`'s own body,
in files added on this branch):

- raw SQL — ``op.execute("ALTER TABLE ... DROP COLUMN ...")`` has no
  ``drop_column`` call for the walk to find. **Closed**: `_calls_drop_column`
  now also inspects string-literal arguments to every `op.execute(...)` for
  a `DROP COLUMN` pattern (case-insensitive).
- a drop inside a module-level helper called from `upgrade()`.
  **Closed**: `_calls_drop_column` walks the call graph from `upgrade()`
  into locally-defined functions it calls (transitively), not just
  `upgrade()`'s own body.
- a drop added to an EXISTING main migration — the original guard's
  `--diff-filter=A` only sees files ADDED on this branch, so editing an
  already-shipped file never triggered it. **Closed** by a second guard,
  `test_destructive_drop_not_added_to_an_existing_migration`, which diffs
  modified files (`--diff-filter=M`) and compares drop_column-status
  between the merge-base and HEAD versions of each.

**Documented, not closed** — both are honest gaps, not silent ones:

- a drop reached through a helper defined in a DIFFERENT module (an
  imported function, not a local one) is invisible to the call-graph walk.
  Closing this would mean statically resolving arbitrary imports, which
  risks false negatives from misresolved imports being mistaken for
  guarantees; not attempted.
- SQL built from an f-string or a variable (`op.execute(f"...{col}...")`) is
  invisible to `_is_raw_drop_column_execute`, which only evaluates
  string-literal arguments (`ast.literal_eval`) — a dynamic string can't be
  evaluated without actually running the migration. Closing this fully
  would need a much fuzzier heuristic (e.g. flag ANY non-literal argument to
  `op.execute`), which trades a hard false-negative for a soft false-positive
  rate this module doesn't yet have data to tune; not attempted.

**Probes must be committed.** Every guard below diffs HEAD against the
merge-base with `origin/main` — an uncommitted file is invisible to `git
diff`, so a sabotage/counter-check probe that only exists in the working tree
will silently pass through every one of these checks. `git add` (or commit)
the probe file before running pytest against it, or the run proves nothing
(cost twenty minutes chasing a "the guard doesn't fire" ghost that was
actually an uncommitted probe, task 3f249d2b).
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
REPO_ROOT = Path(__file__).resolve().parents[2]
LIMIT = 32
_DROP_COLUMN_SQL_RE = re.compile(r"drop\s+column", re.IGNORECASE)


def _parse() -> list[tuple[str, str, tuple[str, ...]]]:
    """(filename, revision, parents) per migration — via ast, not regex.

    A merge revision declares ``down_revision = ("a", "b")``; a regex built for
    the single-string form silently mangles that tuple. ast handles both.
    """
    out = []
    for f in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rev: str | None = None
        parents: tuple[str, ...] = ()
        for node in tree.body:
            # Both spellings exist in this tree: `down_revision = "x"` and the
            # annotated `down_revision: Union[str, None] = "x"` (e.g. 0102).
            if isinstance(node, ast.AnnAssign):
                targets = [node.target] if node.value is not None else []
            elif isinstance(node, ast.Assign):
                targets = node.targets
            else:
                continue
            for target in targets:
                name = getattr(target, "id", None)
                if name == "revision":
                    rev = ast.literal_eval(node.value)
                elif name == "down_revision":
                    val = ast.literal_eval(node.value)
                    if val is None:
                        parents = ()
                    elif isinstance(val, str):
                        parents = (val,)
                    else:
                        parents = tuple(val)
        if rev is not None:
            out.append((f.name, rev, parents))
    return out


def test_chain_is_not_empty() -> None:
    """Guard against a vacuously green suite if the glob ever misses."""
    assert len(_parse()) > 50


def test_exactly_one_head() -> None:
    entries = _parse()
    revs = {r for _, r, _ in entries}
    referenced = {p for _, _, parents in entries for p in parents}
    heads = sorted(revs - referenced)
    assert len(heads) == 1, (
        f"{len(heads)} alembic heads: {heads}. Two migrations share a parent — "
        "either re-point the newer one or add a merge revision "
        "(like 0169_merge_heads). See this module's docstring."
    )


def test_every_parent_exists() -> None:
    entries = _parse()
    revs = {r for _, r, _ in entries}
    missing = sorted(
        {p for _, _, parents in entries for p in parents} - revs
    )
    assert not missing, f"down_revision points at unknown revisions: {missing}"


@pytest.mark.parametrize(
    "name,rev,parents", _parse(), ids=[n for n, _, _ in _parse()]
)
def test_revision_ids_fit_the_version_column(
    name: str, rev: str, parents: tuple[str, ...]
) -> None:
    assert len(rev) <= LIMIT, (
        f"{name}: revision id {rev!r} is {len(rev)} chars, limit is {LIMIT} "
        f"(alembic_version.version_num is varchar(32); a fresh database cannot "
        "record it — see this module's docstring)."
    )


def _merge_base_sha() -> str | None:
    """SHA of the merge-base with origin/main, or None when it can't be
    resolved (shallow clone with no origin/main fetched, sandbox with no
    remote configured, etc.) — the shared "can this guard even run here"
    check every diff-based guard in this module goes through.
    """
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        if merge_base.returncode != 0 or not merge_base.stdout.strip():
            return None
        return merge_base.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _changed_migration_files(diff_filter: str) -> set[str] | None:
    """Filenames under alembic/versions/ changed between the merge-base with
    origin/main and HEAD, restricted to `diff_filter` (git's --diff-filter,
    e.g. "A" for added-only, "M" for modified-only). Returns None (meaning:
    skip the caller) when origin/main isn't available to diff against.
    """
    base_sha = _merge_base_sha()
    if base_sha is None:
        return None
    try:
        diff = subprocess.run(
            [
                "git", "diff", "--name-only", f"--diff-filter={diff_filter}",
                f"{base_sha}...HEAD", "--", "backend/alembic/versions/",
            ],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        if diff.returncode != 0:
            return None
        return {Path(line).name for line in diff.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return None


def _new_migration_files() -> set[str] | None:
    """Filenames added under alembic/versions/ on this branch (not present at
    the merge-base with origin/main)."""
    return _changed_migration_files("A")


def _modified_migration_files() -> set[str] | None:
    """Filenames under alembic/versions/ that already existed at the
    merge-base with origin/main and were edited on this branch."""
    return _changed_migration_files("M")


def _file_at_merge_base(name: str) -> str | None:
    """Source of alembic/versions/<name> as it stood at the merge-base with
    origin/main, or None if it can't be resolved (no merge-base, or the file
    didn't exist there — e.g. a rename this diff-filter doesn't track).
    """
    base_sha = _merge_base_sha()
    if base_sha is None:
        return None
    try:
        show = subprocess.run(
            ["git", "show", f"{base_sha}:backend/alembic/versions/{name}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        if show.returncode != 0:
            return None
        return show.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _is_drop_column_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "drop_column"
    )


def _is_raw_drop_column_execute(node: ast.AST) -> bool:
    """True for `op.execute("ALTER TABLE ... DROP COLUMN ...")` — raw SQL has
    no `op.drop_column(...)` call for the AST walk to find, so this looks
    inside every `op.execute(...)`'s string-literal argument(s) instead.
    Only literal strings are inspected (an f-string or a variable can't be
    evaluated statically) — that's a documented gap, not silently ignored;
    see the module docstring.
    """
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    ):
        return False
    for arg in node.args:
        try:
            value = ast.literal_eval(arg)
        except (ValueError, TypeError):
            continue
        if isinstance(value, str) and _DROP_COLUMN_SQL_RE.search(value):
            return True
    return False


def _calls_drop_column(source: str) -> bool:
    """True if upgrade() drops a column — directly, via raw SQL passed to
    op.execute(...), or via a module-level helper function upgrade() calls
    (transitively).

    Walks the call graph starting at upgrade() instead of only scanning its
    own body: a drop hidden behind a local helper — a module-level
    `def _drop_old_columns(): op.drop_column(...)` that `upgrade()` merely
    calls — otherwise sails through untouched, since ast.walk(upgrade_fn)
    alone never descends into a separately-defined function.

    Deliberately does NOT follow calls into imported/library code (e.g.
    op.* itself, or a helper imported from another module) — only functions
    defined at module level in this same file. A drop hidden behind a
    cross-module helper is a real gap; see the module docstring.

    AST, not a string search: a docstring or comment mentioning
    "drop_column" (as several migrations in this repo do, to explain why
    they deliberately DON'T) must not trip this.
    """
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    upgrade_fn = functions.get("upgrade")
    if upgrade_fn is None:
        return False

    seen: set[str] = set()
    stack = [upgrade_fn]
    while stack:
        fn = stack.pop()
        if fn.name in seen:
            continue
        seen.add(fn.name)
        for node in ast.walk(fn):
            if _is_drop_column_call(node) or _is_raw_drop_column_execute(node):
                return True
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in functions
                and node.func.id not in seen
            ):
                stack.append(functions[node.func.id])
    return False


def test_destructive_migration_not_introduced_alongside_its_own_expand_step() -> None:
    """A migration that drops a column must not be new on this branch at the
    same time as the migration it revises (its own `down_revision`, i.e. the
    expand step it depends on) is ALSO new on this branch.

    That combination is exactly what turned the 0201/0202 split (task
    993840da) into a no-op (task cab1bfdd): 0202 (drop `agents.language`)
    revised 0201 (add the replacement columns), both landed in the same PR,
    0202 was the alembic head, and `docker-entrypoint.sh` runs `alembic
    upgrade head` on every deploy — so the very first deploy ran both back
    to back and dropped the column anyway. A destructive migration only
    achieves anything as a *separate* deploy if the expand step it depends
    on already shipped, which means that expand step must already be on
    main — not introduced in the same branch.

    Deliberately narrow: this does NOT forbid a destructive migration next
    to unrelated new migrations, and does NOT forbid two expand-only
    migrations landing together — either is fine. It only fires on the
    specific shape that broke the 0201/0202 split: new destructive
    migration + its own new parent, together.

    Skips (does not fail) when origin/main can't be diffed against — this
    guard only has meaning in a PR/branch context, not in a checkout with
    no remote history.
    """
    new_files = _new_migration_files()
    if new_files is None:
        pytest.skip("origin/main not reachable to diff against — no branch context to guard")
    if not new_files:
        pytest.skip("no new migrations on this branch")

    entries = {name: (rev, parents) for name, rev, parents in _parse()}
    file_by_rev = {rev: name for name, (rev, _parents) in entries.items()}

    violations = []
    for name in sorted(new_files):
        if name not in entries:
            continue  # renamed away / not a versions file we can parse
        rev, parents = entries[name]
        source = (VERSIONS / name).read_text(encoding="utf-8")
        if not _calls_drop_column(source):
            continue
        for parent_rev in parents:
            parent_file = file_by_rev.get(parent_rev)
            if parent_file is not None and parent_file in new_files:
                violations.append(
                    f"{name} drops a column and its own down_revision "
                    f"{parent_file!r} is new on this branch too — both would "
                    "run in the same `alembic upgrade head` deploy. Move the "
                    "destructive migration to its own, later PR."
                )

    assert not violations, "\n".join(violations)


def test_destructive_drop_not_added_to_an_existing_migration() -> None:
    """A migration file that already existed on origin/main must not be
    edited on this branch to newly introduce a column drop — not via
    op.drop_column(...), not via raw SQL, not via a helper function.

    This is the same failure family as
    test_destructive_migration_not_introduced_alongside_its_own_expand_step
    above, reached a different way: that guard only looks at files ADDED on
    this branch (`--diff-filter=A`), so editing an already-merged migration
    to retroactively become destructive walks straight through it — the
    file's revision id doesn't change, nothing marks it as newly dangerous,
    and a database that already applied the old (non-destructive) version of
    this revision picks up the drop silently on its next `alembic upgrade
    head`.

    Compares drop_column-status at the merge-base version of the file
    against HEAD's version, not the raw diff text — only an edit that flips
    the file from non-destructive to destructive is a violation. A file
    that was already destructive at the merge-base (rare, but not this
    guard's job to re-litigate) or an edit that leaves its drop_column
    status unchanged (e.g. a comment fix) does not fire.

    Skips (does not fail) when origin/main can't be diffed against, same as
    the guard above — see this module's docstring for what that implies in
    CI specifically.
    """
    modified_files = _modified_migration_files()
    if modified_files is None:
        pytest.skip("origin/main not reachable to diff against — no branch context to guard")
    if not modified_files:
        pytest.skip("no modified migrations on this branch")

    violations = []
    for name in sorted(modified_files):
        head_path = VERSIONS / name
        if not head_path.exists():
            continue  # renamed/deleted on this branch — not this guard's shape
        head_source = head_path.read_text(encoding="utf-8")
        if not _calls_drop_column(head_source):
            continue
        base_source = _file_at_merge_base(name)
        if base_source is not None and _calls_drop_column(base_source):
            continue  # already destructive before this branch touched it
        violations.append(
            f"{name} already exists on origin/main and was edited on this "
            "branch to newly introduce a column drop. An already-shipped "
            "migration's behavior must not change retroactively — put the "
            "drop in a new migration instead."
        )

    assert not violations, "\n".join(violations)


def test_ci_cannot_silently_skip_the_destructive_migration_guards() -> None:
    """In CI, `origin/main` must be resolvable via `git merge-base` —
    otherwise the two destructive-migration guards above take their `skip`
    branch instead of running, and the job goes green having checked
    nothing. That is not a hypothetical: `actions/checkout@v4` without
    `fetch-depth: 0` fetches only the PR's merge ref, `origin/main` is never
    present locally, and the guards skip every single time (a
    depth-1 repro, task 3f249d2b: 205 passed / 1 skipped / job green — CI
    reads as fully green either way).

    Gated on the `CI` env var (set to `"true"` on every GitHub Actions
    runner) rather than always running: a local checkout with no `origin`
    remote configured at all is a legitimate, unguarded context — this test
    only asserts that the one place the guards are supposed to have teeth
    (the CI job) actually gives them the history to work with.

    Deliberately does not merely re-skip like the guards it's protecting —
    a skip here would be the exact same silent-disable failure one level up.
    If this fails, the fix is the CI checkout step's fetch-depth, not this
    test.
    """
    if os.environ.get("CI") != "true":
        pytest.skip("only meaningful in CI, where origin/main must be fetched")

    assert _merge_base_sha() is not None, (
        "origin/main is not resolvable via `git merge-base` in this CI job — "
        "the destructive-migration guards in this module will silently SKIP "
        "instead of running. Check the checkout step has `fetch-depth: 0` "
        "(actions/checkout@v4's default is a shallow, single-ref clone, which "
        "never fetches origin/main at all)."
    )
