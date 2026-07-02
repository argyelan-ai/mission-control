"""
Tests fuer die Intelligence API-Endpoints.

GET /api/v1/intelligence/insights — Redis-Cache basiert
GET /api/v1/intelligence/reports  — DB-basiert (BoardMemory)
"""

import json
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.memory import BoardMemory
from app.redis_client import RedisKeys

# Shared engine fuer direkte DB-Zugriffe in Tests
from tests.conftest import test_engine


class TestIntelligenceInsights:
    """GET /api/v1/intelligence/insights"""

    async def test_returns_empty_structure_when_no_cache(self, auth_client: AsyncClient):
        """Kein Redis-Cache → leere Struktur mit analyzed_at=null."""
        resp = await auth_client.get("/api/v1/intelligence/insights")
        assert resp.status_code == 200
        data = resp.json()

        assert data["analyzed_at"] is None
        assert data["task_durations"]["total"] == 0
        assert data["agent_performance"] == []
        assert data["failure_patterns"]["total"] == 0
        assert data["anomalies"] == []

    async def test_returns_cached_insights(self, fake_redis, auth_client: AsyncClient):
        """Redis mit Daten → exakte JSON-Rueckgabe.

        Hinweis: fake_redis muss VOR auth_client stehen damit der Client
        dieselbe Redis-Instanz nutzt (wird via client → dependency_overrides injiziert).
        """
        insights = {
            "task_durations": {"avg_minutes": 15.5, "total": 8, "outliers": [], "per_agent": {"Cody": 12.3}},
            "agent_performance": [
                {"name": "Cody", "agent_id": str(uuid.uuid4()), "done": 5, "failed": 1, "success_rate": 83.3, "avg_minutes": 12.3}
            ],
            "failure_patterns": {"total": 1, "patterns": {"timeout": 1}, "details": []},
            "anomalies": [],
            "analyzed_at": "2026-02-24T10:00:00",
        }
        await fake_redis.set(RedisKeys.intelligence_insights(), json.dumps(insights))

        resp = await auth_client.get("/api/v1/intelligence/insights")
        assert resp.status_code == 200
        data = resp.json()

        assert data["analyzed_at"] == "2026-02-24T10:00:00"
        assert data["task_durations"]["total"] == 8
        assert data["task_durations"]["avg_minutes"] == 15.5
        assert len(data["agent_performance"]) == 1
        assert data["agent_performance"][0]["name"] == "Cody"

    async def test_requires_auth(self, client: AsyncClient):
        """Ohne Auth-Token → 401."""
        resp = await client.get("/api/v1/intelligence/insights")
        assert resp.status_code == 401


class TestIntelligenceReports:
    """GET /api/v1/intelligence/reports"""

    async def test_returns_empty_list_when_no_reports(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/v1/intelligence/reports")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_only_returns_auto_generated_insights(self, auth_client: AsyncClient):
        """Nur memory_type='insight' + auto_generated=True."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            # Insight (sollte auftauchen)
            s.add(BoardMemory(
                id=uuid.uuid4(), content="Auto insight", memory_type="insight",
                source="system", auto_generated=True,
            ))
            # Manuelles Knowledge (sollte NICHT auftauchen)
            s.add(BoardMemory(
                id=uuid.uuid4(), content="Manual knowledge", memory_type="knowledge",
                source="user", auto_generated=False,
            ))
            # Insight aber nicht auto-generated (sollte NICHT auftauchen)
            s.add(BoardMemory(
                id=uuid.uuid4(), content="Manual insight", memory_type="insight",
                source="user", auto_generated=False,
            ))
            await s.commit()

        resp = await auth_client.get("/api/v1/intelligence/reports")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["content"] == "Auto insight"

    async def test_respects_limit_parameter(self, auth_client: AsyncClient):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            for i in range(10):
                s.add(BoardMemory(
                    id=uuid.uuid4(), content=f"Report {i}", memory_type="insight",
                    source="system", auto_generated=True,
                ))
            await s.commit()

        resp = await auth_client.get("/api/v1/intelligence/reports?limit=3")
        assert len(resp.json()) == 3

        resp = await auth_client.get("/api/v1/intelligence/reports")
        assert len(resp.json()) == 5  # Default limit=5
