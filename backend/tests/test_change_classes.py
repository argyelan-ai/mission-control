"""Change classes (ADR-089 §2) — the contract the merge and deploy gates will
stand on.

The table in `app.services.change_classes` is the single source for "what
kind of change is this file". Three properties are pinned here, each against
a specific way the gate could go soft:

  1. **Total:** every tracked file resolves to exactly one known class. A new
     folder nobody classified fails here instead of slipping through as
     "probably harmless".
  2. **Unknown is L3:** a path on no list resolves to the unknown class, and
     that class asks the operator.
  3. **Strictest wins:** a change is as strict as its strictest file, a
     narrow strict rule beats a broad loose one, and a rename counts both
     paths.
"""

import os
import subprocess
from pathlib import Path

import pytest

from app.services.autonomy import AUTONOMY_DEFAULTS
from app.services.change_classes import (
    ACTION_CLASSES,
    CHANGE_CLASSES,
    DATA,
    DECISIONS,
    DOCS_TESTS,
    INFRA,
    INVISIBLE,
    MERGE_DOCS_TESTS,
    MERGE_INVISIBLE,
    DEPLOY_INVISIBLE,
    RUNTIME_SWITCH,
    SECURITY_CORE,
    UNKNOWN,
    VISIBLE_UI,
    classes_for_paths,
    classify_path,
    classify_pr_files,
    matching_classes,
    strictest_class,
    unclassified_paths,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _tracked_files() -> list[str]:
    root = _repo_root()
    if not (root / ".git").exists():
        # A copy without git metadata (e.g. inside a container build) has no
        # notion of "tracked". CI always checks out with git, so never skip there.
        if os.environ.get("CI"):
            pytest.fail("CI run without a git checkout — the contract cannot be checked")
        pytest.skip("not a git checkout; the tracked-file contract needs `git ls-files`")
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True,
    ).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


# ── 1. Total: every tracked file has exactly one known class ──────────────


def test_every_tracked_file_maps_to_exactly_one_known_class():
    files = _tracked_files()
    assert len(files) > 1000, f"suspiciously few tracked files: {len(files)}"
    missing = unclassified_paths(files)
    assert missing == [], (
        f"{len(missing)} tracked file(s) on no class list — add them to the "
        f"table in backend/app/services/change_classes.py: {missing[:20]}"
    )
    for path in files:
        assert classify_path(path) in CHANGE_CLASSES, path


def test_every_rule_pattern_matches_at_least_one_tracked_file():
    """A pattern that matches nothing is a typo or a dead rule — and a typo in
    a strict class silently loosens the gate for the path it meant."""
    from app.services.change_classes import _compile

    files = _tracked_files()
    dead = []
    for cls in CHANGE_CLASSES:
        for pattern in (*cls.patterns, *cls.exclude):
            rx = _compile(pattern)
            if not any(rx.fullmatch(f) for f in files):
                dead.append((cls.key, pattern))
    assert dead == [], f"patterns matching no tracked file: {dead}"


def test_mutation_guard_an_unclassified_path_is_reported():
    """Sabotage built in: a path on no list must show up as unclassified."""
    files = ["backend/app/main.py", "brand-new-folder/thing.py", "README.md"]
    assert unclassified_paths(files) == ["brand-new-folder/thing.py"]


# ── 2. Unknown is L3 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "brand-new-folder/thing.py",
    "some.weird.root.file",
    "frontend-v3/src/app/page.tsx",
    "",
])
def test_a_path_on_no_list_is_unknown_and_l3(path):
    cls = classify_path(path)
    assert cls is UNKNOWN
    assert cls.level == "L3"
    assert matching_classes(path) == []


def test_unknown_and_the_hard_classes_cannot_be_lowered():
    assert UNKNOWN.lowerable is False
    assert SECURITY_CORE.lowerable is False
    assert DECISIONS.lowerable is False
    # The two classes the operator may pre-approve (ADR-089 §2):
    assert INFRA.lowerable is True
    assert DATA.lowerable is True


