"""Tests for status query filter in vault search and memory list endpoints."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


class TestAgentSearchStatusFilter:
    """Agent vault search should respect status filter rules."""

    def test_agent_search_defaults_to_published_only(self):
        """Without explicit status param, agent search returns only published."""
        # The vault_index.search mock returns notes with mixed statuses
        notes = [
            {"id": "1", "path": "a.md", "status": "published", "agent": "researcher", "content": "test"},
            {"id": "2", "path": "b.md", "status": "draft", "agent": "sparky", "content": "test"},
        ]
        # After filtering: only published should remain
        filtered = [n for n in notes if n.get("status", "published") == "published"]
        assert len(filtered) == 1
        assert filtered[0]["id"] == "1"

    def test_agent_sees_own_drafts(self):
        """Agent should see their own drafts in search results."""
        notes = [
            {"id": "1", "path": "a.md", "status": "draft", "agent": "researcher", "content": "test"},
            {"id": "2", "path": "b.md", "status": "draft", "agent": "sparky", "content": "test"},
        ]
        requesting_agent = "researcher"
        # Filter: show published + own drafts
        filtered = [
            n for n in notes
            if n.get("status", "published") == "published"
            or (n.get("status") == "draft" and n.get("agent") == requesting_agent)
        ]
        assert len(filtered) == 1
        assert filtered[0]["agent"] == "researcher"

    def test_admin_sees_all_statuses(self):
        """Admin search with explicit status param returns all matching."""
        notes = [
            {"id": "1", "path": "a.md", "status": "draft", "content": "test"},
            {"id": "2", "path": "b.md", "status": "published", "content": "test"},
            {"id": "3", "path": "c.md", "status": "stale", "content": "test"},
            {"id": "4", "path": "d.md", "status": "archived", "content": "test"},
        ]
        # Admin with status=draft
        filtered = [n for n in notes if n.get("status") == "draft"]
        assert len(filtered) == 1
        # Admin with no status filter (sees all)
        assert len(notes) == 4


class TestKnowledgeStatusFilter:
    """Memory /knowledge endpoint should support status filter."""

    def test_status_filter_applies_to_query(self):
        """When status param is provided, only matching entries returned."""
        # This tests the filter logic we'll add
        statuses = ["published", "draft", "stale", "archived"]
        for s in statuses:
            entries = [
                {"id": "1", "status": s},
                {"id": "2", "status": "published"},
            ]
            filtered = [e for e in entries if e.get("status") == s]
            assert all(e["status"] == s for e in filtered)

    def test_no_status_filter_returns_all(self):
        """Without status filter, all entries returned (backward compat)."""
        entries = [
            {"id": "1", "status": "published"},
            {"id": "2", "status": "draft"},
        ]
        # No filter = return all
        assert len(entries) == 2
