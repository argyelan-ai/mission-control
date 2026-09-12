"""Background services — the shared boot/start/shutdown path, extracted from app.main.

Architektur E, Teil 2 (eigener ``mc-worker``-Container): dieses Modul traegt
ALLES, was der Worker-Prozess braucht, OHNE ``app.main`` zu importieren —
weder auf Modul-Ebene noch im Boot-Pfad von ``prepare_process()``. Der
komplette FastAPI-Rumpf (``app = FastAPI(...)`` + 61 ``include_router()``
+ CORS/Rate-Limit-Middleware + Verticals-Discovery) bleibt ausschliesslich
im API-Prozess — ein Importfehler in irgendeinem Router legt den Worker
nicht mehr lahm. (Rex-Review PR #500, B1: die Vorversion importierte die
Seed-Helfer aus ``app.main`` und zog so doch den ganzen Rumpf in den
Worker; sie leben jetzt in ``app.seeds``.)

Inhalt (alle symmetric moves aus ``app.main``):
- ``prepare_process()``          — Boot-Secret-Guard + DB-Seeds + Channel-/AI-Provider-Overrides
- ``start_background_services()``/``stop_background_services()`` — die 17 ENABLE_BACKGROUND_SERVICES-gated
  Singleton-Dienste (+ gh-Visibility-Monitor-Task)
- ``start_vault_services()``     — Vault-Wiring (Index/Activity/Git/Embeddings/Watcher/Compactor
  + Lint-Cron), vorher inline in ``app.main.lifespan``. Returnt ein Dict der
  Laufzeit-Objekte, damit der Aufrufer sie an ``app.state`` haengen kann.
  Watcher/Compactor/Lint-Cron laufen hinter ``ENABLE_BACKGROUND_SERVICES``
  (B3: der Lint-Cron war vorher unconditional — Doppelstart in beiden
  Prozessen); Index/Activity/Git/Embeddings immer (Read-Pfad).

Request-gebundene Dinge (HTTP-Router, Terminal-/Browser-WebSockets) sind bewusst
NICHT hier — die bleiben immer im API-Prozess.
"""

import asyncio
import logging
import time
from typing import Any

from app.config import settings
from app.database import engine
from app.utils import create_tracked_task as _create_background_task

# Structured logging (structlog) — JSON in production, human-readable in dev.
# Mirrored from app.main: the worker process imports THIS module (never
# app.main), so it must configure logging itself before the first log line.
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

# Standalone-Logging fuer den Worker-Prozess (Rex-Review PR #500, Blocker B5):
# basicConfig + Redaction duerfen nicht vom app.main-Import abhaengen. Vor
# B1 kam beides als Nebenwirkung des app.main-Imports in prepare_process()
# mit; ohne diese Zeilen haette der Worker nach dem B1-Fix still in
# unredigiertes Logging gefallen (kein Root-Handler -> lastResort nur ab
# WARNING; kein Redaction-Filter -> httpx leakt Telegram-Tokens, der
# Live-Befund vom 26.07.2026). install_log_redaction() ist laut eigenem
# Docstring idempotent — der Doppelaufruf aus app.main schadet nicht.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

from app.log_redaction import install_log_redaction
install_log_redaction()

logger = logging.getLogger("mc.startup")


async def _timed_stop(name: str, coro) -> None:
    """Shared shutdown-step timer for stop_background_services() and
    stop_vault_services() (W4, 11.09.2026 — Karte 70d6b417, Rex-Review PR
    #509 DoD: "schuldiger Shutdown-Schritt namentlich aus dem Log"). Vorher
    lebte diese Funktion nur lokal in stop_background_services() (Incident
    Deploy #504) — stop_vault_services() hatte GAR KEIN Timing, obwohl
    genau dort (VaultCompactor/-Watcher/-Index) der im PR-Text gemessene
    30s-Shutdown-Haenger sass. Ohne diese Zeile war "welcher der ~21
    sequenziellen .stop()-Schritte frisst die Zeit" nur zu erraten, nie zu
    belegen.
    """
    started = time.monotonic()
    try:
        await coro
    finally:
        elapsed = time.monotonic() - started
        log = logger.warning if elapsed > 1.0 else logger.info
        log("Shutdown: %s stopped in %.2fs", name, elapsed)

