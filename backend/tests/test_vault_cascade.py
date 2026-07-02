"""Tests for vault_cascade.py — cascading page updates on note publish."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.vault_cascade import (
    cascade_updates,
    CASCADE_REDIS_PREFIX,
    CASCADE_DEPTH_LIMIT,
    _parse_llm_response,
)


class FakeScoredPoint:
    def __init__(self, payload, score):
        self.payload = payload
        self.score = score


class TestParseLlmResponse:
    def test_valid_update_needed(self):
        raw = json.dumps({"update_needed": True, "patch": "New info here.", "reason": "relevant"})
        result = _parse_llm_response(raw)
        assert result["update_needed"] is True
        assert result["patch"] == "New info here."

    def test_valid_no_update(self):
        raw = json.dumps({"update_needed": False, "patch": None, "reason": "unrelated"})
        result = _parse_llm_response(raw)
        assert result["update_needed"] is False

    def test_code_fenced_json(self):
        raw = "```json\n" + json.dumps({"update_needed": True, "patch": "X", "reason": "Y"}) + "\n```"
        result = _parse_llm_response(raw)
        assert result["update_needed"] is True

    def test_invalid_json_returns_no_update(self):
        result = _parse_llm_response("This is not JSON at all")
        assert result["update_needed"] is False

    def test_patch_longer_than_original_rejected(self):
        """Patch longer than 2000 chars is rejected (patch should be an addition, not a rewrite)."""
        raw = json.dumps({"update_needed": True, "patch": "x" * 2001, "reason": "big"})
        result = _parse_llm_response(raw)
        assert result["update_needed"] is False


class TestCascadeUpdates:
    @pytest.fixture
    def spark(self):
        mock = AsyncMock()
        mock.embed = AsyncMock(return_value=[0.1] * 768)
        mock.complete = AsyncMock(return_value=json.dumps({
            "update_needed": True,
            "patch": "Updated based on new findings.",
            "reason": "directly relevant",
        }))
        return mock

    @pytest.fixture
    def qdrant(self):
        mock = AsyncMock()
        mock.query_points = AsyncMock(return_value=MagicMock(points=[
            FakeScoredPoint(
                payload={"path": "agents/boss/knowledge/docker-pattern.md", "slug": "docker-pattern"},
                score=0.82,
            ),
            FakeScoredPoint(
                payload={"path": "agents/boss/knowledge/compose-guide.md", "slug": "compose-guide"},
                score=0.78,
            ),
        ]))
        return mock

    @pytest.fixture
    def redis(self):
        mock = AsyncMock()
        mock.set = AsyncMock(return_value=True)
        mock.get = AsyncMock(return_value=None)  # No cascade lock exists
        return mock

    @pytest.fixture
    def vault_path(self, tmp_path):
        # Create the note that was just published
        note_dir = tmp_path / "agents" / "researcher" / "lessons"
        note_dir.mkdir(parents=True)
        note = note_dir / "new-finding.md"
        note.write_text("---\nid: abc-123\ntitle: New Finding\nagent: researcher\ntype: lesson\ndate: 2026-05-24\nrelated: []\n---\nNew finding about Docker networking.")

        # Create the candidate notes that Qdrant will point to
        boss_dir = tmp_path / "agents" / "boss" / "knowledge"
        boss_dir.mkdir(parents=True)
        (boss_dir / "docker-pattern.md").write_text(
            "---\nid: def-456\ntitle: Docker Pattern\nagent: boss\ntype: knowledge\nstatus: published\ndate: 2026-05-20\nrelated: []\n---\nDocker restart patterns for MC agents."
        )
        (boss_dir / "compose-guide.md").write_text(
            "---\nid: ghi-789\ntitle: Compose Guide\nagent: boss\ntype: knowledge\nstatus: published\ndate: 2026-05-19\nrelated: []\n---\nCompose renderer guidelines."
        )
        return tmp_path

    @pytest.mark.asyncio
    async def test_cascade_creates_draft_patches(self, spark, qdrant, redis, vault_path):
        """Cascade creates draft patch notes in _inbox/ for related notes that need updating."""
        note_path = vault_path / "agents" / "researcher" / "lessons" / "new-finding.md"
        result = await cascade_updates(
            note_path=note_path,
            vault_path=vault_path,
            spark=spark,
            qdrant_client=qdrant,
            redis=redis,
        )
        assert result.candidates_checked >= 1
        assert result.patches_created >= 1
        # Verify inbox envelope was written
        inbox = vault_path / "_inbox"
        assert inbox.exists()
        envelopes = list(inbox.glob("*.md"))
        assert len(envelopes) >= 1

    @pytest.mark.asyncio
    async def test_cascade_skips_when_redis_lock_exists(self, spark, qdrant, redis, vault_path):
        """Cascade is skipped when the note was already cascaded (Redis TTL key)."""
        redis.get = AsyncMock(return_value=b"1")  # Lock exists
        note_path = vault_path / "agents" / "researcher" / "lessons" / "new-finding.md"
        result = await cascade_updates(
            note_path=note_path,
            vault_path=vault_path,
            spark=spark,
            qdrant_client=qdrant,
            redis=redis,
        )
        assert result.candidates_checked == 0
        assert result.patches_created == 0
        assert result.skipped_reason == "cascade_lock"

    @pytest.mark.asyncio
    async def test_cascade_respects_depth_limit(self, spark, qdrant, redis, vault_path):
        """Cascade with depth >= CASCADE_DEPTH_LIMIT is rejected."""
        note_path = vault_path / "agents" / "researcher" / "lessons" / "new-finding.md"
        result = await cascade_updates(
            note_path=note_path,
            vault_path=vault_path,
            spark=spark,
            qdrant_client=qdrant,
            redis=redis,
            depth=CASCADE_DEPTH_LIMIT,
        )
        assert result.candidates_checked == 0
        assert result.skipped_reason == "max_depth"

    @pytest.mark.asyncio
    async def test_cascade_failsoft_on_spark_error(self, qdrant, redis, vault_path):
        """Cascade returns gracefully when Spark is unreachable."""
        spark = AsyncMock()
        spark.embed = AsyncMock(side_effect=RuntimeError("Spark down"))
        note_path = vault_path / "agents" / "researcher" / "lessons" / "new-finding.md"
        result = await cascade_updates(
            note_path=note_path,
            vault_path=vault_path,
            spark=spark,
            qdrant_client=qdrant,
            redis=redis,
        )
        assert result.candidates_checked == 0
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_cascade_no_update_needed(self, spark, qdrant, redis, vault_path):
        """When LLM says no update needed, no patches are created."""
        spark.complete = AsyncMock(return_value=json.dumps({
            "update_needed": False, "patch": None, "reason": "unrelated",
        }))
        note_path = vault_path / "agents" / "researcher" / "lessons" / "new-finding.md"
        result = await cascade_updates(
            note_path=note_path,
            vault_path=vault_path,
            spark=spark,
            qdrant_client=qdrant,
            redis=redis,
        )
        assert result.candidates_checked >= 1
        assert result.patches_created == 0
