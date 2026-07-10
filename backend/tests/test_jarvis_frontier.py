"""Tests for jarvis_core.frontier (ADR-062).

The OpenAI HTTP client is a fake that returns scripted chat-completion responses
(or raises realistic httpx errors), so the delegation, selective fallback,
token cap, and hard-cap timeout are exercised without any network or API key.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from jarvis_core import frontier


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _text_payload(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


def _http_status_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _FakeHTTP:
    """Returns scripted responses in order; records model + body per call.

    A scripted item may be a dict (→ _FakeResponse), or an Exception to raise.
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.is_closed = False
        self.models: list[str] = []
        self.bodies: list[dict] = []

    async def post(self, url, headers=None, json=None, **kwargs):
        self.models.append(json["model"])
        self.bodies.append(json)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


# ── resolve_model ────────────────────────────────────────────────────────


def test_resolve_model_precedence(monkeypatch):
    monkeypatch.delenv("JARVIS_FRONTIER_MODEL", raising=False)
    assert frontier.resolve_model() == frontier.DEFAULT_FRONTIER_MODEL
    assert frontier.resolve_model("gpt-x") == "gpt-x"
    monkeypatch.setenv("JARVIS_FRONTIER_MODEL", "gpt-env")
    assert frontier.resolve_model() == "gpt-env"
    assert frontier.resolve_model("gpt-explicit") == "gpt-explicit"  # explicit wins


# ── _should_fallback ─────────────────────────────────────────────────────


def test_should_fallback_matrix():
    assert frontier._should_fallback(httpx.ReadTimeout("t")) is True
    assert frontier._should_fallback(_http_status_error(500)) is True
    assert frontier._should_fallback(_http_status_error(503)) is True
    assert frontier._should_fallback(_http_status_error(404)) is True
    assert frontier._should_fallback(_http_status_error(400, "unknown model foo")) is True
    # No fallback on auth / bad-request-without-model / rate-limit
    assert frontier._should_fallback(_http_status_error(401)) is False
    assert frontier._should_fallback(_http_status_error(403)) is False
    assert frontier._should_fallback(_http_status_error(429)) is False
    assert frontier._should_fallback(_http_status_error(400, "bad param")) is False


# ── complete_text ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_text_happy_path_sets_token_cap():
    http = _FakeHTTP([_text_payload("  Antwort.  ")])
    out = await frontier.complete_text(
        system="S", user="U", api_key="sk", model="gpt-test",
        http_client=http, max_tokens=800,
    )
    assert out == "Antwort."
    assert http.models == ["gpt-test"]
    # max_completion_tokens (not max_tokens) so reasoning models accept it.
    assert http.bodies[0]["max_completion_tokens"] == 800
    assert "max_tokens" not in http.bodies[0]


@pytest.mark.asyncio
async def test_complete_text_falls_back_on_5xx():
    http = _FakeHTTP([_http_status_error(500), _text_payload("Fallback-Antwort")])
    out = await frontier.complete_text(
        system="S", user="U", api_key="sk", model="gpt-primary",
        http_client=http, fallback_model="gpt-4o",
    )
    assert out == "Fallback-Antwort"
    assert http.models == ["gpt-primary", "gpt-4o"]


@pytest.mark.asyncio
async def test_complete_text_no_fallback_on_401():
    http = _FakeHTTP([_http_status_error(401)])  # only one scripted response
    with pytest.raises(httpx.HTTPStatusError):
        await frontier.complete_text(
            system="S", user="U", api_key="sk", model="gpt-primary",
            http_client=http, fallback_model="gpt-4o",
        )
    assert http.models == ["gpt-primary"]  # fallback NOT attempted


@pytest.mark.asyncio
async def test_complete_text_falls_back_on_timeout():
    http = _FakeHTTP([httpx.ReadTimeout("slow"), _text_payload("ok")])
    out = await frontier.complete_text(
        system="S", user="U", api_key="sk", model="gpt-primary",
        http_client=http, fallback_model="gpt-4o",
    )
    assert out == "ok"
    assert http.models == ["gpt-primary", "gpt-4o"]


@pytest.mark.asyncio
async def test_complete_text_raises_when_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        await frontier.complete_text(system="S", user="U", model="m")


# ── ask_frontier ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_frontier_returns_answer_with_token_cap():
    http = _FakeHTTP([_text_payload("Die Analyse ergibt X.")])
    res = await frontier.ask_frontier(
        "Was ist besser, A oder B?", context_hint="Projektkontext",
        api_key="sk", model="gpt-test", http_client=http,
    )
    assert res["ok"] is True
    assert "Analyse" in res["answer"]
    assert res["model"] == "gpt-test"
    assert http.bodies[0]["max_completion_tokens"] == frontier.ASK_FRONTIER_MAX_TOKENS


@pytest.mark.asyncio
async def test_ask_frontier_no_fallback_error_surfaced():
    http = _FakeHTTP([_http_status_error(401)])
    res = await frontier.ask_frontier(
        "Frage?", api_key="sk", model="gpt-primary", http_client=http,
    )
    assert res["ok"] is False
    assert "error" in res
    assert http.models == ["gpt-primary"]  # no fallback on 401


@pytest.mark.asyncio
async def test_ask_frontier_hard_cap_timeout(monkeypatch):
    monkeypatch.setattr(frontier, "ASK_FRONTIER_HARD_CAP_SECONDS", 0.02)

    class _SlowHTTP:
        is_closed = False

        async def post(self, *a, **k):
            await asyncio.sleep(0.2)  # exceeds the hard cap
            return _FakeResponse(_text_payload("too late"))

    res = await frontier.ask_frontier(
        "Analysier alles", api_key="sk", model="gpt-test", http_client=_SlowHTTP(),
    )
    assert res["ok"] is False
    assert res["reason"] == "timeout"
    assert "zu lange" in res["message"]


@pytest.mark.asyncio
async def test_ask_frontier_empty_answer_is_honest_error():
    http = _FakeHTTP([_text_payload("")])
    res = await frontier.ask_frontier(
        "Frage?", api_key="sk", model="gpt-test", http_client=http,
    )
    assert res["ok"] is False
