#!/usr/bin/env python3
"""B2 drift-guard — bridge.py's MIRRORED close-normalizer must match the
canonical source mc_cli/reflection.py byte-for-byte.

bridge.py cannot cleanly `import mc_cli` in the omp image (different sys.path),
so it duplicates the small sentinel/header normalizer. This test feeds a shared
case matrix through BOTH copies and asserts identical results for the three
public functions (sentinel_present / extract_reflection / validate_reflection).
It also asserts both copies' canonical German fields equal
backend/app/constants.py — so a change in ANY of the three places fails loudly.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.abspath(os.path.join(BRIDGE_DIR, "..", ".."))
MC_CLI_DIR = os.path.join(REPO_ROOT, "scripts", "mc-cli")

sys.path.insert(0, BRIDGE_DIR)
sys.path.insert(0, MC_CLI_DIR)

import bridge  # noqa: E402
from mc_cli import reflection as R  # noqa: E402


CANON = (
    "## Was wurde gemacht\nFeature gebaut und getestet, alles sauber.\n"
    "## Was hat funktioniert\nDer TDD-Loop lief ohne Ueberraschungen durch.\n"
    "## Was war unklar\nDer Scope der Edge-Cases war anfangs offen.\n"
    "## Lesson fuer Agent-Memory\nImmer zuerst die Vertraege pruefen, dann bauen.\n"
    "TASK_COMPLETE"
)

CASES = [
    CANON,
    CANON.replace("TASK_COMPLETE", "task_complete"),
    CANON.replace("TASK_COMPLETE", "**TASK_COMPLETE**"),
    CANON.replace("TASK_COMPLETE", "`TASK_COMPLETE`"),
    CANON.replace("TASK_COMPLETE", "TASK_COMPLETE."),
    CANON + "\n---",
    CANON.replace("## ", "### "),
    CANON.replace("## ", "# "),
    (
        "## What was done\nBuilt and tested end to end.\n"
        "## What worked\nThe TDD loop ran clean.\n"
        "## What was unclear\nEdge-case scope was open.\n"
        "## Lesson for agent memory\nCheck contracts first.\n"
        "TASK_COMPLETE"
    ),
    CANON.replace("Lesson fuer", "Lesson für"),
    "Just prose, no headers.\nTASK_COMPLETE",
    "TASK_COMPLETE",
    CANON.replace("\nTASK_COMPLETE", "\nIch gebe auf."),
    "",
    "## Was wurde gemacht\nnur ein Feld\nTASK_COMPLETE",
    "intro babble\n## Was wurde gemacht\na\nTASK_COMPLETE",
]


def test_sentinel_present_parity():
    for c in CASES:
        assert bridge.sentinel_present(c) == R.sentinel_present(c), repr(c)


def test_extract_reflection_parity():
    for c in CASES:
        assert bridge.extract_reflection(c) == R.extract_reflection(c), repr(c)


def test_validate_reflection_parity():
    for c in CASES:
        b = bridge.extract_reflection(c)
        m = R.extract_reflection(c)
        assert bridge.validate_reflection(b) == R.validate_reflection(m), repr(c)


def test_canonical_fields_parity():
    # bridge REFLECTION_HEADERS carry the `## ` prefix; strip to compare.
    bridge_fields = [h[len("## "):] for h in bridge.REFLECTION_HEADERS]
    assert bridge_fields == R.REFLECTION_REQUIRED_FIELDS
    assert bridge.MIN_REFLECTION_CHARS == R.REFLECTION_MIN_CHARS


def test_both_match_backend_constants():
    const_path = os.path.join(REPO_ROOT, "backend", "app", "constants.py")
    with open(const_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(
        r"REFLECTION_REQUIRED_FIELDS:\s*list\[str\]\s*=\s*\[(.*?)\]", src, re.S
    )
    assert m, "could not locate REFLECTION_REQUIRED_FIELDS in backend constants"
    backend_fields = re.findall(r'"([^"]+)"', m.group(1))
    assert backend_fields == R.REFLECTION_REQUIRED_FIELDS
    assert backend_fields == [h[len("## "):] for h in bridge.REFLECTION_HEADERS]

    m2 = re.search(r"REFLECTION_MIN_CHARS:\s*int\s*=\s*(\d+)", src)
    assert m2 and int(m2.group(1)) == R.REFLECTION_MIN_CHARS
