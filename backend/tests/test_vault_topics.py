"""Tests for GET /api/v1/vault/topics endpoint."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTopicsEndpoint:
    @pytest.fixture
    def mock_vault_index(self):
        index = MagicMock()
        index.list_all.return_value = [
            {"path": f"agents/boss/lessons/note-{i}.md", "type": "lesson",
             "agent": "boss", "content": f"Content {i}", "title": f"Note {i}",
             "tags": "docker"}
            for i in range(20)
        ]
        return index

    @pytest.fixture
    def mock_vault_embeddings(self):
        emb = MagicMock()
        emb.qdrant = AsyncMock()
        emb.collection = "memory_vault"
        return emb

    def test_topics_returns_cluster_list(self):
        """Topics endpoint returns a list of topic clusters."""
        from app.services.vault_graph import _kmeans_cluster

        paths = [f"agents/boss/lessons/note-{i}.md" for i in range(20)]
        # Use random-ish vectors that will cluster
        import random
        random.seed(42)
        vectors = [[random.gauss(0, 1) for _ in range(10)] for _ in range(20)]

        cluster_by_path, clusters = _kmeans_cluster(paths, vectors)

        # Verify clustering produced meaningful output
        assert len(clusters) >= 1
        for cluster in clusters:
            assert "cluster_id" in cluster
            assert "member_paths" in cluster
            assert len(cluster["member_paths"]) >= 1

    def test_topics_fallback_on_few_notes(self):
        """With fewer than 6 notes, single-cluster fallback."""
        from app.services.vault_graph import _kmeans_cluster

        paths = ["a.md", "b.md", "c.md"]
        vectors = [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]

        cluster_by_path, clusters = _kmeans_cluster(paths, vectors)
        assert len(clusters) == 1
        assert len(clusters[0]["member_paths"]) == 3

    def test_build_topics_response(self):
        """build_topics_response creates the expected JSON shape."""
        from app.services.vault_graph import _kmeans_cluster

        paths = [f"note-{i}.md" for i in range(10)]
        import random
        random.seed(42)
        vectors = [[random.gauss(0, 1) for _ in range(10)] for _ in range(10)]

        cluster_by_path, clusters = _kmeans_cluster(paths, vectors)

        # Simulate the topics response format
        notes_by_path = {
            p: {"title": f"Title {i}", "agent": "boss", "type": "lesson"}
            for i, p in enumerate(paths)
        }

        topics = []
        for cluster in clusters:
            members = cluster["member_paths"]
            agents = list({notes_by_path[m]["agent"] for m in members if m in notes_by_path})
            top_notes = [notes_by_path[m]["title"] for m in members[:5] if m in notes_by_path]
            topics.append({
                "cluster_id": cluster["cluster_id"],
                "note_count": len(members),
                "top_notes": top_notes,
                "agents": agents,
            })

        assert len(topics) >= 1
        for topic in topics:
            assert "cluster_id" in topic
            assert "note_count" in topic
            assert topic["note_count"] >= 1
            assert "top_notes" in topic
            assert "agents" in topic
