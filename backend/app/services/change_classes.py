"""Change classes — what kind of change a file is (ADR-089 §2).

ADR-089 lets the lead agent merge, deploy and switch runtimes on its own only
inside a gate that classifies the change by the paths it touches and applies
MC's existing autonomy levels (L1 do · L2 do and report · L3 ask and wait).
This module is the **single source** for that classification: the ADR's
table shows examples; the table below is the rule. No caller yet — the merge
gate (step B9) and the deploy gate (step B11) will ask it.

Three rules, each closing a specific way the gate could go soft:

  1. **Strictest wins, per file and per change.** A file may match several
     classes (`backend/app/**` is an invisible fix, `backend/app/auth.py` is
     security core); it lands in the strictest. So a broad loose pattern can
     never loosen a narrow strict one, and adding a pattern can only make the
     gate stricter for the files it overlaps. A change (a PR) is as strict as
     its strictest file (ADR-089 R1).
  2. **Unknown is L3.** A path on no list resolves to `UNKNOWN`, which asks.
     The contract test (`backend/tests/test_change_classes.py`) walks every
     tracked file and fails on the first one that is unknown, so a new folder
     gets classified on purpose, never by accident.
  3. **A rename counts both paths** — the same rule the ADR gate applies:
     moving a security-core file into a docs folder is a security-core change.

Paths are normalised the way the ADR gate normalises them
(`decision_docs.normalize_repo_path`). Patterns are repo-relative globs:
`*` stays inside one folder, `**` crosses folders, `**/` may match no folder.

The levels here are ADR-089's defaults. Who may lower which class lives here
too (`lowerable`); the effective level per autonomy type is the autonomy
service's job (step B3 adds the four new types there — until then they
resolve as unknown types, which the autonomy service treats as L3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from app.services.decision_docs import normalize_repo_path

# New autonomy types introduced by ADR-089 (registered in step B3).
MERGE_DOCS_TESTS = "merge_docs_tests"
MERGE_INVISIBLE = "merge_invisible"
DEPLOY_INVISIBLE = "deploy_invisible"
RUNTIME_SWITCH_TYPE = "runtime_switch"


@dataclass(frozen=True)
class ChangeClass:
    key: str
    # Higher = stricter. Unique per class; levels never decrease with rank.
    rank: int
    # ADR-089 default level: "L1" do · "L2" do and report · "L3" ask and wait.
    level: str
    # Autonomy action types the gates ask for this class.
    autonomy_types: tuple[str, ...]
    # False = the operator cannot pre-approve the class down to L2.
    lowerable: bool
    # What the lead agent does (ADR-089 §2, last column).
    does: str
    patterns: tuple[str, ...] = ()
    # Paths inside `patterns` that do NOT belong to this class.
    exclude: tuple[str, ...] = ()


# ── Path classes, loosest first ──────────────────────────────────────────

DOCS_TESTS = ChangeClass(
    key="docs_tests",
    rank=10,
    level="L1",
    autonomy_types=(MERGE_DOCS_TESTS,),
    lowerable=True,
    does="merges when CI is green; listed in the daily report",
    patterns=(
        "*.md",
        "docs/**",
        "frontend-v2/docs/**",
        ".superpowers/**",
        ".impeccable/**",
        "backend/tests/**",
        "e2e/**",
        "frontend-v2/src/**/__tests__/**",
        "frontend-v2/src/**/__fixtures__/**",
        "frontend-v2/src/**/*.test.ts",
        "frontend-v2/src/**/*.test.tsx",
        "frontend-v2/src/test-setup.ts",
        "frontend-v2/playwright/**",
        "frontend-v2/scripts/**",
        "frontend-v2/vitest.config.ts",
    ),
)

INVISIBLE = ChangeClass(
    key="invisible",
    rank=20,
    level="L2",
    autonomy_types=(MERGE_INVISIBLE, DEPLOY_INVISIBLE),
    lowerable=True,
    does="merges and deploys, then reports with proof of effect and an undo",
    patterns=(
        "backend/app/**",
        "backend/tools/**",
        "backend/openapi.json",
        "scripts/**",
        "tools/**",
        "jarvis_core/**",
        "voice_worker/**",
    ),
)

VISIBLE_UI = ChangeClass(
    key="visible_ui",
    rank=30,
    level="L3",
    autonomy_types=("visual_review",),
    lowerable=True,
    does="sends pictures (390 px, both languages, both themes); merges and "
    "deploys only after the operator approves the picture",
    patterns=(
        "frontend-v2/src/**",
        "frontend-v2/messages/**",
        "frontend-v2/public/**",
    ),
    # Tests inside the UI tree are tests, not UI.
    exclude=(
        "frontend-v2/src/**/__tests__/**",
        "frontend-v2/src/**/__fixtures__/**",
        "frontend-v2/src/**/*.test.ts",
        "frontend-v2/src/**/*.test.tsx",
        "frontend-v2/src/test-setup.ts",
    ),
)

INFRA = ChangeClass(
    key="infra",
    rank=40,
    level="L3",
    autonomy_types=("config_change", "deploy"),
    lowerable=True,
    does="announces and waits for go, unless the operator pre-approved the "
    "class (Settings → Autonomy: L2)",
    patterns=(
        # containers and compose
        "docker/**",
        "docker-compose*.yml",
        "**/Dockerfile",
        ".dockerignore",
        "backend/docker-entrypoint.sh",
        "e2e/stack/**",
        # host, heads, fleet and GPU boxes
        "scripts/head/**",
        "scripts/start-all.sh",
        "scripts/launchd/**",
        "scripts/node-agent/**",
        "scripts/device/**",
        "scripts/stt-server/**",
        "scripts/install-*.sh",
        "scripts/init-*.sh",
        "tools/*.plist",
        "e2e/*.plist.template",
        "e2e/install-journeys.sh",
        "backend/config/**",
        # install, proxy, database server, distribution
        "install.sh",
        "setup.sh",
        "setup.ps1",
        "Makefile",
        "Caddyfile",
        "caddy/**",
        "pg_hba.conf",
        ".env.example",
        "deploy/**",
        # what gets committed and how
        "**/.gitignore",
        ".gitattributes",
        # dependencies and build configuration
        "backend/pyproject.toml",
        "backend/requirements.lock",
        "voice_worker/requirements.txt",
        "frontend-v2/package.json",
        "frontend-v2/package-lock.json",
        "frontend-v2/next.config.ts",
        "frontend-v2/next-env.d.ts",
        "frontend-v2/postcss.config.mjs",
        "frontend-v2/tsconfig.json",
        "e2e/package.json",
        "e2e/package-lock.json",
    ),
)

DATA = ChangeClass(
    key="data",
    rank=50,
    level="L3",
    autonomy_types=("deploy",),
    lowerable=True,
    does="announces with a backup proof and waits for go",
    patterns=(
        "backend/alembic/**",
        "backend/alembic.ini",
        "backend/scripts/**",
        "backup.sh",
        "scripts/schedule-backup.sh",
        "scripts/migrate-*",
        "scripts/vault-cleanup-*",
    ),
)

# Not a list of paths: everything no other class claims.
UNKNOWN = ChangeClass(
    key="unknown",
    rank=60,
    level="L3",
    autonomy_types=(),
    lowerable=False,
    does="asks; the contract test reports the new path",
)

DECISIONS = ChangeClass(
    key="decisions",
    rank=70,
    level="L3",
    autonomy_types=("adr_gate",),
    lowerable=False,
    does="the operator approves this exact text, then it merges",
    patterns=(
        "docs/decisions/**",
        "docs/PRINCIPLES.md",
        "docs/ROADMAP.md",
        "docs/design/ui-craft.md",
        "docs/produkt/regeln.yaml",
        "AGENTS.md",
        "CLAUDE.md",
        "DESIGN.md",
        "PRODUCT.md",
        "LICENSE",
        # agent instruction cards are rendered from here (ADR-006), the lead
        # agent's own card included (ADR-089 §5)
        "backend/templates/**",
    ),
)

SECURITY_CORE = ChangeClass(
    key="security_core",
    rank=80,
    level="L3",
    autonomy_types=(),
    lowerable=False,
    does="only with the operator's approval; cannot be lowered",
    patterns=(
        # the gate itself
        "backend/app/services/autonomy.py",
        "backend/app/services/change_classes.py",
        "backend/tests/test_change_classes.py",
        "backend/app/services/adr_gate.py",
        "backend/app/services/decision_docs.py",
        "backend/app/scopes.py",
        "backend/app/auth.py",
        # the tests that guard it — weakening one is weakening the gate
        "backend/tests/test_*autonomy*.py",
        "backend/tests/test_adr_gate.py",
        "backend/tests/test_decision_docs.py",
        "backend/tests/test_privacy_scan.py",
        # checks and their baselines (never-list: no editing rules or checks)
        ".github/**",
        ".gitleaks.toml",
        ".kohaerenz.yaml",
        "scripts/privacy-scan.py",
        "docs/privacy-sweep-backlog.md",
        "docs/produkt/luecken-basis.json",
        "frontend-v2/scripts/design/**",
        "tools/hooks/**",
    ),
)

# Every path class plus UNKNOWN, loosest first.
CHANGE_CLASSES: list[ChangeClass] = [
    DOCS_TESTS, INVISIBLE, VISIBLE_UI, INFRA, DATA, UNKNOWN, DECISIONS, SECURITY_CORE,
]

# ── Action classes: no paths, the gate classifies the action itself ──────

RUNTIME_SWITCH = ChangeClass(
    key="runtime_switch",
    rank=25,
    level="L2",
    autonomy_types=(RUNTIME_SWITCH_TYPE,),
    lowerable=True,
    does="switches when the box is free and no head or night job runs; "
    "otherwise waits",
)

ACTION_CLASSES: list[ChangeClass] = [RUNTIME_SWITCH]


# ── Matching ──────────────────────────────────────────────────────────────


@lru_cache(maxsize=None)
def _compile(pattern: str) -> re.Pattern[str]:
    """Repo glob → regex. `**/` = zero or more folders, `**` = anything,
    `*` = anything inside one folder, `?` = one character."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _matches(cls: ChangeClass, path: str) -> bool:
    if not any(_compile(p).fullmatch(path) for p in cls.patterns):
        return False
    return not any(_compile(p).fullmatch(path) for p in cls.exclude)


