"""Tests for VaultCompactor setting status:draft on new canonical notes."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest

from app.services.vault_compactor import VaultCompactor


@pytest.fixture
def tmp_vault(tmp_path):
    (tmp_path / "_inbox").mkdir()
    (tmp_path / "_conflicts").mkdir()
    return tmp_path


@pytest.fixture
def redis_mock():
    mock = AsyncMock()
    mock.set = AsyncMock(return_value=True)  # idempotency always passes
    mock.publish = AsyncMock()
    return mock


def _write_envelope(inbox: Path, target: str, content: str, agent: str = "researcher"):
    """Write a minimal valid envelope to the inbox."""
    from hashlib import sha256
    meta = {
        "op": "upsert",
        "target": target,
        "agent_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "agent": agent,
        "type": "lesson",
        "tags": [],
        "date": "2026-05-24T12:00:00Z",
        "id": f"{agent}-20260524T120000",
        "sha256": sha256(content.encode()).hexdigest(),
        "idempotency_key": f"test-{sha256(content.encode()).hexdigest()[:8]}",
        "related": [],
    }
    post = frontmatter.Post(content, **meta)
    env_path = inbox / f"test_{agent}_lesson.md"
    env_path.write_text(frontmatter.dumps(post))
    return env_path


class TestCompactorDraftStatus:
    def test_new_note_gets_status_draft(self, tmp_vault, redis_mock):
        """Compacted canonical note must have status: draft in frontmatter."""
        target_rel = "agents/researcher/lessons/test-lesson.md"
        _write_envelope(tmp_vault / "_inbox", target_rel, "Rate limiting is 10 RPM")

        compactor = VaultCompactor(tmp_vault, redis_mock)
        stats = asyncio.get_event_loop().run_until_complete(compactor.compact())

        assert stats["written"] == 1
        canonical = tmp_vault / target_rel
        assert canonical.exists()

        post = frontmatter.load(str(canonical))
        assert post.metadata.get("status") == "draft"

    def test_existing_note_conflict_preserves_status(self, tmp_vault, redis_mock):
        """If target exists with different SHA, it's a conflict — status unchanged."""
        target_rel = "agents/researcher/lessons/existing.md"
        target = tmp_vault / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = frontmatter.Post("Old content", **{
            "id": "researcher-20260524T110000",
            "type": "lesson",
            "agent": "researcher",
            "date": "2026-05-24T11:00:00Z",
            "status": "published",
        })
        target.write_text(frontmatter.dumps(existing))

        _write_envelope(tmp_vault / "_inbox", target_rel, "New conflicting content")

        compactor = VaultCompactor(tmp_vault, redis_mock)
        stats = asyncio.get_event_loop().run_until_complete(compactor.compact())

        assert stats["conflicted"] == 1
        # Original status unchanged
        post = frontmatter.load(str(target))
        assert post.metadata.get("status") == "published"

    def test_dedup_does_not_overwrite_status(self, tmp_vault, redis_mock):
        """Dedup (same SHA) should not touch the existing canonical."""
        target_rel = "agents/researcher/lessons/dedup.md"
        target = tmp_vault / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        content = "Already exists with this exact content"
        existing = frontmatter.Post(content, **{
            "id": "researcher-20260524T110000",
            "type": "lesson",
            "agent": "researcher",
            "date": "2026-05-24T11:00:00Z",
            "status": "published",
        })
        target.write_text(frontmatter.dumps(existing))

        _write_envelope(tmp_vault / "_inbox", target_rel, content)

        compactor = VaultCompactor(tmp_vault, redis_mock)
        stats = asyncio.get_event_loop().run_until_complete(compactor.compact())

        assert stats["deduped"] == 1
        post = frontmatter.load(str(target))
        assert post.metadata.get("status") == "published"
