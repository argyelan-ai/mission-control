"""GET /api/v1/runtimes ships which agent API key fits each runtime.

The agent config page offered every stored secret (other agents' MC tokens,
chat/social keys, …) as the agent's provider key. The key an agent binds is
only ever sent as OPENAI_API_KEY for an openai-protocol runtime
(harness_compat.resolve_provider_credentials). The server says which secret
provider fits, so the page does not re-derive vendor rules client-side:

* ``agent_key_used``: False when the runtime signs in on its own (anthropic
  OAuth, grok/kimi CLI logins) — an agent key is ignored there.
* ``agent_key_provider``: the ``secrets.provider`` whose keys fit, or None
  when no provider key applies (e.g. a local vLLM box).
"""
from unittest.mock import patch

import pytest

from app.models.runtime import Runtime


async def _stub_state(*_args, **_kwargs):
    return {"state": "ready", "http_reachable": True, "container_status": None}


@pytest.fixture
async def runtimes(async_session):
    rows = [
        Runtime(slug="akp-ollama", display_name="Cloud model", runtime_type="cloud",
                endpoint="https://ollama.com/v1", ui_order=1, enabled=True),
        Runtime(slug="anthropic-claude-akp", display_name="Claude", runtime_type="cloud",
                endpoint="https://api.anthropic.com", ui_order=2, enabled=True),
        Runtime(slug="akp-local", display_name="Local box", runtime_type="vllm_docker",
                endpoint="http://192.0.2.10:8000/v1", ui_order=3, enabled=True),
    ]
    for r in rows:
        async_session.add(r)
    await async_session.commit()
    return rows


@pytest.mark.asyncio
async def test_runtime_rows_carry_agent_key_fit(runtimes, auth_client):
    with patch("app.services.runtime_manager.get_runtime_state", side_effect=_stub_state):
        resp = await auth_client.get("/api/v1/runtimes")
    assert resp.status_code == 200, resp.text
    by_slug = {r["slug"]: r for r in resp.json()["runtimes"]}

    cloud = by_slug["akp-ollama"]
    assert cloud["agent_key_used"] is True
    assert cloud["agent_key_provider"] == "ollama"

    claude = by_slug["anthropic-claude-akp"]
    assert claude["agent_key_used"] is False
    assert claude["agent_key_provider"] is None

    local = by_slug["akp-local"]
    assert local["agent_key_used"] is True
    assert local["agent_key_provider"] is None
