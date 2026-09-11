"""ENABLE_BACKGROUND_SERVICES (Architektur E, Teil 1 — Vorbereitung Worker-Container).

``start_background_services()``/``stop_background_services()`` in
``app.main`` sind der gemeinsame Startpfad fuer die API-lifespan UND
``backend/app/worker.py``. CI hat keinen echten Postgres/Redis-Service
(siehe conftest.py — alles laeuft gegen In-Memory-SQLite + fakeredis), und
jeder Dienst-``.start()`` haengt an genau diesen beiden — deshalb sind diese
Tests bewusst vollstaendig gemockt statt die echte lifespan() auszufuehren.
Das strukturelle Wiring (source inspection) folgt dem Muster aus
``test_boot_secret_guard.py::test_lifespan_wires_the_guard``.
"""

import asyncio
import contextlib
import inspect
import os
import re
import signal
from unittest.mock import AsyncMock, patch

import pytest

import app.main as main
from app.config import Settings

# Die 17 "einfachen" Singleton-Dienste, die start_/stop_background_services()
# direkt aufrufen (vault_watcher/vault_compactor haengen am Vault-Wiring in
# lifespan() und sind dort separat gegated — siehe PR-Text "Streitfall").
THE_17_SIMPLE_SERVICES = [
    "scheduler",
    "watchdog",
    "task_runner",
    "loop_runner",
    "group_runner",
    "intelligence",
    "file_indexer",
    "embedding_retry",
    "obsidian_export",
    "runtime_schedule_service",
    "runtime_watcher",
    "runtime_pulse",
    "cli_update_checker",
    "model_catalog_checker",
    "local_registry_checker",
    "telegram_bot",
    "slack_socket",
]


def test_enable_background_services_defaults_true():
    # Ohne gesetzte Env darf sich am heutigen Verhalten nichts aendern —
    # alles laeuft weiter im API-Prozess, solange kein Worker existiert (Teil 2).
    assert Settings(_env_file=None).enable_background_services is True


def test_enable_background_services_env_override_false(monkeypatch):
    monkeypatch.setenv("ENABLE_BACKGROUND_SERVICES", "false")
    assert Settings().enable_background_services is False


def test_enable_background_services_constructor_override():
    assert Settings(_env_file=None, enable_background_services=False).enable_background_services is False


def test_lifespan_gates_background_services_on_the_flag():
    # Der Schalter schuetzt nur, wenn lifespan() ihn tatsaechlich abfragt.
    src = inspect.getsource(main.lifespan)
    assert src.count("if settings.enable_background_services:") >= 2  # Start + Shutdown
    assert "await start_background_services(app)" in src
    assert "await stop_background_services(app)" in src


def test_lifespan_gates_vault_watcher_and_compactor_too():
    # Streitfall aus dem PR-Text: vault_watcher/vault_compactor haengen am
    # Vault-Wiring (nicht in start_background_services), muessen aber
    # trotzdem hinter demselben Schalter stehen.
    #
    # Seit Architektur E Teil 2 lebt das Vault-Wiring in app.background
    # (start_vault_services) — die API-lifespan DELEGIERT dorthin. Das
    # Gating muss in der delegierten Implementierung unverraendert
    # bestehen: watcher.start() direkt im if-Block, compactor im try
    # direkt dahinter.
    #
    # Regex statt Quelltext-Vergleich inkl. exakter Einrueckung (Rex-Review
    # PR #479, M4): jede Umformatierung (schwarz/ruff, ein zusaetzlicher
    # Kommentar) brach den alten wortwoertlichen Vergleich, ohne dass sich
    # am Verhalten etwas aendert. \s+ toleriert beliebige
    # Einrueckungstiefe/-art, verlangt aber weiterhin, dass der Aufruf
    # UNMITTELBAR im if-Block steht (Praezedenzfall test_boot_secret_guard.py
    # matcht nur einen Funktionsnamen — hier zusaetzlich die Block-Struktur,
    # weil vault_watcher/vault_compactor sonst unbemerkt aus dem Gating
    # rutschen koennten).
    import app.background as bg_mod

    src = inspect.getsource(bg_mod.start_vault_services)
    assert re.search(
        r"if settings\.enable_background_services:\s*\n\s*await vault_watcher\.start\(\)",
        src,
    ), "vault_watcher.start() muss direkt im ENABLE_BACKGROUND_SERVICES-if-Block stehen"
    assert re.search(
        r"if settings\.enable_background_services:\s*\n\s*try:\s*\n\s*vault_compactor = VaultCompactor\(",
        src,
    ), "vault_compactor = VaultCompactor(...) muss direkt im ENABLE_BACKGROUND_SERVICES-if-Block stehen"
    # Und die API-lifespan delegiert wirklich — kein zweites Vault-Wiring in app.main.
    assert "start_vault_services" in inspect.getsource(main.lifespan)
    assert "stop_vault_services" in inspect.getsource(main.lifespan)


