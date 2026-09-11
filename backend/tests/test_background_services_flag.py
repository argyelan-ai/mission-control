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
from types import SimpleNamespace
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
async def test_start_vault_services_lint_task_is_none_when_flag_false(monkeypatch):
    # Rex-Review PR #500, Blocker B3: start_vault_services() laeuft in BEIDEN
    # Prozessen (API-lifespan + worker.run()). Erzeugte der Lint-Cron seinen
    # asyncio-Task unconditional, lief _vault_lint_loop doppelt — zwei
    # Schreiber auf _lint/YYYY-MM-DD.md, zwei Operator-Pings. Der Cron ist
    # Teil des ENABLE_BACKGROUND_SERVICES-Inventars; bei flag=False darf
    # lint_task None bleiben (watcher/compactor-Objekt darf trotzdem
    # existieren — nur .start() ist gegatet).
    import app.background as bg

    monkeypatch.setattr(bg.settings, "enable_background_services", False)
    # Vault-Wiring von der Disk/Redis entkoppeln: nur das Gating interessiert.
    monkeypatch.setattr(bg.VaultIndex, "rebuild_from_vault", lambda self: {
        "scanned": 0, "indexed": 0, "skipped": 0, "errors": 0,
    })
    monkeypatch.setattr(
        bg.VaultWatcher, "start", AsyncMock(),
    )

    runtime = await bg.start_vault_services(SimpleNamespace(state=SimpleNamespace()))

    assert runtime["vault_lint_task"] is None, (
        "vault_lint_task muss bei ENABLE_BACKGROUND_SERVICES=False None sein "
        "(B3: sonst laeuft der Lint-Cron in beiden Prozessen)"
    )


@pytest.mark.asyncio
async def test_start_vault_services_lint_task_scheduled_when_flag_true(monkeypatch):
    # Gegenstueck: mit flag=True ist der Cron ein laufender Task (und wird
    # beim Shutdown von stop_vault_services gecancelt).
    import app.background as bg

    monkeypatch.setattr(bg.settings, "enable_background_services", True)
    monkeypatch.setattr(bg.settings, "vault_lint_interval_hours", 99999)
    monkeypatch.setattr(bg.VaultIndex, "rebuild_from_vault", lambda self: {
        "scanned": 0, "indexed": 0, "skipped": 0, "errors": 0,
    })
    monkeypatch.setattr(
        bg.VaultWatcher, "start", AsyncMock(),
    )

    runtime = await bg.start_vault_services(SimpleNamespace(state=SimpleNamespace()))
    try:
        assert runtime["vault_lint_task"] is not None
    finally:
        if runtime["vault_lint_task"] is not None:
            runtime["vault_lint_task"].cancel()
            try:
                await runtime["vault_lint_task"]
            except (asyncio.CancelledError, Exception):
                pass


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


