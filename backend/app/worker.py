"""Standalone entrypoint for Mission Control's background services.

Architektur E, Teil 2 (eigener ``mc-worker``-Container — siehe PR-Text).
Startet denselben Boot-Pfad wie die API-lifespan, aber OHNE ``app.main`` zu
importieren: der komplette FastAPI-Rumpf (``app = FastAPI(...)`` + alle
``include_router()``-Aufrufe + CORS/Rate-Limit-Middleware + Verticals-
Discovery) laedt damit ausschliesslich im API-Prozess. Ein Importfehler in
irgendeinem Router legt den Worker nicht mehr lahm — die Schuld aus Teil 1
(hier stand frueher ``from app.main import ...``) ist behoben.

Boot-Pfad (identisch zur API-lifespan, siehe ``app.background``):
    1. ``prepare_process()`` — Boot-Secret-Guard, DB-Seeds,
       Channel-/AI-Provider-Overrides, Qdrant-Index-Setup
    2. ``start_background_services()`` — die ENABLE_BACKGROUND_SERVICES-
       gegateden Singleton-Dienste (Scheduler/Watchdog/Task-Runner/Telegram/
       Slack-Socket/...)
    3. ``start_vault_services()`` — Vault-Wiring (Index/Activity/Git/
       Embeddings/Watcher/Compactor/Lint-Cron). Vor Teil 2 an die API-
       lifespan gebunden, laeuft jetzt HIER.

Ausfuehren (gleiches Image wie die API, eigener Compose-Service
``mc-worker`` mit ``command: python -m app.worker``):

    python -m app.worker

``ENABLE_BACKGROUND_SERVICES`` muss in GENAU EINEM der beiden Prozesse auf
``true`` stehen (compose setzt: API=false, Worker=true) — sonst starten
Scheduler/Watchdog/etc. doppelt. Der Scheduler-Redis-Lock (siehe #133)
sichert das zusaetzlich ab, ist aber bei korrektem Wiring unnoetig.

Bewusst NICHT im Worker (request-gebunden bzw. API-domainspezifisch):
- HTTP-Router, Terminal-/Browser-WebSockets (nur im API-Prozess sinnvoll)
- ``jarvis_briefing_loop`` + ``telegram_topic_purge_loop`` (API-lifespan-
  Crons; haengen an Chat-/Report-Oberflaechen des API-Prozesses — doppelte
  Ausfuehrung in beiden Prozessen waere ein Verhaltenswechsel, der nicht
  Teil dieses Umzugs ist)

Vault-Decay-Cron laeuft weiter in der API (unconditional, wie vor Teil 2);
der Vault-Lint-Cron ist Teil von ``start_vault_services`` und laeuft damit
jetzt im Worker.
"""

import asyncio
import logging
import signal
from types import SimpleNamespace

from app.config import settings
from app.background import (
    prepare_process,
    start_background_services,
    stop_background_services,
    start_vault_services,
    stop_vault_services,
)

logger = logging.getLogger("mc.worker")


class _WorkerState:
    """Minimal state carrier for start_/stop_background_services() — the API
    passes its FastAPI app (uses app.state for obsidian_export_started +
    gh_monitor_task), the worker only needs an attribute bag."""

    def __init__(self) -> None:
        self.state = SimpleNamespace()


async def run() -> None:
    if not settings.enable_background_services:
        logger.warning(
            "ENABLE_BACKGROUND_SERVICES=false — worker hat nichts zu starten, beende."
        )
        return

    await prepare_process()

    logger.info("Worker startet Hintergrund-Dienste (kein HTTP-Router, kein Port offen)")
    state = _WorkerState()
    await start_background_services(state)
    vault_runtime = await start_vault_services(state)
    logger.info("Worker: Hintergrund-Dienste laufen")

    # CPython wandelt SIGTERM NICHT in eine Exception um — die Default-
    # Disposition beendet den Prozess sofort, ohne dass ein `finally` laeuft.
    # `docker stop` schickt SIGTERM (nicht SIGINT). Eigene Handler auf beide
    # Signale registrieren, statt auf asyncio.run()s KeyboardInterrupt-Weg
    # (der nur fuer SIGINT funktioniert) zu vertrauen.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
        registered.append(sig)
    try:
        await stop.wait()
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)
        logger.info("Worker faehrt Hintergrund-Dienste herunter")
        await stop_vault_services(vault_runtime)
        await stop_background_services(state)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