# ── 3. Pinned examples from the ADR-089 §2 table ──────────────────────────


@pytest.mark.parametrize("path,expected", [
    # docs and tests only
    ("docs/journeys.md", DOCS_TESTS),
    ("backend/tests/test_operations.py", DOCS_TESTS),
    ("e2e/journeys/phone-needs-you.spec.ts", DOCS_TESTS),
    ("frontend-v2/src/components/chat/ChatView.test.tsx", DOCS_TESTS),
    ("frontend-v2/src/lib/__tests__/format.test.ts", DOCS_TESTS),
    ("README.md", DOCS_TESTS),
    # invisible fix
    ("backend/app/services/operations.py", INVISIBLE),
    ("backend/app/routers/tasks.py", INVISIBLE),
    ("scripts/mc-cli/mc", INVISIBLE),
    # visible UI
    ("frontend-v2/src/app/settings/page.tsx", VISIBLE_UI),
    ("frontend-v2/src/components/layout/AppShell.tsx", VISIBLE_UI),
    ("frontend-v2/messages/de.json", VISIBLE_UI),
    # decisions and rules
    ("docs/decisions/089-lead-agent-with-hands.md", DECISIONS),
    ("docs/decisions/README.md", DECISIONS),
    ("docs/PRINCIPLES.md", DECISIONS),
    ("AGENTS.md", DECISIONS),
    # data
    ("backend/alembic/versions/0201_operator_language.py", DATA),
    # infra, deploy path, fleet
    ("docker/omp-bridge/bridge.py", INFRA),
    ("docker-compose.yml", INFRA),
    ("scripts/head/mc-head", INFRA),
    ("scripts/start-all.sh", INFRA),
    ("backend/Dockerfile", INFRA),
    # security core
    ("backend/app/services/autonomy.py", SECURITY_CORE),
    ("backend/app/services/change_classes.py", SECURITY_CORE),
    ("backend/app/scopes.py", SECURITY_CORE),
    ("backend/app/auth.py", SECURITY_CORE),
    ("backend/app/services/adr_gate.py", SECURITY_CORE),
    (".github/workflows/ci.yml", SECURITY_CORE),
    ("backend/tests/test_change_classes.py", SECURITY_CORE),
    ("backend/tests/test_adr_gate.py", SECURITY_CORE),
    ("scripts/privacy-scan.py", SECURITY_CORE),
    ("docs/produkt/luecken-basis.json", SECURITY_CORE),
    ("frontend-v2/scripts/design/ratchet-baseline.json", SECURITY_CORE),
])
def test_adr_examples_map_to_their_class(path, expected):
    assert classify_path(path) is expected, (path, classify_path(path).key)


def test_paths_are_normalised_like_the_adr_gate_does():
    assert classify_path("./backend/app/auth.py") is SECURITY_CORE
    assert classify_path("/backend/app/auth.py") is SECURITY_CORE
    assert classify_path("backend\\app\\auth.py") is SECURITY_CORE


# ── 4. Strictest wins ─────────────────────────────────────────────────────


def test_a_narrow_strict_rule_beats_a_broad_loose_one():
    """`backend/app/**` is invisible, `backend/app/auth.py` is security core:
    the file matches both and must land in the stricter one."""
    assert set(matching_classes("backend/app/auth.py")) == {INVISIBLE, SECURITY_CORE}
    assert classify_path("backend/app/auth.py") is SECURITY_CORE
    # scripts/** is invisible, scripts/head/** is infra.
    assert classify_path("scripts/head/mc-head") is INFRA
    # docs/** is docs, docs/decisions/** is decisions.
    assert classify_path("docs/decisions/085-head-per-job.md") is DECISIONS


