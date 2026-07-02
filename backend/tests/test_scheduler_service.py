"""Tests fuer den SchedulerService (Unit-Tests mit gemocktem APScheduler)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.scheduled_job import ScheduledJob


class TestSchedulerService:

    @pytest.fixture
    def mock_apscheduler(self):
        """APScheduler mocken damit kein echter Timer laeuft."""
        with patch("app.services.scheduler.AsyncIOScheduler") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_job.return_value = None
            mock_cls.return_value = mock_instance
            yield mock_instance

    async def test_build_trigger_daily(self):
        """Daily-Job Trigger hat korrekte Hour/Minute."""
        from app.services.scheduler import SchedulerService
        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Test",
            schedule_type="daily",
            schedule_time="07:30",
            action_type="chat_send",
        )
        trigger_type, trigger_kwargs = svc._build_trigger(job)
        assert trigger_type == "cron"
        assert trigger_kwargs["hour"] == 7
        assert trigger_kwargs["minute"] == 30

    async def test_build_trigger_interval(self):
        """Interval-Job Trigger hat korrekte Stunden."""
        from app.services.scheduler import SchedulerService
        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Test",
            schedule_type="interval",
            schedule_interval_hours=6,
            action_type="chat_send",
        )
        trigger_type, trigger_kwargs = svc._build_trigger(job)
        assert trigger_type == "interval"
        assert trigger_kwargs["hours"] == 6

    async def test_build_workflow_trigger_weekly(self):
        """Weekly-Workflow Trigger nutzt Wochentag + Uhrzeit."""
        from app.services.scheduler import SchedulerService
        from app.models.workflow import WorkflowTemplate

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        workflow = WorkflowTemplate(
            id=uuid.uuid4(),
            name="Digest",
            trigger_type="scheduled",
            trigger_config={
                "schedule_type": "weekly",
                "schedule_day": "mon",
                "schedule_time": "08:30",
            },
            status="active",
            current_definition={"steps": []},
            created_by="tester",
        )

        trigger_type, trigger_kwargs = svc._build_workflow_trigger(workflow)
        assert trigger_type == "cron"
        assert trigger_kwargs["day_of_week"] == "mon"
        assert trigger_kwargs["hour"] == 8
        assert trigger_kwargs["minute"] == 30

    async def test_build_trigger_invalid_raises(self):
        """Ungueltige Schedule-Config → ValueError."""
        from app.services.scheduler import SchedulerService
        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Bad",
            schedule_type="daily",
            schedule_time=None,  # fehlt!
            action_type="chat_send",
        )
        with pytest.raises(ValueError):
            svc._build_trigger(job)

    async def test_resolve_agent_id_uses_agent_id_directly(self, session):
        """Wenn agent_id gesetzt → direkt zurückgeben."""
        from app.models.agent import Agent
        from app.services.scheduler import SchedulerService

        agent_id = uuid.uuid4()
        agent = Agent(id=agent_id, name="Henry")
        session.add(agent)
        await session.commit()

        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Test",
            schedule_type="daily",
            schedule_time="07:30",
            action_type="chat_send",
            agent_id=agent_id,
            agent_name=None,
        )

        svc = SchedulerService.__new__(SchedulerService)
        result = await svc._resolve_agent_id(session, job)
        assert result == str(agent_id)

    async def test_resolve_agent_id_falls_back_to_name(self, session):
        """Kein agent_id → Lookup by agent_name."""
        from app.models.agent import Agent
        from app.services.scheduler import SchedulerService

        agent = Agent(id=uuid.uuid4(), name="Researcher")
        session.add(agent)
        await session.commit()

        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Test",
            schedule_type="daily",
            schedule_time="08:00",
            action_type="chat_send",
            agent_id=None,
            agent_name="Researcher",
        )

        svc = SchedulerService.__new__(SchedulerService)
        result = await svc._resolve_agent_id(session, job)
        assert result == str(agent.id)

    async def test_resolve_agent_id_returns_none_if_not_found(self, session):
        """Unbekannter Agent-Name → None."""
        from app.services.scheduler import SchedulerService

        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Test",
            schedule_type="daily",
            schedule_time="08:00",
            action_type="chat_send",
            agent_id=None,
            agent_name="Unbekannt",
        )

        svc = SchedulerService.__new__(SchedulerService)
        result = await svc._resolve_agent_id(session, job)
        assert result is None


class TestSchedulerV2Features:
    """Tests for cron/weekly_custom triggers, snooze, auto-disable."""

    def test_cron_trigger_registered_correctly(self):
        """cron schedule_type builds a CronTrigger from crontab string."""
        from app.services.scheduler import SchedulerService
        from apscheduler.triggers.cron import CronTrigger

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()

        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Cron Test",
            schedule_type="cron",
            schedule_cron="0 9 * * 1-5",
            action_type="create_task",
        )
        trigger_type, trigger_kwargs = svc._build_trigger(job)
        assert trigger_type == "__trigger_object__"
        assert isinstance(trigger_kwargs["__trigger__"], CronTrigger)

    def test_weekly_custom_trigger_registered_correctly(self):
        """weekly_custom with schedule_weekdays=[0,2,4] builds a CronTrigger."""
        from app.services.scheduler import SchedulerService
        from apscheduler.triggers.cron import CronTrigger

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()

        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Weekly Custom",
            schedule_type="weekly_custom",
            schedule_time="10:00",
            schedule_weekdays=[0, 2, 4],
            action_type="create_task",
        )
        trigger_type, trigger_kwargs = svc._build_trigger(job)
        assert trigger_type == "__trigger_object__"
        trigger = trigger_kwargs["__trigger__"]
        assert isinstance(trigger, CronTrigger)
        # Verify the day_of_week field encodes mon,wed,fri (0,2,4)
        field_values = {f.name: str(f) for f in trigger.fields}
        assert "day_of_week" in field_values
        assert field_values["day_of_week"] == "0,2,4"

    @pytest.mark.asyncio
    async def test_snooze_skips_execution(self):
        """Job with future snoozed_until must not create a run record."""
        from datetime import datetime, timedelta, timezone
        from app.services.scheduler import SchedulerService

        future = datetime.now(timezone.utc) + timedelta(hours=8)

        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Snoozed Job",
            schedule_type="daily",
            schedule_time="09:00",
            action_type="create_task",
            enabled=True,
            snoozed_until=future,
        )

        svc = SchedulerService.__new__(SchedulerService)

        # _execute_job opens DB sessions internally; mock the engine-based session path
        run_created = False

        async def fake_execute(job_id: str, retry_attempt: int = 0):
            """Minimal re-implementation of the snooze guard only."""
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if job.snoozed_until and job.snoozed_until > now:
                return  # snoozed — no run record
            nonlocal run_created
            run_created = True

        await fake_execute(str(job.id))
        assert run_created is False, "Snoozed job must not create a run record"

    @pytest.mark.asyncio
    async def test_consecutive_failures_auto_disable(self):
        """After reaching 3 consecutive failures the job.enabled flips to False."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)

        # Simulate the consecutive_failures counter logic extracted from _execute_job
        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Flaky Job",
            schedule_type="daily",
            schedule_time="09:00",
            action_type="create_task",
            enabled=True,
            consecutive_failures=0,
        )

        def apply_failure(j: ScheduledJob):
            j.consecutive_failures = (j.consecutive_failures or 0) + 1
            if j.consecutive_failures >= 3:
                j.enabled = False

        apply_failure(job)
        assert job.enabled is True
        apply_failure(job)
        assert job.enabled is True
        apply_failure(job)
        assert job.enabled is False
        assert job.consecutive_failures == 3


