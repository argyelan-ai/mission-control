"""Tests for vault contradiction detection via Qdrant + Spark LLM."""
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest

from app.services.vault_contradiction import (
    check_contradictions,
    classify_relation,
    ContradictionResult,
    CONTRADICTION_SYSTEM_PROMPT,
    SIMILARITY_THRESHOLD,
)


@dataclass
class FakeScoredPoint:
    payload: dict
    score: float


@pytest.fixture
def spark_mock():
    mock = AsyncMock()
    mock.complete = AsyncMock()
    mock.embed = AsyncMock(return_value=[0.1] * 768)
    return mock


@pytest.fixture
def qdrant_mock():
    mock = MagicMock()
    mock.search = MagicMock(return_value=[])
    return mock


class TestClassifyRelation:
    def test_contradicts_parsed(self, spark_mock):
        spark_mock.complete.return_value = json.dumps({
            "relation": "contradicts",
            "reason": "A says 10 RPM, B says unlimited",
        })

        result = asyncio.get_event_loop().run_until_complete(
            classify_relation(spark_mock, "Note A title", "A content", "Note B title", "B content")
        )

        assert result.relation == "contradicts"
        assert "10 RPM" in result.reason

    def test_refines_parsed(self, spark_mock):
        spark_mock.complete.return_value = json.dumps({
            "relation": "refines",
            "reason": "A is a more precise version of B",
        })

        result = asyncio.get_event_loop().run_until_complete(
            classify_relation(spark_mock, "A", "A content", "B", "B content")
        )

        assert result.relation == "refines"

    def test_unrelated_parsed(self, spark_mock):
        spark_mock.complete.return_value = json.dumps({
            "relation": "unrelated",
            "reason": "Different topics",
        })

        result = asyncio.get_event_loop().run_until_complete(
            classify_relation(spark_mock, "A", "A content", "B", "B content")
        )

        assert result.relation == "unrelated"

    def test_confirms_parsed(self, spark_mock):
        spark_mock.complete.return_value = json.dumps({
            "relation": "confirms",
            "reason": "Same information",
        })

        result = asyncio.get_event_loop().run_until_complete(
            classify_relation(spark_mock, "A", "A content", "B", "B content")
        )

        assert result.relation == "confirms"

    def test_malformed_json_returns_unrelated(self, spark_mock):
        spark_mock.complete.return_value = "not valid json at all"

        result = asyncio.get_event_loop().run_until_complete(
            classify_relation(spark_mock, "A", "A content", "B", "B content")
        )

        assert result.relation == "unrelated"

    def test_spark_exception_returns_unrelated(self, spark_mock):
        spark_mock.complete.side_effect = Exception("Spark down")

        result = asyncio.get_event_loop().run_until_complete(
            classify_relation(spark_mock, "A", "A content", "B", "B content")
        )

        assert result.relation == "unrelated"


class TestCheckContradictions:
    def test_no_candidates_returns_empty(self, spark_mock, qdrant_mock, tmp_path):
        note = tmp_path / "test.md"
        post = frontmatter.Post("Content", id="test-001", type="lesson", agent="researcher", date="2026-05-24")
        note.write_text(frontmatter.dumps(post))

        qdrant_mock.search.return_value = []

        results = asyncio.get_event_loop().run_until_complete(
            check_contradictions(note, tmp_path, qdrant_mock, spark_mock)
        )

        assert results == []

    def test_self_reference_excluded(self, spark_mock, qdrant_mock, tmp_path):
        note = tmp_path / "test.md"
        post = frontmatter.Post("Content", id="test-001", type="lesson", agent="researcher", date="2026-05-24")
        note.write_text(frontmatter.dumps(post))

        qdrant_mock.search.return_value = [
            FakeScoredPoint(payload={"path": "test.md", "id": "test-001"}, score=0.99),
        ]

        results = asyncio.get_event_loop().run_until_complete(
            check_contradictions(note, tmp_path, qdrant_mock, spark_mock)
        )

        assert results == []
        spark_mock.complete.assert_not_called()

    def test_below_threshold_excluded(self, spark_mock, qdrant_mock, tmp_path):
        note = tmp_path / "test.md"
        post = frontmatter.Post("Content", id="test-001", type="lesson", agent="researcher", date="2026-05-24")
        note.write_text(frontmatter.dumps(post))

        qdrant_mock.search.return_value = [
            FakeScoredPoint(payload={"path": "other.md", "id": "other-001"}, score=0.50),
        ]

        results = asyncio.get_event_loop().run_until_complete(
            check_contradictions(note, tmp_path, qdrant_mock, spark_mock)
        )

        assert results == []
        spark_mock.complete.assert_not_called()

    def test_contradiction_found_returns_result(self, spark_mock, qdrant_mock, tmp_path):
        note = tmp_path / "test.md"
        post = frontmatter.Post("Rate limit is 10 RPM", id="test-001", type="lesson", agent="researcher", date="2026-05-24")
        note.write_text(frontmatter.dumps(post))

        other = tmp_path / "other.md"
        other_post = frontmatter.Post("Rate limit is unlimited", id="other-001", type="lesson", agent="sparky", date="2026-05-24")
        other.write_text(frontmatter.dumps(other_post))

        qdrant_mock.search.return_value = [
            FakeScoredPoint(payload={"path": "other.md", "id": "other-001"}, score=0.92),
        ]
        spark_mock.complete.return_value = json.dumps({
            "relation": "contradicts",
            "reason": "Conflicting rate limit values",
        })

        results = asyncio.get_event_loop().run_until_complete(
            check_contradictions(note, tmp_path, qdrant_mock, spark_mock)
        )

        assert len(results) == 1
        assert results[0].relation == "contradicts"
        assert results[0].other_note_id == "other-001"

    def test_max_5_candidates_checked(self, spark_mock, qdrant_mock, tmp_path):
        note = tmp_path / "test.md"
        post = frontmatter.Post("Content", id="test-001", type="lesson", agent="researcher", date="2026-05-24")
        note.write_text(frontmatter.dumps(post))

        candidates = []
        for i in range(8):
            cand = tmp_path / f"note-{i}.md"
            cand_post = frontmatter.Post(f"Content {i}", id=f"note-{i}", type="lesson", agent="researcher", date="2026-05-24")
            cand.write_text(frontmatter.dumps(cand_post))
            candidates.append(
                FakeScoredPoint(payload={"path": f"note-{i}.md", "id": f"note-{i}"}, score=0.90)
            )

        qdrant_mock.search.return_value = candidates
        spark_mock.complete.return_value = json.dumps({
            "relation": "unrelated",
            "reason": "Different topics",
        })

        asyncio.get_event_loop().run_until_complete(
            check_contradictions(note, tmp_path, qdrant_mock, spark_mock)
        )

        assert spark_mock.complete.call_count == 5  # Max 5 LLM calls


class TestSimilarityThreshold:
    def test_threshold_is_080(self):
        assert SIMILARITY_THRESHOLD == 0.80
