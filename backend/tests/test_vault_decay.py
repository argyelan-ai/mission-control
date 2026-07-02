"""Tests for vault_decay.py — soft decay cron."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.vault_decay import (
    DecayResult,
    demote_confidence,
    run_decay,
    DECAY_EXEMPT_TYPES,
    GRACE_PERIOD_DAYS,
)


class TestDemoteConfidence:
    def test_high_to_medium(self):
        assert demote_confidence("high") == "medium"

    def test_medium_to_low(self):
        assert demote_confidence("medium") == "low"

    def test_low_stays_low(self):
        assert demote_confidence("low") == "low"

    def test_unknown_returns_low(self):
        assert demote_confidence("unknown") == "low"


class FakeRow:
    """Simulates a BoardMemory row for decay tests."""
    def __init__(
        self,
        id=None,
        status="published",
        confidence="medium",
        is_pinned=False,
        memory_type="lesson",
        last_viewed_at=None,
        created_at=None,
        contradiction_ids=None,
        vault_path=None,
    ):
        self.id = id or uuid.uuid4()
        self.status = status
        self.confidence = confidence
        self.is_pinned = is_pinned
        self.memory_type = memory_type
        self.last_viewed_at = last_viewed_at
        self.created_at = created_at or (datetime.now(timezone.utc) - timedelta(days=120))
        self.contradiction_ids = contradiction_ids or []
        self.vault_path = vault_path or f"agents/test/lessons/note-{self.id}.md"
        self.updated_at_content = self.created_at


class TestRunDecay:
    @pytest.fixture
    def session(self):
        mock = AsyncMock()
        mock.commit = AsyncMock()
        mock.add = MagicMock()
        return mock

    @pytest.fixture
    def vault_log(self):
        mock = MagicMock()
        mock.append = MagicMock()
        return mock

    @pytest.mark.asyncio
    async def test_decay_demotes_stale_note(self, session, vault_log):
        """Note not viewed for 90+ days with confidence=medium -> demoted to low."""
        now = datetime.now(timezone.utc)
        row = FakeRow(
            status="published",
            confidence="medium",
            last_viewed_at=now - timedelta(days=100),
            created_at=now - timedelta(days=200),
        )
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))

        result = await run_decay(
            session=session,
            vault_path=MagicMock(),
            vault_log=vault_log,
            migration_date=now - timedelta(days=GRACE_PERIOD_DAYS + 1),
        )
        assert result.demoted >= 1
        assert row.confidence == "low"

    @pytest.mark.asyncio
    async def test_decay_skips_pinned_notes(self, session, vault_log):
        """Pinned notes are never decayed."""
        now = datetime.now(timezone.utc)
        row = FakeRow(
            is_pinned=True,
            last_viewed_at=now - timedelta(days=200),
            created_at=now - timedelta(days=200),
        )
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))

        result = await run_decay(
            session=session,
            vault_path=MagicMock(),
            vault_log=vault_log,
            migration_date=now - timedelta(days=GRACE_PERIOD_DAYS + 1),
        )
        assert result.demoted == 0
        assert result.skipped_pinned >= 1

    @pytest.mark.asyncio
    async def test_decay_skips_exempt_types(self, session, vault_log):
        """Reference and weekly_review types are exempt from decay."""
        now = datetime.now(timezone.utc)
        row = FakeRow(
            memory_type="reference",
            last_viewed_at=now - timedelta(days=200),
            created_at=now - timedelta(days=200),
        )
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))

        result = await run_decay(
            session=session,
            vault_path=MagicMock(),
            vault_log=vault_log,
            migration_date=now - timedelta(days=GRACE_PERIOD_DAYS + 1),
        )
        assert result.demoted == 0
        assert result.skipped_type >= 1

    @pytest.mark.asyncio
    async def test_decay_skips_notes_with_contradictions(self, session, vault_log):
        """Notes with active contradictions are not decayed."""
        now = datetime.now(timezone.utc)
        row = FakeRow(
            contradiction_ids=["other-note-id"],
            last_viewed_at=now - timedelta(days=200),
            created_at=now - timedelta(days=200),
        )
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))

        result = await run_decay(
            session=session,
            vault_path=MagicMock(),
            vault_log=vault_log,
            migration_date=now - timedelta(days=GRACE_PERIOD_DAYS + 1),
        )
        assert result.demoted == 0
        assert result.skipped_contradiction >= 1

    @pytest.mark.asyncio
    async def test_grace_period_prevents_early_decay(self, session, vault_log):
        """During grace period (first 90 days after migration), no decay fires."""
        now = datetime.now(timezone.utc)
        row = FakeRow(
            last_viewed_at=None,
            created_at=now - timedelta(days=200),
        )
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))

        result = await run_decay(
            session=session,
            vault_path=MagicMock(),
            vault_log=vault_log,
            # Migration was only 30 days ago — grace period active
            migration_date=now - timedelta(days=30),
        )
        assert result.demoted == 0
        assert result.skipped_grace_period is True

    @pytest.mark.asyncio
    async def test_archive_stage_180_days(self, session, vault_log):
        """Note with confidence=low + 180d no views -> archived."""
        now = datetime.now(timezone.utc)
        row = FakeRow(
            status="stale",
            confidence="low",
            last_viewed_at=now - timedelta(days=200),
            created_at=now - timedelta(days=300),
        )
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[row])))

        result = await run_decay(
            session=session,
            vault_path=MagicMock(),
            vault_log=vault_log,
            migration_date=now - timedelta(days=GRACE_PERIOD_DAYS + 1),
        )
        assert result.archived >= 1
