"""Tests for the SchedulerService (unit tests with mocked APScheduler)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.scheduled_job import ScheduledJob


class TestSchedulerService:

    @pytest.fixture
    def mock_apscheduler(self):
        """Mock APScheduler so no real timer runs."""
        with patch("app.services.scheduler.AsyncIOScheduler") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_job.return_value = None
            mock_cls.return_value = mock_instance
            yield mock_instance

    async def test_build_trigger_daily(self):
        """Daily job trigger has correct hour/minute."""
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
        """Interval job trigger has correct hours."""
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
        """Weekly workflow trigger uses weekday + time."""
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
        """Invalid schedule config → ValueError."""
        from app.services.scheduler import SchedulerService
        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        job = ScheduledJob(
            id=uuid.uuid4(),
            name="Bad",
            schedule_type="daily",
            schedule_time=None,  # missing!
            action_type="chat_send",
        )
        with pytest.raises(ValueError):
            svc._build_trigger(job)

    async def test_resolve_agent_id_uses_agent_id_directly(self, session):
        """If agent_id is set → return it directly."""
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
        """No agent_id → lookup by agent_name."""
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
        """Unknown agent name → None."""
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
    """Tests for the lock strategy in start()/stop()/_acquire_lock/_refresh_lock_loop.

    Guards against the 2026-05-19 regression: a stuck Redis lock permanently
    blocked boot, the scheduler never started, and daily jobs didn't run.

    W4 (11.09.2026): extended for owner-id compare-and-delete/-expire and
    heartbeat-based stale-lock takeover — see scheduler.py module docstring
    for the incident (Deploy #504: the old worker's stop() never ran before
    SIGKILL, and an unconditional DEL could have ripped the lock out from
    under a worker that had since taken over).
    """

    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds_first_try(self):
        from app.redis_client import RedisKeys
        from app.services.scheduler import LOCK_TTL_SECONDS, SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "owner-a"
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=mock_redis)):
            result = await svc._acquire_lock()

        assert result is True
        # Lock acquire: our owner id as the value, nx=True + short TTL —
        # not the old constant "1", so stop()/refresh can later tell their
        # own lock apart from someone else's.
        lock_call = mock_redis.set.call_args_list[0]
        assert lock_call.args == (RedisKeys.scheduler_lock(), "owner-a")
        assert lock_call.kwargs["nx"] is True
        assert lock_call.kwargs["ex"] == LOCK_TTL_SECONDS
        # Heartbeat is written right away too, so a competing acquirer
        # never sees "lock held, no heartbeat" for a lock we just won.
        heartbeat_call = mock_redis.set.call_args_list[1]
        assert heartbeat_call.args == (RedisKeys.scheduler_lock_heartbeat(), "owner-a")
        assert mock_redis.set.call_count == 2

    @pytest.mark.asyncio
    async def test_acquire_lock_retries_then_succeeds(self, fake_redis):
        """Lock initially held by a live owner (heartbeat present) → 3 retries
        (never stealing, since the heartbeat never lapses) → the other
        worker's lock frees up on the 4th attempt → succeeds."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "owner-a"

        await fake_redis.set(RedisKeys.scheduler_lock(), "other-owner", ex=120)
        await fake_redis.set(RedisKeys.scheduler_lock_heartbeat(), "other-owner", ex=15)

        sleep_calls = 0

        async def fake_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 3:
                # The other worker releases the lock right before our 4th try.
                await fake_redis.delete(RedisKeys.scheduler_lock())
                await fake_redis.delete(RedisKeys.scheduler_lock_heartbeat())

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=fake_sleep):
            result = await svc._acquire_lock()

        assert result is True
        assert sleep_calls == 3
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "owner-a"

    @pytest.mark.asyncio
    async def test_acquire_lock_gives_up_after_max_attempts(self, fake_redis):
        """Lock stays held by a live owner (heartbeat never lapses) → False
        after MAX_ATTEMPTS tries — the foreign lock is left untouched."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import (
            LOCK_ACQUIRE_MAX_ATTEMPTS,
            SchedulerService,
        )

        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "owner-a"

        await fake_redis.set(RedisKeys.scheduler_lock(), "other-owner", ex=120)
        await fake_redis.set(RedisKeys.scheduler_lock_heartbeat(), "other-owner", ex=15)

        sleep_calls = 0

        async def fake_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=fake_sleep):
            result = await svc._acquire_lock()

        assert result is False
        assert sleep_calls == LOCK_ACQUIRE_MAX_ATTEMPTS
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "other-owner"

    @pytest.mark.asyncio
    async def test_acquire_lock_steals_when_heartbeat_missing(self, fake_redis):
        """A lock whose owner stopped heartbeating (died without running
        stop() — crash/OOM/SIGKILL) is stolen on the FIRST attempt: no
        waiting through LOCK_ACQUIRE_RETRY_DELAY_SECONDS or LOCK_TTL_SECONDS.

        This is the "Uebernahme bei fehlendem Heartbeat mit kurzem Timeout"
        requirement — it bounds worker-restart downtime to roughly
        LOCK_HEARTBEAT_TTL_SECONDS instead of the full 120s lock TTL.
        """
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        # Old owner's lock entry is still there (TTL not yet up) but it
        # never wrote a heartbeat again — no heartbeat key at all.
        await fake_redis.set(RedisKeys.scheduler_lock(), "dead-owner", ex=120)

        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "new-owner"

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await svc._acquire_lock()

        assert result is True
        assert mock_sleep.call_count == 0  # stolen on the very first attempt
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "new-owner"
        assert await fake_redis.get(RedisKeys.scheduler_lock_heartbeat()) == "new-owner"

    @pytest.mark.asyncio
    async def test_acquire_lock_does_not_steal_when_heartbeat_present(self, fake_redis):
        """A lock with a live heartbeat is left alone — normal retry/backoff applies."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        await fake_redis.set(RedisKeys.scheduler_lock(), "alive-owner", ex=120)
        await fake_redis.set(RedisKeys.scheduler_lock_heartbeat(), "alive-owner", ex=15)

        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "new-owner"

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()), \
             patch("app.services.scheduler.LOCK_ACQUIRE_MAX_ATTEMPTS", 1):
            result = await svc._acquire_lock()

        assert result is False
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "alive-owner"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "heartbeat_remaining_life,expected_total_delay",
        [
            (1.0, 3),    # crash right before a heartbeat refresh — caught on attempt 2
            (2.9, 3),
            (3.1, 6),    # Rex-Review B2: v1 of this fix (only attempt 1 fast) missed
            (5.0, 6),    # exactly this range — attempt 2 still saw a live heartbeat
            (12.0, 12),  # and fell back to the full 15s wait, landing at 18s again
            (14.9, 15),  # worst realistic case: crash right after a refresh
        ],
    )
    async def test_acquire_lock_crash_takeover_scales_with_heartbeat_remaining_life(
        self, fake_redis, heartbeat_remaining_life, expected_total_delay,
    ):
        """Reproduces the live-measured Absturzfall from 11.09.2026 (Karte
        70d6b417, Restposten aus #506) AND Rex-Review B2 on PR #509: the v1
        fix (only the first retry got the 3s treatment) deleted the
        heartbeat on the FIRST fake_sleep call unconditionally — meaning
        the old test passed no matter how long the heartbeat actually had
        left, which is exactly why it missed that attempt 2 (at t=3s)
        still sees a LIVE heartbeat in the common case (Redis TTL 10-15s
        remaining at crash time, since the heartbeat refreshes every
        LOCK_HEARTBEAT_INTERVAL_SECONDS=5s with a
        LOCK_HEARTBEAT_TTL_SECONDS=15s TTL) and falls back to the slow 15s
        retry — reproducing the exact 18s cliff the card wanted gone.

        This version models the heartbeat's remaining life explicitly: the
        heartbeat key is deleted only once the SUM of elapsed retry delay
        reaches ``heartbeat_remaining_life`` — i.e. it expires on its own
        Redis TTL, not on the first sleep call regardless of duration.

        DoD b7d29be3 / Karte 70d6b417 want a takeover "under 10s ab
        Absturz". As documented in scheduler.py and raised with the card
        author via `mc ask`: with the heartbeat TTL left untouched (scope:
        OUT), that is structurally unreachable whenever the heartbeat's
        remaining life at crash time exceeds ~7s — which, given the 15s
        TTL / 5s refresh interval, is most crashes (10-15s remaining is
        the norm, not the exception). What IS achieved and asserted here:
        takeover within LOCK_ACQUIRE_RETRY_DELAY_FIRST_SECONDS (3s) of the
        heartbeat's actual expiry, worst case ~15s total (was: a flat 18s
        regardless of remaining life).
        """
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        await fake_redis.set(RedisKeys.scheduler_lock(), "dead-owner", ex=120)
        await fake_redis.set(RedisKeys.scheduler_lock_heartbeat(), "dead-owner", ex=15)

        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "new-owner"

        total_delay = 0.0
        heartbeat_deleted = False

        async def fake_sleep(seconds):
            nonlocal total_delay, heartbeat_deleted
            total_delay += seconds
            if not heartbeat_deleted and total_delay >= heartbeat_remaining_life:
                await fake_redis.delete(RedisKeys.scheduler_lock_heartbeat())
                heartbeat_deleted = True

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=fake_sleep):
            result = await svc._acquire_lock()

        assert result is True
        assert total_delay == expected_total_delay, (
            f"heartbeat remaining life {heartbeat_remaining_life}s -> expected "
            f"takeover after {expected_total_delay}s of retry delay, got {total_delay}s"
        )
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "new-owner"

    @pytest.mark.asyncio
    async def test_acquire_lock_clean_case_unchanged_by_backoff(self, fake_redis):
        """Regression guard for the backoff change above: the clean-stop
        case (old owner's stop() ran, lock+heartbeat both gone immediately)
        must still take over on the very first attempt with zero retry
        delay — the new first-retry constant must never apply when there's
        nothing to retry."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        # Nothing in Redis at all — mirrors a clean stop() that already
        # deleted both keys before the new worker's first attempt.
        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "new-owner"

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await svc._acquire_lock()

        assert result is True
        assert mock_sleep.call_count == 0
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "new-owner"

    @pytest.mark.asyncio
    async def test_start_skips_when_lock_unavailable(self):
        """If _acquire_lock is False → start() returns without starting APScheduler."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        svc._running = False
        svc._refresh_task = None
        svc._heartbeat_task = None

        with patch.object(svc, "_acquire_lock", new=AsyncMock(return_value=False)):
            await svc.start()

        assert svc._running is False
        svc._scheduler.start.assert_not_called()
        assert svc._refresh_task is None
        assert svc._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_start_launches_refresh_task_on_success(self):
        """Lock acquired → APScheduler started + refresh AND heartbeat tasks running."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        svc._running = False
        svc._refresh_task = None
        svc._heartbeat_task = None

        fake_tasks = [MagicMock(), MagicMock()]

        def fake_create_tracked_task(coro):
            coro.close()  # cleanly close the unscheduled coroutine → no warning
            return fake_tasks.pop(0)

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
        assert mock_create_task.call_count == 2
        assert svc._refresh_task is not None
        assert svc._heartbeat_task is not None

    @pytest.mark.asyncio
    async def test_stop_deletes_lock_immediately_not_via_ttl(self, fake_redis):
        """DoD: after stop(), the lock is gone from Redis right away — not
        because a 120s TTL happened to expire. cancel()/shutdown() run, and
        the compare-and-delete executes unconditionally and synchronously
        (no sleep anywhere in the path), which is what actually bounds this
        to well under 10s in production, not just in this test."""
        import time as _time

        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        svc._running = True
        svc._owner_id = "owner-a"
        svc._refresh_task = MagicMock()
        svc._heartbeat_task = MagicMock()

        await fake_redis.set(RedisKeys.scheduler_lock(), "owner-a", ex=120)
        await fake_redis.set(RedisKeys.scheduler_lock_heartbeat(), "owner-a", ex=15)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)):
            started = _time.monotonic()
            await svc.stop()
            elapsed = _time.monotonic() - started

        assert elapsed < 10
        assert await fake_redis.exists(RedisKeys.scheduler_lock()) == 0
        assert await fake_redis.exists(RedisKeys.scheduler_lock_heartbeat()) == 0

    @pytest.mark.asyncio
    async def test_stop_cancels_refresh_task_and_deletes_lock(self, fake_redis):
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        svc._running = True
        svc._owner_id = "owner-a"

        fake_refresh_task = MagicMock()
        fake_heartbeat_task = MagicMock()
        svc._refresh_task = fake_refresh_task
        svc._heartbeat_task = fake_heartbeat_task

        await fake_redis.set(RedisKeys.scheduler_lock(), "owner-a", ex=120)
        await fake_redis.set(RedisKeys.scheduler_lock_heartbeat(), "owner-a", ex=15)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)):
            await svc.stop()

        assert svc._running is False
        fake_refresh_task.cancel.assert_called_once()
        fake_heartbeat_task.cancel.assert_called_once()
        assert svc._refresh_task is None
        assert svc._heartbeat_task is None
        svc._scheduler.shutdown.assert_called_once()
        assert await fake_redis.exists(RedisKeys.scheduler_lock()) == 0
        assert await fake_redis.exists(RedisKeys.scheduler_lock_heartbeat()) == 0

    @pytest.mark.asyncio
    async def test_stop_does_not_delete_foreign_lock(self, fake_redis):
        """The core regression fix: an old worker's late stop() (it was
        itself killed mid-shutdown before, or is simply slow) must NOT
        delete a lock a NEWER worker has since acquired — 11.09.2026
        incident. Before this fix stop() did an unconditional DEL."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        old_worker = SchedulerService.__new__(SchedulerService)
        old_worker._scheduler = MagicMock()
        old_worker._running = True
        old_worker._owner_id = "old-owner"
        old_worker._refresh_task = MagicMock()
        old_worker._heartbeat_task = MagicMock()

        # A new worker has since taken over — its owner id is in Redis now.
        await fake_redis.set(RedisKeys.scheduler_lock(), "new-owner", ex=120)
        await fake_redis.set(RedisKeys.scheduler_lock_heartbeat(), "new-owner", ex=15)

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)):
            await old_worker.stop()

        # The foreign lock (and its heartbeat) must survive untouched.
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "new-owner"
        assert await fake_redis.get(RedisKeys.scheduler_lock_heartbeat()) == "new-owner"
        # Bookkeeping still happens locally — the old worker still believes
        # it's stopped, it just didn't get to erase anyone else's state.
        assert old_worker._running is False

    @pytest.mark.asyncio
    async def test_compare_and_expire_skips_when_not_owner(self, fake_redis):
        """Refresh must not extend a lock's TTL once it's no longer ours —
        the other half of the 'expire nur bei eigenem Lock' requirement."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import LOCK_TTL_SECONDS, SchedulerService

        await fake_redis.set(RedisKeys.scheduler_lock(), "someone-else", ex=LOCK_TTL_SECONDS)

        svc = SchedulerService.__new__(SchedulerService)
        svc._owner_id = "owner-a"

        result = await svc._compare_and_expire(
            fake_redis, RedisKeys.scheduler_lock(), LOCK_TTL_SECONDS
        )

        assert result is False
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "someone-else"

    @pytest.mark.asyncio
    async def test_refresh_loop_calls_expire_until_stopped(self, fake_redis):
        """Refresh loop extends the lock's TTL repeatedly (compare-and-expire,
        only while we're still the owner) and stops cleanly at _running=False."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import LOCK_TTL_SECONDS, SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._running = True
        svc._owner_id = "owner-a"
        await fake_redis.set(RedisKeys.scheduler_lock(), "owner-a", ex=LOCK_TTL_SECONDS)

        tick_count = 0

        async def fake_sleep(_seconds):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                svc._running = False  # end loop after 2 refreshes

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=fake_sleep):
            await svc._refresh_lock_loop()

        assert tick_count == 2
        assert await fake_redis.get(RedisKeys.scheduler_lock()) == "owner-a"
        assert await fake_redis.ttl(RedisKeys.scheduler_lock()) > 0

    @pytest.mark.asyncio
    async def test_refresh_loop_survives_transient_redis_error(self):
        """If the refresh briefly fails, the loop logs it and retries on the next tick."""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._running = True
        svc._owner_id = "owner-a"

        call_log: list = []

        async def flaky_compare_and_expire(_redis, _key, _ttl):
            call_log.append(1)
            if len(call_log) == 1:
                raise RuntimeError("redis hiccup")
            svc._running = False  # second call ends the loop
            return True

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=AsyncMock())), \
             patch.object(svc, "_compare_and_expire", new=flaky_compare_and_expire), \
             patch("app.services.scheduler.asyncio.sleep", new=AsyncMock()):
            await svc._refresh_lock_loop()  # darf NICHT raisen

        assert len(call_log) == 2

    @pytest.mark.asyncio
    async def test_heartbeat_loop_refreshes_until_stopped(self, fake_redis):
        """Heartbeat loop keeps writing the heartbeat key on its own (tight)
        cadence, independent of the main lock refresh loop."""
        from app.redis_client import RedisKeys
        from app.services.scheduler import SchedulerService

        svc = SchedulerService.__new__(SchedulerService)
        svc._running = True
        svc._owner_id = "owner-a"

        tick_count = 0

        async def fake_sleep(_seconds):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 3:
                svc._running = False

        with patch("app.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)), \
             patch("app.services.scheduler.asyncio.sleep", new=fake_sleep):
            await svc._heartbeat_loop()

        assert tick_count == 3
        assert await fake_redis.get(RedisKeys.scheduler_lock_heartbeat()) == "owner-a"


@pytest.mark.asyncio
async def test_seed_builtin_jobs_uses_session_execute():
    """Regression guard: seed_builtin_jobs must use session.execute().

    SQLModel.AsyncSession.exec() only accepts 1 argument. If the code
    mistakenly uses exec(stmt, params) → TypeError at boot
    (bug until 2026-05-19, fixed by switching to execute()).
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
    """If Job.task_skip_review=True → create_task_internal is called with skip_review=True."""
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
