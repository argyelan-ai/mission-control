"""Plattenplatz: preflight gate + report-only watchdog.

Card context (2026-09-16): the disk filled up. `docker system df` showed
75.8 GB of build cache — Docker never garbage-collects it on its own — and
`docker compose up --build` died mid-layer-write with a raw "no space left on
device", after minutes of work and with nothing pointing at the cause. Two
halves are tested here, both behavioral:

* the preflight REFUSES a build below the threshold and ALLOWS it above it,
* the watchdog REPORTS at/above the threshold and STAYS SILENT below it —
  and never deletes anything (report-only is the card's hard requirement).

`df` is stubbed rather than faked through 376 GB of real free space, and the
threshold is set through the project's config path (``settings.build_min_free_gb``)
— which is also the assertion that no threshold is hardcoded.
"""
from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.services import disk_preflight


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


def _df_output(free_gb: float) -> str:
    """`df -Pk` output with `free_gb` available (column 4 = Avail)."""
    blocks = int(free_gb * 1024 * 1024)
    return (
        "Filesystem     1024-blocks     Used Available Capacity Mounted on\n"
        f"/dev/sda1        100000000 50000000 {blocks}      50% /\n"
    )


def _fake_df(free_gb: float, returncode: int = 0):
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, returncode, stdout=_df_output(free_gb) if returncode == 0 else "", stderr=""
        )

    return _run


# ── Preflight: both directions ──────────────────────────────────────────


def test_preflight_allows_build_with_enough_space():
    """The normal run: plenty of space → no reason to refuse."""
    with patch.object(disk_preflight.subprocess, "run", _fake_df(40)):
        assert disk_preflight.build_preflight_error("/") is None


def test_preflight_refuses_build_below_threshold():
    """The gate: 3 GB free against the shipped 15 GB default must refuse.

    A refusal is worthless unless it says why — the whole card exists because
    the raw docker error named no cause and no remedy. So the message must
    carry both numbers, the variable that sets the threshold, and the fix.
    """
    with patch.object(disk_preflight.subprocess, "run", _fake_df(3)):
        error = disk_preflight.build_preflight_error("/")

    assert error is not None, "3 GB frei bei 15 GB Schwelle muss den Bau verweigern"
    assert "3.0 GB" in error, f"freier Platz fehlt in der Meldung: {error}"
    assert "15 GB" in error, f"Schwelle fehlt in der Meldung: {error}"
    assert "BUILD_MIN_FREE_GB" in error, f"Quelle der Schwelle fehlt: {error}"
    assert "builder prune" in error, f"Handlungsanweisung fehlt: {error}"
    # The bound in the fix must be the CACHE ceiling, never the free space:
    # `--keep-storage 3g` would be wrong here and `--keep-storage 373g` on the
    # full disk this card is about would advise the opposite of the fix.
    assert "keep-storage 20g" in error, f"Cache-Obergrenze fehlt im Fix: {error}"


def test_preflight_refusal_follows_configured_cache_bound():
    """The advice must track BUILD_CACHE_KEEP_GB, not a literal.

    Same drift bug as a hardcoded threshold: the operator changes the setting
    and the message keeps recommending a prune that contradicts it. The shell
    twin of this test is `test_refusal_message_follows_configured_cache_bound`.
    """
    with patch.object(disk_preflight.subprocess, "run", _fake_df(3)), \
         patch.object(disk_preflight.settings, "build_cache_keep_gb", 7):
        error = disk_preflight.build_preflight_error("/")

    assert error is not None and "keep-storage 7g" in error, (
        f"Fix ignoriert BUILD_CACHE_KEEP_GB (erwartet 'keep-storage 7g'): {error}"
    )


def test_preflight_threshold_comes_from_settings():
    """No threshold hardcoded in code: settings is the only source.

    Setting it to a value ABOVE the measured free space must flip the verdict
    from allow to refuse without touching any other input — if the module had
    its own constant, this would stay green.
    """
    with patch.object(disk_preflight.subprocess, "run", _fake_df(40)):
        assert disk_preflight.build_preflight_error("/") is None
        with patch.object(disk_preflight.settings, "build_min_free_gb", 100):
            assert disk_preflight.build_preflight_error("/") is not None


def test_preflight_allows_build_when_df_is_unreadable():
    """Unknown is not zero — a failed measurement must not block a build.

    An unreadable `df` (exotic filesystem, `df` missing) is not evidence of a
    full disk. Refusing there would invent a problem and, on such a mount,
    block every build forever.
    """
    with patch.object(disk_preflight.subprocess, "run", _fake_df(0, returncode=1)):
        assert disk_preflight.build_preflight_error("/") is None


def test_preflight_allows_build_when_df_raises():
    """`df` not installed at all is the same class as unreadable."""
    def _boom(argv, **kwargs):
        raise FileNotFoundError("df")

    with patch.object(disk_preflight.subprocess, "run", _boom):
        assert disk_preflight.build_preflight_error("/") is None


