#!/usr/bin/env python3
"""B1 — forgiving close-parsing for the omp bridge.

Local models (Qwen/DeepSeek) fail the strict close contract on trivial variance
(lowercase sentinel, ``### `` headers, English headers, trailing punctuation,
a trailing ``---``). These used to classify as MALFORMED_REFLECTION /
SILENT_ABORT_NO_SENTINEL -> blocker. The tolerant parser must accept them AND
normalize the reflection to canonical German (so `mc finish` + the memory
pipeline see canonical headers), while a genuinely missing reflection still
fails and strict canonical input still passes unchanged.
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
sys.path.insert(0, BRIDGE_DIR)

import bridge  # noqa: E402
from bridge import Kind  # noqa: E402


def _stop_stream(final_text: str, *, tool_error: bool = False) -> io.StringIO:
    """Build a minimal well-formed stop-stream whose final assistant text is
    `final_text` (the only knob the close contract reads)."""
    lines = [
        {"type": "session", "id": "t-close", "version": 3},
        {"type": "agent_start"},
        {"type": "turn_start"},
    ]
    if tool_error:
        lines.append({
            "type": "tool_execution_end", "toolCallId": "x", "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "boom"}]}, "isError": True,
        })
    lines.append({
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": final_text}],
            "stopReason": "stop",
        },
    })
    lines.append({"type": "agent_end", "messages": []})
    return io.StringIO("\n".join(json.dumps(o) for o in lines) + "\n")


def _classify(final_text: str, *, tool_error: bool = False):
    return bridge.classify_stream(_stop_stream(final_text, tool_error=tool_error))


CANON = (
    "## Was wurde gemacht\nFeature gebaut und getestet, alles sauber.\n"
    "## Was hat funktioniert\nDer TDD-Loop lief ohne Ueberraschungen durch.\n"
    "## Was war unklar\nDer Scope der Edge-Cases war anfangs offen.\n"
    "## Lesson fuer Agent-Memory\nImmer zuerst die Vertraege pruefen, dann bauen.\n"
    "TASK_COMPLETE"
)


# ── Strict canonical still passes unchanged ────────────────────────────────

def test_canonical_still_finishes():
    outcome, cls = _classify(CANON)
    assert cls.kind is Kind.FINISH, cls
    assert outcome.reflection_valid is True
    assert "## Lesson fuer Agent-Memory" in outcome.reflection_block


# ── Sentinel tolerance ─────────────────────────────────────────────────────

def test_lowercase_sentinel_accepted():
    _, cls = _classify(CANON.replace("TASK_COMPLETE", "task_complete"))
    assert cls.kind is Kind.FINISH, cls


def test_bold_sentinel_accepted():
    _, cls = _classify(CANON.replace("TASK_COMPLETE", "**TASK_COMPLETE**"))
    assert cls.kind is Kind.FINISH, cls


def test_sentinel_with_trailing_rule_accepted():
    _, cls = _classify(CANON + "\n---")
    assert cls.kind is Kind.FINISH, cls


def test_sentinel_trailing_punctuation_accepted():
    _, cls = _classify(CANON.replace("TASK_COMPLETE", "TASK_COMPLETE."))
    assert cls.kind is Kind.FINISH, cls


# ── Header tolerance + normalization ───────────────────────────────────────

def test_hash_level_variant_accepted():
    text = (
        "### Was wurde gemacht\nFeature gebaut und getestet, alles sauber.\n"
        "### Was hat funktioniert\nDer TDD-Loop lief ohne Ueberraschungen durch.\n"
        "### Was war unklar\nDer Scope der Edge-Cases war anfangs offen.\n"
        "### Lesson fuer Agent-Memory\nImmer zuerst die Vertraege pruefen.\n"
        "TASK_COMPLETE"
    )
    outcome, cls = _classify(text)
    assert cls.kind is Kind.FINISH, cls
    # normalized down to canonical ## level
    assert "## Was wurde gemacht" in outcome.reflection_block
    assert "### Was wurde gemacht" not in outcome.reflection_block


def test_english_headers_accepted_and_normalized_to_german():
    text = (
        "## What was done\nBuilt and tested the feature end to end.\n"
        "## What worked\nThe TDD loop ran clean the whole way.\n"
        "## What was unclear\nThe edge-case scope was open at first.\n"
        "## Lesson for agent memory\nAlways check the contracts first.\n"
        "TASK_COMPLETE"
    )
    outcome, cls = _classify(text)
    assert cls.kind is Kind.FINISH, cls
    # The reflection PASSED ON to `mc finish` must be canonical German.
    for f in bridge.REFLECTION_HEADERS:
        assert f in outcome.reflection_block, f"missing {f}"
    assert "What was done" not in outcome.reflection_block
    assert "Lesson for agent memory" not in outcome.reflection_block


# ── Negatives — must NOT be false-accepted ─────────────────────────────────

def test_missing_reflection_still_fails():
    _, cls = _classify("Alles erledigt.\nTASK_COMPLETE")
    assert cls.kind is Kind.MALFORMED_REFLECTION, cls


def test_missing_sentinel_still_fails():
    _, cls = _classify(CANON.replace("\nTASK_COMPLETE", "\nIch gebe auf."))
    assert cls.kind is Kind.SILENT_ABORT_NO_SENTINEL, cls


def test_sentinel_only_no_reflection_fails():
    _, cls = _classify("TASK_COMPLETE")
    assert cls.kind is Kind.MALFORMED_REFLECTION, cls