def test_a_change_is_as_strict_as_its_strictest_file():
    assert strictest_class(["docs/journeys.md", "backend/tests/test_x.py"]) is DOCS_TESTS
    assert strictest_class(["docs/journeys.md", "backend/app/routers/tasks.py"]) is INVISIBLE
    assert strictest_class([
        "backend/app/routers/tasks.py", "frontend-v2/src/app/settings/page.tsx",
    ]) is VISIBLE_UI
    assert strictest_class([
        "frontend-v2/src/app/settings/page.tsx", "docker-compose.yml",
    ]) is INFRA
    assert strictest_class([
        "docker-compose.yml", "backend/alembic/versions/0300_x.py",
    ]) is DATA
    assert strictest_class(["backend/alembic/versions/0300_x.py", "new-dir/x"]) is UNKNOWN
    assert strictest_class(["new-dir/x", "docs/PRINCIPLES.md"]) is DECISIONS
    assert strictest_class(["docs/PRINCIPLES.md", "backend/app/auth.py"]) is SECURITY_CORE


def test_an_empty_change_is_never_treated_as_harmless():
    assert strictest_class([]) is UNKNOWN


def test_classes_for_paths_keeps_every_class_present():
    """The gate needs every class, not only the strictest: a PR with UI and
    infra needs the picture *and* the go."""
    got = classes_for_paths([
        "frontend-v2/src/app/settings/page.tsx", "docker-compose.yml", "docs/x.md",
    ])
    assert got == [INFRA, VISIBLE_UI, DOCS_TESTS]


def test_a_rename_counts_both_paths():
    """Moving a security-core file into a loose folder is a security-core
    change, not a docs change — same rule the ADR gate applies to renames."""
    files = [("docs/notes/auth.py", "backend/app/auth.py"), ("docs/journeys.md", None)]
    assert classify_pr_files(files) is SECURITY_CORE


# ── 5. The table itself ───────────────────────────────────────────────────


def test_strictness_is_a_total_order_and_levels_follow_it():
    ranks = [c.rank for c in CHANGE_CLASSES]
    assert len(set(ranks)) == len(ranks), "two classes with the same rank"
    order = {"L1": 1, "L2": 2, "L3": 3}
    by_rank = sorted(CHANGE_CLASSES, key=lambda c: c.rank)
    levels = [order[c.level] for c in by_rank]
    assert levels == sorted(levels), "a stricter class with a lower level"
    assert by_rank[0] is DOCS_TESTS and by_rank[-1] is SECURITY_CORE
    keys = [c.key for c in CHANGE_CLASSES + ACTION_CLASSES]
    assert len(set(keys)) == len(keys)


def test_levels_and_types_match_adr_089():
    assert (DOCS_TESTS.level, DOCS_TESTS.autonomy_types) == ("L1", (MERGE_DOCS_TESTS,))
    assert (INVISIBLE.level, INVISIBLE.autonomy_types) == (
        "L2", (MERGE_INVISIBLE, DEPLOY_INVISIBLE),
    )
    assert (VISIBLE_UI.level, VISIBLE_UI.autonomy_types) == ("L3", ("visual_review",))
    assert (DECISIONS.level, DECISIONS.autonomy_types) == ("L3", ("adr_gate",))
    assert (DATA.level, DATA.autonomy_types) == ("L3", ("deploy",))
    assert (INFRA.level, INFRA.autonomy_types) == ("L3", ("config_change", "deploy"))
    assert SECURITY_CORE.level == "L3"
    assert (RUNTIME_SWITCH.level, RUNTIME_SWITCH.patterns) == ("L2", ())
    assert RUNTIME_SWITCH not in CHANGE_CLASSES, "an action is not a path class"


def test_existing_autonomy_types_it_names_really_exist():
    """visual_review, deploy and config_change are reused, not invented.
    The four new types arrive with step B3; until then the autonomy service
    resolves them as unknown = L3, which is the safe side."""
    from app.services.adr_gate import ADR_GATE_ACTION_TYPE

    for cls in CHANGE_CLASSES:
        for t in cls.autonomy_types:
            if t in (MERGE_DOCS_TESTS, MERGE_INVISIBLE, DEPLOY_INVISIBLE):
                continue
            assert t in AUTONOMY_DEFAULTS or t == ADR_GATE_ACTION_TYPE, (cls.key, t)