# Service singletons — extracted from app.main so start/stop live next to
# their wiring. app.main re-imports the same singletons (module identity is
# shared: both modules import from app.services.*, never from each other).
from app.services.embedding_retry import embedding_retry
from app.services.cli_update_check import cli_update_checker
from app.services.model_catalog_check import model_catalog_checker
from app.services.local_registry import local_registry_checker
from app.services.intelligence import intelligence
from app.services.file_indexer import file_indexer
from app.services.obsidian_export import obsidian_export
from app.services.scheduler import scheduler
from app.services.runtime_schedule_service import runtime_schedule_service
from app.services.runtime_watcher import runtime_watcher
from app.services.runtime_pulse import runtime_pulse
from app.services.task_runner import task_runner
from app.services.group_runner import group_runner
from app.services.loop_runner import loop_runner
from app.services.slack_socket import slack_socket
from app.services.telegram_bot import telegram_bot
from app.services.watchdog import watchdog
# Vault Memory (M.1 Read Foundation + M.2 Write Path) — services.
from app.services.vault_activity import VaultActivity
from app.services.vault_compactor import VaultCompactor
from app.services.vault_embeddings import VaultEmbeddings
from app.services.vault_git import VaultGit
from app.services.vault_index import VaultIndex
from app.services.vault_watcher import VaultWatcher
from app.services import vault_lint as vault_lint_module  # M.3 T4: 24h cron
from app.services import vault_decay as vault_decay_module


async def start_background_services(app: Any) -> None:
    """Start the ENABLE_BACKGROUND_SERVICES-gated singleton services.

    Architektur E, Teil 1 (Vorbereitung Worker-Container — siehe
    backend/app/worker.py): extrahiert, damit die API-lifespan und der
    eigenstaendige Worker-Einstiegspunkt genau denselben Startpfad teilen.
    Request-gebundene Dinge (HTTP-Router, Terminal-/Browser-WebSockets)
    sind bewusst NICHT enthalten — die bleiben immer in der API.

    vault_watcher/vault_compactor sind NICHT hier drin — sie haengen am
    Vault-Wiring in lifespan() (brauchen vault_index/_activity/_git/
    _embeddings aus demselben Scope) und werden dort separat gegated.
    Siehe Inventar-Tabelle im PR-Text (Streitfall).
    """
    await scheduler.start()
    # Fix "tote Dispatch-Warteschlange": on boot, drop queue entries whose
    # task is done/aborted/failed or deleted (the 11 legacy boss-queue
    # entries were the motivating case). Idempotent, runs before watchdog
    # + task_runner so the first drain tick sees a clean queue.
    try:
        from app.services.task_queue import purge_finished_queue_entries
        _purged = await purge_finished_queue_entries()
        if _purged:
            logging.getLogger("mc.startup").info(
                "Startup queue purge: removed %d stale dispatch-queue entries", _purged,
            )
    except Exception as e:
        logging.getLogger("mc.startup").warning(
            "Startup queue purge failed (non-fatal): %s", e,
        )
    await watchdog.start()
    await task_runner.start()
    await loop_runner.start()  # Loops L1 (ADR-051) — Runden-Meta-Controller
    await group_runner.start()  # Gruppenchat (ADR-075) — Runden-Engine
    await intelligence.start()
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
    await runtime_schedule_service.start()
    await runtime_watcher.start()  # Runtime & Model Management v1 (ADR-054)
    await runtime_pulse.start()  # Runtimes-Buehne v2 PR 1 — tok/s heat strip poller
    await cli_update_checker.start()  # CLI Tool Updates — periodic version check
    # Provider Model Catalog — hourly probe + "model.new_available" notification
    # so a newly shipped provider model no longer waits for someone to open the
    # /runtimes page.
    await model_catalog_checker.start()
    # Local Model Registry — refresh the curated local-model catalogue from the
    # configured registries. Inert unless settings.local_registry_sources is set.
    await local_registry_checker.start()
    await telegram_bot.start()
    # Slack inbound (ADR-072). MC has no public URL, so Slack cannot call us —
    # this opens the Socket Mode websocket outbound. Silently inert unless the
    # Slack channel is switched on; one Redis lock keeps multi-worker setups
    # from reading every message twice.
    await slack_socket.start()
    # Defense-in-depth: agents that call `gh repo create` without --private
    # get auto-privatized every 5 min. Fail-safe for SOUL rule violations.
    import asyncio as _asyncio
    from app.services.github_visibility_monitor import run_forever as _gh_monitor
    app.state.gh_monitor_task = _asyncio.create_task(_gh_monitor(), name="github_visibility_monitor")


