"""Tests for Phase 2 frontmatter validation: status, confidence, updated."""
import pytest

from app.helpers.vault_frontmatter import (
    FrontmatterError,
    validate_frontmatter,
    VALID_STATUS,
    VALID_CONFIDENCE,
)


def _base_meta(**overrides):
    """Minimal valid frontmatter metadata dict."""
    m = {
        "id": "test-20260524T120000",
        "type": "lesson",
        "agent": "researcher",
        "date": "2026-05-24T12:00:00Z",
    }
    m.update(overrides)
    return m


class TestStatusValidation:
    def test_valid_status_values_accepted(self):
        for s in ("draft", "published", "stale", "archived"):
            validate_frontmatter(_base_meta(status=s))

    def test_invalid_status_rejected(self):
        with pytest.raises(FrontmatterError, match="invalid status"):
            validate_frontmatter(_base_meta(status="deleted"))

    def test_missing_status_accepted(self):
        """status is optional — old notes without it stay valid."""
        validate_frontmatter(_base_meta())

    def test_valid_status_set_contains_four(self):
        assert VALID_STATUS == {"draft", "published", "stale", "archived"}


class TestConfidenceValidation:
    def test_valid_confidence_values_accepted(self):
        for c in ("high", "medium", "low"):
            validate_frontmatter(_base_meta(confidence=c))

    def test_invalid_confidence_rejected(self):
        with pytest.raises(FrontmatterError, match="invalid confidence"):
            validate_frontmatter(_base_meta(confidence="very-high"))

    def test_missing_confidence_accepted(self):
        """confidence is optional — old notes without it stay valid."""
        validate_frontmatter(_base_meta())

    def test_valid_confidence_set_contains_three(self):
        assert VALID_CONFIDENCE == {"high", "medium", "low"}


class TestUpdatedFieldValidation:
    def test_valid_updated_iso_string_accepted(self):
        validate_frontmatter(_base_meta(updated="2026-05-24T14:00:00Z"))

    def test_valid_updated_datetime_accepted(self):
        from datetime import datetime, timezone
        validate_frontmatter(_base_meta(updated=datetime(2026, 5, 24, tzinfo=timezone.utc)))

    def test_invalid_updated_string_rejected(self):
        with pytest.raises(FrontmatterError, match="invalid updated"):
            validate_frontmatter(_base_meta(updated="not-a-date"))

    def test_missing_updated_accepted(self):
        """updated is optional."""
        validate_frontmatter(_base_meta())
