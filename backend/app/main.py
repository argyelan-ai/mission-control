import asyncio
import logging
from contextlib import asynccontextmanager

from app.utils import create_tracked_task as _create_background_task

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine
from app.log_redaction import install_log_redaction
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

# Secrets aus Log-Zeilen entfernen, BEVOR ein Handler sie schreibt (httpx
# loggt ausgehende URLs inkl. Telegram-Bot-Token, uvicorn.access die
# `?token=<JWT>`-Query von SSE/WebSocket-Verbindungen). Direkt nach
# basicConfig, damit die Root-Handler existieren.
install_log_redaction()
from app.routers import (
    activity,
    agent_chat,
    agent_comments,
    agent_scoped,
    agent_task_status,
    agent_thread_attachments,
    agent_templates,
    agents,
    ai_providers,
    automations,
    approvals,
    auth,
    files,
    boards,
    channels,
    clawhub,
    cli_plugins,
    cli_tools,
    browser_live,
    cli_terminal,
    consensus,
    credentials,
    groups,
    deploy,
    discord as discord_router,
    hosts,
    host_recipes,
    install_requests,
    internal,
    meetings,
    memory,
    mcp_servers,
    model_prices,
    models,
    local_registry,
    loops,
    nodes,
    references,
    project_git,
    projects,
    prompt_templates,
    repos,
    runtimes,
    runtime_schedules,
    playbooks,
    research,
    schedule,
    secrets,
    settings as settings_router,
    slack,
    skills,
    skill_lab,
    system,
    tags,
    tasks,
    voice,
    workflows,
    webhooks,
    x_posts,
)
from app.background import (
    prepare_process,
    start_background_services,
    stop_background_services,
    start_vault_services,
    stop_vault_services,
    _vault_lint_loop,
    _vault_decay_loop,
    _telegram_topic_purge_loop,
    TELEGRAM_TOPIC_PURGE_INTERVAL_SECONDS,
    TELEGRAM_TOPIC_RETENTION_DAYS,
)
from app.routers import vault as vault_router_module
# Architektur E Teil 2 (Rex-Review PR #500, B1): die acht Seed-Helfer leben
# in app.seeds (geteilt mit app.background.prepare_process) — app.main
# importiert sie von dort, damit der Worker app.main nicht mehr anziehen muss.
from app.seeds import (
    _seed_templates,
    _seed_scheduled_jobs,
    _seed_playbook_assets,
    _seed_runtimes,
    _seed_local_recipes,
    _seed_hosts,
    _ensure_slot_runtimes,
    _seed_github_token,
)

