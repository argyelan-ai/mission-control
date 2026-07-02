"""Unit tests for validate_comment_content (defense-in-depth, Bug 2026-05-17).

The Researcher sent `mc comment progress "$(python3 -c "import json;
print(json.dumps({'content': '...'}))")"` and the raw JSON envelope landed
literally in task_comments.content (UI showed `{"content": "...}` instead
of formatted markdown). The mc CLI now refuses this client-side; this
validator is the symmetric backend guard.

The detection is intentionally narrow: only reject strings that parse to a
dict whose key-set is EXACTLY {"content"} or {"content","comment_type"}. A
legitimate comment that *starts* with a `{` (placeholder, code snippet,
explanation of the bug itself) must still pass.
"""
from __future__ import annotations

import pytest

from app.comment_types import validate_comment_content


# ── Reject cases ───────────────────────────────────────────────────────

def test_rejects_empty_string():
    with pytest.raises(ValueError, match="empty"):
        validate_comment_content("")


def test_rejects_whitespace_only():
    with pytest.raises(ValueError, match="empty"):
        validate_comment_content("   \n\t  ")


def test_rejects_json_envelope_single_key():
    """The exact Researcher bug from 2026-05-17."""
    payload = '{"content": "**Update** — Briefing fertig"}'
    with pytest.raises(ValueError, match="JSON envelope"):
        validate_comment_content(payload)


def test_rejects_json_envelope_with_comment_type():
    """Some agents wrap both fields together — same anti-pattern."""
    payload = '{"content": "x", "comment_type": "progress"}'
    with pytest.raises(ValueError, match="JSON envelope"):
        validate_comment_content(payload)


def test_rejects_envelope_with_surrounding_whitespace():
    payload = '   \n{"content": "x"}\n  '
    with pytest.raises(ValueError, match="JSON envelope"):
        validate_comment_content(payload)


# ── Accept cases ───────────────────────────────────────────────────────

def test_accepts_plain_markdown():
    assert validate_comment_content("**Update** — research done.") == "**Update** — research done."


def test_accepts_markdown_starting_with_brace():
    """A user comment that happens to start with `{` must pass."""
    msg = "{foo} is a placeholder in our template syntax"
    assert validate_comment_content(msg) == msg


def test_accepts_json_array():
    """`[1,2,3]` is valid JSON but not a dict — not an envelope."""
    assert validate_comment_content("[1,2,3]") == "[1,2,3]"


def test_accepts_json_string_literal():
    """`"hi"` is valid JSON (a string) but not a dict — not an envelope."""
    assert validate_comment_content('"hi"') == '"hi"'


def test_accepts_dict_with_other_keys():
    """A dict that happens to contain `content` plus other keys is NOT
    a pure envelope — likely a legitimate code-snippet or technical comment."""
    msg = '{"content": "x", "author": "alice", "id": 42}'
    assert validate_comment_content(msg) == msg


def test_accepts_malformed_json_starting_with_brace():
    """If it doesn't parse as JSON, it can't be an envelope — pass through."""
    msg = '{ not valid json } — example from docs'
    assert validate_comment_content(msg) == msg


def test_accepts_unicode_markdown():
    msg = "🔍 Researcher · Briefing — alle Quellen kreuzvalidiert ✅"
    assert validate_comment_content(msg) == msg


def test_accepts_multiline_with_code_block():
    msg = '## Was wurde gemacht\n```python\nx = {"key": "value"}\n```\nDone.'
    assert validate_comment_content(msg) == msg
