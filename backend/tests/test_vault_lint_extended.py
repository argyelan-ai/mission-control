"""Tests for extended vault lint — broken wikilinks, missing cross-refs, auto-fix."""
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest

from app.services.vault_lint import (
    lint_vault,
    _find_broken_wikilinks,
    _find_missing_confidence,
    _auto_fix_missing_confidence,
    _auto_fix_broken_wikilinks,
)


def _write_note(vault_path: Path, rel: str, metadata: dict, content: str):
    """Helper: write a note with frontmatter to the vault."""
    full = vault_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content, **metadata)
    full.write_text(frontmatter.dumps(post))
    return full


class TestFindBrokenWikilinks:
    def test_detects_broken_link(self):
        tmp = Path(tempfile.mkdtemp())
        _write_note(tmp, "agents/boss/lessons/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "Link to [[nonexistent-note]] here.")
        result = _find_broken_wikilinks(tmp)
        assert len(result) == 1
        assert result[0]["target"] == "nonexistent-note"
        assert result[0]["source"] == "agents/boss/lessons/a.md"

    def test_valid_link_not_reported(self):
        tmp = Path(tempfile.mkdtemp())
        _write_note(tmp, "agents/boss/lessons/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "Link to [[b]] here.")
        _write_note(tmp, "agents/boss/lessons/b.md", {
            "id": "b-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "I am note B.")
        result = _find_broken_wikilinks(tmp)
        assert len(result) == 0

    def test_excluded_dirs_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        _write_note(tmp, "_inbox/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "Link to [[nonexistent]] here.")
        result = _find_broken_wikilinks(tmp)
        assert len(result) == 0


class TestFindMissingConfidence:
    def test_detects_missing_confidence(self):
        tmp = Path(tempfile.mkdtemp())
        _write_note(tmp, "agents/boss/lessons/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "No confidence field.")
        result = _find_missing_confidence(tmp)
        assert len(result) == 1

    def test_skips_note_with_confidence(self):
        tmp = Path(tempfile.mkdtemp())
        _write_note(tmp, "agents/boss/lessons/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [], "confidence": "high",
        }, "Has confidence.")
        result = _find_missing_confidence(tmp)
        assert len(result) == 0


class TestAutoFixMissingConfidence:
    def test_sets_confidence_medium(self):
        tmp = Path(tempfile.mkdtemp())
        path = _write_note(tmp, "agents/boss/lessons/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "No confidence.")
        fixed = _auto_fix_missing_confidence(tmp)
        assert fixed == 1
        post = frontmatter.load(path)
        assert post.metadata["confidence"] == "medium"


class TestAutoFixBrokenWikilinks:
    def test_removes_broken_links(self):
        tmp = Path(tempfile.mkdtemp())
        path = _write_note(tmp, "agents/boss/lessons/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "See [[nonexistent]] and [[also-missing]] for details.")
        fixed = _auto_fix_broken_wikilinks(tmp)
        assert fixed >= 1
        content = path.read_text()
        assert "[[nonexistent]]" not in content
        assert "[[also-missing]]" not in content
        assert "nonexistent" in content  # Text preserved, just brackets removed


class TestExtendedLintVault:
    def test_extended_stats_present(self):
        tmp = Path(tempfile.mkdtemp())
        _write_note(tmp, "agents/boss/lessons/a.md", {
            "id": "a-1", "type": "lesson", "agent": "boss",
            "date": "2026-05-24", "related": [],
        }, "Link to [[missing-note]].")
        stats = lint_vault(tmp)
        assert "broken_wikilink_count" in stats
        assert "missing_confidence_count" in stats
        assert stats["broken_wikilink_count"] == 1
        assert stats["missing_confidence_count"] == 1