def test_parse_free_gb_ignores_unparseable_output():
    """Garbage must yield None (unknown), never a fabricated 0 GB.

    Returning 0.0 would make every build on that machine refuse — the failure
    mode is loud and wrong, which is the one outcome the card forbids.
    """
    assert disk_preflight.parse_free_gb("") is None
    assert disk_preflight.parse_free_gb("Filesystem 1024-blocks Used Available\n") is None
    assert disk_preflight.parse_free_gb("garbage\nnot a table\n") is None


# ── Cleanup ─────────────────────────────────────────────────────────────


def test_cache_cleanup_bounds_the_cache():
    """The regression itself: an UNBOUNDED build cache hit 75.8 GB.

    `docker builder prune --force` without `--keep-storage` wipes the cache the
    next build depends on; the assertion is on the bound, not on the call.
    """
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch.object(disk_preflight.subprocess, "run", _run):
        disk_preflight.build_cache_cleanup()

    assert calls, "Cleanup hat docker gar nicht aufgerufen"
    argv = calls[0]
    assert argv[:3] == ["docker", "builder", "prune"], f"falscher Aufruf: {argv}"
    assert "--keep-storage" in argv, f"Begrenzung fehlt — Cache wuerde geloescht: {argv}"
    keep = argv[argv.index("--keep-storage") + 1]
    assert keep == f"{disk_preflight.settings.build_cache_keep_gb}g"


def test_cache_cleanup_never_raises():
    """The build already produced its image — a prune error is not a build error."""
    def _boom(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    with patch.object(disk_preflight.subprocess, "run", _boom):
        disk_preflight.build_cache_cleanup()  # must not raise

    def _fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    with patch.object(disk_preflight.subprocess, "run", _fail):
        disk_preflight.build_cache_cleanup()  # must not raise


# ── Watchdog: report-only, both directions ──────────────────────────────


class _Disk:
    def __init__(self, percent: float, free_gb: float = 10.0, total_gb: float = 400.0):
        self.percent = percent
        self.free = int(free_gb * 1024 ** 3)
        self.used = int(total_gb * 1024 ** 3) - self.free
        self.total = int(total_gb * 1024 ** 3)


async def _run_watchdog(percent: float, free_gb: float = 10.0):
    """Drive the real metric collection with a stubbed disk reading.

    Returns the patched ``emit_event`` mock so a test can assert on both the
    call and its arguments — and, crucially, on the ABSENCE of a call.
    """
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.health_checks.psutil.cpu_percent", return_value=5.0), \
         patch("app.services.watchdog.health_checks.psutil.virtual_memory",
               return_value=type("M", (), {"percent": 40.0, "used": 1, "total": 2})()), \
         patch("app.services.watchdog.health_checks.psutil.disk_usage",
               return_value=_Disk(percent, free_gb)), \
         patch("app.services.watchdog.health_checks.emit_event",
               new_callable=AsyncMock) as emit:
        svc = WatchdogService()
        async with _session() as session:
            await svc._collect_system_metrics(1.0, 1.0, session)
    return emit


@pytest.mark.asyncio
async def test_watchdog_reports_above_threshold():
    """At/above the threshold the operator gets told."""
    emit = await _run_watchdog(96.0)
    assert emit.await_count == 1, "96 % Belegung muss gemeldet werden"

    args, kwargs = emit.call_args
    assert args[1] == "system.disk_high"
    # `critical`, not `warning`: a warning waits up to DIGEST_WINDOW_SECONDS
    # (1800) in the Discord digest — half an hour during which every build keeps
    # dying. `critical` goes out immediately (discord_notify.py:151-154).
    assert kwargs["severity"] == "critical", "Plattennotfall darf nicht im Digest warten"


@pytest.mark.asyncio
async def test_watchdog_is_silent_below_threshold():
    """Normal occupancy must produce NOTHING — otherwise the alert is noise
    and gets ignored exactly when it matters."""
    emit = await _run_watchdog(40.0)
    assert emit.await_count == 0, "40 % Belegung darf nicht gemeldet werden"


@pytest.mark.asyncio
async def test_watchdog_reports_exactly_at_threshold():
    """The boundary: `>=` threshold, so 95.0 reports. An off-by-one that only
    fires at 95.1 would leave the operator blind on a round number."""
    emit = await _run_watchdog(95.0)
    assert emit.await_count == 1, "95.0 % muss melden (>= Schwelle, nicht >)"


@pytest.mark.asyncio
async def test_watchdog_does_not_delete_or_change_anything():
    """REPORT-ONLY is the card's hard requirement.

    Nothing may be pruned, removed or executed: a watchdog that cleans up on
    its own can destroy the very build it is meant to protect. The patch on
    subprocess.run is the assertion — autouse `block_real_docker` would raise
    on a `prune`, and outside the test suite nothing would raise at all.
    """
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.health_checks.psutil.disk_usage",
               return_value=_Disk(99.0)), \
         patch("app.services.watchdog.health_checks.emit_event",
               new_callable=AsyncMock) as emit, \
         patch("subprocess.run") as run, \
         patch("subprocess.check_call") as check_call, \
         patch("subprocess.Popen") as popen:
        svc = WatchdogService()
        async with _session() as session:
            await svc._collect_system_metrics(1.0, 1.0, session)

    assert emit.await_count == 1
    assert run.call_count == 0, "Waechter darf keine Prozesse starten (Meldung nur)"
    assert check_call.call_count == 0
    assert popen.call_count == 0


