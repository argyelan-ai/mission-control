"""Tests fuer Deploy-API — Health-Checks und History-Tracking."""

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from unittest.mock import AsyncMock, patch

from .conftest import test_engine


@pytest.mark.asyncio
class TestDeployServicesUserEndpoint:
    """User-Endpoint: GET /api/v1/deploy/services"""

    async def test_returns_401_without_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/deploy/services")
        assert resp.status_code == 401

    async def test_returns_services_with_auth(self, auth_client: AsyncClient):
        with patch("app.routers.deploy.check_all_services", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {"service": "backend", "status": "healthy", "status_code": 200},
                {"service": "frontend", "status": "healthy", "status_code": 200},
            ]
            resp = await auth_client.get("/api/v1/deploy/services")
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data
        assert "deployable" in data
        assert "backend" in data["deployable"]
        assert "frontend" in data["deployable"]


@pytest.mark.asyncio
class TestDeployHistoryUserEndpoint:
    """User-Endpoint: GET /api/v1/deploy/history"""

    async def test_returns_401_without_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/deploy/history")
        assert resp.status_code == 401

    async def test_returns_empty_history(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/v1/deploy/history")
        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.asyncio
class TestDeployAgentEndpoints:
    """Agent-Endpoints: require deploy:execute scope."""

    async def test_agent_services_returns_403_without_scope(
        self, client: AsyncClient, make_agent
    ):
        """Agent ohne deploy:execute Scope wird abgelehnt."""
        from app.auth import generate_agent_token

        agent = await make_agent(
            name="No-Deploy-Agent",
            scopes=["tasks:read"],
        )
        raw_token, token_hash = generate_agent_token()
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            from sqlmodel import select
            from app.models.agent import Agent

            result = await s.exec(select(Agent).where(Agent.id == agent.id))
            db_agent = result.one()
            db_agent.agent_token_hash = token_hash
            s.add(db_agent)
            await s.commit()

        client.headers["Authorization"] = f"Bearer {raw_token}"
        with patch("app.routers.deploy.check_all_services", new_callable=AsyncMock) as mock:
            mock.return_value = []
            resp = await client.get("/api/v1/agent/deploy/services")
        assert resp.status_code == 403

    async def test_agent_record_deploy(self, client: AsyncClient, make_agent):
        """Agent mit deploy:execute kann Deploys aufzeichnen."""
        from app.auth import generate_agent_token

        agent = await make_agent(
            name="Deployer",
            scopes=["deploy:execute"],
        )
        raw_token, token_hash = generate_agent_token()
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            from sqlmodel import select
            from app.models.agent import Agent

            result = await s.exec(select(Agent).where(Agent.id == agent.id))
            db_agent = result.one()
            db_agent.agent_token_hash = token_hash
            s.add(db_agent)
            await s.commit()

        client.headers["Authorization"] = f"Bearer {raw_token}"
        resp = await client.post(
            "/api/v1/agent/deploy/record",
            json={
                "service": "backend",
                "action": "rebuild",
                "success": True,
                "health_status": "healthy",
                "duration_seconds": 45.2,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["recorded"] is True


@pytest.mark.asyncio
class TestDeployServiceHealth:
    """Unit-Tests fuer den Health-Check Service."""

    async def test_check_unknown_service(self):
        from app.services.deploy import check_service_health

        result = await check_service_health("nonexistent")
        assert result["status"] == "unknown"

    async def test_check_service_unreachable(self):
        from app.services.deploy import check_service_health

        # Frontend ist im Test-Environment nicht erreichbar
        result = await check_service_health("frontend")
        assert result["status"] in ("unreachable", "error", "timeout")
