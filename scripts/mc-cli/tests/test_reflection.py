"""Tests for mc_cli.reflection — the tolerant close-contract normalizer.

This is the SINGLE in-container source of truth the `mc` CLI and the omp
bridge both enforce (docker/omp-bridge/bridge.py mirrors it; a parity test in
docker/omp-bridge/tests/ asserts they agree). Strict/canonical inputs must
still pass byte-identical; the tolerance is purely additive so local models
(Qwen/DeepSeek) that emit trivial variance are no longer wrongly blocked.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import reflection as R  # noqa: E402


CANONICAL = (
    "## Was wurde gemacht\nFeature X gebaut und getestet.\n\n"
    "## Was hat funktioniert\nTDD-Loop lief sauber durch.\n\n"
    "## Was war unklar\nScope der Edge-Cases war offen.\n\n"
    "## Lesson fuer Agent-Memory\nImmer zuerst die Vertraege pruefen.\n"
    "TASK_COMPLETE"
)


# ── Sentinel tolerance ─────────────────────────────────────────────────────

def test_sentinel_canonical_last_line():
    assert R.sentinel_present("bla\nTASK_COMPLETE") is True


def test_sentinel_lowercase():
    assert R.sentinel_present("bla\ntask_complete") is True


def test_sentinel_bold_markdown():
    assert R.sentinel_present("bla\n**TASK_COMPLETE**") is True


def test_sentinel_backticks():
    assert R.sentinel_present("bla\n`TASK_COMPLETE`") is True


def test_sentinel_trailing_punctuation():
    assert R.sentinel_present("bla\nTASK_COMPLETE.") is True


def test_sentinel_then_trailing_rule():
    assert R.sentinel_present("bla\nTASK_COMPLETE\n---") is True


def test_sentinel_absent():
    assert R.sentinel_present("bla\nDone.") is False


def test_sentinel_not_alone_on_line_still_ok_if_only_markdown():
    # A sentinel wrapped only in emphasis is still the sentinel.
    assert R.sentinel_present("x\n__TASK_COMPLETE__") is True


def test_sentinel_embedded_in_sentence_is_not_accepted():
    assert R.sentinel_present("Ich schreibe TASK_COMPLETE wenn fertig.") is False


# ── Header tolerance + normalization ───────────────────────────────────────

def test_extract_canonical_unchanged():
    block = R.extract_reflection(CANONICAL)
    assert block is not None
    assert "## Was wurde gemacht" in block
    assert "## Lesson fuer Agent-Memory" in block
    assert "TASK_COMPLETE" not in block


def test_extract_hash_level_variants_normalized_to_double_hash():
    text = (
        "# Was wurde gemacht\na\n"
        "### Was hat funktioniert\nb\n"
        "## Was war unklar\nc\n"
        "#### Lesson fuer Agent-Memory\nd\n"
        "TASK_COMPLETE"
    )
    block = R.extract_reflection(text)
    # #### is level>3, not a recognised header -> only 3 canonical, but the
    # three valid ones are normalized to ## level.
    assert block.count("## Was wurde gemacht") == 1
    assert block.startswith("## Was wurde gemacht")


def test_extract_english_headers_normalized_to_german():
    text = (
        "## What was done\nBuilt the parser.\n"
        "## What worked\nThe TDD loop.\n"
        "## What was unclear\nEdge cases.\n"
        "## Lesson for agent memory\nCheck contracts first.\n"
        "TASK_COMPLETE"
    )
    block = R.extract_reflection(text)
    for f in R.REFLECTION_REQUIRED_FIELDS:
        assert f"## {f}" in block, f"missing canonical {f}"
    assert "What was done" not in block
    assert "Lesson for agent memory" not in block


def test_extract_umlaut_fuer_variant():
    text = (
        "## Was wurde gemacht\na aaaaaaaaaaaaaaaaaaaa\n"
        "## Was hat funktioniert\nb bbbbbbbbbbbbbbbbbbbb\n"
        "## Was war unklar\nc cccccccccccccccccccc\n"
        "## Lesson für Agent-Memory\nd dddddddddddddddddddd\n"
        "TASK_COMPLETE"
    )
    block = R.extract_reflection(text)
    assert "## Lesson fuer Agent-Memory" in block


def test_extract_case_insensitive_and_trailing_colon():
    text = (
        "## was wurde gemacht:\na\n"
        "## WAS HAT FUNKTIONIERT :\nb\n"
        "## Was War Unklar\nc\n"
        "## lesson fuer agent-memory:\nd\n"
        "TASK_COMPLETE"
    )
    block = R.extract_reflection(text)
    for f in R.REFLECTION_REQUIRED_FIELDS:
        assert f"## {f}" in block


def test_extract_returns_none_when_no_header():
    assert R.extract_reflection("Just some prose, no headers.\nTASK_COMPLETE") is None


def test_extract_starts_at_first_recognised_header():
    text = "intro babble\n## Was wurde gemacht\na\nTASK_COMPLETE"
    block = R.extract_reflection(text)
    assert block.startswith("## Was wurde gemacht")
    assert "intro babble" not in block


# ── validate_reflection ────────────────────────────────────────────────────

def test_validate_canonical_ok():
    block = R.extract_reflection(CANONICAL)
    assert R.validate_reflection(block) is True


def test_validate_too_short():
    assert R.validate_reflection("## Was wurde gemacht\nx") is False


def test_validate_missing_field():
    block = (
        "## Was wurde gemacht\n" + "a" * 30 + "\n"
        "## Was hat funktioniert\n" + "b" * 30
    )
    assert R.validate_reflection(block) is False


def test_validate_none():
    assert R.validate_reflection(None) is False


# ── normalize_reflection (used by mc finish before POST) ───────────────────

def test_normalize_idempotent_on_canonical():
    body = CANONICAL.replace("\nTASK_COMPLETE", "")
    assert R.normalize_reflection(body) == body


def test_normalize_rewrites_english_to_german():
    text = (
        "## What was done\nBuilt it.\n"
        "## What worked\nAll green.\n"
        "## What was unclear\nNothing.\n"
        "## Lesson for agent memory\nShip it."
    )
    out = R.normalize_reflection(text)
    for f in R.REFLECTION_REQUIRED_FIELDS:
        assert f"## {f}" in out


# ── Drift guard: mc_cli canonical == backend constants ─────────────────────

def test_canonical_matches_backend_constants():
    """The in-container source of truth must not drift from the backend."""
    import re

    root = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
    const_path = os.path.join(root, "backend", "app", "constants.py")
    with open(const_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(
        r"REFLECTION_REQUIRED_FIELDS:\s*list\[str\]\s*=\s*\[(.*?)\]", src, re.S
    )
    assert m, "could not locate REFLECTION_REQUIRED_FIELDS in backend constants"
    backend_fields = re.findall(r'"([^"]+)"', m.group(1))
    assert backend_fields == R.REFLECTION_REQUIRED_FIELDS

    m2 = re.search(r"REFLECTION_MIN_CHARS:\s*int\s*=\s*(\d+)", src)
    assert m2 and int(m2.group(1)) == R.REFLECTION_MIN_CHARS
