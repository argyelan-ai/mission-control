"""Tests for VaultPromoter — 24h auto-promotion with Redis sorted-set delayed queue."""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import frontmatter
import pytest

from app.services.vault_promoter import VaultPromoter, PROMOTE_DELAY_SECONDS, PROMOTE_QUEUE_KEY


@pytest.fixture
def tmp_vault(tmp_path):
    (tmp_path / "agents" / "researcher" / "lessons").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def redis_mock():
    mock = AsyncMock()
    mock.zadd = AsyncMock(return_value=1)
    mock.zrangebyscore = AsyncMock(return_value=[])
    mock.zrem = AsyncMock(return_value=1)
    mock.delete = AsyncMock()
    mock.publish = AsyncMock()
    return mock


@pytest.fixture
def db_session_mock():
    mock = AsyncMock()
    mock.execute = AsyncMock()
    mock.commit = AsyncMock()
    return mock


def _write_draft_note(vault: Path, rel_path: str, note_id: str, content: str = "Test content"):
    """Write a vault note with status: draft."""
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": note_id,
        "type": "lesson",
        "agent": "researcher",
        "date": "2026-05-24T12:00:00Z",
        "status": "draft",
        "confidence": "medium",
    }
    post = frontmatter.Post(content, **meta)
    full.write_text(frontmatter.dumps(post))
    return full


class TestSchedulePromotion:
    def test_schedule_adds_to_sorted_set(self, tmp_vault, redis_mock):
        promoter = VaultPromoter(tmp_vault, redis_mock)
        note_id = "researcher-20260524T120000"
        rel_path = "agents/researcher/lessons/test.md"

        asyncio.get_event_loop().run_until_complete(
            promoter.schedule_promotion(note_id, rel_path)
        )

        redis_mock.zadd.assert_called_once()
        call_args = redis_mock.zadd.call_args
        assert call_args[0][0] == PROMOTE_QUEUE_KEY
        mapping = call_args[0][1]
        # The mapping is {json_payload: due_at_score}
        payload_str = list(mapping.keys())[0]
        payload = json.loads(payload_str)
        assert payload["note_id"] == note_id
        assert payload["rel_path"] == rel_path
        score = list(mapping.values())[0]
        # Score should be ~24h from now
        assert score > time.time()
        assert score <= time.time() + PROMOTE_DELAY_SECONDS + 1

    def test_promote_delay_is_24h(self):
        assert PROMOTE_DELAY_SECONDS == 86400


class TestPromoteNote:
    def test_promote_updates_frontmatter_and_db(self, tmp_vault, redis_mock, db_session_mock):
        note_id = "researcher-20260524T120000"
        rel_path = "agents/researcher/lessons/test.md"
        _write_draft_note(tmp_vault, rel_path, note_id)

        promoter = VaultPromoter(tmp_vault, redis_mock)

        asyncio.get_event_loop().run_until_complete(
            promoter.promote_note(note_id, rel_path, db_session=db_session_mock)
        )

        # Frontmatter updated on disk
        post = frontmatter.load(str(tmp_vault / rel_path))
        assert post.metadata["status"] == "published"

        # Redis promotion key cleaned up
        redis_mock.delete.assert_called_with(f"mc:vault:promote:{note_id}")

    def test_promote_blocked_by_contradictions(self, tmp_vault, redis_mock, db_session_mock):
        note_id = "researcher-20260524T120000"
        rel_path = "agents/researcher/lessons/test.md"
        full = _write_draft_note(tmp_vault, rel_path, note_id)

        # Add contradiction_ids to frontmatter
        post = frontmatter.load(str(full))
        post.metadata["contradiction_ids"] = ["other-note-id-123"]
        full.write_text(frontmatter.dumps(post))

        promoter = VaultPromoter(tmp_vault, redis_mock)

        asyncio.get_event_loop().run_until_complete(
            promoter.promote_note(note_id, rel_path, db_session=db_session_mock)
        )

        # Status should still be draft
        post = frontmatter.load(str(full))
        assert post.metadata["status"] == "draft"

    def test_promote_nonexistent_note_is_noop(self, tmp_vault, redis_mock, db_session_mock):
        promoter = VaultPromoter(tmp_vault, redis_mock)

        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            promoter.promote_note("nonexistent-id", "agents/x/lessons/gone.md", db_session=db_session_mock)
        )

        # Redis key still cleaned up (stale timer)
        redis_mock.delete.assert_called_with("mc:vault:promote:nonexistent-id")


class TestManualPromote:
    def test_force_promote_ignores_contradictions(self, tmp_vault, redis_mock, db_session_mock):
        note_id = "researcher-20260524T120000"
        rel_path = "agents/researcher/lessons/test.md"
        full = _write_draft_note(tmp_vault, rel_path, note_id)

        post = frontmatter.load(str(full))
        post.metadata["contradiction_ids"] = ["other-note-id-123"]
        full.write_text(frontmatter.dumps(post))

        promoter = VaultPromoter(tmp_vault, redis_mock)

        asyncio.get_event_loop().run_until_complete(
            promoter.promote_note(note_id, rel_path, db_session=db_session_mock, force=True)
        )

        post = frontmatter.load(str(full))
        assert post.metadata["status"] == "published"


class TestRejectNote:
    def test_reject_moves_to_rejected_dir(self, tmp_vault, redis_mock):
        note_id = "researcher-20260524T120000"
        rel_path = "agents/researcher/lessons/test.md"
        _write_draft_note(tmp_vault, rel_path, note_id)

        promoter = VaultPromoter(tmp_vault, redis_mock)

        asyncio.get_event_loop().run_until_complete(
            promoter.reject_note(note_id, rel_path)
        )

        # Original gone
        assert not (tmp_vault / rel_path).exists()
        # Moved to _rejected/
        rejected_dir = tmp_vault / "_rejected"
        assert rejected_dir.exists()
        rejected_files = list(rejected_dir.glob("*.md"))
        assert len(rejected_files) == 1