@pytest.mark.asyncio
async def test_vault_reindex_does_not_block_sigterm_handling(monkeypatch):
    """W4 (11.09.2026, Karte 70d6b417 — Restposten aus #506, Punkt 2):
    live gemessen haengte ein `docker stop` direkt nach einem
    Worker-Recreate die vollen 30s, waehrend der Worker noch im
    Vault-Reindex war -> SIGKILL (der Lock blieb liegen, wurde vom neuen
    Worker aber korrekt auf Versuch 1 gestohlen -- der #506-Fix hielt).

    Ursache (gemessen, nicht vermutet -- siehe
    tools/repro_sigterm.py-artiges Setup unten): ``VaultIndex(...)`` und
    ``rebuild_from_vault()`` liefen in ``start_vault_services()`` synchron
    (kein await, kein Yield-Punkt). asyncio liefert einen per
    ``add_signal_handler`` registrierten Callback nur aus, wenn der
    Event-Loop pollt -- ein rein synchroner Call haelt den Loop komplett
    an, also auch jede Signalverarbeitung, fuer seine gesamte Laufzeit.
    Ein waehrend des Reindex eintreffendes SIGTERM (echtes ``docker
    stop``) konnte dadurch erst verarbeitet werden, NACHDEM der Rebuild
    fertig war -- bei einem hinreichend grossen Vault laenger als
    Dockers Stop-Timeout, daher SIGKILL statt eines sauberen Shutdowns.

    Reproduziert das Signal real (``os.kill`` auf den eigenen Prozess,
    gleiches Muster wie ``test_worker_run_shuts_down_gracefully_on_signal``
    oben) waehrend eines kuenstlich verlangsamten Rebuilds -- misst also
    tatsaechlich, ob der Event-Loop responsive bleibt, statt es zu
    behaupten.
    """
    import os
    import signal
    import threading
    import time

    import app.background as bg

    # first_boot ist in der session-weiten Test-Vault meist schon False
    # (siehe conftest._TEST_VAULT_ROOT) -- das Flag erzwingt den Rebuild-Pfad
    # unabhaengig davon.
    monkeypatch.setattr(bg.settings, "vault_index_rebuild_on_boot", True)
    monkeypatch.setattr(bg.VaultWatcher, "start", AsyncMock())

    REINDEX_SECONDS = 2.0

    def _slow_rebuild(self):
        time.sleep(REINDEX_SECONDS)  # Stellvertreter fuer einen echten Vault-Scan
        return {"scanned": 0, "indexed": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr(bg.VaultIndex, "rebuild_from_vault", _slow_rebuild)

    loop = asyncio.get_running_loop()
    signal_received_at: dict[str, float] = {}

    def _on_term():
        signal_received_at["t"] = time.monotonic()

    loop.add_signal_handler(signal.SIGTERM, _on_term)
    try:
        sent_at = time.monotonic()

        def _sender():
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_sender, daemon=True).start()

        await bg.start_vault_services(SimpleNamespace(state=SimpleNamespace()))

        # Dem Loop einen Moment geben, den bereits gequeuten Callback
        # auszuliefern, falls er nicht schon gefeuert hat.
        await asyncio.sleep(0.1)

        assert "t" in signal_received_at, "SIGTERM-Handler ist nie gefeuert"
        delay = signal_received_at["t"] - sent_at
        assert delay < 1.0, (
            f"SIGTERM wurde erst nach {delay:.2f}s verarbeitet -- der "
            f"Vault-Reindex ({REINDEX_SECONDS}s) hat den Event-Loop "
            "blockiert statt in einem eigenen Thread zu laufen"
        )
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def test_worker_run_registers_signal_handlers_before_boot_steps():
    """W4 (11.09.2026, Karte 70d6b417 — Rex-Review PR #509, Blocker B1):

    Der obige Test (test_vault_reindex_does_not_block_sigterm_handling)
    registriert seinen SIGTERM-Handler VOR dem Aufruf von
    start_vault_services() -- also in genau der falschen Reihenfolge im
    Vergleich zu worker.run(), das den Handler bis nach prepare_process()
    + start_background_services() + start_vault_services() gar nicht
    registrierte. Ein SIGTERM, das waehrend dieser drei Schritte eintraf,
    lief damit auf SIG_DFL -- keinen Python-Handler. Fuer einen GEWOEHNLICHEN
    Prozess killt SIG_DFL/SIGTERM sofort (Sub-Prozess-Repro, siehe PR-Text:
    rc=-15, keine messbare Verzoegerung) -- mc-worker laeuft aber als PID 1
    seines Containers, wo der Kernel ein Signal ohne Handler laut
    `man 7 pid_namespaces` gar nicht per Default-Aktion verarbeitet, sondern
    verwirft (siehe worker.py-Kommentar oben fuer Details) -- was den
    gemessenen 30s-Haenger-bis-SIGKILL erst erklaert. Der obige Test bewies
    also nur, dass der Loop responsive bleibt, WENN schon ein Handler
    existiert -- nicht, dass im Worker ueberhaupt einer existiert, wenn
    es darauf ankommt. Fix: Handler-Registrierung an den Anfang von
    run() gezogen, vor jeden Boot-Schritt.

    Diese Guard-Assertion laeuft bewusst OHNE echtes Signal (ein
    unbehandeltes SIGTERM im Test-Prozess selbst wuerde nicht den Test,
    sondern den gesamten pytest-Lauf beenden -- das eigentliche
    Sub-Prozess-Repro dafuer steht im PR-Text). Stattdessen strukturelle
    Quellcode-Pruefung, analog zum Muster in test_boot_secret_guard.py::
    test_lifespan_wires_the_guard: Ohne den Fix (Handler-Block NACH den
    drei Boot-Calls) ist diese Assertion ROT -- mit dem Fix GRUEN.
    """
    import app.worker as worker

    src = inspect.getsource(worker.run)
    handler_idx = src.index("add_signal_handler")
    prepare_idx = src.index("await prepare_process()")
    start_bg_idx = src.index("await start_background_services(state)")
    start_vault_idx = src.index("await start_vault_services(state)")

    assert handler_idx < prepare_idx, (
        "add_signal_handler() muss VOR prepare_process() stehen -- sonst "
        "trifft ein frueh eintreffendes SIGTERM auf SIG_DFL"
    )
    assert handler_idx < start_bg_idx, (
        "add_signal_handler() muss VOR start_background_services() stehen"
    )
    assert handler_idx < start_vault_idx, (
        "add_signal_handler() muss VOR start_vault_services() stehen -- "
        "das war Blocker B1 aus Rex-Review PR #509"
    )


