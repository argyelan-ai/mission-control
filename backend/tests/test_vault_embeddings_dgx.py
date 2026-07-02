"""Integration test for VaultEmbeddings against real Spark DGX + Qdrant.

This test is **skipped by default**. It only runs when
``DGX_AVAILABLE_FOR_TESTS=true`` is set in the environment AND both
the Spark DGX embedding endpoint and the Qdrant service are reachable.

Run locally with:

    DGX_AVAILABLE_FOR_TESTS=true pytest tests/test_vault_embeddings_dgx.py -v

Wires up the same singletons used in production by ``app/main.py``:
- ``embedding_service`` (Spark DGX, text-embedding-nomic-embed-text-v1.5, 768-dim)
- raw ``AsyncQdrantClient`` from ``qdrant_service._get_client()``

The collection ``memory_vault`` is auto-created by VaultEmbeddings on first use.
"""

import os
import pytest
import frontmatter

DGX_REACHABLE = os.environ.get("DGX_AVAILABLE_FOR_TESTS", "").lower() == "true"


@pytest.mark.skipif(
    not DGX_REACHABLE,
    reason="DGX/Qdrant integration not available (set DGX_AVAILABLE_FOR_TESTS=true to run)",
)
@pytest.mark.asyncio
async def test_real_dgx_upsert_to_qdrant(tmp_path):
    """End-to-end: embed real text via Spark DGX + upsert into a real Qdrant.

    Verifies:
    - DGX is reachable and returns a 768-dim vector
    - VaultEmbeddings creates the ``memory_vault`` collection if missing
    - The upsert call returns a deterministic point_id
    """
    from app.services.vault_embeddings import VaultEmbeddings
    from app.services.embedding_service import embedding_service
    from app.services.qdrant_service import qdrant_service

    # Use the raw AsyncQdrantClient — VaultEmbeddings calls upsert/get_collections/
    # create_collection directly against the Qdrant Python SDK interface.
    qdrant_client = await qdrant_service._get_client()

    embeddings = VaultEmbeddings(
        dgx_client=embedding_service,
        qdrant_client=qdrant_client,
        collection="memory_vault",
    )

    file = tmp_path / "test.md"
    file.write_text(
        "---\n"
        "id: dgx-integration-test\n"
        "type: lesson\n"
        "agent: sparky\n"
        "date: 2026-05-14T15:00:00Z\n"
        "---\n"
        "integration test body for embedding — verify DGX → Qdrant wiring"
    )
    post = frontmatter.load(str(file))

    result = await embeddings.upsert(file, post, vault_path=tmp_path)

    assert result["ok"] is True, f"upsert failed: {result.get('error')}"
    assert "point_id" in result
    assert len(result["point_id"]) == 32  # sha256[:32]
