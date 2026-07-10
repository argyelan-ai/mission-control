"""Tests for jarvis_core.frontier (ADR-062).

The OpenAI HTTP client is a fake that returns scripted chat-completion
responses, so the delegation + fallback chain are exercised without any network
or API key.
"""
from __future__ import annotations

import pytest

from jarvis_core import frontier


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self):
        return self._payload


def _text_payload(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


class _FakeHTTP:
    """Returns scripted responses in order; records the model per call."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.is_closed = False
        self.models: list[str] = []

    async def post(self, url, headers=None, json=None, **kwargs):
        self.models.append(json["model"])
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_resolve_model_precedence(monkeypatch):
    monkeypatch.delenv("JARVIS_FRONTIER_MODEL", raising=False)
    assert frontier.resolve_model() == frontier.DEFAULT_FRONTIER_MODEL
    assert frontier.resolve_model("gpt-x") == "gpt-x"
    monkeypatch.setenv("JARVIS_FRONTIER_MODEL", "gpt-env")
    assert frontier.resolve_model() == "gpt-env"
    assert frontier.resolve_model("gpt-explicit") == "gpt-explicit"  # explicit wins


@pytest.mark.asyncio
async def test_complete_text_happy_path():
    http = _FakeHTTP([_FakeResponse(_text_payload("  Antwort.  "))])
    out = await frontier.complete_text(
        system="S", user="U", api_key="sk", model="gpt-test", http_client=http,
    )
    assert out == "Antwort."
    assert http.models == ["gpt-test"]


@pytest.mark.asyncio
async def test_complete_text_falls_back_on_primary_failure():
    http = _FakeHTTP([
        _FakeResponse({}, status=500),           # primary fails
        _FakeResponse(_text_payload("Fallback-Antwort")),  # fallback ok
    ])
    out = await frontier.complete_text(
        system="S", user="U", api_key="sk", model="gpt-primary",
        http_client=http, fallback_model="gpt-4o",
    )
    assert out == "Fallback-Antwort"
    assert http.models == ["gpt-primary", "gpt-4o"]


@pytest.mark.asyncio
async def test_complete_text_raises_when_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        await frontier.complete_text(system="S", user="U", model="m")


@pytest.mark.asyncio
async def test_ask_frontier_returns_answer():
    http = _FakeHTTP([_FakeResponse(_text_payload("Die Analyse ergibt X."))])
    res = await frontier.ask_frontier(
        "Was ist besser, A oder B?", context_hint="Projektkontext",
        api_key="sk", model="gpt-test", http_client=http,
    )
    assert res["ok"] is True
    assert "Analyse" in res["answer"]
    assert res["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_ask_frontier_error_is_surfaced_as_data():
    http = _FakeHTTP([
        _FakeResponse({}, status=500),  # primary fails
        _FakeResponse({}, status=500),  # fallback fails too
    ])
    res = await frontier.ask_frontier(
        "Frage?", api_key="sk", model="gpt-primary", http_client=http,
    )
    assert res["ok"] is False
    assert "error" in res


@pytest.mark.asyncio
async def test_ask_frontier_empty_answer_is_honest_error():
    http = _FakeHTTP([_FakeResponse(_text_payload(""))])
    res = await frontier.ask_frontier(
        "Frage?", api_key="sk", model="gpt-test", http_client=http,
    )
    assert res["ok"] is False