import app.background as bg_mod


@contextlib.asynccontextmanager
async def _patched_service_starts(mocks: dict[str, AsyncMock]):
    with contextlib.ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch.object(getattr(bg_mod, name), "start", mock))
        yield



@contextlib.asynccontextmanager
async def _patched_service_stops(mocks: dict[str, AsyncMock]):
    with contextlib.ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch.object(getattr(bg_mod, name), "stop", mock))
        yield


async def _noop_gh_monitor() -> None:
    return


@pytest.mark.asyncio
async def test_start_background_services_starts_all_17_plus_gh_monitor(monkeypatch):
    # obsidian_export.start() haengt zusaetzlich an seinem eigenen Flag
    # (obsidian_export_enabled, Default False) — fuer diesen Test an, damit
    # wirklich alle 17 durchlaufen.
    monkeypatch.setattr(main.settings, "obsidian_export_enabled", True)
    monkeypatch.setattr(
        "app.services.task_queue.purge_finished_queue_entries",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.services.github_visibility_monitor.run_forever",
        _noop_gh_monitor,
    )

    start_mocks = {name: AsyncMock() for name in THE_17_SIMPLE_SERVICES}
    async with _patched_service_starts(start_mocks):
        await main.start_background_services(main.app)

    for name, mock in start_mocks.items():
        mock.assert_awaited_once()

    # gh_monitor laeuft als eigener Task, nicht als .start()-Methode.
    gh_task = main.app.state.gh_monitor_task
    assert gh_task is not None
    await gh_task  # _noop_gh_monitor() gibt sofort zurueck

    # Aufraeumen, damit spaetere Tests keinen bereits-fertigen Task erben.
    monkeypatch.setattr(main.settings, "obsidian_export_enabled", False)
    # m6 (Rex-Review PR #479): main.app ist das echte globale FastAPI-Objekt
    # (kein Test-Double) — ohne diesen Reset erbt jeder spaetere Test, der
    # app.state anfasst, den bereits-fertigen Task von hier.
    del main.app.state.gh_monitor_task