@pytest.mark.asyncio
async def test_watchdog_alert_reaches_the_database_and_touches_no_task():
    """End-to-end through the REAL `emit_event`, not a mock.

    Every other test here patches `emit_event`, which proves the call happens
    but not that it lands anywhere. This one lets it through so the operator's
    view is checked: an ActivityEvent row exists with the right type, and —
    the report-only half — no task status moved and no comment was posted.
    """
    from app.models.activity import ActivityEvent
    from app.services.watchdog.core import WatchdogService
    from sqlmodel import select

    with patch("app.services.watchdog.health_checks.psutil.disk_usage",
               return_value=_Disk(97.0)):
        svc = WatchdogService()
        async with _session() as session:
            await svc._collect_system_metrics(1.0, 1.0, session)

    async with _session() as s:
        events = list((await s.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "system.disk_high")
        )).all())

    assert len(events) == 1, f"Warnung hat die Datenbank nicht erreicht: {events}"
    assert events[0].severity == "critical"
    assert "97.0" in events[0].title
    assert events[0].detail["threshold_percent"] == 95


@pytest.mark.asyncio
async def test_watchdog_note_names_the_configured_cleanup_bound():
    """The operator hint must match their own settings.

    A literal `--keep-storage 20g` in the message would be the same drift bug
    this card is about: change `BUILD_CACHE_KEEP_GB` and the warning starts
    telling the operator to run something that contradicts their config.
    """
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.health_checks.psutil.disk_usage",
               return_value=_Disk(97.0)), \
         patch("app.services.watchdog.health_checks.settings.build_cache_keep_gb", 33), \
         patch("app.services.watchdog.health_checks.emit_event",
               new_callable=AsyncMock) as emit:
        svc = WatchdogService()
        async with _session() as session:
            await svc._collect_system_metrics(1.0, 1.0, session)

    note = emit.call_args.kwargs["detail"]["note"]
    assert "--keep-storage 33g" in note, f"Hinweis folgt nicht der Konfiguration: {note}"


@pytest.mark.asyncio
async def test_watchdog_does_not_restack_within_ttl():
    """Two ticks inside the dedup TTL → one alert, not one per tick.

    `severity="critical"` deliberately bypasses discord_notify's own gates
    (discord_notify.py:151-154: never counted, never deduped), so the Redis
    marker set here is the ONLY thing preventing a message every watchdog tick.
    """
    from app.services.watchdog.core import WatchdogService

    fake = AsyncMock()
    fake.exists = AsyncMock(return_value=True)  # marker already present

    with patch("app.services.watchdog.health_checks.psutil.disk_usage",
               return_value=_Disk(97.0)), \
         patch("app.services.watchdog.health_checks.get_redis", new=AsyncMock(return_value=fake)), \
         patch("app.services.watchdog.health_checks.emit_event",
               new_callable=AsyncMock) as emit:
        svc = WatchdogService()
        async with _session() as session:
            await svc._collect_system_metrics(1.0, 1.0, session)

    assert emit.await_count == 0, "innerhalb der TTL darf nicht erneut gemeldet werden"
    # `set` is also how the metrics snapshot writes its current-value key, so
    # the assertion is on the DEDUP key specifically — a blanket
    # assert_not_awaited would fail on the snapshot, which is supposed to run.
    dedup_writes = [
        c for c in fake.set.await_args_list
        if c.args and str(c.args[0]).startswith("mc:watchdog:disk_notified")
    ]
    assert dedup_writes == [], f"Dedup-Marke wurde trotz vorhandenem Marker gesetzt: {dedup_writes}"


@pytest.mark.asyncio
async def test_watchdog_metrics_snapshot_survives_a_failing_check():
    """A disk-check error must not cost the metrics snapshot.

    `_collect_system_metrics` writes cpu/memory/disk into Redis for the
    dashboard; the disk alert rides on the same try block. If the alert could
    take the snapshot down with it, a Redis hiccup in the alert path would blank
    the operator's metrics view.
    """
    from app.services.watchdog.core import WatchdogService

    fake = AsyncMock()
    fake.exists = AsyncMock(side_effect=RuntimeError("redis down"))

    with patch("app.services.watchdog.health_checks.psutil.disk_usage",
               return_value=_Disk(97.0)), \
         patch("app.services.watchdog.health_checks.get_redis", new=AsyncMock(return_value=fake)):
        svc = WatchdogService()
        async with _session() as session:
            await svc._collect_system_metrics(1.0, 1.0, session)  # must not raise
