import asyncio
import logging
from contextlib import asynccontextmanager

from app.utils import create_tracked_task as _create_background_task

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings, validate_boot_secrets
from app.database import engine
from app.redis_client import close_redis

# Structured logging (structlog) — JSON in production, human-readable in dev
import structlog

shared_processors = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
]

if settings.environment == "production":
    # JSON for docker compose logs | jq
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
else:
    # Human-readable for local development
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
from app.routers import (
    activity,
    agent_comments,
    agent_scoped,
    agent_task_status,
    agent_templates,
    agents,
    automations,
    approvals,
    auth,
    files,
    boards,
    clawhub,
    cli_plugins,
    cli_terminal,
    consensus,
    credentials,
    deploy,
    discord as discord_router,
    hosts,
    install_requests,
    internal,
    meetings,
    memory,
    mcp_servers,
    model_prices,
    models,
    loops,
    references,
    project_git,
    projects,
    repos,
    runtimes,
    runtime_schedules,
    playbooks,
    research,
    schedule,
    secrets,
    settings as settings_router,
    skills,
    skill_lab,
    system,
    tags,
    tasks,
    voice,
    workflows,
    webhooks,
)
from app.services.embedding_retry import embedding_retry
from app.services.intelligence import intelligence
from app.services.file_indexer import file_indexer
from app.services.obsidian_export import obsidian_export
from app.services.scheduler import scheduler
from app.services.runtime_schedule_service import runtime_schedule_service
from app.services.runtime_watcher import runtime_watcher
from app.services.task_runner import task_runner
from app.services.loop_runner import loop_runner
from app.services.telegram_bot import telegram_bot
from app.services.watchdog import watchdog
# Vault Memory (M.1 Read Foundation + M.2 Write Path) — services + router.
# M.2 (2026-05-14): VaultEmbeddings is now imported here — the M.1 no-op
# stub inside the lifespan has been replaced with the real Spark DGX →
# Qdrant adapter (collection ``memory_vault``). See task M.2-T4 plan.
from app.services.vault_activity import VaultActivity
from app.services.vault_compactor import VaultCompactor
from app.services.vault_embeddings import VaultEmbeddings
from app.services.vault_git import VaultGit
from app.services.vault_index import VaultIndex
from app.services.vault_watcher import VaultWatcher
from app.services import vault_lint as vault_lint_module  # M.3 T4: 24h cron
from app.services import vault_decay as vault_decay_module
from app.services import vault_cascade as vault_cascade_module
from app.routers import vault as vault_router_module

