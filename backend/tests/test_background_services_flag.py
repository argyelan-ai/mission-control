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

import contextlib
import inspect
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
    # Vault-Wiring in lifespan() selbst (nicht in start_background_services),
    # muessen aber trotzdem hinter demselben Schalter stehen.
    src = inspect.getsource(main.lifespan)
    assert "if settings.enable_background_services:\n            await vault_watcher.start()" in src
    assert "if settings.enable_background_services:\n            try:\n                vault_compactor = VaultCompactor(" in src


@contextlib.asynccontextmanager
async def _patched_service_starts(mocks: dict[str, AsyncMock]):
    with contextlib.ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch.object(getattr(main, name), "start", mock))
        yield


@contextlib.asynccontextmanager
async def _patched_service_stops(mocks: dict[str, AsyncMock]):
    with contextlib.ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch.object(getattr(main, name), "stop", mock))
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


def test_worker_module_shares_the_same_start_stop_functions():
    # backend/app/worker.py MUSS denselben Startpfad wie die API-lifespan
    # benutzen — sonst laufen zwei unterschiedliche Dienst-Listen auseinander.
    import app.worker as worker

    assert worker.start_background_services is main.start_background_services
    assert worker.stop_background_services is main.stop_background_services
    assert worker.app is main.app


@pytest.mark.asyncio
async def test_worker_run_does_nothing_when_flag_is_false(monkeypatch):
    import app.worker as worker

    monkeypatch.setattr(worker.settings, "enable_background_services", False)
    start_mock = AsyncMock()
    monkeypatch.setattr(worker, "start_background_services", start_mock)

    await worker.run()

    start_mock.assert_not_awaited()
