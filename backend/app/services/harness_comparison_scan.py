"""Scanner for the harness/runtime_type comparison ratchet (PRINCIPLES §3.4).

PRINCIPLES §3.4 says the backend knows no harness details except inside the
existing harness layer (``harness_compat.py``, ``harness_catalog.py``) — start
commands and harness-name branching live there, nowhere else. This module
counts direct string comparisons against a harness or ``runtime_type`` value
(``== "omp"``, ``in ("claude", ...)``, ...) outside that layer, per file, so a
ratchet test can fail when a new one is added and point at exactly where.

This is a plain line-pattern scan, not an AST walk: it is meant to catch the
shapes the operator named as examples, not to be a perfect static analyser.
It only ever under-counts odd formatting, which only makes the ratchet err on
the lenient side.
"""
from __future__ import annotations

import re
from pathlib import Path

# Files that make up the harness layer PRINCIPLES §3.4 and ARCHITECTURE.md
# name as the one exception — harness-name branching is expected there.
ALLOWED_LAYER = {
    "backend/app/services/harness_compat.py",
    "backend/app/services/harness_catalog.py",
}

# Trees this ratchet watches. Backend first (PRINCIPLES §3.4's explicit
# subject); frontend-v2/src is included too since the same leak shows up
# there as scattered harness-name string checks.
WATCHED_TREES = (
    "backend/app",
    "frontend-v2/src",
)

# File types worth scanning in each tree.
SUFFIXES = {".py", ".ts", ".tsx"}

# Never scan test files or this scanner's own fixtures/tests — they legitimately
# reference harness names as data, not as branching logic.
EXCLUDE_NAME_PARTS = ("__tests__", "/tests/", ".test.", "_test.py", "test_")

_PATTERN = re.compile(
    r'\b(harness|runtime_type)\s*(===?|!==?)\s*["\']'
    r'|["\']\s*(===?|!==?)\s*[\w.]*\.(harness|runtime_type)\b'
    r'|\b(harness|runtime_type)\s+(in|not in)\s*[\(\[\{]'
)


def _is_excluded(rel_path: str) -> bool:
    if rel_path in ALLOWED_LAYER:
        return True
    return any(part in rel_path for part in EXCLUDE_NAME_PARTS)


def scan_repo(repo_root: Path) -> dict[str, int]:
    """Count harness/runtime_type literal comparisons per file.

    Returns {relative_path: count} for every non-excluded, non-zero file
    under the watched trees, with paths relative to ``repo_root`` using
    forward slashes (stable across platforms, stable in the baseline file).
    """
    counts: dict[str, int] = {}
    for tree in WATCHED_TREES:
        tree_path = repo_root / tree
        if not tree_path.is_dir():
            continue
        for path in sorted(tree_path.rglob("*")):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            rel = path.relative_to(repo_root).as_posix()
            if _is_excluded(rel):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            n = len(_PATTERN.findall(text))
            if n:
                counts[rel] = n
    return counts