@pytest.mark.asyncio
async def test_worker_run_survives_sigterm_during_vault_reindex(monkeypatch):
    """W4 (11.09.2026, Karte 70d6b417 — Rex-Review PR #509, Blocker B1):

    Faehrt den ECHTEN Produktionspfad (worker.run(), nicht nur
    start_vault_services() isoliert) mit einem SIGTERM, das waehrend
    eines verlangsamten Vault-Reindex eintrifft -- genau das vom Review
    verlangte Szenario. prepare_process()/start_background_services()/
    stop_background_services() sind gemockt (gleiches Muster wie
    test_worker_run_shuts_down_gracefully_on_signal oben; CI hat kein
    echtes Postgres/Redis fuer diese 17 Dienste), start_vault_services()/
    stop_vault_services() sind ECHT -- das ist der Teil, um den es beim
    B1-Blocker ging.

    Sicher, weil mit dem B1-Fix der Handler laengst registriert ist, BEVOR
    dieser Test das Signal ueberhaupt schickt -- ein unbehandeltes SIGTERM
    (das den Testlauf killen wuerde) kann hier nicht mehr auftreten. Genau
    das ist die Verhaltensaenderung, die bewiesen werden soll: vorher
    (Handler-Registrierung nach start_vault_services) waere dieser Test
    bei einem Direktaufruf real mit rc=-15 gestorben statt zu asserten --
    siehe Sub-Prozess-Repro im PR-Text fuer den Beleg dieses roten
    Verhaltens (in-process ist ein SIGTERM-Tod nicht sicher reproduzierbar,
    siehe Docstring von test_worker_run_registers_signal_handlers_before_boot_steps).
    """
    import os
    import signal
    import time

    import app.background as bg
    import app.worker as worker

    monkeypatch.setattr(worker, "prepare_process", AsyncMock())
    start_mock = AsyncMock()
    stop_mock = AsyncMock()
    monkeypatch.setattr(worker, "start_background_services", start_mock)
    monkeypatch.setattr(worker, "stop_background_services", stop_mock)
    monkeypatch.setattr(worker.settings, "enable_background_services", True)

    monkeypatch.setattr(bg.settings, "vault_index_rebuild_on_boot", True)
    monkeypatch.setattr(bg.VaultWatcher, "start", AsyncMock())

    REINDEX_SECONDS = 0.5

    def _slow_rebuild(self):
        time.sleep(REINDEX_SECONDS)
        return {"scanned": 0, "indexed": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr(bg.VaultIndex, "rebuild_from_vault", _slow_rebuild)

    async def _send_signal_mid_reindex():
        await asyncio.sleep(0.1)  # nach start(), waehrend des 0.5s-Reindex
        os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(
        asyncio.gather(worker.run(), _send_signal_mid_reindex()), timeout=5
    )

    start_mock.assert_awaited_once()
    stop_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_vault_compactor_stop_does_not_block_event_loop(monkeypatch):
    """W4 (11.09.2026, Karte 70d6b417): zweiter Fund beim Nacharbeiten von
    Blocker B1 (Rex-Review PR #509) -- Grep nach jedem synchronen
    ``.join(`` im Backend (``grep -rn "\\.join(" backend/app/``) zeigte,
    dass ``VaultCompactor.stop()`` (vault_compactor.py:60) exakt dieselbe
    Bug-Klasse hat wie der urspruengliche Vault-Reindex und
    ``VaultWatcher.stop()`` -- ein plain-sync ``Thread.join(timeout=5)``
    im Event-Loop, das jede Signalverarbeitung fuer bis zu 5s blockieren
    kann. Im urspruenglichen PR wurde ``VaultWatcher.stop()`` gefixt,
    ``VaultCompactor.stop()`` (identischer Code, anderes Modul) aber
    uebersehen.

    Gleiches Testmuster wie test_vault_reindex_does_not_block_sigterm_handling:
    Handler VOR dem Aufruf registrieren (das ist hier sicher -- es geht
    um Responsivitaet bei einem bereits existierenden Handler, nicht um
    dessen Existenz), Observer.join kuenstlich verlangsamen, SIGTERM
    waehrend des Joins schicken, Verarbeitungsverzoegerung messen.
    """
    import os
    import signal
    import threading
    import time
    from unittest.mock import MagicMock

    from app.services.vault_compactor import VaultCompactor

    redis_mock = MagicMock(set=AsyncMock(return_value=True), publish=AsyncMock())
    compactor = VaultCompactor(vault_path=None, redis=redis_mock)

    JOIN_SECONDS = 1.5

    class _FakeObserver:
        def stop(self):
            pass

        def join(self, timeout=None):
            time.sleep(JOIN_SECONDS)  # Stellvertreter fuer einen echten, langsam drainenden Observer-Thread

    compactor._observer = _FakeObserver()

    loop = asyncio.get_running_loop()
    signal_received_at: dict[str, float] = {}

    def _on_term():
        signal_received_at["t"] = time.monotonic()

    loop.add_signal_handler(signal.SIGTERM, _on_term)
    try:
        sent_at = time.monotonic()

        def _sender():
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_sender, daemon=True).start()

        await compactor.stop()

        await asyncio.sleep(0.1)

        assert "t" in signal_received_at, "SIGTERM-Handler ist nie gefeuert"
        delay = signal_received_at["t"] - sent_at
        assert delay < 1.0, (
            f"SIGTERM wurde erst nach {delay:.2f}s verarbeitet -- "
            f"VaultCompactor.stop() ({JOIN_SECONDS}s Observer.join) hat "
            "den Event-Loop blockiert statt in einem eigenen Thread zu laufen"
        )
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


