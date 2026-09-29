"""A8 — runtime_protocols(): classified protocol + probed Anthropic route."""
from __future__ import annotations

import httpx
import pytest

from app.models.runtime import Runtime
from app.services import runtime_protocols as rp


@pytest.fixture(autouse=True)
def _clear():
    rp.clear_cache()
    yield
    rp.clear_cache()


def _rt(**kw) -> Runtime:
    base = dict(slug="local-glm", display_name="GLM local", runtime_type="vllm_docker",
                endpoint="http://127.0.0.1:8000/v1", model_identifier="glm")
    base.update(kw)
    return Runtime(**base)


def _mock(monkeypatch, status: int, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, json={"error": "x"})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(rp.httpx, "AsyncClient", factory)


async def test_vllm_answering_400_serves_anthropic_too(monkeypatch):
    calls: list = []
    _mock(monkeypatch, 400, calls)
    assert await rp.runtime_protocols(_rt()) == {"openai", "anthropic"}
    assert calls == ["http://127.0.0.1:8000/v1/messages"]


async def test_404_means_openai_only(monkeypatch):
    _mock(monkeypatch, 404, [])
    assert await rp.runtime_protocols(_rt()) == {"openai"}


async def test_probe_is_cached_per_runtime(monkeypatch):
    calls: list = []
    _mock(monkeypatch, 400, calls)
    rt = _rt()
    await rp.runtime_protocols(rt, now=1000.0)
    await rp.runtime_protocols(rt, now=1100.0)
    assert len(calls) == 1
    await rp.runtime_protocols(rt, now=1000.0 + rp.PROBE_TTL_S + 1)
    assert len(calls) == 2


async def test_anthropic_runtime_is_not_probed(monkeypatch):
    calls: list = []
    _mock(monkeypatch, 400, calls)
    rt = _rt(slug="anthropic-claude-opus", runtime_type="cloud",
             endpoint="https://api.anthropic.com/v1/messages")
    assert await rp.runtime_protocols(rt) == {"anthropic"}
    assert calls == []


async def test_unreachable_engine_is_openai_only(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    real = httpx.AsyncClient
    monkeypatch.setattr(rp.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    assert await rp.runtime_protocols(_rt()) == {"openai"}


def test_engine_root_strips_v1():
    assert rp.engine_root("http://h:8000/v1") == "http://h:8000"
    assert rp.engine_root("http://h:8000/v1/") == "http://h:8000"
    assert rp.engine_root("http://h:8000") == "http://h:8000"
