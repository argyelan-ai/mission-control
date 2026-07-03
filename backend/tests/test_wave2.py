"""
Tests for MC Product Wave 2 Features.

Theme 1: Task Hierarchy (Parent/Children, Report-Back, Credentials)
Theme 3: Autonomy (3-Tier Levels: L1/L2/L3, Defaults, Overrides)
Theme 4: Usage Analytics V1 (Agent Usage Endpoint, Heartbeat Model Tracking)
Theme 5: Agent Health Fields (last_seen_at, context_tokens, run_state)
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Theme 1: Task Hierarchy ────────────────────────────────────────────────


class TestTaskHierarchy:
    """GET /api/v1/boards/{board_id}/tasks/{task_id}/hierarchy"""

    async def test_task_hierarchy_with_parent_and_children(
        self, auth_client: AsyncClient, make_board, make_task
    ):
        """Create parent + 2 children, check the hierarchy response."""
        board = await make_board(name="Hierarchy Board", slug="hierarchy")

        parent = await make_task(
            board.id,
            title="Phase 1: Backend",
            status="in_progress",
            priority="high",
        )
        child1 = await make_task(
            board.id,
            title="Set up models",
            status="done",
            priority="medium",
            parent_task_id=parent.id,
            sort_order=0,
        )
        child2 = await make_task(
            board.id,
            title="Create API routes",
            status="in_progress",
            priority="high",
            parent_task_id=parent.id,
            sort_order=1,
        )

        # Test hierarchy from the parent's perspective
        resp = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{parent.id}/hierarchy"
        )
        assert resp.status_code == 200
        data = resp.json()

        # Parent has no parent
        assert data["parent"] is None

        # 2 children in sort_order
        assert len(data["children"]) == 2
        assert data["children"][0]["id"] == str(child1.id)
        assert data["children"][0]["title"] == "Set up models"
        assert data["children"][0]["status"] == "done"
        assert data["children"][1]["id"] == str(child2.id)
        assert data["children"][1]["title"] == "Create API routes"

        # Test hierarchy from the child's perspective — sees the parent
        resp2 = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{child1.id}/hierarchy"
        )
        assert resp2.status_code == 200
        data2 = resp2.json()

        assert data2["parent"] is not None
        assert data2["parent"]["id"] == str(parent.id)
        assert data2["parent"]["title"] == "Phase 1: Backend"
        assert data2["parent"]["status"] == "in_progress"
        assert data2["parent"]["priority"] == "high"
        assert data2["children"] == []

    async def test_task_hierarchy_no_children(
        self, auth_client: AsyncClient, make_board, make_task
    ):
        """Single task without a parent and without children."""
        board = await make_board(name="Solo Board", slug="solo")
        task = await make_task(board.id, title="Standalone Task")

        resp = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{task.id}/hierarchy"
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["parent"] is None
        assert data["children"] == []
        assert data["report_back"] is None
        assert data["has_credentials"] is False
        assert data["requester"] is None

    async def test_task_hierarchy_report_back(
        self, auth_client: AsyncClient, make_board, make_task
    ):
        """Task with report_back fields set."""
        board = await make_board(name="ReportBack Board", slug="reportback")
        task = await make_task(
            board.id,
            title="Deploy to Production",
            report_back_required=True,
            report_back_channel="telegram",
            report_back_status="pending",
            report_back_requirements="summary,screenshot",
        )

        resp = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{task.id}/hierarchy"
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["report_back"] is not None
        rb = data["report_back"]
        assert rb["required"] is True
        assert rb["channel"] == "telegram"
        assert rb["status"] == "pending"
        assert rb["requirements"] == "summary,screenshot"

    async def test_task_hierarchy_credentials_indicator(
        self, auth_client: AsyncClient, make_board, make_task
    ):
        """Task with credentials_encrypted set → has_credentials=True."""
        board = await make_board(name="Creds Board", slug="creds")
        task_with = await make_task(
            board.id,
            title="Task with Credentials",
            credentials_encrypted="gAAAAA_encrypted_blob",
        )
        task_without = await make_task(
            board.id,
            title="Task without Credentials",
        )

        # With credentials
        resp1 = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{task_with.id}/hierarchy"
        )
        assert resp1.status_code == 200
        assert resp1.json()["has_credentials"] is True

        # Without credentials
        resp2 = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{task_without.id}/hierarchy"
        )
        assert resp2.status_code == 200
        assert resp2.json()["has_credentials"] is False


# ── Theme 3: Autonomy ──────────────────────────────────────────────────────


class TestAutonomyDefaults:
    """AUTONOMY_DEFAULTS configuration."""

    async def test_autonomy_defaults(self):
        """All expected keys and levels are present."""
        from app.services.autonomy import AUTONOMY_DEFAULTS

        # Expected keys
        expected_keys = {
            "deploy", "external_post", "config_change", "browser_action",
            "visual_review", "blocker_decision", "question", "code_change",
            "mark_done", "dispatch_escalation", "recovery_failed",
        }
        assert set(AUTONOMY_DEFAULTS.keys()) == expected_keys

        # Spot check: concrete levels
        assert AUTONOMY_DEFAULTS["code_change"] == "L1"
        assert AUTONOMY_DEFAULTS["mark_done"] == "L1"
        assert AUTONOMY_DEFAULTS["browser_action"] == "L2"
        assert AUTONOMY_DEFAULTS["deploy"] == "L3"
        assert AUTONOMY_DEFAULTS["question"] == "L3"

        # All values are valid levels
        valid_levels = {"L1", "L2", "L3"}
        for key, level in AUTONOMY_DEFAULTS.items():
            assert level in valid_levels, f"{key} hat ungueltiges Level: {level}"


class TestAutonomyEnforcement:
    """enforce_autonomy() — 3-tier behavior."""

    async def _setup_data(self):
        """Create board + agent for autonomy tests."""
        from app.models.board import Board
        from app.models.agent import Agent

        board_id = uuid.uuid4()
        agent_id = uuid.uuid4()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = Board(id=board_id, name="Autonomy Board", slug="autonomy-test")
            s.add(board)
            agent = Agent(id=agent_id, name="TestBot", board_id=board_id)
            s.add(agent)
            await s.commit()

        return board_id, agent_id

    @patch("app.services.autonomy.get_redis", new_callable=AsyncMock)
    async def test_l1_no_approval(self, mock_get_redis):
        """L1 (code_change) → no approval, return 'L1'."""
        from app.services.autonomy import enforce_autonomy

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)  # No overrides
        mock_get_redis.return_value = mock_redis

        board_id, agent_id = await self._setup_data()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            with patch("app.services.autonomy.emit_event", new_callable=AsyncMock):
                result = await enforce_autonomy(
                    action_type="code_change",
                    session=s,
                    agent_id=agent_id,
                    board_id=board_id,
                    description="Refactored utils.py",
                )

        assert result == "L1"

        # No approval in the DB
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            from app.models.approval import Approval
            approvals = (await s.exec(select(Approval))).all()
            assert len(approvals) == 0

    @patch("app.services.autonomy.get_redis", new_callable=AsyncMock)
    async def test_l2_emits_event(self, mock_get_redis):
        """L2 (browser_action) → emit_event called, return 'L2'."""
        from app.services.autonomy import enforce_autonomy

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_get_redis.return_value = mock_redis

        board_id, agent_id = await self._setup_data()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            with patch(
                "app.services.autonomy.emit_event", new_callable=AsyncMock
            ) as mock_emit:
                result = await enforce_autonomy(
                    action_type="browser_action",
                    session=s,
                    agent_id=agent_id,
                    board_id=board_id,
                    description="Screenshot taken",
                )

        assert result == "L2"
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][1] == "autonomy.l2.browser_action"
        assert "[L2 Notify]" in call_args[0][2]

    @patch("app.services.autonomy.get_redis", new_callable=AsyncMock)
    async def test_l3_creates_approval(self, mock_get_redis):
        """L3 (deploy) → approval in DB with autonomy_level='L3'."""
        from app.services.autonomy import enforce_autonomy
        from app.models.approval import Approval

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_get_redis.return_value = mock_redis

        board_id, agent_id = await self._setup_data()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            with patch(
                "app.services.autonomy.emit_event", new_callable=AsyncMock
            ):
                result = await enforce_autonomy(
                    action_type="deploy",
                    session=s,
                    agent_id=agent_id,
                    board_id=board_id,
                    description="Deploy frontend to Vercel",
                    payload={"target": "production"},
                    confidence=0.95,
                )

        assert result == "L3"

        # Check the approval in the DB
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            approvals = (await s.exec(select(Approval))).all()
            assert len(approvals) == 1
            a = approvals[0]
            assert a.action_type == "deploy"
            assert a.description == "Deploy frontend to Vercel"
            assert a.autonomy_level == "L3"
            assert a.board_id == board_id
            assert a.agent_id == agent_id
            assert a.payload == {"target": "production"}
            assert a.confidence == 0.95
            assert a.status == "pending"

    @patch("app.services.autonomy.get_redis", new_callable=AsyncMock)
    async def test_autonomy_override(self, mock_get_redis):
        """Override via set_autonomy_config → resolve_autonomy returns the new value."""
        from app.services.autonomy import set_autonomy_config, resolve_autonomy

        # Simulate: deploy was changed from L3 to L1
        stored_overrides = {}

        async def fake_get(key):
            return json.dumps(stored_overrides) if stored_overrides else None

        async def fake_set(key, value):
            nonlocal stored_overrides
            stored_overrides = json.loads(value)

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=fake_get)
        mock_redis.set = AsyncMock(side_effect=fake_set)
        mock_get_redis.return_value = mock_redis

        # Before: deploy = L3 (default)
        level_before = await resolve_autonomy("deploy")
        assert level_before == "L3"

        # Set override
        result = await set_autonomy_config({"deploy": "L1"})
        assert result["deploy"] == "L1"

        # After: deploy = L1
        level_after = await resolve_autonomy("deploy")
        assert level_after == "L1"

        # Other defaults remain unchanged
        assert (await resolve_autonomy("code_change")) == "L1"
        assert (await resolve_autonomy("browser_action")) == "L2"


class TestAutonomyAPI:
    """GET/PATCH /api/v1/settings/autonomy"""

    async def test_get_autonomy_settings(
        self, fake_redis, auth_client: AsyncClient
    ):
        """GET /settings/autonomy returns the defaults."""
        resp = await auth_client.get("/api/v1/settings/autonomy")
        assert resp.status_code == 200
        data = resp.json()

        assert "levels" in data
        assert "defaults" in data
        assert data["levels"]["deploy"] == "L3"
        assert data["levels"]["code_change"] == "L1"
        assert data["defaults"]["browser_action"] == "L2"


# ── Theme 4: Usage Analytics V1 ───────────────────────────────────────────


class TestUsageAnalytics:
    """GET /api/v1/analytics/usage"""

    async def test_usage_endpoint_returns_agents(
        self, fake_redis, auth_client: AsyncClient, make_board, make_agent
    ):
        """Usage endpoint returns an agent list with correct fields."""
        board = await make_board(name="Usage Board", slug="usage")
        agent = await make_agent(
            name="Cody",
            board_id=board.id,
            model="claude-sonnet-4-20250514",
            status="online",
            context_tokens=45000,
            context_max=200000,
            total_tasks_completed=12,
        )

        resp = await auth_client.get("/api/v1/analytics/usage")
        assert resp.status_code == 200
        data = resp.json()

        assert "agents" in data
        assert "models" in data
        assert data["total_agents"] >= 1

        # Find the agent in the list
        agent_data = next(
            (a for a in data["agents"] if a["agent_id"] == str(agent.id)),
            None,
        )
        assert agent_data is not None
        assert agent_data["name"] == "Cody"
        assert agent_data["model"] == "claude-sonnet-4-20250514"
        assert agent_data["context_tokens"] == 45000
        assert agent_data["context_max"] == 200000
        assert agent_data["context_pct"] == 22  # round(45000/200000*100) = round(22.5) = 22
        assert agent_data["tasks_completed"] == 12

        # Model-Summary
        assert "claude-sonnet-4-20250514" in data["models"]


class TestHeartbeatModelTracking:
    """POST /api/v1/agent/heartbeat with model_id."""

    async def test_heartbeat_model_id_saved(self, client: AsyncClient, fake_redis):
        """Heartbeat with model_id → Redis key mc:agent:{id}:heartbeat_model exists."""
        from app.models.board import Board
        from app.models.agent import Agent
        from app.auth import generate_agent_token

        board_id = uuid.uuid4()
        agent_id = uuid.uuid4()
        token_raw, token_hash = generate_agent_token()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = Board(id=board_id, name="HB Board", slug="hb-board")
            s.add(board)
            agent = Agent(
                id=agent_id,
                name="HeartbeatBot",
                board_id=board_id,
                agent_token_hash=token_hash,
                scopes=["heartbeat"],
                status="online",
            )
            s.add(agent)
            await s.commit()

        # Heartbeat handler imports get_redis inline — must be patched
        async def override_get_redis():
            return fake_redis

        with patch("app.redis_client.get_redis", new=override_get_redis):
            resp = await client.post(
                "/api/v1/agent/heartbeat",
                json={
                    "context_tokens": 80000,
                    "model_id": "claude-sonnet-4-20250514",
                },
                headers={"Authorization": f"Bearer {token_raw}"},
            )
        assert resp.status_code == 200

        # Check the Redis key
        redis_key = f"mc:agent:{agent_id}:heartbeat_model"
        value = await fake_redis.get(redis_key)
        assert value is not None
        # fakeredis can return str or bytes
        val = value.decode() if isinstance(value, bytes) else value
        assert val == "claude-sonnet-4-20250514"


# ── Theme 5: Agent Health Fields ───────────────────────────────────────────


class TestAgentHealthFields:
    """Agent model has the wave-2 health fields."""

    async def test_agent_has_health_fields(self, make_board, make_agent):
        """Agent model has last_seen_at, context_tokens, run_state."""
        from app.models.agent import Agent

        board = await make_board(name="Health Board", slug="health")

        # Create agent with explicit health values
        agent = await make_agent(
            name="HealthBot",
            board_id=board.id,
            context_tokens=120000,
            run_state="running",
        )

        # Read from DB and verify
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_agent = await s.get(Agent, agent.id)
            assert db_agent is not None

            # Health fields exist and have correct values
            assert db_agent.context_tokens == 120000
            assert db_agent.run_state == "running"

            # last_seen_at is nullable (None for a new agent)
            assert hasattr(db_agent, "last_seen_at")
            assert db_agent.last_seen_at is None

            # More health fields
            assert hasattr(db_agent, "context_max")
            assert db_agent.context_max == 150_000  # Default
            assert hasattr(db_agent, "total_compactions")
            assert db_agent.total_compactions == 0

    async def test_agent_default_health_values(self, make_board, make_agent):
        """Agent without explicit health values has sensible defaults."""
        board = await make_board(name="Defaults Board", slug="defaults")
        agent = await make_agent(name="DefaultBot", board_id=board.id)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            from app.models.agent import Agent
            db_agent = await s.get(Agent, agent.id)

            assert db_agent.context_tokens == 0
            assert db_agent.context_max == 150_000
            assert db_agent.run_state == "idle"
            assert db_agent.session_message_count == 0
            assert db_agent.total_tasks_completed == 0
            assert db_agent.total_compactions == 0
            assert db_agent.last_seen_at is None