@pytest.mark.asyncio
async def test_stop_vault_services_names_the_slow_step_in_log(caplog):
    """W4 (11.09.2026, Karte 70d6b417 — Rex-Review PR #509 DoD-Punkt 4):
    "der schuldige Shutdown-Schritt ist aus dem `_timed_stop`-Log
    NAMENTLICH benannt, nicht vermutet". stop_vault_services() hatte VOR
    diesem Fix ueberhaupt kein Timing -- anders als stop_background_services(),
    das seine 17 Schritte schon seit Incident Deploy #504 einzeln loggt.

    Simuliert einen langsamen Compactor-Stop (die real gefundene Ursache,
    siehe test_vault_compactor_stop_does_not_block_event_loop) und prueft,
    dass das Log den Schritt beim Namen nennt -- nicht "irgendwas war
    langsam", sondern konkret "vault_compactor".
    """
    import logging
    import time

    import app.background as bg

    class _SlowCompactor:
        async def stop(self):
            await asyncio.to_thread(time.sleep, 1.2)

    runtime = {
        "vault_lint_task": None,
        "vault_compactor": _SlowCompactor(),
        "vault_watcher": None,
        "vault_index": None,
    }

    with caplog.at_level(logging.INFO, logger="mc.startup"):
        await bg.stop_vault_services(runtime)

    matching = [
        r for r in caplog.records
        if "Shutdown: vault_compactor stopped in" in r.getMessage()
    ]
    assert matching, (
        "Log nennt den langsamen Schritt nicht namentlich -- gefundene "
        f"Zeilen: {[r.getMessage() for r in caplog.records]}"
    )
    logged_seconds = float(matching[0].getMessage().rsplit(" ", 1)[-1].rstrip("s"))
    assert logged_seconds >= 1.0, (
        f"geloggte Dauer ({logged_seconds}s) passt nicht zum simulierten "
        "1.2s-Compactor-Stop"
    )
    assert matching[0].levelname == "WARNING", (
        "Shutdown-Schritte >1s sollen als WARNING geloggt werden (siehe "
        "_timed_stop), damit sie im Deploy-Log auffallen statt in INFO "
        "unterzugehen"
    )
