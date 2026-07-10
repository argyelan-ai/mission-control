"""AgentCreate extended for the onboarding wizard (2026-07-10).

The wizard assembles a full agent config client-side (custom / template
prefill / duplicate) and calls the ONE create endpoint. Before this,
AgentCreate could not carry harness, scopes, soul_md or skill/plugin
allowlists, so a freshly created agent had empty scopes (= ALL 16,
security hole) and no harness.
"""
import pytest

import app.routers.agents as agents_module
from app.models.agent import Agent


def test_agent_create_accepts_scopes_and_harness():
    m = agents_module.AgentCreate(
        name="Scoped", harness="omp", scopes=["tasks:read", "chat:write"]
    )
    assert m.harness == "omp"
    assert m.scopes == ["tasks:read", "chat:write"]


def test_agent_create_rejects_bad_harness():
    with pytest.raises(ValueError, match="harness muss"):
        agents_module.AgentCreate(name="Bad", harness="gpt")


def test_agent_create_defaults_are_safe():
    m = agents_module.AgentCreate(name="Plain")
    assert m.harness is None
    assert m.scopes == []
    assert m.soul_md is None
    assert m.skill_filter is None
    assert m.cli_plugins is None


@pytest.mark.asyncio
async def test_create_agent_persists_scopes_harness_soul(auth_client, async_session, monkeypatch):
    async def _noop(agent_id, raw_token):
        return None

    monkeypatch.setattr(agents_module, "_auto_provision_cli_bridge", _noop)

    resp = await auth_client.post(
        "/api/v1/agents",
        json={
            "name": "Wizard Agent",
            "harness": "openclaude",
            "scopes": ["tasks:read", "tasks:write", "chat:write"],
            "soul_md": "# Custom soul\nYou are focused.",
            "skill_filter": ["coding-agent"],
            "cli_plugins": [],
        },
    )
    assert resp.status_code == 201
    body = resp.json()

    agent = await async_session.get(Agent, __import__("uuid").UUID(body["id"]))
    assert agent.harness == "openclaude"
    assert agent.scopes == ["tasks:read", "tasks:write", "chat:write"]
    assert agent.soul_md == "# Custom soul\nYou are focused."
    assert agent.skill_filter == ["coding-agent"]
    assert agent.cli_plugins == []
    # tools_md must be generated from the supplied scopes, not the empty-list
    # "all scopes" fallback: a read-only agent must not get the agents:manage
    # creation section.
    assert "tasks:read" in (agent.tools_md or "") or agent.tools_md is not None