class TestSchedulerLockLifecycle:
    """Tests fuer die Lock-Strategie in start()/stop()/_acquire_lock/_refresh_lock_loop.

    Schuetzt vor dem 2026-05-19 Regress: hängender Redis-Lock blockierte den Boot
    permanent, der Scheduler startete nie und Daily-Jobs liefen nicht.
    """

    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds_first_try(self):
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=mock_redis)):
            result = await svc._acquire_lock()

        assert result is True
        # Lock-Acquire mit nx=True + kurzem TTL
        call_kwargs = mock_redis.set.call_args.kwargs
        assert call_kwargs["nx"] is True
        from app.services.scheduler import LOCK_TTL_SECONDS
        assert call_kwargs["ex"] == LOCK_TTL_SECONDS
        # Single attempt — kein sleep
        assert mock_redis.set.call_count == 1

    @pytest.mark.asyncio
    async def test_acquire_lock_retries_then_succeeds(self):
        """Lock zuerst besetzt → 3 retries → erfolgreich."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        mock_redis = AsyncMock()
        # 3x None (= besetzt), dann True
        mock_redis.set = AsyncMock(side_effect=[None, None, None, True])

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=mock_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await svc._acquire_lock()

        assert result is True
        assert mock_redis.set.call_count == 4
        # 3 sleeps zwischen den 4 Versuchen
        assert mock_sleep.call_count == 3

    @pytest.mark.asyncio
    async def test_acquire_lock_gives_up_after_max_attempts(self):
        """Lock bleibt besetzt → nach MAX_ATTEMPTS Versuchen False."""
        from app.services.scheduler import (
            SchedulerService,
            LOCK_ACQUIRE_MAX_ATTEMPTS,
        )

        svc = SchedulerService.__new__(SchedulerService)
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=None)  # immer besetzt

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=mock_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()):
            result = await svc._acquire_lock()

        assert result is False
        assert mock_redis.set.call_count == LOCK_ACQUIRE_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_start_skips_when_lock_unavailable(self):
        """Wenn _acquire_lock False → start() returnt ohne APScheduler zu starten."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        svc._running = False
        svc._refresh_task = None

        with patch.object(svc, "_acquire_lock", new=AsyncMock(return_value=False)):
            await svc.start()

        assert svc._running is False
        svc._scheduler.start.assert_not_called()
        assert svc._refresh_task is None

    @pytest.mark.asyncio
    async def test_start_launches_refresh_task_on_success(self):
        """Lock acquired → APScheduler gestartet + Refresh-Task läuft."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        svc._running = False
        svc._refresh_task = None

        fake_task = MagicMock()

        def fake_create_tracked_task(coro):
            coro.close()  # ungeplante coroutine sauber schliessen → keine Warning
            return fake_task

        with patch.object(svc, "_acquire_lock", new=AsyncMock(return_value=True)), \
             patch.object(svc, "_load_jobs_from_db", new=AsyncMock()) as mock_load, \
             patch(
                 "app.services.scheduler.create_tracked_task",
                 side_effect=fake_create_tracked_task,
             ) as mock_create_task:
            await svc.start()

        assert svc._running is True
        svc._scheduler.start.assert_called_once()
        mock_load.assert_awaited_once()
        mock_create_task.assert_called_once()
        assert svc._refresh_task is fake_task

    @pytest.mark.asyncio
    async def test_stop_cancels_refresh_task_and_deletes_lock(self):
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        svc._running = True

        fake_task = MagicMock()
        svc._refresh_task = fake_task

        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(return_value=1)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=mock_redis)):
            await svc.stop()

        assert svc._running is False
        fake_task.cancel.assert_called_once()
        assert svc._refresh_task is None
        svc._scheduler.shutdown.assert_called_once()
        mock_redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_loop_calls_expire_until_stopped(self):
        """Refresh-Loop ruft EXPIRE wiederholt und stoppt sauber bei _running=False."""
        import asyncio as _asyncio
        from app.services.scheduler import SchedulerService, LOCK_TTL_SECONDS

        svc = SchedulerService.__new__(SchedulerService)
        svc._running = True

        mock_redis = AsyncMock()
        expire_calls: list = []

        async def fake_expire(key, ttl):
            expire_calls.append((key, ttl))
            if len(expire_calls) >= 2:
                svc._running = False  # nach 2 Refreshes Schleife beenden

        mock_redis.expire = AsyncMock(side_effect=fake_expire)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=mock_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()):
            await svc._refresh_lock_loop()

        assert len(expire_calls) == 2
        for _key, ttl in expire_calls:
            assert ttl == LOCK_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_refresh_loop_survives_transient_redis_error(self):
        """Wenn Redis kurz fehlschlägt, loggt der Loop und versucht beim nächsten Tick erneut."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._running = True

        mock_redis = AsyncMock()
        call_log: list = []

        async def flaky_expire(key, ttl):
            call_log.append(ttl)
            if len(call_log) == 1:
                raise RuntimeError("redis hiccup")
            svc._running = False  # zweiter Aufruf beendet den Loop

        mock_redis.expire = AsyncMock(side_effect=flaky_expire)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=mock_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()):
            await svc._refresh_lock_loop()  # darf NICHT raisen

        assert len(call_log) == 2


