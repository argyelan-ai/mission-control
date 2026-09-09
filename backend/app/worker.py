"""Standalone entrypoint for Mission Control's background services.

Architektur E, Teil 1 (Vorbereitung eines eigenen Worker-Containers — siehe
PR-Text fuer das Inventar aller betroffenen Dienste). Dieses Modul startet
GENAU dieselben Hintergrund-Dienste wie ``app.main.lifespan`` (ueber die dort
extrahierten ``start_background_services``/``stop_background_services``),
aber OHNE FastAPI-HTTP-Router — kein Uvicorn, kein offener Port. Request-
gebundene Dinge (Terminal-/Browser-WebSockets, alle HTTP-Endpunkte) bleiben
ausschliesslich im API-Prozess (``app.main``).

Teil 1 bindet dieses Modul NOCH NICHT in docker-compose ein — es gibt noch
keinen ``mc-worker``-Service. Ausfuehren (gleiches Image wie die API):

    python -m app.worker

``ENABLE_BACKGROUND_SERVICES`` muss dafuer auf einem der beiden Prozesse auf
``false`` stehen (sonst starten Scheduler/Watchdog/etc. doppelt — siehe
Scheduler-Redis-Lock, der genau das absichert, aber unnoetig waere). Solange
kein Worker-Container existiert, bleibt der Default ``true`` in der API
unveraendert, und dieses Modul ist reine Vorbereitung.

Vault-Watcher/-Compactor sind NICHT Teil dieses Moduls (siehe PR-Text,
"Streitfall") — sie haengen am Vault-Wiring in ``app.main.lifespan`` und
werden dort separat gegated. Das in eine von hier aufrufbare Funktion zu
ziehen ist Teil 2.
"""

import asyncio
import logging

from app.config import settings
from app.main import app, start_background_services, stop_background_services

logger = logging.getLogger("mc.worker")


async def run() -> None:
    if not settings.enable_background_services:
        logger.warning(
            "ENABLE_BACKGROUND_SERVICES=false — worker hat nichts zu starten, beende."
        )
        return

    logger.info("Worker startet Hintergrund-Dienste (kein HTTP-Router, kein Port offen)")
    await start_background_services(app)
    logger.info("Worker: Hintergrund-Dienste laufen")
    try:
        await asyncio.Event().wait()  # laeuft bis SIGTERM/SIGINT (asyncio.run() cancelt den Task)
    finally:
        logger.info("Worker faehrt Hintergrund-Dienste herunter")
        await stop_background_services(app)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