async def stop_background_services(app: Any) -> None:
    """Mirror shutdown for start_background_services().

    Safe to call even when the services were never started — every
    .stop() implementation in this codebase no-ops on a None/absent task
    (verified across all 17 services below during the ADR-E audit).

    Every step is timed and logged via the module-level ``_timed_stop()``
    (W4, 11.09.2026 — incident: Deploy #504, the old worker's
    SchedulerService.stop() never ran because the process was SIGKILLed
    before this sequential chain reached it; the scheduler's Redis lock
    then only healed via its 120s TTL). Without per-service timing there
    was no way to tell WHICH of the 17 services ate the shutdown grace
    period — this logs each one so the next incident has that answer
    immediately instead of requiring a repro.
    """
    import asyncio as _asyncio

    _gh_monitor_task = getattr(app.state, "gh_monitor_task", None)
    if _gh_monitor_task is not None:
        async def _cancel_gh_monitor() -> None:
            _gh_monitor_task.cancel()
            try:
                await _gh_monitor_task
            except (_asyncio.CancelledError, Exception):
                pass
        await _timed_stop("github_visibility_monitor", _cancel_gh_monitor())
    await _timed_stop("slack_socket", slack_socket.stop())
    await _timed_stop("telegram_bot", telegram_bot.stop())
    await _timed_stop("intelligence", intelligence.stop())
    await _timed_stop("file_indexer", file_indexer.stop())
    await _timed_stop("embedding_retry", embedding_retry.stop())
    if getattr(app.state, "obsidian_export_started", False):
        await _timed_stop("obsidian_export", obsidian_export.stop())
    await _timed_stop("runtime_watcher", runtime_watcher.stop())
    await _timed_stop("runtime_pulse", runtime_pulse.stop())
    await _timed_stop("cli_update_checker", cli_update_checker.stop())
    await _timed_stop("model_catalog_checker", model_catalog_checker.stop())
    await _timed_stop("local_registry_checker", local_registry_checker.stop())
    await _timed_stop("runtime_schedule_service", runtime_schedule_service.stop())
    await _timed_stop("group_runner", group_runner.stop())
    await _timed_stop("loop_runner", loop_runner.stop())
    await _timed_stop("task_runner", task_runner.stop())
    await _timed_stop("watchdog", watchdog.stop())
    await _timed_stop("scheduler", scheduler.stop())


async def prepare_process() -> None:
    """Shared boot preparation for the API process and the worker process.

    Architektur E, Teil 1 (siehe backend/app/worker.py): muss VOR
    ``start_background_services()`` laufen, in JEDEM Prozess der sie
    aufruft — nicht nur einmal API-seitig (Rex-Review PR #479, Blocker B2).

    - ``validate_boot_secrets()`` ist ein Fail-Fast-Check pro Prozess:
      ein Worker mit leerem ``SECRETS_ENCRYPTION_KEY``/Platzhalter-JWT soll
      beim Boot krachen, nicht erst beim ersten Secrets-Zugriff zur Laufzeit.
    - ``apply_channel_overrides()``/``apply_ai_provider_overrides()`` patchen
      den SETTINGS-SINGLETON DES AUFRUFENDEN PROZESSES aus DB/Secrets-Store.
      API und Worker sind getrennte Python-Prozesse mit je einer eigenen
      ``settings``-Instanz — ``telegram_bot.start()`` (laeuft im Worker)
      liest ``settings.telegram_team_chat_enabled`` von GENAU dieser
      Instanz. Nur weil die API ihre eigene Kopie schon gepatcht hat, ist
      die des Workers noch nicht gepatcht.
    - Die Seed-Schritte sind idempotent und laufen hier zusaetzlich, damit
      der Worker nicht von der Boot-Reihenfolge zum API-Container abhaengt
      (``_seed_scheduled_jobs`` fuettert genau den Scheduler, den
      ``start_background_services()`` gleich startet).

    Reihenfolge identisch zur vorherigen lifespan()-Reihenfolge (keine
    zusaetzliche Verhaltensaenderung durch diese Extraktion).
    """
    from app.config import validate_boot_secrets

    # Fail fast on placeholder secrets (default JWT key = forgeable admin
    # tokens) BEFORE anything else touches the DB or starts services.
    validate_boot_secrets()
    # Seed helpers + Slot-Runtime-Abgleich leben in app.seeds (Rex-Review
    # PR #500, Blocker B1: vorher wurden sie aus app.main importiert, was
    # den kompletten FastAPI-Rumpf samt aller Router in den Worker zog).
    # Sie seeden via app.database.engine und app.models — beide importieren
    # app.main nicht. app.seeds ist bewusst main-frei gehalten.
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
    await _seed_templates()
    await _seed_scheduled_jobs()
    await _seed_playbook_assets()
    await _seed_runtimes()
    await _seed_local_recipes()
    await _seed_hosts()
    # NACH _seed_hosts: die Slot-Zeilen leiten sich aus den Boxen ab.
    await _ensure_slot_runtimes()
    await _seed_github_token()
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
    # Channels settings (DB overrides + secrets-stored Telegram tokens) MUST
    # be applied before the chat loops start — telegram_bot.start() decides
    # "configured?" from the settings singleton this call patches. Applied
    # unconditionally: routers send via diese Singletons unabhaengig von
    # ENABLE_BACKGROUND_SERVICES (nur die Inbound-Poll-Loops sind gegated).
    try:
        from app.database import async_session_maker
        from app.services.channel_config import apply_channel_overrides

        async with async_session_maker() as _cc_session:
            await apply_channel_overrides(_cc_session)
    except Exception as e:
        logger.warning("channel overrides at startup failed (env defaults stay): %s", e)
    # AI provider routing (embeddings / insights) — same three-layer contract
    # as the channels above; without this the settings page would need a
    # restart to take effect.
    try:
        from app.database import async_session_maker
        from app.services.ai_provider_config import apply_ai_provider_overrides

        async with async_session_maker() as _ai_session:
            await apply_ai_provider_overrides(_ai_session)
    except Exception as e:
        logger.warning("ai provider overrides at startup failed (env defaults stay): %s", e)



