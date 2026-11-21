"""A hosted provider MC cannot probe must never read as "stopped".

Live (user test 21.11.2026): every Claude runtime row has the endpoint
``https://api.anthropic.com/v1/messages``. ``get_runtime_state`` probed it
like an OpenAI engine (``endpoint + /v1/models``) and ``join_probe_url``
only shortened a trailing ``/v1`` — so the backend asked
``…/v1/messages/v1/models`` (404, 306 times an hour) and reported all nine
runtimes as ``stopped``. These rows sign in through their own CLI; MC holds
no key to ask the provider with, so the honest answer is "not checked".
"""

import pytest

from app.services import runtime_manager
from app.services.runtime_manager import get_runtime_state, join_probe_url

ANTHROPIC = "https://api.anthropic.com/v1/messages"


def _cloud(endpoint: str, healthcheck_path: str | None = None) -> dict:
    return {
        "id": "rt-1",
        "slug": "anthropic-claude-test",
        "runtime_type": "cloud",
        "endpoint": endpoint,
        "healthcheck_path": healthcheck_path,
    }


@pytest.fixture
def probes(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def fake_probe(endpoint, healthcheck_path):
        seen.append((endpoint, healthcheck_path))
        return False

    monkeypatch.setattr(runtime_manager, "_probe_http", fake_probe)
    return seen


@pytest.mark.asyncio
async def test_cli_signed_in_cloud_provider_is_not_probed_and_not_stopped(probes):
    state = await get_runtime_state(_cloud(ANTHROPIC))

    assert probes == []
    assert state["state"] != "stopped"
    assert state["state"] == "unknown"
    assert state["http_reachable"] is None
    # Same slot as today's cloud answer: the runtimes page shows nothing new.
    assert state["container_status"] is None


@pytest.mark.asyncio
async def test_cloud_provider_with_a_public_models_list_is_still_probed(probes):
    """Ollama Cloud answers /v1/models without a key (live: 200) — keep it."""
    await get_runtime_state(_cloud("https://ollama.com/v1", "/models"))

    assert probes == [("https://ollama.com/v1", "/models")]


@pytest.mark.asyncio
async def test_unknown_cloud_endpoint_is_still_probed(probes):
    state = await get_runtime_state(_cloud("https://llm.example.test/v1"))

    assert probes == [("https://llm.example.test/v1", "/v1/models")]
    assert state["state"] == "stopped"  # the probe said no — unchanged


@pytest.mark.parametrize(
    ("endpoint", "path", "expected"),
    [
        (ANTHROPIC, "/v1/models", "https://api.anthropic.com/v1/models"),
        (ANTHROPIC, None, "https://api.anthropic.com/v1/models"),
        (ANTHROPIC + "/", "/models", "https://api.anthropic.com/v1/models"),
        ("http://box:8000/v1/chat/completions", "/v1/models", "http://box:8000/v1/models"),
        ("http://box:8000/v1/completions", "/models", "http://box:8000/v1/models"),
    ],
)
def test_an_api_route_is_never_used_as_the_probe_base(endpoint, path, expected):
    url = join_probe_url(endpoint, path)
    assert url == expected
    assert "/messages/" not in url
