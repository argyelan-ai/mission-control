import pytest
from unittest.mock import AsyncMock, patch
import httpx
from app.services.spark_client import SparkClient, SparkUnreachableError


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check_returns_model_name():
    """Integration test — checks live Spark endpoint health.

    Model identifier is no longer asserted to a fixed string because it is
    resolved dynamically from the DB at runtime (and changes when the
    sparkrun recipe is swapped). We just assert it's a non-empty string.
    """
    client = SparkClient()
    health = await client.health_check()
    assert isinstance(health["llm_model"], str) and health["llm_model"]
    assert health["embedding_model"] == "text-embedding-nomic-embed-text-v1.5"
    assert health["llm_ready"] is True
    assert health["embedding_ready"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_returns_response():
    client = SparkClient()
    out = await client.complete(
        prompt="Reply with the single word: hello",
        max_tokens=10,
        temperature=0.0,
    )
    assert "hello" in out.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_embed_returns_768_dim_vector():
    client = SparkClient()
    vec = await client.embed("hello world")
    assert len(vec) == 768
    assert all(isinstance(x, float) for x in vec)


@pytest.mark.asyncio
async def test_unreachable_raises_clear_error():
    """If the vLLM endpoint is down, ``complete`` must surface a clear error.

    Mocks the resolver so the test stays hermetic (no DB / Redis required).
    """
    resolver_mock = AsyncMock(return_value="test-fallback-model")
    with patch(
        "app.services.runtime_model_resolver.get_active_spark_model",
        resolver_mock,
    ):
        with patch(
            "httpx.AsyncClient.post",
            side_effect=httpx.ConnectError("conn refused"),
        ):
            client = SparkClient(llm_url="http://192.0.2.99:9999/v1")
            with pytest.raises(SparkUnreachableError) as exc:
                await client.complete("test")
            assert "192.0.2.99" in str(exc.value)
