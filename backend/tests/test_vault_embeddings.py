import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
from types import SimpleNamespace
import frontmatter
from app.services.vault_embeddings import VaultEmbeddings


@pytest.fixture
def mock_dgx():
    client = MagicMock()
    client.embed = AsyncMock(return_value=[0.1] * 768)
    return client


@pytest.fixture
def mock_qdrant():
    """Mocks the raw AsyncQdrantClient API used by VaultEmbeddings:
    - get_collections() → response.collections (iterable with .name)
    - create_collection(...) → coroutine (only called on first upsert if missing)
    - upsert(collection_name=, points=[...]) → coroutine
    """
    client = MagicMock()
    # Pretend the collection already exists so create_collection is not invoked
    # by default — individual tests can override to exercise the create path.
    existing = SimpleNamespace(collections=[SimpleNamespace(name="memory_vault")])
    client.get_collections = AsyncMock(return_value=existing)
    client.create_collection = AsyncMock(return_value=None)
    client.upsert = AsyncMock(return_value={"status": "ok"})
    return client


@pytest.fixture
def embeddings(mock_dgx, mock_qdrant):
    return VaultEmbeddings(dgx_client=mock_dgx, qdrant_client=mock_qdrant, collection="memory_vault")


@pytest.mark.asyncio
async def test_upsert_embeds_content_and_writes_to_qdrant(embeddings, mock_dgx, mock_qdrant, tmp_path):
    file = tmp_path / "test.md"
    file.write_text("---\nid: abc\ntype: lesson\nagent: sparky\ndate: 2026-05-14T15:00:00Z\n---\ntest body")
    post = frontmatter.load(str(file))

    result = await embeddings.upsert(file, post, vault_path=tmp_path)

    assert result["ok"] is True
    assert "point_id" in result
    mock_dgx.embed.assert_awaited_once_with("test body")
    mock_qdrant.upsert.assert_awaited_once()
    call_args = mock_qdrant.upsert.await_args
    assert call_args.kwargs["collection_name"] == "memory_vault"
    # Collection already existed in the mock → no create_collection call
    mock_qdrant.create_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_collection_auto_created_when_missing(mock_dgx, tmp_path):
    """When memory_vault is absent, VaultEmbeddings creates it (768-dim, Cosine)
    before the first upsert."""
    client = MagicMock()
    # First call: empty list → collection missing → triggers create
    empty = SimpleNamespace(collections=[])
    client.get_collections = AsyncMock(return_value=empty)
    client.create_collection = AsyncMock(return_value=None)
    client.upsert = AsyncMock(return_value={"status": "ok"})

    embeddings = VaultEmbeddings(dgx_client=mock_dgx, qdrant_client=client, collection="memory_vault")
    file = tmp_path / "test.md"
    file.write_text("---\nid: abc\ntype: lesson\nagent: sparky\n---\nbody")
    post = frontmatter.load(str(file))

    result = await embeddings.upsert(file, post, vault_path=tmp_path)

    assert result["ok"] is True
    client.create_collection.assert_awaited_once()
    create_kwargs = client.create_collection.await_args.kwargs
    assert create_kwargs["collection_name"] == "memory_vault"


@pytest.mark.asyncio
async def test_fail_soft_on_dgx_outage(embeddings, mock_dgx, mock_qdrant, tmp_path):
    mock_dgx.embed.side_effect = ConnectionError("DGX unreachable")
    file = tmp_path / "test.md"
    file.write_text("---\nid: abc\ntype: lesson\nagent: sparky\ndate: 2026-05-14T15:00:00Z\n---\nbody")
    post = frontmatter.load(str(file))

    # Should not raise — fail-soft
    result = await embeddings.upsert(file, post, vault_path=tmp_path)
    assert result["ok"] is False
    assert "DGX" in result["error"]
    assert result["kind"] == "dgx_failure"
    mock_qdrant.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_fail_soft_on_qdrant_outage(embeddings, mock_dgx, mock_qdrant, tmp_path):
    mock_qdrant.upsert.side_effect = RuntimeError("Qdrant down")
    file = tmp_path / "test.md"
    file.write_text("---\nid: abc\ntype: lesson\nagent: sparky\n---\nbody")
    post = frontmatter.load(str(file))

    result = await embeddings.upsert(file, post, vault_path=tmp_path)
    assert result["ok"] is False
    assert "Qdrant" in result["error"]
    assert result["kind"] == "qdrant_failure"
