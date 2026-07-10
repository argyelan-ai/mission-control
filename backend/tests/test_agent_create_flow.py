"""One-click agent creation (Day-2 basics, 2026-07-03).

"Neuer Agent" used to end at provision_status='local' with no hint what was
missing: the create endpoint deliberately skipped provisioning for
cli-bridge, the schema default was the retired 'openclaw' runtime (a latent
CHECK-constraint 500 for API callers), and nothing surfaced whether the
host-side cli-bridge helper was even running.
"""
import uuid

import pytest
from sqlmodel import select

import app.routers.agents as agents_module
import app.routers.cli_terminal as cli_terminal
from tests.conftest import test_engine


# ── Schema default ───────────────────────────────────────────────────────────

def test_agent_create_schema_defaults_to_cli_bridge():
    # 'openclaw' is retired (ADR-039) and forbidden by a CHECK constraint —
    # an API caller omitting agent_runtime got a 500 instead of an agent.
    assert agents_module.AgentCreate(name="x").agent_runtime == "cli-bridge"


@pytest.mark.asyncio
async def test_create_agent_without_runtime_yields_cli_bridge(auth_client, monkeypatch):
    async def _noop(agent_id, raw_token):
        return None

    monkeypatch.setattr(agents_module, "_auto_provision_cli_bridge", _noop)
    resp = await auth_client.post("/api/v1/agents", json={"name": "Fresh Default"})
    assert resp.status_code == 201
    assert resp.json()["agent_runtime"] == "cli-bridge"


# ── LLM runtime binding at create time ───────────────────────────────────────