def matching_classes(path: str) -> list[ChangeClass]:
    """Every path class that claims `path` (UNKNOWN never claims anything)."""
    clean = normalize_repo_path(path)
    if not clean:
        return []
    return [c for c in CHANGE_CLASSES if c.patterns and _matches(c, clean)]


# rule: R-change-class-total - strictest claiming class wins; no claim = UNKNOWN (L3)
def classify_path(path: str) -> ChangeClass:
    """The class of one file: the strictest claiming class, else UNKNOWN."""
    claims = matching_classes(path)
    if not claims:
        return UNKNOWN
    return max(claims, key=lambda c: c.rank)


def classes_for_paths(paths: Iterable[str]) -> list[ChangeClass]:
    """Every class present in a change, strictest first. A gate needs all of
    them (a PR with UI and infra needs the picture and the go), not only the
    strictest."""
    found = {classify_path(p).key: classify_path(p) for p in paths}
    return sorted(found.values(), key=lambda c: c.rank, reverse=True)


def strictest_class(paths: Iterable[str]) -> ChangeClass:
    """ADR-089 R1. An empty change is UNKNOWN — "nothing to classify" must
    never read as "harmless"."""
    present = classes_for_paths(paths)
    return present[0] if present else UNKNOWN


def classify_pr_files(files: Iterable[tuple[str, str | None]]) -> ChangeClass:
    """Strictest class over a PR's `(path, previous_path)` list — the shape
    `adr_gate.fetch_pr_files` returns. A rename counts its old path too."""
    paths: list[str] = []
    for path, previous in files:
        paths.append(path)
        if previous:
            paths.append(previous)
    return strictest_class(paths)


def unclassified_paths(paths: Iterable[str]) -> list[str]:
    """The paths no class claims, in input order — the contract test's
    failure list."""
    return [p for p in paths if not matching_classes(p)]
