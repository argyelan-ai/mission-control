"""The chat asks whether the agent's engine is working while its live row is
quiet (``GET /agents/{id}/chat/engine``, ``services/chat_engine_activity``).

Unknown is never busy: no runtime, a cloud runtime or a failed probe give
``engine: null``. One probe per engine per TTL."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import chat_engine_activity
from app.services.heads import engine
from tests.test_recipe_switcher_p3 import _host, _runtime


@pytest.fixture(autouse=True)
def _fresh_cache():
    chat_engine_activity.clear_cache()
    yield
    chat_engine_activity.clear_cache()


def _engine_says(running, reason=None, calls=None):
    async def fake(endpoint):
        if calls is not None:
            calls.append(endpoint)
        return running, reason

    return patch.object(engine, "probe_running_requests", fake)


async def _bound_agent(session, make_agent, **runtime_kw):
    box = await _host(session, "box-a")
    rt = await _runtime(session, "local-engine", box, **runtime_kw)
    return await make_agent(name="agent-a", slug="agent-a", runtime_id=rt.id)


@pytest.mark.asyncio
async def test_busy_engine_reads_busy(auth_client, session, make_agent):
    agent = await _bound_agent(session, make_agent)
    with _engine_says(1):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/engine")
    assert resp.status_code == 200, resp.text
    body = resp.json()["engine"]
    assert body["busy"] is True
    assert body["running"] == 1
    assert body["checkedAt"].endswith("Z")


@pytest.mark.asyncio
async def test_idle_engine_reads_not_busy(auth_client, session, make_agent):
    agent = await _bound_agent(session, make_agent)
    with _engine_says(0):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/engine")
    assert resp.json()["engine"]["busy"] is False


@pytest.mark.asyncio
async def test_probe_failure_is_absent_never_busy(auth_client, session, make_agent):
    agent = await _bound_agent(session, make_agent)
    with _engine_says(None, "unreachable"):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/engine")
    assert resp.json() == {"engine": None}


@pytest.mark.asyncio
async def test_probe_exception_is_absent(auth_client, session, make_agent):
    agent = await _bound_agent(session, make_agent)

    async def boom(endpoint):
        raise RuntimeError("socket gone")

    with patch.object(engine, "probe_running_requests", boom):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/engine")
    assert resp.json() == {"engine": None}


@pytest.mark.asyncio
async def test_agent_without_runtime_is_absent_and_never_probes(auth_client, make_agent):
    agent = await make_agent(name="agent-b", slug="agent-b")
    calls: list[str] = []
    with _engine_says(5, calls=calls):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/engine")
    assert resp.json() == {"engine": None}
    assert calls == []


@pytest.mark.asyncio
async def test_cloud_runtime_is_absent_and_never_probes(auth_client, session, make_agent):
    from app.models.runtime import Runtime

    rt = Runtime(slug="cloud-api", display_name="cloud", runtime_type="openai_compatible",
                 endpoint="https://api.example.com/v1")
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    agent = await make_agent(name="agent-c", slug="agent-c", runtime_id=rt.id)
    calls: list[str] = []
    with _engine_says(5, calls=calls):
        resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/engine")
    assert resp.json() == {"engine": None}
    assert calls == []


@pytest.mark.asyncio
async def test_one_probe_per_engine_within_ttl(auth_client, session, make_agent):
    agent = await _bound_agent(session, make_agent)
    calls: list[str] = []
    with _engine_says(2, calls=calls):
        for _ in range(4):
            resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/engine")
            assert resp.json()["engine"]["busy"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch):
    calls: list[str] = []
    clock = [1000.0]
    monkeypatch.setattr(chat_engine_activity.time, "monotonic", lambda: clock[0])
    with _engine_says(1, calls=calls):
        await chat_engine_activity.engine_state("http://192.0.2.10:8000/v1")
        clock[0] += chat_engine_activity.TTL_S + 0.1
        await chat_engine_activity.engine_state("http://192.0.2.10:8000/v1")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unknown_agent_is_404(auth_client):
    import uuid

    resp = await auth_client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/engine")
    assert resp.status_code == 404
