"""Insights distillation — which provider does the thinking.

Before this PR there was exactly one path: Ollama-native ``/api/generate``
against ``settings.ollama_url``, i.e. host.docker.internal — a LOCAL Ollama on
the Mac, which this hardware must never run. The default therefore moved to
the GPU box (OpenAI-compatible, DB-resolved model), with ollama.com as the
opt-in cloud arm and ``off`` as a first-class choice.

Every arm degrades to None: a failed distillation skips the daily report, it
never fails a task.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services import ai_provider_config
from app.services.intelligence import IntelligenceService

AI_KEYS = ["ai_insights_provider", "ai_insights_model"]


@pytest.fixture(autouse=True)
def _restore_settings():
    before = {k: getattr(settings, k) for k in AI_KEYS}
    yield
    for k, v in before.items():
        setattr(settings, k, v)


class _Config:
    """Stand-in for the Redis-stored IntelligenceConfig."""

    def __init__(self, ollama_model="qwen2.5-coder:14b", temperature=0.3, max_tokens=1024):
        self.ollama_model = ollama_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system_prompt = ""


def _mock_ollama_response(json_data, status_code=200, capture: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = str(json_data)
    resp.json.return_value = json_data

    async def post(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        return resp

    client = MagicMock()
    client.post = post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ── 1. The default is the GPU box, not local Ollama ──────────────────────


@pytest.mark.asyncio
async def test_default_provider_routes_to_the_gpu_box():
    service = IntelligenceService()
    complete = AsyncMock(return_value="  Drei Erkenntnisse.  ")
    with patch("app.services.spark_client.SparkClient.complete", complete):
        out = await service._call_insights_llm("prompt", _Config())

    assert out == "Drei Erkenntnisse."
    assert complete.await_args.kwargs["max_tokens"] == 1024
    assert complete.await_args.kwargs["temperature"] == 0.3
    # No model pinned -> the DB-driven runtime resolver decides, as it does
    # for every other Spark caller.
    assert complete.await_args.kwargs["model"] is None


@pytest.mark.asyncio
async def test_no_call_reaches_the_local_ollama_url():
    """The retired path. If this ever fires again, MC is one kernel panic
    away from taking the Mac down."""
    service = IntelligenceService()
    with patch("app.services.spark_client.SparkClient.complete", AsyncMock(return_value="ok")), \
         patch("httpx.AsyncClient") as client_cls:
        await service._call_insights_llm("prompt", _Config())
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_pinned_model_overrides_the_resolver():
    settings.ai_insights_model = "Qwen/Some-Other-Model"
    service = IntelligenceService()
    complete = AsyncMock(return_value="ok")
    with patch("app.services.spark_client.SparkClient.complete", complete):
        await service._call_insights_llm("prompt", _Config())
    assert complete.await_args.kwargs["model"] == "Qwen/Some-Other-Model"


@pytest.mark.asyncio
async def test_spark_outage_degrades_to_none():
    from app.services.spark_client import SparkUnreachableError

    service = IntelligenceService()
    with patch(
        "app.services.spark_client.SparkClient.complete",
        AsyncMock(side_effect=SparkUnreachableError("box is off")),
    ):
        assert await service._call_insights_llm("prompt", _Config()) is None


# ── 2. off ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_makes_no_call_at_all():
    settings.ai_insights_provider = "off"
    service = IntelligenceService()
    with patch("app.services.spark_client.SparkClient.complete", AsyncMock()) as spark, \
         patch("httpx.AsyncClient") as client_cls:
        assert await service._call_insights_llm("prompt", _Config()) is None
    spark.assert_not_awaited()
    client_cls.assert_not_called()


# ── 3. ollama_cloud — the explicit ollama_api_key consumer ───────────────


@pytest.mark.asyncio
async def test_ollama_cloud_sends_the_named_key_to_ollama_dot_com(monkeypatch):
    settings.ai_insights_provider = "ollama_cloud"
    capture: dict = {}
    monkeypatch.setattr(
        ai_provider_config, "get_ollama_api_key", AsyncMock(return_value="oll-TESTONLY")
    )
    service = IntelligenceService()
    with patch(
        "httpx.AsyncClient",
        return_value=_mock_ollama_response({"response": "Bericht"}, capture=capture),
    ):
        out = await service._call_insights_llm("prompt", _Config())

    assert out == "Bericht"
    assert capture["url"] == "https://ollama.com/api/generate"
    assert capture["headers"]["Authorization"] == "Bearer oll-TESTONLY"
    assert capture["json"]["model"] == settings.ollama_cloud_insights_model
    assert capture["json"]["options"] == {"temperature": 0.3, "num_predict": 1024}


@pytest.mark.asyncio
async def test_ollama_cloud_without_a_key_warns_and_still_reports_the_rejection(
    monkeypatch, caplog
):
    settings.ai_insights_provider = "ollama_cloud"
    monkeypatch.setattr(
        ai_provider_config, "get_ollama_api_key", AsyncMock(return_value=None)
    )
    service = IntelligenceService()
    with patch(
        "httpx.AsyncClient",
        return_value=_mock_ollama_response({"error": "unauthorized"}, status_code=401),
    ), caplog.at_level("WARNING", logger="mc.intelligence"):
        assert await service._call_insights_llm("prompt", _Config()) is None
    assert any("ollama_api_key" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_ollama_cloud_model_precedence(monkeypatch):
    """Pinned setting wins; an untouched legacy Redis value is ignored; a
    deliberately changed one is honoured."""
    settings.ai_insights_provider = "ollama_cloud"
    monkeypatch.setattr(
        ai_provider_config, "get_ollama_api_key", AsyncMock(return_value="oll-x")
    )
    service = IntelligenceService()

    async def model_used(config) -> str:
        capture: dict = {}
        with patch(
            "httpx.AsyncClient",
            return_value=_mock_ollama_response({"response": "x"}, capture=capture),
        ):
            await service._call_insights_llm("prompt", config)
        return capture["json"]["model"]

    # Untouched legacy default -> the cloud default, not a local 14b model.
    assert await model_used(_Config()) == settings.ollama_cloud_insights_model
    # Operator changed the legacy field -> honoured.
    assert await model_used(_Config(ollama_model="deepseek-v3.1:671b-cloud")) == (
        "deepseek-v3.1:671b-cloud"
    )
    # The new setting beats everything.
    settings.ai_insights_model = "pinned-model"
    assert await model_used(_Config(ollama_model="deepseek-v3.1:671b-cloud")) == (
        "pinned-model"
    )


@pytest.mark.asyncio
async def test_ollama_cloud_transport_failure_degrades_to_none(monkeypatch):
    settings.ai_insights_provider = "ollama_cloud"
    monkeypatch.setattr(
        ai_provider_config, "get_ollama_api_key", AsyncMock(return_value="oll-x")
    )
    service = IntelligenceService()
    with patch("httpx.AsyncClient", side_effect=RuntimeError("network gone")):
        assert await service._call_insights_llm("prompt", _Config()) is None
