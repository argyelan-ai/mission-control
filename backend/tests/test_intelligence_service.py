"""
Unit tests for IntelligenceService analysis methods.

Tests the core logic in isolation:
- _analyze_task_durations()
- _analyze_agent_performance()
- _detect_failure_patterns()
- _detect_anomalies()
- _build_destillation_prompt()
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.memory import BoardMemory
from app.models.task import TaskComment
from app.services.intelligence import IntelligenceService

from tests.conftest import test_engine


class TestAnalyzeTaskDurations:
    """IntelligenceService._analyze_task_durations()"""

    async def test_empty_when_no_tasks(self):
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._analyze_task_durations(session)

            assert result["total"] == 0
            assert result["avg_minutes"] == 0
            assert result["outliers"] == []
            assert result["per_agent"] == {}

    async def test_calculates_average_duration(self, make_board, make_agent, make_task):
        board = await make_board()
        agent = await make_agent(name="Cody")
        now = datetime.utcnow()

        await make_task(
            board_id=board.id, title="Task A",
            assigned_agent_id=agent.id, status="done",
            started_at=now - timedelta(minutes=10),
            completed_at=now,
        )
        await make_task(
            board_id=board.id, title="Task B",
            assigned_agent_id=agent.id, status="done",
            started_at=now - timedelta(minutes=20),
            completed_at=now,
        )

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._analyze_task_durations(session)

        assert result["total"] == 2
        assert result["avg_minutes"] == 15.0

    async def test_detects_outliers(self, make_board, make_task):
        board = await make_board()
        now = datetime.utcnow()

        # 3 quick tasks (5 min each) + 1 slow one (60 min)
        for i in range(3):
            await make_task(
                board_id=board.id, title=f"Quick {i}", status="done",
                started_at=now - timedelta(minutes=5), completed_at=now,
            )
        await make_task(
            board_id=board.id, title="Slow task", status="done",
            started_at=now - timedelta(minutes=60), completed_at=now,
        )

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._analyze_task_durations(session)

        # Avg = (5+5+5+60)/4 = 18.75, outlier threshold = 37.5
        assert len(result["outliers"]) == 1
        assert result["outliers"][0]["title"] == "Slow task"

    async def test_ignores_tasks_older_than_7_days(self, make_board, make_task):
        board = await make_board()
        now = datetime.utcnow()

        await make_task(
            board_id=board.id, title="Old task", status="done",
            started_at=now - timedelta(days=8, minutes=10),
            completed_at=now - timedelta(days=8),
        )

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._analyze_task_durations(session)

        assert result["total"] == 0


class TestAnalyzeAgentPerformance:
    """IntelligenceService._analyze_agent_performance()"""

    async def test_empty_when_no_agents_with_gateway(self):
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._analyze_agent_performance(session)
        assert result == []

    async def test_calculates_success_rate(self, make_board, make_agent, make_task):
        board = await make_board()
        agent = await make_agent(name="Cody")
        now = datetime.utcnow()

        # 3 done, 1 failed
        for i in range(3):
            await make_task(
                board_id=board.id, title=f"Done {i}",
                assigned_agent_id=agent.id, status="done",
                started_at=now - timedelta(minutes=10), completed_at=now,
            )
        await make_task(
            board_id=board.id, title="Failed",
            assigned_agent_id=agent.id, status="failed",
        )

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._analyze_agent_performance(session)

        assert len(result) == 1
        assert result[0]["name"] == "Cody"
        assert result[0]["done"] == 3
        assert result[0]["failed"] == 1
        assert result[0]["success_rate"] == 75.0


class TestDetectFailurePatterns:
    """IntelligenceService._detect_failure_patterns()"""

    async def test_empty_when_no_failures(self):
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._detect_failure_patterns(session)
        assert result["total"] == 0
        assert result["patterns"] == {}

    async def test_matches_timeout_pattern(self, make_board, make_task):
        board = await make_board()
        task = await make_task(
            board_id=board.id, title="API call timed out", status="failed",
        )

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            comment = TaskComment(
                id=uuid.uuid4(), task_id=task.id, author_type="agent",
                content="Request timed out after 30 seconds",
            )
            s.add(comment)
            await s.commit()

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._detect_failure_patterns(session)

        assert result["total"] == 1
        assert result["patterns"].get("timeout") == 1

    async def test_unknown_pattern_for_unrecognized_errors(self, make_board, make_task):
        board = await make_board()
        await make_task(
            board_id=board.id, title="Something weird happened", status="failed",
        )

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            svc = IntelligenceService(interval=9999)
            result = await svc._detect_failure_patterns(session)

        assert result["total"] == 1
        assert result["patterns"].get("unknown") == 1


class TestDetectAnomalies:
    """IntelligenceService._detect_anomalies()"""

    async def test_no_anomalies_on_healthy_data(self, fake_redis):
        svc = IntelligenceService(interval=9999)
        insights = {
            "task_durations": {"outliers": [], "avg_minutes": 10},
            "agent_performance": [
                {"name": "Cody", "agent_id": str(uuid.uuid4()), "done": 10, "failed": 0, "success_rate": 100}
            ],
            "failure_patterns": {"total": 0, "patterns": {}},
        }

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            with patch("app.services.intelligence.emit_event", new_callable=AsyncMock), \
                 patch("app.services.intelligence.get_redis", return_value=fake_redis):
                result = await svc._detect_anomalies(session, insights)

        assert result == []

    async def test_detects_low_success_rate(self, fake_redis):
        svc = IntelligenceService(interval=9999)
        insights = {
            "task_durations": {"outliers": []},
            "agent_performance": [
                {"name": "BadAgent", "agent_id": str(uuid.uuid4()), "done": 1, "failed": 4, "success_rate": 20.0}
            ],
            "failure_patterns": {"total": 0, "patterns": {}},
        }

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            with patch("app.services.intelligence.emit_event", new_callable=AsyncMock), \
                 patch("app.services.intelligence.get_redis", return_value=fake_redis):
                result = await svc._detect_anomalies(session, insights)

        assert len(result) == 1
        assert result[0]["type"] == "low_success_rate"
        assert result[0]["severity"] == "warning"

    async def test_detects_high_failure_rate(self, fake_redis):
        svc = IntelligenceService(interval=9999)
        insights = {
            "task_durations": {"outliers": []},
            "agent_performance": [],
            "failure_patterns": {"total": 8, "patterns": {"timeout": 5, "permission": 3}},
        }

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            with patch("app.services.intelligence.emit_event", new_callable=AsyncMock), \
                 patch("app.services.intelligence.get_redis", return_value=fake_redis):
                result = await svc._detect_anomalies(session, insights)

        assert any(a["type"] == "high_failure_rate" for a in result)

    async def test_persistent_anomaly_pushes_once_per_cooldown(self, fake_redis):
        """Regression: a persistent condition must alert Discord only once per
        cooldown window, not every analysis cycle (Discord-spam bug)."""
        svc = IntelligenceService(interval=9999)
        insights = {
            "task_durations": {"outliers": []},
            "agent_performance": [
                {"name": "BadAgent", "agent_id": str(uuid.uuid4()), "done": 1, "failed": 4, "success_rate": 20.0}
            ],
            "failure_patterns": {"total": 8, "patterns": {"timeout": 8}},
        }

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            with patch("app.services.intelligence.emit_event", new_callable=AsyncMock) as emit, \
                 patch("app.services.intelligence.get_redis", return_value=fake_redis):
                # First cycle: both warnings fire.
                first = await svc._detect_anomalies(session, insights)
                emits_after_first = emit.await_count
                # Second cycle: same conditions, cooldown active → no new pushes.
                second = await svc._detect_anomalies(session, insights)
                emits_after_second = emit.await_count

        # Warnings: low_success_rate (per-agent) + high_failure_rate (global).
        assert emits_after_first == 2
        assert emits_after_second == 2, "persistent anomaly re-alerted within cooldown"
        # Dashboard list stays complete on every cycle (dedup only gates push).
        assert len(second) == len(first) == 2

    async def test_distinct_anomaly_types_dedup_independently(self, fake_redis):
        """Different anomaly types/agents get independent cooldown keys."""
        svc = IntelligenceService(interval=9999)
        agent_a = str(uuid.uuid4())
        agent_b = str(uuid.uuid4())
        insights = {
            "task_durations": {"outliers": []},
            "agent_performance": [
                {"name": "A", "agent_id": agent_a, "done": 1, "failed": 4, "success_rate": 20.0},
                {"name": "B", "agent_id": agent_b, "done": 1, "failed": 4, "success_rate": 20.0},
            ],
            "failure_patterns": {"total": 0, "patterns": {}},
        }

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            with patch("app.services.intelligence.emit_event", new_callable=AsyncMock) as emit, \
                 patch("app.services.intelligence.get_redis", return_value=fake_redis):
                await svc._detect_anomalies(session, insights)

        # Two distinct agents → two independent pushes on first cycle.
        assert emit.await_count == 2


class TestBuildDestillationPrompt:
    """IntelligenceService._build_destillation_prompt()"""

    def test_includes_all_sections(self):
        svc = IntelligenceService(interval=9999)
        insights = {
            "task_durations": {"total": 5, "avg_minutes": 12.5, "outliers": []},
            "agent_performance": [
                {"name": "Cody", "done": 3, "failed": 1, "success_rate": 75.0, "avg_minutes": 10.0}
            ],
            "failure_patterns": {"total": 1, "patterns": {"timeout": 1}},
            "anomalies": [{"severity": "warning", "description": "Test anomaly"}],
        }

        prompt = svc._build_destillation_prompt(insights)

        assert "Tasks erledigt: 5" in prompt
        assert "12.5" in prompt
        assert "Cody" in prompt
        assert "timeout" in prompt
        assert "Test anomaly" in prompt
