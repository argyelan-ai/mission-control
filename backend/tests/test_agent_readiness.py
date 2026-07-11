"""Readiness check after wizard provisioning (2026-07-10).

Step 5's readiness gate. The synchronous trigger endpoint is retired
(410, Phase 29 gateway sunset), so 'ready' means provisioned + live, not
a round-tripped test message.
"""
import pytest

import app.routers.agents as agents_module
import app.routers.cli_terminal as cli_terminal


@pytest.mark.asyncio
async def test_readiness_cli_bridge_all_green(auth_client, make_agent, monkeypatch):
    monkeypatch.setattr(cli_terminal, "_bridge_get", lambda path: {"ok": True})
    agent = await make_agent(
        "Ready One", agent_runtime="cli-bridge",
        provision_status="provisioned", status="idle",
    )
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/health-check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["runtime"] == "cli-bridge"
    assert all(c["ok"] for c in body["checks"])


@pytest.mark.asyncio
async def test_readiness_cli_bridge_helper_down(auth_client, make_agent, monkeypatch):
    monkeypatch.setattr(cli_terminal, "_bridge_get", lambda path: None)
    agent = await make_agent(
        "Not Ready", agent_runtime="cli-bridge",
        provision_status="provisioned", status="offline",
    )
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/health-check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    helper = [c for c in body["checks"] if c["label"] == "cli-bridge helper"][0]
    assert helper["ok"] is False


@pytest.mark.asyncio
async def test_readiness_unknown_agent_404s(auth_client):
    import uuid
    resp = await auth_client.post(f"/api/v1/agents/{uuid.uuid4()}/health-check")
    assert resp.status_code == 404