# Phase 29 (ADR-039): _openclaw_startup, _deferred_gateway_sync, and
# _startup_recovery_sweep removed. Stale-task recovery is now owned solely by
# task_runner._check_dispatch_ack (Phase 26 hardening), which runs every 60s
# against the local DB — no Gateway dependency needed.


async def _vault_lint_loop(vault_path) -> None:
    """24h vault-lint cron — runs structural lint + writes daily report.

    Sleep-first semantics: on boot, the loop waits the full interval before
    the first run. This prevents restart-storms from re-linting repeatedly.
    Per-iteration failures are logged and the loop continues — only an
    explicit ``CancelledError`` (from shutdown) breaks out.

    Operator ping (reports adapter: Telegram + Slack) on >5 total issues;
    failures swallowed (don't kill the loop if no report backend is
    configured or a backend is down).
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
                    from app.services.operator_reports import (
                        report_backends,
                        send_report,
                    )

                    if report_backends():
                        msg = (
                            f"<b>Vault Lint</b>: {total} issues "
                            f"(orphans={stats.get('orphan_count', 0)}, "
                            f"invalid_fm={stats.get('frontmatter_invalid_count', 0)}, "
                            f"dup_ids={stats.get('duplicate_id_count', 0)}). "
                            f"See <code>_lint/{stats.get('linted_at', '')[:10]}.md</code>."
                        )
                        await send_report(msg)
                    else:
                        logger.info(
                            "vault_lint: %d issues but no report backend configured; report written only",
                            total,
                        )
                except Exception as te:
                    logger.warning("vault_lint operator ping failed (non-fatal): %s", te)
        except asyncio.CancelledError:
            logger.info("vault_lint_loop cancelled")
            break
        except Exception as e:
            # Don't break — sleep + retry on next interval.
            logger.error("vault_lint_loop iteration error: %s", e, exc_info=True)



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


TELEGRAM_TOPIC_PURGE_INTERVAL_SECONDS = 24 * 3600
TELEGRAM_TOPIC_RETENTION_DAYS = 30


async def _telegram_topic_purge_loop() -> None:
    """Taeglicher Purge alter Chat-Raeume (Marks Regel: nach 30 Tagen weg).

    Sleep-first wie die Vault-Loops: nach einem Neustart laeuft nicht sofort ein
    Purge (Restart-Sturm). Die eigentliche Arbeit steckt in
    ``chat_rooms.purge_rooms_tick`` — es faechert ueber alle aktiven Kanaele auf
    (ADR-072); pro Kanal sitzen Feature-Flag, eigene Session und das
    Fehler-Schlucken, damit dieser Loop nie stirbt. (Name + Konstanten bleiben
    aus Kompatibilitaet telegram-benannt.)
    """
    from app.services.chat_rooms import purge_rooms_tick as purge_topics_tick

    logger.info(
        "telegram_topic_purge_loop started (interval=%ds, retention=%dd)",
        TELEGRAM_TOPIC_PURGE_INTERVAL_SECONDS, TELEGRAM_TOPIC_RETENTION_DAYS,
    )
    while True:
        try:
            await asyncio.sleep(TELEGRAM_TOPIC_PURGE_INTERVAL_SECONDS)
            await purge_topics_tick(older_than_days=TELEGRAM_TOPIC_RETENTION_DAYS)
        except asyncio.CancelledError:
            logger.info("telegram_topic_purge_loop cancelled")
            break
        except Exception as e:  # pragma: no cover — Tick schluckt bereits alles
            logger.error("telegram_topic_purge_loop iteration error: %s", e, exc_info=True)

async def start_vault_services(app) -> dict:
    """Wire the Vault stack (M.1/M.2) — moved verbatim out of app.main.lifespan.

    Attach-Contract mit dem Aufrufer: die zurueckgegebenen Objekte haengt der
    Aufrufer an ``app.state`` (API: fuer die Vault-Read-Routen; Worker:
    nur der Ordnung halber). Startet watcher + compactor + lint-cron hinter
    ``ENABLE_BACKGROUND_SERVICES``; index/activity/git/embeddings laufen
    IMMER (Read-Pfad, siehe Kommentar im Original).

    Failure semantics unchanged: any failure here is non-fatal — the caller
    logs and the backend boots with vault routes 500ing (API) bzw. ohne
    Vault-Pipeline (Worker).
    """
    runtime: dict = {
        "vault_index": None,
        "vault_activity": None,
        "vault_git": None,
        "vault_embeddings": None,
        "vault_watcher": None,
        "vault_compactor": None,
        "vault_lint_task": None,
    }
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

        # W4 (11.09.2026 — Restposten aus #506, Karte 70d6b417): VaultIndex(...)
        # und rebuild_from_vault() sind plain-sync (kein await, kein
        # Yield-Punkt). Inline aufgerufen blockieren sie den Event-Loop fuer
        # ihre gesamte Laufzeit. Das allein war NICHT die volle Ursache des
        # gemessenen 30s-Shutdown-Haengers (Rex-Review PR #509, Blocker B1):
        # der Worker registriert seinen SIGTERM-Handler erst NACH diesem
        # Aufruf (siehe backend/app/worker.py, run() — der Handler-Block
        # steht dort inzwischen bewusst VOR start_vault_services()). Ohne
        # Handler traf ein SIGTERM hier auf gar keinen Callback, den ein
        # blockierter Loop haette verzoegern koennen — der eigentliche
        # Mechanismus ist, dass mc-worker als PID 1 laeuft und ein Signal
        # ohne Handler dort vom Kernel verworfen statt per Default-Aktion
        # verarbeitet wird (Details im worker.py-Kommentar). `asyncio.to_thread`
        # bleibt trotzdem richtig und noetig: es ist die Voraussetzung dafuer,
        # dass der jetzt FRUEH registrierte Handler waehrend eines laufenden
        # Reindex ueberhaupt feuern kann, statt selbst vom blockierten Loop
        # verzoegert zu werden (siehe test_vault_reindex_does_not_block_sigterm_handling
        # + test_worker_run_survives_sigterm_during_vault_reindex in
        # backend/tests/test_background_services_flag.py). VaultIndex ist
        # dafuer bereits ausgelegt (check_same_thread=False + eigener
        # threading.Lock, siehe vault_index.py) — kein Rewrite noetig.
        vault_index = await asyncio.to_thread(VaultIndex, db_path=index_db, vault_path=vault_path)
        if first_boot or settings.vault_index_rebuild_on_boot:
            _rebuild_started = time.monotonic()
            stats = await asyncio.to_thread(vault_index.rebuild_from_vault)
            _rebuild_elapsed = time.monotonic() - _rebuild_started
            logger.info(
                "Vault index rebuild (%s, %.2fs): scanned=%d indexed=%d skipped=%d errors=%d",
                "first boot" if first_boot else "forced",
                _rebuild_elapsed,
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
        if settings.enable_background_services:
            await vault_watcher.start()

        runtime["vault_index"] = vault_index
        runtime["vault_activity"] = vault_activity
        runtime["vault_git"] = vault_git
        runtime["vault_embeddings"] = vault_embeddings
        runtime["vault_watcher"] = vault_watcher
        logger.info(
            "Vault services wired (path=%s, watcher %s)",
            vault_path,
            "running" if settings.enable_background_services else "NOT started (ENABLE_BACKGROUND_SERVICES=false)",
        )

        # ── VaultCompactor (M.2: inbox-pattern for cross-agent writes) ────
        # Runs after the watcher so compaction events dispatch into a
        # live watcher pipeline. Fault-tolerant: a compactor failure does
        # not block boot or affect the rest of the vault stack.
        if settings.enable_background_services:
            try:
                vault_compactor = VaultCompactor(vault_path=vault_path, redis=_redis_for_vault)
                await vault_compactor.start()
                runtime["vault_compactor"] = vault_compactor
                logger.info("VaultCompactor started")
            except Exception as e:
                logger.error("VaultCompactor failed to start: %s", e, exc_info=True)
                runtime["vault_compactor"] = None

        # ── Vault Lint Cron (M.3 T4) ──────────────────────────────────────
        # 24h asyncio loop that runs structural lint (orphans, invalid
        # frontmatter, duplicate IDs) and writes the report as a vault note
        # under `_lint/YYYY-MM-DD.md`. Sleeps first, then runs — so backend
        # restart-storms do not trigger repeat scans. Non-fatal: a failed
        # iteration logs + waits for the next tick. Configurable interval
        # via VAULT_LINT_INTERVAL_HOURS. Tests set this to 99999 so the
        # loop never fires (conftest Pitfall 4 mirror).
        #
        # Teil 2 (Architektur E) geklaert: der Cron GEHOERT zum
        # ENABLE_BACKGROUND_SERVICES-Inventar. start_vault_services() laeuft
        # jetzt in BEIDEN Prozessen (API-lifespan + worker.run()) —
        # unconditional hiesse zwei Schreiber auf _lint/YYYY-MM-DD.md und
        # zwei Operator-Pings (Rex-Review PR #500, Blocker B3). Analog zum
        # vault_compactor direkt darueber gegatet.
        if settings.enable_background_services:
            try:
                runtime["vault_lint_task"] = _create_background_task(
                    _vault_lint_loop(vault_path),
                    name="vault_lint_loop",
                )
                logger.info(
                    "Vault lint cron scheduled (interval=%dh)",
                    settings.vault_lint_interval_hours,
                )
            except Exception as e:
                logger.error("Vault lint loop failed to schedule: %s", e, exc_info=True)
                runtime["vault_lint_task"] = None
    except Exception as e:
        logger.warning("Vault wiring failed (non-fatal, vault routes will 500): %s", e)
    return runtime


async def stop_vault_services(runtime: dict) -> None:
    """Mirror shutdown for start_vault_services() — moved from lifespan-shutdown.

    Order preserved: lint cron first (lightest, kill before mid-lint write),
    then compactor (drain final compaction into the still-running watcher
    pipeline), then the watcher itself (drains observer thread), then close
    the SQLite index connection.

    Every step timed via the shared ``_timed_stop()`` (W4, 11.09.2026 —
    Karte 70d6b417, Rex-Review PR #509 DoD): vorher hatte diese Funktion
    KEIN Timing, obwohl genau hier (VaultCompactor/-Watcher, beide mit
    einem synchronen ``Thread.join()`` im Unterbau) der im PR-Text
    gemessene Shutdown-Haenger sass — siehe
    test_stop_vault_services_names_the_slow_step fuer den Beweis-Log-Lauf.
    """
    _lint_task = runtime.get("vault_lint_task")
    if _lint_task is not None:
        async def _cancel_lint() -> None:
            _lint_task.cancel()
            try:
                await _lint_task
            except (asyncio.CancelledError, Exception):
                pass
        await _timed_stop("vault_lint_cron", _cancel_lint())
    try:
        if runtime.get("vault_compactor") is not None:
            await _timed_stop("vault_compactor", runtime["vault_compactor"].stop())
    except Exception as e:
        logger.warning("Vault compactor stop failed (non-fatal): %s", e)
    try:
        if runtime.get("vault_watcher") is not None:
            await _timed_stop("vault_watcher", runtime["vault_watcher"].stop())
    except Exception as e:
        logger.warning("Vault watcher stop failed (non-fatal): %s", e)
    try:
        if runtime.get("vault_index") is not None:
            async def _close_index() -> None:
                runtime["vault_index"].close()
            await _timed_stop("vault_index", _close_index())
    except Exception as e:
        logger.warning("Vault index close failed (non-fatal): %s", e)
