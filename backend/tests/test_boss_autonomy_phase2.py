"""Tests for Phase 2/3/5 features — Boss Autonomy & Memory (2026-04-11).

Coverage:
- Spawn-approval endpoint (happy + dedupe + non-lead 403)
- Plugin-PATCH privilege guard (C4: board leads cannot set anything on each other)
- Memory-query helper + keyword fallback
- Memory-indexing layer mapping
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.board import Board
from app.models.memory import BoardMemory
from tests.conftest import test_engine


# ── Helpers ─────────────────────────────────────────────────────────────


async def _make_board_lead(
    name: str = "TestLead",
    board_id: uuid.UUID | None = None,
    is_board_lead: bool = True,
    token: str = "test-token-raw",
) -> tuple[Agent, str]:
    """Creates a board-lead agent + token, returns (agent, raw_token)."""
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    bid = board_id or uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Board must exist for the FK to work
        existing_board = await s.get(Board, bid)
        if not existing_board:
            board = Board(
                id=bid, name=f"Board-{name}", slug=f"b-{name.lower()}",
                require_review_before_done=True,
            )
            s.add(board)
            await s.commit()

        agent = Agent(
            id=uuid.uuid4(),
            board_id=bid,
            name=name,
            role="orchestrator" if is_board_lead else "developer",
            is_board_lead=is_board_lead,
            scopes=["agents:manage", "memory:read", "memory:write", "tasks:create", "tasks:write"],
            agent_token_hash=token_hash,
            model="glm-5.1:cloud",
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent, raw_token


# ── Spawn-Approval Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_request_board_lead_creates_approval(client):
    """Happy path: board lead → POST request-spawn → approval created."""
    agent, token = await _make_board_lead(name="BossRL1")

    resp = await client.post(
        "/api/v1/agent/agents/request-spawn",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "EphemeralHelper",
            "role": "researcher",
            "reason": "Research-Task fuer neues Projekt",
            "ephemeral": True,
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert "approval_id" in data

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = await s.get(Approval, uuid.UUID(data["approval_id"]))
        assert approval is not None
        assert approval.action_type == "spawn_agent"
        assert approval.payload["name"] == "EphemeralHelper"
        assert approval.payload["ephemeral"] is True


@pytest.mark.asyncio
async def test_spawn_request_non_lead_forbidden(client):
    """Non-board-lead gets 403, even with the agents:manage scope."""
    agent, token = await _make_board_lead(name="FakeLead", is_board_lead=False)

    resp = await client.post(
        "/api/v1/agent/agents/request-spawn",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Foo", "role": "developer", "reason": "test"},
    )
    assert resp.status_code == 403
    assert "board lead" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_spawn_request_dedupe_by_name(client):
    """Second spawn request with the same name is rejected with 409."""
    agent, token = await _make_board_lead(name="BossRL3")

    r1 = await client.post(
        "/api/v1/agent/agents/request-spawn",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "DupedName", "role": "developer", "reason": "erste"},
    )
    assert r1.status_code == 200

    r2 = await client.post(
        "/api/v1/agent/agents/request-spawn",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "DupedName", "role": "developer", "reason": "zweite"},
    )
    assert r2.status_code == 409
    assert "race-lock" in r2.json()["detail"].lower() or "bereits" in r2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_spawn_request_missing_required_fields(client):
    """Required fields name/role/reason missing → 400."""
    agent, token = await _make_board_lead(name="BossRL4")

    resp = await client.post(
        "/api/v1/agent/agents/request-spawn",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "", "role": "developer", "reason": "test"},
    )
    assert resp.status_code == 400


# ── Plugin-PATCH Privilege-Guard (C4) ────────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_patch_board_lead_cannot_set_other_lead(client):
    """C4: a board lead may not modify ANOTHER board lead."""
    bid = uuid.uuid4()
    boss, boss_token = await _make_board_lead(name="BossPL1", board_id=bid, is_board_lead=True)
    henry, _ = await _make_board_lead(name="HenryPL1", board_id=bid, is_board_lead=True)

    resp = await client.patch(
        f"/api/v1/agent/agents/{henry.id}/plugins",
        headers={"Authorization": f"Bearer {boss_token}"},
        json={"cli_plugins": ["malicious-plugin"]},
    )
    assert resp.status_code == 403
    assert "board-lead" in resp.json()["detail"].lower() or "selbst" in resp.json()["detail"].lower() or "self" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_plugin_patch_self_allowed(client):
    """Board lead may set their own plugins."""
    boss, boss_token = await _make_board_lead(name="BossPL2")

    resp = await client.patch(
        f"/api/v1/agent/agents/{boss.id}/plugins",
        headers={"Authorization": f"Bearer {boss_token}"},
        json={"cli_plugins": ["superpowers@claude-plugins-official"]},
    )
    # Can be 200 or possibly 500 on disk-sync fail (fail-soft), but not 403
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_plugin_patch_other_board_forbidden(client):
    """Cross-board plugin edit is blocked."""
    boss, boss_token = await _make_board_lead(name="BossPL3", board_id=uuid.uuid4())
    other, _ = await _make_board_lead(name="OtherWorker", board_id=uuid.uuid4(), is_board_lead=False)

    resp = await client.patch(
        f"/api/v1/agent/agents/{other.id}/plugins",
        headers={"Authorization": f"Bearer {boss_token}"},
        json={"cli_plugins": []},
    )
    assert resp.status_code == 403


# ── GET /plugins (shared cache list) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_list_plugins_board_lead_ok(client, monkeypatch):
    """Board lead may list the shared cache."""
    from app.services.plugin_manager import CliPlugin

    mock_plugins = [
        CliPlugin(key="superpowers@claude-plugins-official", name="superpowers", source="claude-plugins-official", version="1.0"),
        CliPlugin(key="higgsfield-mcp@anthropic-agent-skills", name="higgsfield-mcp", source="anthropic-agent-skills", version="0.2"),
    ]
    monkeypatch.setattr(
        "app.services.plugin_manager.list_available_plugins",
        lambda: mock_plugins,
    )

    boss, token = await _make_board_lead(name="BossPLIST1")

    resp = await client.get(
        "/api/v1/agent/plugins",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 2
    keys = [p["key"] for p in data["plugins"]]
    assert "superpowers@claude-plugins-official" in keys


@pytest.mark.asyncio
async def test_agent_list_plugins_non_lead_forbidden(client):
    """Worker without is_board_lead gets 403 even with the agents:manage scope."""
    worker, token = await _make_board_lead(name="FakePLIST", is_board_lead=False)

    resp = await client.get(
        "/api/v1/agent/plugins",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ── GET /agents/{id}/plugins (current assignment) ─────────────────────────


@pytest.mark.asyncio
async def test_agent_get_plugins_same_board_ok(client):
    """Board lead reads a worker's plugin assignment in the same board."""
    bid = uuid.uuid4()
    boss, boss_token = await _make_board_lead(name="BossGP1", board_id=bid)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.auth import generate_agent_token
        _, th = generate_agent_token()
        worker = Agent(
            id=uuid.uuid4(), board_id=bid, name="WorkerGP1", role="developer",
            is_board_lead=False, scopes=["tasks:write"], agent_token_hash=th,
            model="x", provision_status="provisioned",
            cli_plugins=["superpowers@claude-plugins-official"],
            agent_runtime="cli-bridge",
        )
        s.add(worker)
        await s.commit()
        worker_id = worker.id

    resp = await client.get(
        f"/api/v1/agent/agents/{worker_id}/plugins",
        headers={"Authorization": f"Bearer {boss_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["agent_name"] == "WorkerGP1"
    assert data["cli_plugins"] == ["superpowers@claude-plugins-official"]


@pytest.mark.asyncio
async def test_agent_get_plugins_cross_board_forbidden(client):
    """Cross-board GET is blocked analogous to PATCH."""
    bid_a = uuid.uuid4()
    bid_b = uuid.uuid4()
    boss, boss_token = await _make_board_lead(name="BossGPX", board_id=bid_a)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Board B to satisfy the FK
        s.add(Board(id=bid_b, name="OtherBoardGPX", slug="other-gpx"))
        await s.commit()
        from app.auth import generate_agent_token
        _, th = generate_agent_token()
        worker = Agent(
            id=uuid.uuid4(), board_id=bid_b, name="WorkerOtherGPX",
            role="developer", is_board_lead=False, scopes=["tasks:write"],
            agent_token_hash=th, model="x", provision_status="provisioned",
        )
        s.add(worker)
        await s.commit()
        worker_id = worker.id

    resp = await client.get(
        f"/api/v1/agent/agents/{worker_id}/plugins",
        headers={"Authorization": f"Bearer {boss_token}"},
    )
    assert resp.status_code == 403


# ── PATCH with restart_worker ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_patch_restart_worker_cli_bridge(client, monkeypatch):
    """restart_worker=true triggers a bridge call for cli-bridge agents."""
    bid = uuid.uuid4()
    boss, boss_token = await _make_board_lead(name="BossRW1", board_id=bid)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.auth import generate_agent_token
        _, th = generate_agent_token()
        worker = Agent(
            id=uuid.uuid4(), board_id=bid, name="WorkerRW1", role="developer",
            is_board_lead=False, scopes=["tasks:write"], agent_token_hash=th,
            model="x", provision_status="provisioned",
            agent_runtime="cli-bridge",
        )
        s.add(worker)
        await s.commit()
        worker_id = worker.id

    calls: list[str] = []
    def fake_bridge(path, _body):
        calls.append(path)
        return {"ok": True}
    monkeypatch.setattr("app.routers.cli_terminal._bridge_post", fake_bridge)

    resp = await client.patch(
        f"/api/v1/agent/agents/{worker_id}/plugins",
        headers={"Authorization": f"Bearer {boss_token}"},
        json={
            "cli_plugins": ["superpowers@claude-plugins-official"],
            "restart_worker": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["worker_restarted"] is True
    assert calls == ["/worker/workerrw1/restart"]


@pytest.mark.asyncio
async def test_plugin_patch_restart_worker_host_runtime_skipped(client, monkeypatch):
    """restart_worker=true is ignored for host runtime (no bridge call)."""
    boss, boss_token = await _make_board_lead(name="BossRW2")
    # Boss is host-runtime by default (no worker process)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Agent, boss.id)
        fresh.agent_runtime = "host"
        s.add(fresh)
        await s.commit()

    calls: list[str] = []
    monkeypatch.setattr(
        "app.routers.cli_terminal._bridge_post",
        lambda p, b: calls.append(p) or {"ok": True},
    )

    resp = await client.patch(
        f"/api/v1/agent/agents/{boss.id}/plugins",
        headers={"Authorization": f"Bearer {boss_token}"},
        json={"cli_plugins": [], "restart_worker": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["worker_restarted"] is False
    assert calls == []  # Bridge was NOT called


# ── POST /agents/{id}/worker/restart ─────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_restart_cli_bridge_ok(client, monkeypatch):
    bid = uuid.uuid4()
    boss, boss_token = await _make_board_lead(name="BossWR1", board_id=bid)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.auth import generate_agent_token
        _, th = generate_agent_token()
        worker = Agent(
            id=uuid.uuid4(), board_id=bid, name="WorkerWR1", role="developer",
            is_board_lead=False, scopes=["tasks:write"], agent_token_hash=th,
            model="x", provision_status="provisioned",
            agent_runtime="cli-bridge",
        )
        s.add(worker)
        await s.commit()
        worker_id = worker.id

    calls: list[str] = []
    monkeypatch.setattr(
        "app.routers.cli_terminal._bridge_post",
        lambda p, b: (calls.append(p), {"ok": True})[1],
    )

    resp = await client.post(
        f"/api/v1/agent/agents/{worker_id}/worker/restart",
        headers={"Authorization": f"Bearer {boss_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls == ["/worker/workerwr1/restart"]


@pytest.mark.asyncio
async def test_worker_restart_host_runtime_rejected(client):
    """Host-runtime agents have no worker → 400."""
    boss, boss_token = await _make_board_lead(name="BossWRH")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Agent, boss.id)
        fresh.agent_runtime = "host"
        s.add(fresh)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/agents/{boss.id}/worker/restart",
        headers={"Authorization": f"Bearer {boss_token}"},
    )
    assert resp.status_code == 400
    assert "agent_runtime" in resp.json()["detail"].lower()


# ── Memory-Query Helper Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_query_invalid_raises(client):
    from app.services.memory_query import run_memory_query, InvalidQueryError

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with pytest.raises(InvalidQueryError):
            await run_memory_query(s, query="", layers=["semantic"])
        with pytest.raises(InvalidQueryError):
            await run_memory_query(s, query="ok", layers=[])
        with pytest.raises(InvalidQueryError):
            await run_memory_query(s, query="ok", layers=["bogus"])
        with pytest.raises(InvalidQueryError):
            await run_memory_query(s, query="ok", layers=["semantic"], top_k=0)


@pytest.mark.asyncio
async def test_memory_query_keyword_fallback_on_embedding_fail():
    """When embedding_service.embed() raises, keyword fallback returns results."""
    from app.services.memory_query import run_memory_query

    # Test data: 1 semantic memory (knowledge) with a matching keyword
    mem_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        mem = BoardMemory(
            id=mem_id,
            title="Vercel Rollback Guide",
            content="Wenn ein Vercel Deploy broken ist, nutze gh cli vercel rollback.",
            memory_type="knowledge",  # → semantic layer
            source="test",
        )
        s.add(mem)
        await s.commit()

    # Patch embedding_service.embed to raise an error
    with patch(
        "app.services.embedding_service.embedding_service.embed",
        new=AsyncMock(side_effect=RuntimeError("Spark down")),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await run_memory_query(
                s, query="vercel rollback", layers=["semantic"], top_k=5,
            )

    assert result["fallback"] is True
    hits = result["results"]["semantic"]
    assert len(hits) == 1
    assert hits[0]["source"] == "keyword_fallback"
    assert "Vercel" in hits[0]["title"]


# ── Memory-Indexing Layer-Mapping Tests ──────────────────────────────────


def test_layer_for_semantic_types():
    from app.services.memory_indexing import layer_for

    m = BoardMemory(memory_type="knowledge", content="x", source="t")
    assert layer_for(m) == "semantic"
    m.memory_type = "reference"
    assert layer_for(m) == "semantic"
    m.memory_type = "research"
    assert layer_for(m) == "semantic"


def test_layer_for_episodic_types():
    from app.services.memory_indexing import layer_for

    m = BoardMemory(memory_type="journal", content="x", source="t")
    assert layer_for(m) == "episodic"
    m.memory_type = "weekly_review"
    assert layer_for(m) == "episodic"
    m.memory_type = "insight"
    assert layer_for(m) == "episodic"
    m.memory_type = "task_log"
    assert layer_for(m) == "episodic"


def test_layer_for_agent_lesson_needs_agent_id():
    from app.services.memory_indexing import layer_for

    m_no_agent = BoardMemory(memory_type="lesson", content="x", source="t", agent_id=None)
    assert layer_for(m_no_agent) is None  # lesson without agent_id falls out

    m_with_agent = BoardMemory(
        memory_type="lesson", content="x", source="t", agent_id=uuid.uuid4(),
    )
    assert layer_for(m_with_agent) == "agent"


def test_layer_for_unknown_type():
    from app.services.memory_indexing import layer_for

    m = BoardMemory(memory_type="random", content="x", source="t")
    assert layer_for(m) is None
