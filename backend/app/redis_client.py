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


RECOVERY_COMMENT_COOLDOWN_TTL = 600  # seconds — see RedisKeys.recovery_comment_cooldown


async def try_claim_recovery_comment_cooldown(redis: aioredis.Redis, task_id: str) -> bool:
    """Atomically claim the shared per-task "continue"-comment cooldown (G6).

    Returns True if the caller won the race and should post its system
    TaskComment (and has now set the cooldown for everyone else). Returns
    False if another recovery mechanism already posted within the last
    RECOVERY_COMMENT_COOLDOWN_TTL seconds — the caller must skip posting.

    Uses SET NX EX (atomic check-and-set) rather than GET-then-SET so two
    mechanisms racing on the same watchdog tick can't both observe "not set"
    and both post.

    Takes an already-resolved ``redis`` client (rather than calling
    get_redis() itself) so callers keep using whichever get_redis reference
    their own module/tests already patch — task_runner.py and internal.py
    both hold a local ``redis`` from an earlier ``await get_redis()`` in the
    same function.
    """
    claimed = await redis.set(
        RedisKeys.recovery_comment_cooldown(task_id),
        "1",
        nx=True,
        ex=RECOVERY_COMMENT_COOLDOWN_TTL,
    )
    return bool(claimed)


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
    def agent_chat_channel(agent_id: str) -> str:
        """Per-agent Redis pub/sub channel for the live transcript-tail chat
        view (Sessions Chat View, Task A4). Every SSE frame published here
        carries the wire event type ``chat_event`` — mirrors the naming
        convention of ``agent_runtime_switch.terminal_remount_channel``."""
        return f"mc:agent:{agent_id}:chat"

    @staticmethod
    def group_events(group_id: str) -> str:
        """Per-Gruppe Pub/Sub-Kanal (Gruppenchat V1) — SSE-Events wie
        group.message_posted / group.member_changed; die Runden-Engine (PR B)
        publiziert hier round_started/round_completed/doc_updated."""
        return f"mc:events:group:{group_id}"

    @staticmethod
    def bench_entry_rerender_cooldown(entry_id: str) -> str:
        """Per-entry rerender rate limit (SET NX EX) — bench_studio's
        per-video rerender button. Prevents double-click/spam fan-out of
        overlapping render+compose runs for the same entry."""
        return f"mc:bench:entry:{entry_id}:rerender-cooldown"

    @staticmethod
    def model_window_observations() -> str:
        """Hash: model id -> context_window_size, one field per model ever
        seen in a FRESH statusline-state read across the whole fleet
        (harness_catalog.observe_model_window) — newest write always wins
        (plain HSET, no versioning). Read by
        harness_catalog.get_observed_model_windows() as the middle tier of
        resolve_context_window's precedence chain (current-session
        statusline > this observed map > the static config seed > None).
        No TTL — an observation stays valid until overwritten; the whole
        hash is small (one field per distinct model id, not per agent)."""
        return "mc:catalog:model-windows"

    @staticmethod
    def model_catalog(harness: str, cli_version: str) -> str:
        """JSON-encoded list of {"command","label"} rows discovered from a
        harness's own /model picker (harness_catalog.discover_model_catalog)
        — keyed by (harness, cli_version) so a CLI upgrade invalidates the
        old catalog automatically instead of serving stale rows forever.
        TTL is set by the writer (24h — see harness_catalog)."""
        return f"mc:catalog:models:{harness}:{cli_version}"

    @staticmethod
    def model_catalog_discovery_lock(harness: str, cli_version: str) -> str:
        """SET NX EX lock so concurrent cache-miss requests for the same
        (harness, cli_version) don't each spin up their own throwaway
        discovery window at once."""
        return f"mc:catalog:models:{harness}:{cli_version}:discovery-lock"

    @staticmethod
    def effort_levels_drift_logged(cli_version: str) -> str:
        """SET NX EX dedup so the ALLOWED_EFFORT_LEVELS version-drift
        warning (agent_chat_input._check_effort_levels_version_drift) logs
        ONCE per CLI version across the whole fleet/all workers, not once
        per request. Never auto-triggers re-discovery — /effort argument
        commands persist to the agent's settings.json, so an unattended
        reprobe would silently change a real agent's default effort level;
        this is purely an observability signal for a manual re-verification
        pass."""
        return f"mc:catalog:effort-levels:{cli_version}:drift-logged"

    @staticmethod
    def bench_challenge_run_claim(challenge_id: str) -> str:
        """Per-challenge run claim (SET NX EX) — bench_studio's rerender/
        recompose endpoints (challenge-wide AND per-entry) all touch the
        same challenge.composed_video_path/status, so two overlapping runs
        on the same challenge (e.g. two different entries' rerender buttons
        clicked in quick succession — the per-entry rate limit alone
        doesn't prevent this) must never race. The router claims it before
        scheduling the background task; the background task releases it in
        a finally. TTL is a self-heal net if the task dies without
        releasing."""
        return f"mc:bench:challenge:{challenge_id}:run-claim"

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
    def dispatch_resume_suppress(task_id: str) -> str:
        """G4 (W2-A): set right after a Tier-3 recovery resume re-dispatches
        a task (dispatched_at/ack_at reset + redispatch). _check_dispatch_ack
        checks this before escalating an ACK-timeout approval — a resume is
        semantically a RESUME, not a fresh dispatch, so it must not re-arm
        the ACK escalation ladder and fire its own Approval concurrently
        with the recovery that caused it. TTL = the agent's ack timeout +
        margin, so a genuinely-never-acked resume still escalates once the
        suppression window elapses."""
        return f"mc:dispatch:resume_suppress:{task_id}"

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

    @staticmethod
    def intelligence_anomaly_dedup(anomaly_type: str, target: str) -> str:
        """Cooldown key so a persistent anomaly pushes to Discord at most once
        per cooldown window instead of every analysis cycle. `target` is the
        agent_id for agent-scoped anomalies, else 'global'."""
        return f"mc:intelligence:anomaly:{anomaly_type}:{target}"

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

    @staticmethod
    def recovery_comment_cooldown(task_id: str) -> str:
        """Shared per-task cooldown for "continue"-style system TaskComments (G6).

        Four independent mechanisms can each decide to post a "please
        continue" system comment on the same task within minutes of each
        other: Tier-3 recovery_recap (task_runner._run_tiered_recovery),
        unblock_notify (agent_task_status.py), the ADR-046 lifecycle-watchdog
        nudge (task_runner._check_stuck_in_progress), and the bootstrap
        recovery recap (routers/internal.py). Each checks this key before
        posting and sets it after — first mechanism to fire wins, the others
        skip silently. TTL 600s: comfortably longer than any single
        mechanism's own internal wait/retry window, short enough that a
        genuinely NEW stall a few minutes later still gets its own nudge.

        Does NOT gate operator-facing Approvals or Telegram notifications —
        only the TaskComment spam."""
        return f"mc:recovery:comment_cooldown:{task_id}"

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
    def runtime_context_drift_candidate(slug: str) -> str:
        """Two-probe confirmation for a changed served context window.

        Separate from runtime_drift_candidate: an engine restarted with a new
        --max-model-len keeps its model id, so the two candidates must be able
        to be pending independently.
        """
        return f"mc:runtime-ctx-drift:{slug}"

    # ── Switch Grace + Auto-Recovery (PR5, services/runtime_grace.py) ────
    # runtime_switching: "this runtime is expected to be unreachable right
    # now" — set by switch_recipe/start_runtime, cleared by the watcher once a
    # probe succeeds. TTL is the safety net against a backend crash mid-switch.
    @staticmethod
    def runtime_switching(slug: str) -> str:
        return f"mc:runtime-switching:{slug}"

    # One auto-recovery attempt per 15 min per runtime; the SET-nx claim on
    # this key is what keeps two workers from starting the engine twice.
    @staticmethod
    def runtime_recovery_cooldown(slug: str) -> str:
        return f"mc:runtime-recovery:cooldown:{slug}"

    # Consecutive FAILED auto-recoveries. At 2 the watcher gives up until an
    # operator intervenes ("after 2 failed attempts, stop and ask").
    @staticmethod
    def runtime_recovery_failures(slug: str) -> str:
        return f"mc:runtime-recovery:failures:{slug}"

    # ── Pre-start memory prep (PR8, services/host_memory_prep.py) ────────
    # host_mem_prep: "this host currently has a lowered vm.min_free_kbytes and
    # a cache-dropper container running, and here is the value to put back".
    # It outlives the request on purpose: a backend restart mid-start must not
    # leave a 2 GiB watermark on the box, so the watcher repairs orphans.
    @staticmethod
    def host_mem_prep(host_key: str) -> str:
        return f"mc:host-memprep:{host_key}"

    # uname -m per host, cached — the arch decides whether the GB10 memory
    # dance applies at all, and it cannot change without a reboot.
    @staticmethod
    def host_arch(host_key: str) -> str:
        return f"mc:host-arch:{host_key}"

    # First RestartCount seen for a container while it was NOT serving. The
    # delta against it is what turns `restart: unless-stopped` from invisible
    # into a detectable crash loop (runtime_watcher).
    @staticmethod
    def runtime_restart_baseline(slug: str) -> str:
        return f"mc:runtime-restarts:{slug}"

    # ── Runtime Ownership Nonce (Task #22, services/runtime_ownership.py) ─
    # "This is the value MC stamped onto the container it most recently
    # created for this slug." No TTL — it must outlive the whole runtime
    # lifetime, not just a switch window. Overwritten on every fresh
    # container creation (switch_recipe / a new docker run), read back
    # before any docker stop MC issues against a container it believes is
    # its own, so a container someone hand-recreated under the same name
    # or label is never silently killed (Local Studio's "never stop what
    # we cannot prove is ours").
    @staticmethod
    def runtime_nonce(slug: str) -> str:
        return f"mc:runtime-nonce:{slug}"

    @staticmethod
    def agent_switch_progress(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:runtime-switch-progress"

    @staticmethod
    def agent_model_sync_fails(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:model-sync-fails"

    @staticmethod
    def agent_recreate_fails(agent_id: str) -> str:
        return f"mc:agent:{agent_id}:recreate-fails"

    # ── Provider Model Catalog ───────────────────────────────────────────
    # One key per discovery target (protocol, or "openai:<runtime-slug>") so a
    # single unreachable provider never invalidates everyone else's cache.
    @staticmethod
    def model_catalog_provider(provider_key: str) -> str:
        return f"mc:model-catalog:{provider_key}"

    # Background check (services/model_catalog_check.py). Same shape as the CLI
    # update check: one lock so only one worker probes per tick, plus one
    # long-lived "already told the operator" key per provider+model so a new
    # model is announced ONCE — not on every tick and not again after a
    # backend restart.
    @staticmethod
    def model_catalog_check_lock() -> str:
        return "mc:model-catalog:check-lock"

    @staticmethod
    def model_catalog_notified(provider_key: str, model_id: str) -> str:
        return f"mc:model-catalog:notified:{provider_key}:{model_id}"

    # ── Local Model Registry ─────────────────────────────────────────────
    # Same shape as the provider catalog above: one lock so only one worker
    # refreshes per tick, plus one long-lived "already told the operator" key
    # per recipe slug so a new local model is announced ONCE.
    @staticmethod
    def local_registry_check_lock() -> str:
        return "mc:local-registry:check-lock"

    @staticmethod
    def local_registry_notified(slug: str) -> str:
        return f"mc:local-registry:notified:{slug}"

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