@pytest.mark.asyncio
async def test_create_agent_with_runtime_slug_binds_runtime(
    auth_client, async_session, monkeypatch
):
    # The create dialog can bind the LLM runtime directly — previously this
    # was only possible post-create on the detail page (hidden from noobs),
    # so fresh agents silently fell back to docker-compose env.
    from app.models.runtime import Runtime

    rt = Runtime(
        slug="create-flow-rt",
        display_name="Create Flow RT",
        runtime_type="lmstudio",
        endpoint="http://example.com/v1",
        model_identifier="test-model",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    async def _noop(agent_id, raw_token):
        return None

    monkeypatch.setattr(agents_module, "_auto_provision_cli_bridge", _noop)

    resp = await auth_client.post(
        "/api/v1/agents",
        json={"name": "Bound At Birth", "runtime_id": "create-flow-rt"},
    )
    assert resp.status_code == 201
    assert resp.json()["runtime_id"] == str(rt.id)


@pytest.mark.asyncio
async def test_create_agent_with_unknown_runtime_404s(auth_client, monkeypatch):
    async def _noop(agent_id, raw_token):
        return None

    monkeypatch.setattr(agents_module, "_auto_provision_cli_bridge", _noop)
    resp = await auth_client.post(
        "/api/v1/agents",
        json={"name": "Bad Binding", "runtime_id": "does-not-exist"},
    )
    assert resp.status_code == 404


# ── Auto-provision wiring ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_cli_bridge_agent_schedules_auto_provision(auth_client, monkeypatch):
    calls: list[tuple[uuid.UUID, str]] = []

    async def _recorder(agent_id, raw_token):
        calls.append((agent_id, raw_token))

    monkeypatch.setattr(agents_module, "_auto_provision_cli_bridge", _recorder)

    resp = await auth_client.post(
        "/api/v1/agents", json={"name": "AutoProv", "agent_runtime": "cli-bridge"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(calls) == 1
    assert calls[0][0] == uuid.UUID(body["id"])
    # The token shown once in the UI must stay valid — auto-provision gets
    # the SAME raw token so it does not rotate it away seconds later.
    assert calls[0][1] == body["token"]


@pytest.mark.asyncio
async def test_create_manual_agent_does_not_auto_provision(auth_client, monkeypatch):
    calls = []

    async def _recorder(agent_id, raw_token):
        calls.append(agent_id)

    monkeypatch.setattr(agents_module, "_auto_provision_cli_bridge", _recorder)

    resp = await auth_client.post(
        "/api/v1/agents", json={"name": "Manual Guy", "agent_runtime": "manual"}
    )
    assert resp.status_code == 201
    assert calls == []


@pytest.mark.asyncio
async def test_create_host_agent_does_not_schedule_background_provisioning(
    auth_client, async_session, monkeypatch
):
    # Regression (2026-07-10): create_agent used to schedule
    # _provision_agent_background for host agents too, which hits the no-op
    # host stub and falsely flips provision_status -> "provisioned" (no files
    # staged), racing the wizard's explicit POST /provision call.
    calls = []

    async def _recorder(agent_id):
        calls.append(agent_id)

    monkeypatch.setattr(agents_module, "_provision_agent_background", _recorder)

    resp = await auth_client.post(
        "/api/v1/agents", json={"name": "Host Hopeful", "agent_runtime": "host"}
    )
    assert resp.status_code == 201
    assert calls == []

    from app.models.agent import Agent
    agent_id = uuid.UUID(resp.json()["id"])
    refreshed = await async_session.get(Agent, agent_id)
    assert refreshed.provision_status == "local"


# ── Auto-provision behavior ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_provision_bridge_down_stays_local_with_actionable_event(
    make_agent, async_session, monkeypatch, fake_redis
):
    monkeypatch.setattr("app.database.engine", test_engine)
    monkeypatch.setattr(cli_terminal, "_bridge_get", lambda path: None)

    # emit_event → sse.broadcast fetches Redis directly (not via Depends).
    import app.services.sse as sse_mod

    async def _fake_get_redis():
        return fake_redis

    monkeypatch.setattr(sse_mod, "get_redis", _fake_get_redis)

    agent = await make_agent("Bridgeless", provision_status="local")
    await agents_module._auto_provision_cli_bridge(agent.id, "tok-123")

    from app.models.agent import Agent
    refreshed = await async_session.get(Agent, agent.id)
    assert refreshed.provision_status == "local"

    from app.models.activity import ActivityEvent
    events = (await async_session.exec(select(ActivityEvent))).all()
    failed = [e for e in events if e.event_type == "agent.provision_failed"]
    assert failed, "expected an agent.provision_failed activity event"
    # The message must tell a noob WHAT to start and WHERE to read on.
    assert "cli-bridge" in failed[-1].title
    assert "first-agent" in failed[-1].title


@pytest.mark.asyncio
async def test_auto_provision_bridge_up_runs_full_chain(
    make_agent, monkeypatch
):
    monkeypatch.setattr("app.database.engine", test_engine)
    monkeypatch.setattr(cli_terminal, "_bridge_get", lambda path: {"ok": True})

    chain: list[str] = []

    async def _fake_provision_cli(agent_id, payload, session, current_user):
        chain.append(f"bridge:{payload.mc_token}")
        return {"provision_status": "provisioned", "bridge_result": {"ok": True}}

    async def _fake_background(agent_id):
        chain.append("container")

    monkeypatch.setattr(cli_terminal, "provision_cli_agent", _fake_provision_cli)
    monkeypatch.setattr(
        "app.services.provisioning.provision_agent_background", _fake_background
    )

    agent = await make_agent("Bridged", provision_status="local")
    await agents_module._auto_provision_cli_bridge(agent.id, "tok-456")

    # Order matters: first the host helper renders ~/.mc/agents/<slug>/,
    # then the container half (compose + file sync + start).
    assert chain == ["bridge:tok-456", "container"]


@pytest.mark.asyncio
async def test_auto_provision_never_raises(monkeypatch):
    # Best-effort background task: infra errors (DB down, bridge probe
    # exploding) must be logged, never crash the create request/worker.
    def _boom(path):
        raise RuntimeError("bridge probe exploded")

    monkeypatch.setattr(cli_terminal, "_bridge_get", _boom)
    # No engine patch on purpose — even an unreachable DB must not raise.
    await agents_module._auto_provision_cli_bridge(uuid.uuid4(), "tok-789")


# ── Token reuse in provision_cli_agent ───────────────────────────────────────

@pytest.mark.asyncio
async def test_provision_cli_agent_reuses_supplied_token(
    make_agent, async_session, monkeypatch
):
    captured: dict = {}

    def _fake_post(path, body, timeout=5):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True}

    async def _fake_sync(session, agent):
        return {}

    monkeypatch.setattr(cli_terminal, "_bridge_post", _fake_post)
    monkeypatch.setattr(
        "app.services.docker_agent_sync.sync_docker_agent_files", _fake_sync
    )

    agent = await make_agent("Token Keeper", agent_token_hash="orig-hash")
    payload = cli_terminal.CliProvisionPayload(mc_token="keep-me-token")
    result = await cli_terminal.provision_cli_agent(
        agent.id, payload, async_session, None
    )

    # Supplied token is used verbatim — no rotation.
    assert captured["body"]["mc_agent_token"] == "keep-me-token"
    assert result["token"] == "keep-me-token"

    from app.models.agent import Agent
    refreshed = await async_session.get(Agent, agent.id)
    assert refreshed.agent_token_hash == "orig-hash"


# ── Bridge health endpoint ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bridge_health_endpoint_up(auth_client, monkeypatch):
    monkeypatch.setattr(cli_terminal, "_bridge_get", lambda path: {"ok": True})
    resp = await auth_client.get("/api/v1/cli-bridge/health")
    assert resp.status_code == 200
    assert resp.json()["reachable"] is True


@pytest.mark.asyncio
async def test_bridge_health_endpoint_down(auth_client, monkeypatch):
    monkeypatch.setattr(cli_terminal, "_bridge_get", lambda path: None)
    resp = await auth_client.get("/api/v1/cli-bridge/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False
    # The UI needs the URL to tell the operator what exactly to start.
    assert "18792" in body["bridge_url"]