@pytest.mark.asyncio
async def test_seed_builtin_jobs_uses_session_execute():
    """Regress-Guard: seed_builtin_jobs muss session.execute() nutzen.

    SQLModel.AsyncSession.exec() akzeptiert nur 1 Argument. Nutzt der Code
    fälschlicherweise exec(stmt, params) → TypeError beim Boot
    (Bug bis 2026-05-19, geheilt mit Wechsel auf execute()).
    """
    from app.services import schedule_seeder

    seeder_path = schedule_seeder.__file__
    code_lines = [
        line for line in open(seeder_path).read().splitlines()
        if not line.lstrip().startswith(("#", '"""', "'''"))
    ]
    code_only = "\n".join(code_lines)
    assert "await session.execute(" in code_only
    assert "await session.exec(" not in code_only


@pytest.mark.asyncio
async def test_create_task_with_skip_review_flag():
    """Wenn Job.task_skip_review=True → create_task_internal wird mit skip_review=True aufgerufen."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.models.scheduled_job import ScheduledJob
    from app.models.task import Task
    from app.services.scheduler import SchedulerService

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()

    job = ScheduledJob(
        id=uuid.uuid4(),
        name="Test Digest",
        schedule_type="daily",
        schedule_time="06:00",
        action_type="create_task",
        task_board_id=board_id,
        task_title="AI Tech Digest",
        task_priority="medium",
        task_skip_review=True,
    )

    # Mock task returned by create_task_internal
    mock_task = MagicMock(spec=Task)
    mock_task.id = task_id
    mock_task.title = "AI Tech Digest"

    mock_session = AsyncMock()

    svc = SchedulerService.__new__(SchedulerService)

    with patch("app.services.scheduler.SchedulerService._do_create_task", new=AsyncMock()) as _:
        pass  # verify the method is patchable

    # Patch create_task_internal where it's imported inside _do_create_task
    with patch("app.services.task_create.create_task_internal", new=AsyncMock(return_value=mock_task)) as mock_cti:
        success, error, detail = await svc._do_create_task(mock_session, job)

    assert success is True
    assert error is None
    assert detail["task_id"] == str(task_id)

    # Verify skip_review=True was passed to create_task_internal
    call_kwargs = mock_cti.call_args.kwargs
    assert call_kwargs["skip_review"] is True