logger = logging.getLogger("mc.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — check templates + seed builtin templates + start background services.
    # Phase 29 (ADR-039): OpenClaw RPC connect removed. Backend no longer dials a Gateway.
    # Fail fast on placeholder secrets (default JWT key = forgeable admin
    # tokens) BEFORE anything else touches the DB or starts services.
    validate_boot_secrets()
    _verify_jinja_templates()
    await _seed_templates()
    await _seed_scheduled_jobs()
    await _seed_playbook_assets()
    await _seed_runtimes()
    await _seed_hosts()
    await _seed_github_token()
    await scheduler.start()
    await watchdog.start()
    await task_runner.start()
    await loop_runner.start()  # Loops L1 (ADR-051) — Runden-Meta-Controller
    await intelligence.start()
    # Portability fail-loud: warn (don't crash) if the MC home mount is absent.
    from app.services.fs_roots import mc_home as _mc_home
    if not _mc_home().is_dir():
        logging.getLogger("mc.startup").warning(
            "MC_HOME %s is not a directory — Files API + deliverables will be empty. "
            "Set HOME_HOST to the host's $HOME.", _mc_home()
        )
    await file_indexer.start()
    # Phase 5 MSY-04: drain mc:embeddings:retry on a 60s tick when the
    # embedding service returns. Singleton mirror of intelligence; tests
    # set embedding_retry_interval=99999 in conftest so the loop never
    # auto-fires (Pitfall 4).
    await embedding_retry.start()
    # Phase 7 OBS-02: vault-export singleton — periodic (default 300s)
    # walk over board_memory + Markdown render into ${HOME_HOST}/.mc/vault/.
    # Tests set obsidian_export_interval=99999 in conftest so the loop
    # never auto-fires (Pitfall 4 mirror).
    # M.2: Disabled by default — Vault is now Source of Truth.
    # Re-enable via OBSIDIAN_EXPORT_ENABLED=true for rollback.
    if settings.obsidian_export_enabled:
        await obsidian_export.start()
        app.state.obsidian_export_started = True
        logger.info("Phase 7 (obsidian_export) enabled — running in parallel with Vault")
    else:
        app.state.obsidian_export_started = False
        logger.info("Phase 7 (obsidian_export) DISABLED — Vault is now Source of Truth (M.2)")
    # MEM-04 (Phase 2): ensure Qdrant has agent_id + board_id keyword
    # indexes on all three memory layers. Idempotent — safe across restarts.
    # Existing collections created before this code do NOT have the full
    # index set; this call adds the missing ones in a single startup.
    # Non-fatal if Qdrant is offline — agents and dispatch are unaffected
    # by missing indexes (queries just get slower).
    try:
        from app.services.qdrant_service import qdrant_service
        await qdrant_service.ensure_payload_indexes()
    except Exception as e:
        logger.warning("Qdrant payload index setup failed (non-fatal): %s", e)
    await runtime_schedule_service.start()
    await runtime_watcher.start()  # Runtime & Model Management v1 (ADR-053)
    await telegram_bot.start()
    # Defense-in-depth: agents that call `gh repo create` without --private
    # get auto-privatized every 5 min. Fail-safe for SOUL rule violations.
    import asyncio as _asyncio
    from app.services.github_visibility_monitor import run_forever as _gh_monitor
    _gh_monitor_task = _asyncio.create_task(_gh_monitor(), name="github_visibility_monitor")
    # ── Vault Memory (M.1 Read Foundation) ────────────────────────────────
    # Init: VaultIndex (FTS5 SQLite) + Activity + Git (stub) + Embeddings
    # (M.1 no-op stub) + Watcher. Phase 7 ``obsidian_export`` continues
    # running in parallel — it stops in M.2.
    #
    # Failure here is non-fatal: backend boots even when the vault dir is
    # unwritable or watchdog can't bind. Vault routes will 500 in that case,
    # but the rest of MC keeps working.
    app.state.vault_index = None
    app.state.vault_activity = None
    app.state.vault_git = None
    app.state.vault_embeddings = None
    app.state.vault_watcher = None
    app.state.vault_compactor = None
    app.state.vault_lint_task = None  # M.3 T4: 24h vault-lint cron
    try:
        vault_path = settings.vault_path
        vault_path.mkdir(parents=True, exist_ok=True)
        # Attachments tree for deliverable files (Phase 0 vault-as-brain).
        # Hardlinks from ~/.mc/deliverables land here, plus voice memos
        # later. Idempotent — exist_ok=True makes restart storms harmless.
        for _kind in ("files", "images", "audio"):
            (vault_path / "attachments" / _kind).mkdir(parents=True, exist_ok=True)
        index_db = vault_path / ".mc_index.db"
        first_boot = not index_db.exists()

        vault_index = VaultIndex(db_path=index_db, vault_path=vault_path)
        if first_boot or settings.vault_index_rebuild_on_boot:
            stats = vault_index.rebuild_from_vault()
            logger.info(
                "Vault index rebuild (%s): scanned=%d indexed=%d skipped=%d errors=%d",
                "first boot" if first_boot else "forced",
                stats["scanned"], stats["indexed"], stats["skipped"], stats["errors"],
            )

        from app.redis_client import get_redis
        _redis_for_vault = await get_redis()
        vault_activity = VaultActivity(redis=_redis_for_vault)
        vault_git = VaultGit(vault_path=vault_path, stub_mode=True)

        # M.2 (2026-05-14): real Spark DGX → Qdrant wiring (replaces the
        # M.1 no-op stub). VaultEmbeddings.upsert() now embeds vault file
        # content via ``embedding_service`` (Spark LM Studio,
        # text-embedding-nomic-embed-text-v1.5, 768-dim) and upserts into
        # the ``memory_vault`` Qdrant collection (auto-created on first use).
        # Fail-soft semantics preserved: DGX or Qdrant outages return a
        # structured ``{"ok": False, "error": ..., "kind": ...}`` instead
        # of bubbling — the watcher pipeline keeps running.
        from app.services.embedding_service import embedding_service as _embedding_service
        from app.services.qdrant_service import qdrant_service as _qdrant_service
        _qdrant_raw_client = await _qdrant_service._get_client()
        vault_embeddings = VaultEmbeddings(
            dgx_client=_embedding_service,
            qdrant_client=_qdrant_raw_client,
            collection="memory_vault",
        )

        vault_watcher = VaultWatcher(
            vault_path=vault_path,
            index=vault_index,
            activity=vault_activity,
            embeddings=vault_embeddings,
            git=vault_git,
            redis=_redis_for_vault,
        )
        await vault_watcher.start()

        app.state.vault_index = vault_index
        app.state.vault_activity = vault_activity
        app.state.vault_git = vault_git
        app.state.vault_embeddings = vault_embeddings
        app.state.vault_watcher = vault_watcher
        logger.info("Vault services wired (path=%s, watcher running)", vault_path)

        # ── VaultCompactor (M.2: inbox-pattern for cross-agent writes) ────
        # Runs after the watcher so compaction events dispatch into a
        # live watcher pipeline. Fault-tolerant: a compactor failure does
        # not block boot or affect the rest of the vault stack.
        try:
            vault_compactor = VaultCompactor(vault_path=vault_path, redis=_redis_for_vault)
            await vault_compactor.start()
            app.state.vault_compactor = vault_compactor
            logger.info("VaultCompactor started")
        except Exception as e:
            logger.error("VaultCompactor failed to start: %s", e, exc_info=True)
            app.state.vault_compactor = None

        # ── Vault Lint Cron (M.3 T4) ──────────────────────────────────────
        # 24h asyncio loop that runs structural lint (orphans, invalid
        # frontmatter, duplicate IDs) and writes the report as a vault note
        # under `_lint/YYYY-MM-DD.md`. Sleeps first, then runs — so backend
        # restart-storms do not trigger repeat scans. Non-fatal: a failed
        # iteration logs + waits for the next tick. Configurable interval
        # via VAULT_LINT_INTERVAL_HOURS. Tests set this to 99999 so the
        # loop never fires (conftest Pitfall 4 mirror).
        try:
            app.state.vault_lint_task = _create_background_task(
                _vault_lint_loop(vault_path),
                name="vault_lint_loop",
            )
            logger.info(
                "Vault lint cron scheduled (interval=%dh)",
                settings.vault_lint_interval_hours,
            )
        except Exception as e:
            logger.error("Vault lint loop failed to schedule: %s", e, exc_info=True)
            app.state.vault_lint_task = None
    except Exception as e:
        logger.warning("Vault wiring failed (non-fatal, vault routes will 500): %s", e)
    # ── Vault Decay Cron (Phase 3 Intelligence) ───────────────────────
    # Weekly asyncio loop: soft-decay unread notes (90d->confidence drop,
    # 180d+low->archive). Grace period: no decay fires for 90 days after
    # migration 0126 (earliest Aug 2026). Configurable via settings.
    _vault_decay_task = None
    try:
        _vault_decay_task = _create_background_task(
            _vault_decay_loop(),
            name="vault_decay_loop",
        )
        logger.info("Vault decay cron scheduled (weekly)")
    except Exception as e:
        logger.error("Vault decay loop failed to schedule: %s", e, exc_info=True)
    yield
    # Shutdown — stop Telegram + Intelligence + Task Runner + Watchdog.
    # Phase 29 (ADR-039): Gateway RPC lifecycle removed (no socket to drain).
    _gh_monitor_task.cancel()
    try:
        await _gh_monitor_task
    except (_asyncio.CancelledError, Exception):
        pass
    # Vault Memory shutdown — vault_lint cron first (lightest, kill before
    # mid-lint write), then compactor (drain final compaction into the
    # still-running watcher pipeline), then the watcher itself (drains
    # observer thread), then close the SQLite index connection.
    try:
        _lint_task = getattr(app.state, "vault_lint_task", None)
        if _lint_task is not None:
            _lint_task.cancel()
            try:
                await _lint_task
            except (_asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.warning("Vault lint cron stop failed (non-fatal): %s", e)
    try:
        if getattr(app.state, "vault_compactor", None) is not None:
            await app.state.vault_compactor.stop()
    except Exception as e:
        logger.warning("Vault compactor stop failed (non-fatal): %s", e)
    try:
        if app.state.vault_watcher is not None:
            await app.state.vault_watcher.stop()
    except Exception as e:
        logger.warning("Vault watcher stop failed (non-fatal): %s", e)
    try:
        if app.state.vault_index is not None:
            app.state.vault_index.close()
    except Exception as e:
        logger.warning("Vault index close failed (non-fatal): %s", e)
    if _vault_decay_task and not _vault_decay_task.done():
        _vault_decay_task.cancel()
        try:
            await _vault_decay_task
        except (_asyncio.CancelledError, Exception):
            pass
    await telegram_bot.stop()
    await intelligence.stop()
    await file_indexer.stop()
    await embedding_retry.stop()
    if getattr(app.state, "obsidian_export_started", False):
        await obsidian_export.stop()
    await runtime_watcher.stop()
    await runtime_schedule_service.stop()
    await loop_runner.stop()
    await task_runner.stop()
    await watchdog.stop()
    await scheduler.stop()
    # Memory subsystem cleanup (Phase 3)
    try:
        from app.services.embedding_service import embedding_service
        await embedding_service.close()
    except Exception:
        pass
    try:
        from app.services.qdrant_service import qdrant_service
        await qdrant_service.close()
    except Exception:
        pass
    await close_redis()
    await engine.dispose()


def _verify_jinja_templates() -> None:
    """Parse check of all Jinja2 templates at backend startup.

    Catches syntax errors EARLY (startup instead of on the first render
    call). Rendering doesn't test all paths — only whether the templates
    can be parsed at all. We catch semantic issues (missing variables
    etc.) at render time.

    Non-fatal: only warns, doesn't block startup. A broken template is
    better than no backend.
    """
    try:
        from jinja2 import TemplateSyntaxError
        from app.services.template_renderer import TEMPLATES_DIR, _get_env

        if not TEMPLATES_DIR.exists():
            logger.warning("Template-Verify: %s nicht gefunden — skip", TEMPLATES_DIR)
            return

        env = _get_env()
        j2_files = sorted(TEMPLATES_DIR.glob("*.j2"))
        broken: list[tuple[str, str]] = []

        for path in j2_files:
            try:
                env.parse(path.read_text(encoding="utf-8"))
            except TemplateSyntaxError as e:
                broken.append((path.name, f"line {e.lineno}: {e.message}"))
            except Exception as e:
                broken.append((path.name, f"{type(e).__name__}: {e}"))

        if broken:
            logger.warning(
                "Template-Verify: %d/%d Templates haben Syntax-Fehler:",
                len(broken),
                len(j2_files),
            )
            for name, msg in broken:
                logger.warning("  ❌ %s — %s", name, msg)
        else:
            logger.info("Template-Verify: %d Templates OK", len(j2_files))
    except Exception as e:
        logger.warning("Template-Verify failed (non-critical): %s", e)


async def _seed_templates() -> None:
    """Create builtin agent templates at startup (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.template_seeder import seed_builtin_templates

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_builtin_templates(session)
    except Exception as e:
        logger.warning("Template seeding failed (non-critical): %s", e)


async def _seed_scheduled_jobs() -> None:
    """Create built-in scheduled jobs at startup (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.schedule_seeder import seed_builtin_jobs

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_builtin_jobs(session)
    except Exception as e:
        logger.warning("Schedule seeding failed (non-critical): %s", e)


async def _seed_runtimes() -> None:
    """Import runtimes.json into DB on first run (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.runtime_seeder import seed_runtimes

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_runtimes(session)
    except Exception as e:
        logger.warning("Runtime seeding failed (non-critical): %s", e)


async def _seed_hosts() -> None:
    """Bootstrap the host registry from settings + legacy runtime fields (ADR-048).

    Runs AFTER _seed_runtimes — the porsche host is derived from the
    unsloth-porsche runtime row. Idempotent; fresh installs without a GPU
    box end up with 0 hosts and 0 errors (cloud runtimes need no host).
    """
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.host_seeder import seed_hosts

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_hosts(session)
    except Exception as e:
        logger.warning("Host seeding failed (non-critical): %s", e)


async def _seed_github_token() -> None:
    """Seed Vault with GH_TOKEN from backend env on first startup.

    Idempotent: checks if a Secret with key='github_token' already exists
    in the vault. If not, and $GH_TOKEN is set in the backend process
    env, creates it encrypted. This is the only path a secret needs to
    travel from .env into the vault; subsequent rotations happen via the
    normal /api/v1/secrets admin API.

    Non-fatal: a missing or invalid GH_TOKEN is logged as warning —
    backend still boots, agents just can't push to GitHub autonomously.
    """
    import os
    try:
        from sqlmodel import select as _select
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.models.secret import Secret
        from app.services.encryption import encrypt

        gh_token = os.environ.get("GH_TOKEN")
        if not gh_token:
            logger.info("_seed_github_token: GH_TOKEN env var not set — skip")
            return

        async with AsyncSession(engine, expire_on_commit=False) as session:
            existing = (await session.exec(
                _select(Secret).where(Secret.key == "github_token")
            )).first()
            if existing:
                logger.debug("_seed_github_token: already in vault")
                return
            secret = Secret(
                key="github_token",
                encrypted_value=encrypt(gh_token),
                provider="github",
                label="GitHub Personal Access Token",
                description="Delivered to agents via bootstrap for autonomous git push + gh CLI operations.",
            )
            session.add(secret)
            await session.commit()
            logger.info("_seed_github_token: seeded Vault with GH_TOKEN from env")
    except Exception as e:
        logger.warning("_seed_github_token failed (non-critical): %s", e)


async def _seed_playbook_assets() -> None:
    """Seed core skill packs for Henry/Playbooks (idempotent)."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.services.playbook_seeder import seed_skill_packs

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_skill_packs(session)
    except Exception as e:
        logger.warning("Playbook asset seeding failed (non-critical): %s", e)


async def _vault_lint_loop(vault_path) -> None:
    """24h vault-lint cron — runs structural lint + writes daily report.

    Sleep-first semantics: on boot, the loop waits the full interval before
    the first run. This prevents restart-storms from re-linting repeatedly.
    Per-iteration failures are logged and the loop continues — only an
    explicit ``CancelledError`` (from shutdown) breaks out.

    Telegram pings on >5 total issues; failures swallowed (don't kill the loop
    if Telegram is unconfigured).
    """
    interval_seconds = settings.vault_lint_interval_hours * 3600
    logger.info("vault_lint_loop started (interval=%ds)", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            stats = vault_lint_module.lint_vault(vault_path)
            try:
                await vault_lint_module.write_lint_report(vault_path, stats)
            except Exception as e:
                # Lint produced numbers but report write failed — log + keep going,
                # next tick will overwrite the (missing) report.
                logger.error("vault_lint write_lint_report failed: %s", e, exc_info=True)

            total = (
                int(stats.get("orphan_count", 0))
                + int(stats.get("frontmatter_invalid_count", 0))
                + int(stats.get("duplicate_id_count", 0))
            )
            if total > 5:
                try:
                    if telegram_bot.configured:
                        msg = (
                            f"<b>Vault Lint</b>: {total} issues "
                            f"(orphans={stats.get('orphan_count', 0)}, "
                            f"invalid_fm={stats.get('frontmatter_invalid_count', 0)}, "
                            f"dup_ids={stats.get('duplicate_id_count', 0)}). "
                            f"See <code>_lint/{stats.get('linted_at', '')[:10]}.md</code>."
                        )
                        await telegram_bot.send_message(msg)
                    else:
                        logger.info(
                            "vault_lint: %d issues but telegram unconfigured; report written only",
                            total,
                        )
                except Exception as te:
                    logger.warning("vault_lint telegram ping failed (non-fatal): %s", te)
        except asyncio.CancelledError:
            logger.info("vault_lint_loop cancelled")
            break
        except Exception as e:
            # Don't break — sleep + retry on next interval.
            logger.error("vault_lint_loop iteration error: %s", e, exc_info=True)


# Phase 29 (ADR-039): _openclaw_startup, _deferred_gateway_sync, and
# _startup_recovery_sweep removed. Stale-task recovery is now owned solely by
# task_runner._check_dispatch_ack (Phase 26 hardening), which runs every 60s
# against the local DB — no Gateway dependency needed.


async def _vault_decay_loop() -> None:
    """Weekly vault-decay cron — demotes confidence + archives stale notes.

    Runs every 7 days. Sleep-first semantics (like github_visibility_monitor).
    Grace period: skips if migration 0126 was applied less than 90 days ago.
    """
    from datetime import datetime, timezone

    interval_seconds = 7 * 24 * 3600  # 1 week
    logger.info("vault_decay_loop started (interval=%ds)", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            from sqlmodel.ext.asyncio.session import AsyncSession

            async with AsyncSession(engine, expire_on_commit=False) as session:
                vault_log_instance = None
                try:
                    from app.services.vault_log import VaultLog
                    vault_log_instance = VaultLog(settings.vault_path)
                except Exception:
                    pass

                result = await vault_decay_module.run_decay(
                    session=session,
                    vault_path=settings.vault_path,
                    vault_log=vault_log_instance,
                    migration_date=datetime(2026, 5, 24, tzinfo=timezone.utc),
                )
                logger.info(
                    "vault_decay: demoted=%d archived=%d",
                    result.demoted, result.archived,
                )
        except asyncio.CancelledError:
            logger.info("vault_decay_loop cancelled")
            break
        except Exception as e:
            logger.error("vault_decay_loop iteration error: %s", e, exc_info=True)


app = FastAPI(
    title="Mission Control v2",
    version=settings.app_version,
    description="AI Agent Command Center API",
    lifespan=lifespan,
)

# CORS origins — localhost defaults + configured external host (ADR-035).
# Tailscale/LAN IPs are NO LONGER hardcoded: set PUBLIC_HOST=<ip-or-host> and/or
# EXTRA_CORS_ORIGINS=<comma,list> in .env so other deployers aren't bound to
# the operator's machine. (Operator: set PUBLIC_HOST to your Tailscale IP to keep phone access.)
_cors_origins = [
    "http://localhost", "http://localhost:80", "http://localhost:3000",
    "http://localhost:3001", "http://localhost:3002",  # preview ports
    "http://frontend:3000", "https://mc.local",
]
if settings.public_host:
    _h = settings.public_host
    _cors_origins += [f"http://{_h}", f"http://{_h}:80", f"https://{_h}"]
_cors_origins += [o.strip() for o in settings.extra_cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rate Limiting (slowapi) ─────────────────────────────────────────────────
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Register all routers
app.include_router(auth.router)
app.include_router(system.router)
app.include_router(boards.router)
app.include_router(tasks.router)
app.include_router(files.router)  # /api/v1/files — global Files browser (portable, sandboxed)
app.include_router(agents.router)
app.include_router(agent_templates.router)
app.include_router(agent_scoped.router)
app.include_router(agent_comments.router)  # REF-02 step 3 — extracted from agent_scoped (Plan 04-08 finalizes)
app.include_router(agent_task_status.router)  # REF-02 step 4 — extracted from agent_scoped (Plan 04-08 finalizes)
app.include_router(install_requests.router)
app.include_router(approvals.router)
app.include_router(mcp_servers.router)
app.include_router(projects.router)
app.include_router(project_git.router)
app.include_router(memory.router)
app.include_router(activity.router)
# Phase 29-09 (ADR-039): gateway.router deleted. Discord channel CRUD now
# lives exclusively on routers/discord.py (Plan 29-01, D-04).
app.include_router(discord_router.router)
app.include_router(internal.router)  # /api/v1/internal/bootstrap — agent containers fetch tokens from Vault
app.include_router(model_prices.router)
app.include_router(models.router)
app.include_router(runtimes.router)
app.include_router(hosts.router)  # /api/v1/hosts — host registry CRUD + metrics (ADR-048)
app.include_router(repos.router)  # /api/v1/repos — repo registry + per-repo rules (ADR-050)
app.include_router(loops.router)  # /api/v1/loops — ergebnisgesteuerte Task-Schleifen (ADR-051)
app.include_router(references.router)  # /api/v1/references — Referenz-Uploads für Tasks/Projekte (ADR-053)
app.include_router(runtime_schedules.router)
app.include_router(tags.router)
app.include_router(secrets.router)
app.include_router(credentials.router)
app.include_router(skills.router)
app.include_router(clawhub.router)
# planner.router removed 2026-04-11 (Phase 6) — Boss now plans itself via
# openclaude subagents, delegation guards are gone, the router file was deleted.
app.include_router(research.router)
app.include_router(playbooks.router)
app.include_router(automations.router)
app.include_router(skill_lab.router)
app.include_router(workflows.router)
app.include_router(settings_router.router)
app.include_router(webhooks.router)
app.include_router(deploy.router)
app.include_router(cli_plugins.router)
app.include_router(cli_terminal.router)
app.include_router(consensus.router)
app.include_router(schedule.router)
app.include_router(voice.router)

# ── Verticals (optional, strippable feature bundles — ADR-044) ──────────────
# Discovery loads every subpackage of app/verticals/ with register(app).
# Public release without e.g. news_studio/: app boots unchanged without those routes.
from app.verticals import register_all as _register_verticals
_loaded_verticals = _register_verticals(app)
app.include_router(meetings.router)

# Vault Memory (M.1 Read Foundation) — both routers already bake their
# /api/v1/... prefix into APIRouter(prefix=...). Do NOT add another prefix
# or routes will register as /api/v1/api/v1/vault/...
app.include_router(vault_router_module.router)
app.include_router(vault_router_module.agent_router)
