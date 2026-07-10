import redis.asyncio as aioredis

from app.config import settings

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# Redis key helpers
class RedisKeys:
    @staticmethod
    def board_events(board_id: str) -> str:
        return f"mc:events:board:{board_id}"

    @staticmethod
    def agents_events() -> str:
        return "mc:events:agents"

    @staticmethod
    def approvals_events() -> str:
        return "mc:events:approvals"

    @staticmethod
    def activity_events() -> str:
        return "mc:events:activity"

    @staticmethod
    def board_stats_cache(board_id: str) -> str:
        return f"mc:cache:board:{board_id}:stats"

    @staticmethod
    def agent_metrics_cache(agent_id: str) -> str:
        return f"mc:cache:agent:{agent_id}:metrics"

    @staticmethod
    def dashboard_cache() -> str:
        return "mc:cache:dashboard:overview"

    @staticmethod
    def agent_rate_limit(agent_id: str) -> str:
        return f"mc:ratelimit:agent:{agent_id}:api"

    @staticmethod
    def system_metrics_history() -> str:
        return "mc:metrics:system:history"

    @staticmethod
    def system_metrics_current() -> str:
        return "mc:metrics:system:current"

    @staticmethod
    def intelligence_lock() -> str:
        return "mc:intelligence:lock"

    @staticmethod
    def intelligence_insights() -> str:
        return "mc:intelligence:insights"

    @staticmethod
    def intelligence_daily_dedup() -> str:
        return "mc:intelligence:daily_destillation"

    @staticmethod
    def intelligence_config() -> str:
        return "mc:intelligence:config"

    @staticmethod
    def schedule_events() -> str:
        return "mc:events:schedule"

    @staticmethod
    def jarvis_daily_briefing(date_iso: str) -> str:
        """Per-day generated morning briefing (ADR-062).

        Holds the LLM-generated German briefing text for one day. Doubles as the
        idempotency guard (SET NX) so the job never generates twice per day, and
        as the fast read-path the /agent/vault/briefing endpoint uses to surface
        today's generated briefing without vault-compaction lag.
        """
        return f"mc:jarvis:briefing:{date_iso}"

    @staticmethod
    def workflow_events() -> str:
        return "mc:events:workflows"

    @staticmethod
    def workflow_run_signal(run_id: str) -> str:
        return f"mc:workflow:run:{run_id}:signal"

    # ── Watchdog ─────────────────────────────────────────────────────────
    @staticmethod
    def watchdog_lock() -> str:
        return "mc:watchdog:lock"

    @staticmethod
    def session_health(task_id: str) -> str:
        return f"mc:session_health:{task_id}"

    @staticmethod
    def session_health_escalated(task_id: str) -> str:
        return f"mc:session_health_escalated:{task_id}"

    # ── Scheduler ────────────────────────────────────────────────────────
    @staticmethod
    def scheduler_lock() -> str:
        return "mc:scheduler:lock"

    # ── Task Runner ──────────────────────────────────────────────────────
    @staticmethod
    def task_runner_lock() -> str:
        return "mc:task_runner:lock"

    @staticmethod
    def dispatch_ack_check(task_id: str) -> str:
        return f"mc:dispatch:ack_check:{task_id}"

    @staticmethod
    def dispatch_pending_warn(task_id: str) -> str:
        return f"mc:dispatch:pending_warn:{task_id}"

    @staticmethod
    def task_runner_stale(task_id: str) -> str:
        return f"mc:task_runner:stale:{task_id}"

    @staticmethod
    def task_runner_stale_count(task_id: str) -> str:
        return f"mc:task_runner:stale_count:{task_id}"

    @staticmethod
    def task_runner_stale_escalated(task_id: str) -> str:
        return f"mc:task_runner:stale_escalated:{task_id}"

    # ── Lifecycle Safety Watchdog (ADR-046) ──────────────────────────────
    # Silent-Abort auto-block: agent acked a task then went silent without a
    # terminal PATCH. Separate namespace from stale* so the block has its own
    # 24h dedup + its own ≥2-tick persistence counter.
    @staticmethod
    def task_runner_stuck_block(task_id: str) -> str:
        return f"mc:task_runner:stuck_block:{task_id}"

    @staticmethod
    def task_runner_stuck_block_count(task_id: str) -> str:
        return f"mc:task_runner:stuck_block_count:{task_id}"

    # ── Embedding Retry (Phase 5 MSY-04) ─────────────────────────────────
    @staticmethod
    def embedding_retry() -> str:
        return "mc:embeddings:retry"  # Redis LIST

    @staticmethod
    def embedding_retry_lock() -> str:
        return "mc:embeddings:retry:lock"

    # ── Auto Memory ──────────────────────────────────────────────────────
    @staticmethod
    def auto_memory_task_done(task_id: str) -> str:
        return f"mc:auto_memory:task_done:{task_id}"

    @staticmethod
    def auto_memory_task_failed(task_id: str) -> str:
        return f"mc:auto_memory:task_failed:{task_id}"

    @staticmethod
    def auto_memory_phase_done(parent_task_id: str) -> str:
        return f"mc:auto_memory:phase_done:{parent_task_id}"

    @staticmethod
    def auto_memory_weekly_digest() -> str:
        return "mc:auto_memory:weekly_digest"

    @staticmethod
    def auto_memory_feedback(task_id: str, feedback_type: str) -> str:
        return f"mc:auto_memory:feedback:{task_id}:{feedback_type}"

    # ── Auto-Memory Reflection Fold (Phase 5 MSY-01) ─────────────────────
    @staticmethod
    def auto_memory_reflection_fold(task_id: str, hash16: str) -> str:
        return f"mc:auto_memory:reflection_fold:{task_id}:{hash16}"

    # ── Intelligence ─────────────────────────────────────────────────────
    @staticmethod
    def intelligence_metrics_dedup(agent_id: str, hour_key: str) -> str:
        return f"mc:intelligence:metrics:{agent_id}:{hour_key}"

    # ── Task Queue / Dispatch ────────────────────────────────────────────
    @staticmethod
    def agent_task_queue(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:task_queue"

    @staticmethod
    def agent_pending_dispatch(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:pending_dispatch"

    @staticmethod
    def agent_dispatch_lock(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:dispatch_lock"

    @staticmethod
    def task_rejection_count(task_id: str) -> str:
        return f"mc:task:{task_id}:rejection_count"

    # ── Recovery Dedup ─────────────────────────────────────────────────
    @staticmethod
    def recovery_attempt(task_id: str, recovery_type: str) -> str:
        """Central dedup key for all recovery attempts.

        recovery_type: aborted | session_loss | spawn_timeout | dependency_zombie
        """
        return f"mc:recovery:{task_id}:{recovery_type}"

    @staticmethod
    def recovery_inprogress(agent_id: str, task_id: str) -> str:
        """Dedup key for REC-01 tiered recovery — active during Tiers 1-3.
        TTL 600s covers Tier 1 (10s probe) + Tier 2 (30s restart wait) +
        Tier 3 (5min ACK-wait). See 06-CONTEXT.md D-18."""
        return f"mc:recovery:inprogress:{agent_id}:{task_id}"

    @staticmethod
    def bootstrap_recovery_sent(agent_id: str, task_id: str) -> str:
        """Dedup key for the bootstrap-triggered recovery recap (restart
        signal). Prevents crash-loop / repeated container starts from
        spamming the task timeline with duplicate recovery_recap comments.
        TTL 10min — a fresh bootstrap after that window is treated as a
        new restart worth re-recapping."""
        return f"mc:bootstrap:recovery_sent:{agent_id}:{task_id}"

    # ── Compaction Lock (Phase 6 CTX-02) ──────────────────────────────
    @staticmethod
    def compaction_lock(agent_id: str) -> str:
        """Dedup key for CTX-02 compaction — 90s TTL prevents double-trigger
        during the 60s checkpoint wait (D-09 in 06-CONTEXT.md)."""
        return f"mc:compaction:{agent_id}"

    # ── System Mode (Operational Controls) ────────────────────────────
    @staticmethod
    def system_mode() -> str:
        return "mc:system:mode"

    @staticmethod
    def system_mode_meta() -> str:
        return "mc:system:mode:meta"

    # ── Meetings ────────────────────────────────────────────────────────
    @staticmethod
    def meeting_lock(board_id: str) -> str:
        return f"mc:meeting:{board_id}:lock"

    @staticmethod
    def meeting_events() -> str:
        return "mc:events:meetings"

    # ── Obsidian Export (Phase 7 OBS-02) ─────────────────────────────────
    @staticmethod
    def obsidian_export_lock() -> str:
        return "mc:obsidian_export:lock"

    # ── Runtime Watcher (ADR-054) ───────────────────────────────────────
    @staticmethod
    def runtime_watcher_lock() -> str:
        return "mc:runtime-watcher:lock"

    @staticmethod
    def runtime_live(slug: str) -> str:
        return f"mc:runtime-live:{slug}"

    @staticmethod
    def runtime_drift_candidate(slug: str) -> str:
        return f"mc:runtime-drift:{slug}"

    @staticmethod
    def agent_switch_progress(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:runtime-switch-progress"

    @staticmethod
    def agent_model_sync_fails(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:model-sync-fails"

    @staticmethod
    def agent_recreate_fails(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:recreate-fails"

    # ── CLI Tool Update Check ────────────────────────────────────────────
    @staticmethod
    def cli_update_check_lock() -> str:
        return "mc:cli:check-lock"

    @staticmethod
    def cli_versions_cache() -> str:
        return "mc:cli:versions"

    @staticmethod
    def cli_update_notified(tool: str, version: str) -> str:
        return f"mc:cli:notified:{tool}:{version}"

    # ── CLI Tool Update Orchestration (Task 6) ───────────────────────────
    @staticmethod
    def cli_update_lock() -> str:
        return "mc:cli:update-lock"

    @staticmethod
    def cli_update_progress() -> str:
        return "mc:cli:update-progress"