@pytest.mark.asyncio
async def test_stop_background_services_stops_all_17_plus_gh_monitor(monkeypatch):
    monkeypatch.setattr(
        "app.services.github_visibility_monitor.run_forever",
        _noop_gh_monitor,
    )
    monkeypatch.setattr(
        "app.services.task_queue.purge_finished_queue_entries",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(main.settings, "obsidian_export_enabled", True)

    start_mocks = {name: AsyncMock() for name in THE_17_SIMPLE_SERVICES}
    async with _patched_service_starts(start_mocks):
        await main.start_background_services(main.app)

    stop_mocks = {name: AsyncMock() for name in THE_17_SIMPLE_SERVICES}
    async with _patched_service_stops(stop_mocks):
        await main.stop_background_services(main.app)

    for name, mock in stop_mocks.items():
        mock.assert_awaited_once()

    monkeypatch.setattr(main.settings, "obsidian_export_enabled", False)
    # m6 (Rex-Review PR #479): stop_background_services() cancelt den Task,
    # loescht das Attribut aber nicht — gleicher Reset wie im Start-Test.
    del main.app.state.gh_monitor_task


@pytest.mark.asyncio
async def test_worker_run_does_nothing_when_flag_is_false(monkeypatch):
    import app.worker as worker

    monkeypatch.setattr(worker.settings, "enable_background_services", False)
    prepare_mock = AsyncMock()
    start_mock = AsyncMock()
    monkeypatch.setattr(worker, "prepare_process", prepare_mock)
    monkeypatch.setattr(worker, "start_background_services", start_mock)

    await worker.run()

    prepare_mock.assert_not_awaited()
    start_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_run_prepares_process_before_starting_services(monkeypatch):
    # Rex-Review PR #479, Blocker B2: der alte Identitaets-Check
    # (`worker.start_background_services is main.start_background_services`)
    # konnte nicht fehlschlagen, solange der Import stand — er hat NICHT
    # geprueft, dass worker.run() denselben Vorbereitungspfad (Boot-Secret-
    # Guard, DB-Seeds, Channel-/AI-Provider-Overrides) durchlaeuft wie
    # main.lifespan(), bevor die Dienste starten. Dieser Test faehrt
    # worker.run() echt (mit gemockten Diensten) und prueft die Reihenfolge.
    import app.worker as worker

    call_order: list[str] = []

    async def _fake_prepare():
        call_order.append("prepare_process")

    async def _fake_start(app):
        call_order.append("start_background_services")

    async def _fake_stop(app):
        call_order.append("stop_background_services")

    monkeypatch.setattr(worker, "prepare_process", _fake_prepare)
    monkeypatch.setattr(worker, "start_background_services", _fake_start)
    monkeypatch.setattr(worker, "stop_background_services", _fake_stop)
    monkeypatch.setattr(worker.settings, "enable_background_services", True)

    async def _stop_soon():
        await asyncio.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(asyncio.gather(worker.run(), _stop_soon()), timeout=5)

    assert call_order == [
        "prepare_process",
        "start_background_services",
        "stop_background_services",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_worker_run_shuts_down_gracefully_on_signal(monkeypatch, sig):
    # Blocker B1 (Rex-Review PR #479): CPython wandelt SIGTERM NICHT in eine
    # Exception um — die Default-Disposition beendet den Prozess sofort,
    # OHNE dass ein `finally`-Block laeuft. `docker stop` schickt SIGTERM
    # (nicht SIGINT). Vor dem Fix lief stop_background_services() bei
    # SIGTERM nie — der Redis-Scheduler-Lock (RedisKeys.scheduler_lock(),
    # siehe #133) blieb stehen, Slack-Socket/Telegram-Poller blieben offen.
    #
    # Probe (aus dem Review): "SIGTERM -> rc=-15, finally lief NICHT" vs.
    # "SIGINT -> rc=0, finally lief". Dieser Test schickt beide Signale
    # real an den eigenen Prozess (nicht simuliert) und prueft, dass
    # stop_background_services() in JEDEM Fall laeuft, weil worker.run()
    # jetzt eigene Handler fuer beide Signale registriert statt sich auf
    # asyncio.run()s KeyboardInterrupt-Pfad zu verlassen (der nur fuer
    # SIGINT funktioniert).
    import app.worker as worker

    monkeypatch.setattr(worker, "prepare_process", AsyncMock())
    start_mock = AsyncMock()
    stop_mock = AsyncMock()
    monkeypatch.setattr(worker, "start_background_services", start_mock)
    monkeypatch.setattr(worker, "stop_background_services", stop_mock)
    monkeypatch.setattr(worker.settings, "enable_background_services", True)

    async def _send_signal_soon():
        await asyncio.sleep(0.02)
        os.kill(os.getpid(), sig)

    await asyncio.wait_for(asyncio.gather(worker.run(), _send_signal_soon()), timeout=5)

    start_mock.assert_awaited_once()
    stop_mock.assert_awaited_once()