logger = logging.getLogger("mc.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — check templates + seed builtin templates + start background services.
    # Phase 29 (ADR-039): OpenClaw RPC connect removed. Backend no longer dials a Gateway.
    # Zweiter Durchlauf: uvicorn legt `uvicorn.access` & Co. erst beim
    # Server-Start an (eigener Handler, propagate=False) — beim Import oben
    # existierten sie noch nicht. Ohne diesen Aufruf leaken Access-Zeilen
    # weiter den `?token=<JWT>` (Live-Befund 26.07.2026). Idempotent.
    install_log_redaction()
    _verify_jinja_templates()
    # validate_boot_secrets() + DB-Seeds + Channel-/AI-Provider-Overrides +
    # Qdrant-Index-Setup: siehe prepare_process() — geteilter Boot-Vorbe-
    # reitungspfad mit backend/app/worker.py (Rex-Review PR #479, Blocker B2).
    await prepare_process()
    # Portability fail-loud: warn (don't crash) if the MC home mount is absent.
    # Unconditional — Files API + deliverables (HTTP, nicht nur file_indexer)
    # haengen von MC_HOME ab, unabhaengig von ENABLE_BACKGROUND_SERVICES.
    from app.services.fs_roots import mc_home as _mc_home
    if not _mc_home().is_dir():
        logging.getLogger("mc.startup").warning(
            "MC_HOME %s is not a directory — Files API + deliverables will be empty. "
            "Set HOME_HOST to the host's $HOME.", _mc_home()
        )
    # ENABLE_BACKGROUND_SERVICES (Architektur E, Teil 1): Default True, damit
    # sich am heutigen Verhalten nichts aendert, solange kein Worker-Container
    # existiert (Teil 2). Siehe backend/app/worker.py + Inventar-Tabelle im PR.
    if settings.enable_background_services:
        await start_background_services(app)
    else:
        logger.info(
            "ENABLE_BACKGROUND_SERVICES=false — background services stay off in this process"
        )
    # ── Vault Memory (M.1/M.2/M.3) ────────────────────────────────────────
    # Vollstaendig delegiert an app.background.start_vault_services() —
    # dasselbe Wiring wie im Worker-Prozess, KEINE zweite Kopie des Inventars.
    # Return-Contract: Dict der Laufzeit-Objekte (vault_index/activity/git/
    # embeddings/watcher/compactor/lint_task) — hier an app.state haengen,
    # die Vault-Read-Routen lesen sie von dort.
    _vault_runtime = await start_vault_services(app)
    for _key, _obj in _vault_runtime.items():
        setattr(app.state, _key, _obj)
    # ── Vault Decay Cron (Phase 3 Intelligence) ───────────────────────
    # Weekly asyncio loop: soft-decay unread notes (90d->confidence drop,
    # 180d+low->archive). Grace period: no decay fires for 90 days after
    # migration 0126 (earliest Aug 2026). Configurable via settings.
    # NICHT Teil des ENABLE_BACKGROUND_SERVICES-Inventars (siehe vault_lint
    # oben) — laeuft unconditional weiter.
    _vault_decay_task = None
    try:
        _vault_decay_task = _create_background_task(
            _vault_decay_loop(),
            name="vault_decay_loop",
        )
        logger.info("Vault decay cron scheduled (weekly)")
    except Exception as e:
        logger.error("Vault decay loop failed to schedule: %s", e, exc_info=True)

    # ── Jarvis Morning Briefing (ADR-062) ─────────────────────────────
    # Daily LLM-generated briefing as a vault note. Feature-gated: the loop
    # returns immediately unless JARVIS_BRIEFING_ENABLED + OPENAI_API_KEY are set.
    # NICHT Teil des ENABLE_BACKGROUND_SERVICES-Inventars — laeuft unconditional.
    app.state.jarvis_briefing_task = None
    try:
        from app.services.jarvis_briefing import jarvis_briefing_loop
        app.state.jarvis_briefing_task = _create_background_task(
            jarvis_briefing_loop(),
            name="jarvis_briefing_loop",
        )
    except Exception as e:
        logger.error("Jarvis briefing loop failed to schedule: %s", e, exc_info=True)

    # ── Telegram-Themen-Purge (P3.1) ──────────────────────────────────
    # Taeglicher asyncio-Loop: loescht Telegram-Themen, die seit 30 Tagen
    # erledigt sind (Task-Threads mit closed_at, abgeschlossene Projekte).
    # Feature-gated ueber TELEGRAM_TEAM_CHAT_ENABLED — der Tick kehrt sonst
    # sofort zurueck. Das Allgemein-Thema wird nie angefasst.
    # NICHT Teil des ENABLE_BACKGROUND_SERVICES-Inventars — laeuft unconditional.
    app.state.telegram_topic_purge_task = None
    try:
        app.state.telegram_topic_purge_task = _create_background_task(
            _telegram_topic_purge_loop(),
            name="telegram_topic_purge_loop",
        )
    except Exception as e:
        logger.error("Telegram topic purge loop failed to schedule: %s", e, exc_info=True)
    yield
    # Shutdown — stop Telegram + Intelligence + Task Runner + Watchdog.
    # Phase 29 (ADR-039): Gateway RPC lifecycle removed (no socket to drain).
    # B6 (Rex-Review PR #500): jarvis_briefing_task cancel restored — it was
    # the FIRST shutdown step on main and got lost in the lifespan split.
    # Same guard shape as the vault-decay/topic-purge cancels below.
    _jarvis_briefing_task = getattr(app.state, "jarvis_briefing_task", None)
    if _jarvis_briefing_task is not None and not _jarvis_briefing_task.done():
        _jarvis_briefing_task.cancel()
        try:
            await _jarvis_briefing_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    # Vault Memory shutdown — delegiert an app.background.stop_vault_services
    # (gleiche Reihenfolge wie im Worker: lint cron -> compactor -> watcher
    # -> index close). `_vault_runtime` oben gecached, damit der Shutdown
    # nicht von app.state-Hygiene abhaengt.
    try:
        await stop_vault_services(_vault_runtime)
    except Exception as e:
        logger.warning("Vault services stop failed (non-fatal): %s", e)
    if _vault_decay_task and not _vault_decay_task.done():
        _vault_decay_task.cancel()
        try:
            await _vault_decay_task
        except (_asyncio.CancelledError, Exception):
            pass
    _topic_purge_task = getattr(app.state, "telegram_topic_purge_task", None)
    if _topic_purge_task is not None and not _topic_purge_task.done():
        _topic_purge_task.cancel()
        try:
            await _topic_purge_task
        except (_asyncio.CancelledError, Exception):
            pass
    if settings.enable_background_services:
        await stop_background_services(app)
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



# Phase 29 (ADR-039): _openclaw_startup, _deferred_gateway_sync, and
# _startup_recovery_sweep removed. Stale-task recovery is now owned solely by
# task_runner._check_dispatch_ack (Phase 26 hardening), which runs every 60s
# against the local DB — no Gateway dependency needed.



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
# PR #404 Rex review (MEDIUM): Limiter + default_limits + the exception
# handler were wired up, but without SlowAPIMiddleware (or a single
# @limiter.limit decorator — repo-wide grep found zero) default_limits was
# never enforced. It read as "there is global rate limiting" while there was
# none. SlowAPIMiddleware makes default_limits apply to every HTTP route.
# PathExemptSlowAPIMiddleware is that middleware minus the routes that must
# never be counted (Agent-Container, /api/v1/internal, /health, SSE) —
# Begruendung in app/rate_limit.py (PR #404 Review, MITTEL-1).
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.rate_limit import PathExemptSlowAPIMiddleware

limiter = Limiter(
    key_func=get_remote_address,
    # 600/minute, not 120: the key is the client IP, and Marks Browser is ONE
    # IP for the entire UI (TanStack-Polling ueber viele Endpunkte + SSE-
    # Reconnects). Bei 120 sieht ein 429 in der UI aus wie ein Backend-Ausfall.
    # Maschinen-Verkehr, /health und SSE zaehlen gar nicht mehr mit — siehe
    # app/rate_limit.py (PR #404 Review, MITTEL-1).
    default_limits=["600/minute"],
    # Every test-client request shares one fake client address (slowapi's
    # get_remote_address falls back to "127.0.0.1" when request.client is
    # unset, which is how httpx's ASGITransport calls look) — a live
    # 120/minute budget shared across the ENTIRE test session would make
    # unrelated tests fail with spurious 429s once ~120 requests had run.
    # test_rate_limiting_middleware.py exercises the real enforcement path
    # by swapping in its own low-limit Limiter, independent of this flag.
    enabled=settings.environment != "test",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(PathExemptSlowAPIMiddleware)

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
app.include_router(agent_thread_attachments.router)  # /api/v1/agent/threads/{id}/attachment — Agenten-Anhänge (Gruppenchat, 07.09.2026)
app.include_router(install_requests.router)
app.include_router(x_posts.router)
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
app.include_router(local_registry.router)  # /api/v1/local-registry — curated local model/recipe registry
app.include_router(hosts.router)  # /api/v1/hosts — host registry CRUD + metrics (ADR-048)
app.include_router(host_recipes.router)  # /api/v1/hosts/{id}/recipes — Rezept-Umschalter (Vertrag 02.09.2026)
app.include_router(nodes.router)  # /api/v1/nodes — mc-node-agent pairing + push telemetry (Fleet & Rezepte v2, Phase 1)
app.include_router(repos.router)  # /api/v1/repos — repo registry + per-repo rules (ADR-050)
app.include_router(loops.router)  # /api/v1/loops — ergebnisgesteuerte Task-Schleifen (ADR-051)
app.include_router(groups.router)  # /api/v1/groups — Multi-Agent-Gruppenchat (V1)
app.include_router(references.router)  # /api/v1/references — Referenz-Uploads für Tasks/Projekte (ADR-054)
app.include_router(prompt_templates.router)  # /api/v1/prompt-templates — Prompt Library (Benchmark Studio core, PR 2)
app.include_router(runtime_schedules.router)
app.include_router(tags.router)
app.include_router(secrets.router)
app.include_router(slack.router)  # /api/v1/slack — Slack channel setup + connection test
app.include_router(channels.router)  # /api/v1/channels — channel settings page + Telegram connection test
app.include_router(ai_providers.router)  # /api/v1/ai-providers — provider routing for MC's own AI functions
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
app.include_router(cli_tools.router)  # /api/v1/cli-tools — CLI update cockpit (Task 7)
app.include_router(browser_live.router)
app.include_router(cli_terminal.router)
app.include_router(agent_chat.router)  # /api/v1/agents/{id}/chat/history|stream — Sessions Chat View (Task A4)
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
