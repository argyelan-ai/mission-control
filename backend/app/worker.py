"""Standalone entrypoint for Mission Control's background services.

Architektur E, Teil 1 (Vorbereitung eines eigenen Worker-Containers — siehe
PR-Text fuer das Inventar aller betroffenen Dienste). Dieses Modul startet
GENAU denselben Boot-Pfad wie ``app.main.lifespan``: erst ``prepare_process()``
(Boot-Secret-Guard + DB-Seeds + Channel-/AI-Provider-Overrides — siehe
Docstring dort), dann dieselben Hintergrund-Dienste ueber die extrahierten
``start_background_services``/``stop_background_services``. Kein Uvicorn,
kein offener Port — Request-gebundene Dinge (Terminal-/Browser-WebSockets,
alle HTTP-Endpunkte) bleiben ausschliesslich im API-Prozess (``app.main``).

Achtung: der Import unten (``from app.main import ...``) fuehrt den kompletten
Modulrumpf von ``app.main`` aus, inklusive ``app = FastAPI(...)`` und aller
61 ``include_router()``-Aufrufe. "Ohne HTTP-Router" stimmt nur auf Netzwerk-
ebene (kein Uvicorn bindet den FastAPI-``app``) — ein Importfehler in
irgendeinem Router legt trotzdem auch den Worker lahm, und der volle Router-
Importgraph liegt im Worker-Prozess. Fuer Teil 1 vertretbar; Teil 2 muss die
Dienste aus ``main.py`` herausziehen statt ``main`` zu importieren, wenn
Architektur E echte Prozesstrennung erreichen soll.

Teil 2 (siehe docker-compose.yml, Service ``mc-worker``) bindet dieses Modul
als eigenen Dienst ein, mit ``ENABLE_BACKGROUND_SERVICES=true`` (die API hat
dort ``false`` — genau ein Prozess startet Scheduler/Watchdog/etc., siehe
Scheduler-Redis-Lock, der das zusaetzlich absichert). Ausfuehren (gleiches
Image wie die API):

    python -m app.worker

Vault-Watcher/-Compactor (Rex-Review PR #495, Blocker B1): ``app.main.
start_vault_services()``/``stop_vault_services()`` sind der gemeinsame
Vault-Startpfad, den dieses Modul jetzt genauso aufruft wie ``app.main.
lifespan()`` — vorher haengten beide am Vault-Wiring in ``lifespan()``
selbst, das dieses Modul nie ausfuehrte. Mit ``ENABLE_BACKGROUND_SERVICES=
false`` auf der API (der Normalfall seit Teil 2) liefen sie dadurch in
KEINEM Prozess. Siehe die Docstrings der beiden Funktionen in ``app.main``.
"""

import asyncio
import logging
import signal

from app.config import settings
from app.main import (
    app,
    prepare_process,
    start_background_services,
    start_vault_services,
    stop_background_services,
    stop_vault_services,
)

logger = logging.getLogger("mc.worker")


async def run() -> None:
    if not settings.enable_background_services:
        logger.warning(
            "ENABLE_BACKGROUND_SERVICES=false — worker hat nichts zu starten, beende."
        )
        return

    await prepare_process()

    logger.info("Worker startet Hintergrund-Dienste (kein HTTP-Router, kein Port offen)")
    await start_background_services(app)
    await start_vault_services(app)
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
        # Vault-Stop vor stop_background_services(), spiegelbildlich zur
        # Startreihenfolge oben und zu lifespan()'s Shutdown-Reihenfolge.
        await stop_vault_services(app)
        await stop_background_services(app)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
