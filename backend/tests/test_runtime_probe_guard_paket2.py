"""Paket 2 — Probe-Guard, Exklusiv-Bremse, Crash-Loop-Robustheit, Eviction per Name.

Live-Vorfall 15.08.26 (Runtime-Vereinheitlichung): mehrere Runtime-Zeilen teilen
sich :8000 auf der Spark. Der Watcher probte jede Zeile gegen denselben Port und
schrieb die Identität des gerade laufenden Motors in FREMDE Zeilen (ds4 und
omp-qwen trugen plötzlich das Qwen-Modell + 1M Kontext). Ausserdem belebte die
Auto-Recovery eine nicht-exklusive Leichen-Runtime wieder, während der exklusive
vLLM-Motor lief — zwei Motoren parallel, Box 10 Minuten eingefroren.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import sse as sse_mod
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_manager import _parse_eviction_discovery, _eviction_discovery_script
from app.services.runtime_watcher import RuntimeWatcher


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


def _ssh_host():
    return SimpleNamespace(kind="ssh", tailscale_host=None)


async def _mk_rt(session, **kw):
    defaults = dict(
        slug="rt", display_name="rt", runtime_type="vllm_docker",
        endpoint="http://spark:8000/v1", model_identifier="old-model", enabled=True,
    )
    defaults.update(kw)
    rt = Runtime(**defaults)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


# ── 1) Probe-Guard: Anker nicht laufend → Identität wird NIE überschrieben ──

@pytest.mark.asyncio
async def test_drift_blocked_when_own_container_not_running(async_session, fake_redis):
    """ds4/qwen-Szenario: Zeile hat einen eigenen Container, der NICHT läuft —
    die Antwort auf dem geteilten Port gehört einem fremden Motor und darf
    weder Modell noch Kontextfenster dieser Zeile beschreiben."""
    rt = await _mk_rt(
        async_session, slug="anchored-down", container_name="my-engine",
        max_context_len=262144, preferred_context_len=262144,
    )
    watcher = RuntimeWatcher(interval=90)

    ssh = AsyncMock(return_value=("exited", "", 0))  # inspect: container exited
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("foreign-model", 1_000_000)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.runtime_watcher.resolve_host_for_runtime",
        new=AsyncMock(return_value=_ssh_host()),
    ), patch(
        "app.services.runtime_manager._ssh_run", new=ssh,
    ):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)   # zweite Probe würde sonst bestätigen

    await async_session.refresh(rt)
    assert rt.model_identifier == "old-model"
    assert rt.max_context_len == 262144
    assert rt.preferred_context_len == 262144


@pytest.mark.asyncio
async def test_drift_allowed_when_own_container_running(async_session, fake_redis):
    """Läuft der eigene Container, ist die Antwort seine — Drift greift normal."""
    rt = await _mk_rt(async_session, slug="anchored-up", container_name="my-engine")
    watcher = RuntimeWatcher(interval=90)

    ssh = AsyncMock(return_value=("running", "", 0))
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("new-model", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.runtime_watcher.resolve_host_for_runtime",
        new=AsyncMock(return_value=_ssh_host()),
    ), patch(
        "app.services.runtime_manager._ssh_run", new=ssh,
    ), patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.model_identifier == "new-model"


@pytest.mark.asyncio
async def test_drift_blocked_for_ssh_process_when_process_absent(async_session, fake_redis):
    """ds4 (ssh_process): pgrep findet den Prozess nicht → fremde Antwort,
    kein Write."""
    rt = await _mk_rt(
        async_session, slug="ds4-like", runtime_type="ssh_process",
        process_name="ds4-server",
    )
    watcher = RuntimeWatcher(interval=90)

    ssh = AsyncMock(return_value=("", "", 1))  # pgrep: kein Prozess
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("foreign-model", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.runtime_watcher.resolve_host_for_runtime",
        new=AsyncMock(return_value=_ssh_host()),
    ), patch(
        "app.services.runtime_manager._ssh_run", new=ssh,
    ):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.model_identifier == "old-model"


@pytest.mark.asyncio
async def test_drift_still_follows_for_pointer_rows_without_anchor(async_session, fake_redis):
    """omp-qwen-Semantik («switchbar»): Zeilen OHNE eigenen Container/Prozess
    zeigen bewusst auf «was auch immer gerade serviert» — sie folgen weiter."""
    rt = await _mk_rt(async_session, slug="pointer-row", runtime_type="omp")
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("new-model", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.model_identifier == "new-model"


@pytest.mark.asyncio
async def test_drift_blocked_when_anchor_state_unknown(async_session, fake_redis):
    """SSH wirft (Box unter Last, wie beim Freeze am 15.08.): Zustand des
    eigenen Ankers unbekannt → konservativ NICHT schreiben."""
    rt = await _mk_rt(async_session, slug="anchored-unknown", container_name="my-engine")
    watcher = RuntimeWatcher(interval=90)

    ssh = AsyncMock(side_effect=OSError("connection reset"))
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("foreign-model", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis),
    ), patch.object(
        sse_mod, "get_redis", _fake_get_redis(fake_redis),
    ), patch(
        "app.services.runtime_watcher.resolve_host_for_runtime",
        new=AsyncMock(return_value=_ssh_host()),
    ), patch(
        "app.services.runtime_manager._ssh_run", new=ssh,
    ):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.model_identifier == "old-model"


# ── 2) Crash-Loop-Check: SSH-Ausfall darf keine Exception werfen ──

@pytest.mark.asyncio
async def test_crash_loop_check_survives_ssh_failure(async_session, fake_redis):
    rt = await _mk_rt(async_session, slug="crash-ssh-down", container_name="c1")
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.resolve_host_for_runtime",
        new=AsyncMock(return_value=_ssh_host()),
    ), patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(side_effect=OSError("banner exchange timeout")),
    ):
        acted = await watcher._check_crash_loop(async_session, fake_redis, rt)

    assert acted is False  # «nicht feststellbar» ist kein Crash-Loop-Fund


# ── 3) Auto-Recovery: exklusiver Motor hält die Box → NICHTS wiederbeleben ──

@pytest.mark.asyncio
async def test_auto_recovery_skips_non_exclusive_when_exclusive_sibling_active(
    async_session, fake_redis,
):
    """Der 15.08.-Vorfall: qwen-general (exclusive_memory=False) wurde
    wiederbelebt, während der exklusive vLLM-Motor dieselbe Box hielt.
    Ein aktiver exklusiver Sibling muss JEDE Auto-Recovery auf der Box
    blockieren — auch die einer nicht-exklusiven Zeile."""
    exclusive = await _mk_rt(
        async_session, slug="excl-up", container_name="engine-a",
        exclusive_memory=True,
    )
    parked = await _mk_rt(
        async_session, slug="parked-lich", container_name="engine-b",
        exclusive_memory=False,
    )
    await fake_redis.set(
        RedisKeys.runtime_live(exclusive.slug), json.dumps({"reachable": True})
    )

    watcher = RuntimeWatcher(interval=90)
    start_mock = AsyncMock(return_value={"ok": True, "message": "started"})

    with patch(
        "app.services.runtime_watcher.settings"
    ) as mock_settings, patch(
        "app.services.runtime_watcher.resolve_host_for_runtime",
        new=AsyncMock(return_value=_ssh_host()),
    ), patch(
        "app.services.runtime_manager.start_runtime", new=start_mock,
    ), patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=("", "", 0)),
    ):
        mock_settings.runtime_auto_recovery_enabled = True
        await watcher._maybe_auto_recover(
            async_session, fake_redis, parked, fails=3,
        )

    start_mock.assert_not_awaited()


# ── 4) Eviction: vierter Matcher — exakter Containername der Runtime-Zeile ──

def test_eviction_discovery_script_includes_name_section():
    script = _eviction_discovery_script("some-slug", container_name="my-engine")
    assert "__NAME__" in script

    script_without = _eviction_discovery_script("some-slug", container_name=None)
    assert "__NAME__" in script_without  # Sektion immer da, ggf. leer geparst


def test_eviction_parse_matches_exact_container_name():
    out = (
        "__TOTAL__\n3\n"
        "__LABEL__\n"
        "__SOLO__\n"
        "__MANUAL__\n"
        "__PROJECT__\n"
        "__NAME__\n"
        "aaa111|my-engine\n"
        "bbb222|my-engine-2\n"          # kein exakter Treffer
        "ccc333|open-webui\n"
    )
    found = _parse_eviction_discovery(out, container_name="my-engine")
    assert found["name"] == ["aaa111"]
    assert "aaa111" in found["all"]
    assert "bbb222" not in found["all"]

    found_none = _parse_eviction_discovery(out, container_name=None)
    assert found_none["name"] == []


# ── 5) Start-Verifikation: dreistufig statt vorschnell «tot» ──

@pytest.mark.asyncio
async def test_verify_vllm_process_unknown_on_ssh_errors():
    """Box unter Last, docker top nicht lesbar → «unknown», nicht «absent»."""
    from app.services import runtime_manager as rm

    with patch.object(rm, "_verify_poll_interval", 0), patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(side_effect=OSError("ssh stalled")),
    ):
        state = await rm.verify_spark_vllm_process_started(
            "some-slug", host=None, timeout=0.05,
        )
    assert state == "unknown"


@pytest.mark.asyncio
async def test_verify_vllm_process_absent_on_clean_reads():
    """Container läuft, docker top liest sauber, kein vllm-Prozess im ganzen
    Fenster → bestätigt «absent» (ADR-059 bleibt scharf)."""
    from app.services import runtime_manager as rm

    with patch.object(rm, "_verify_poll_interval", 0), patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=("abc123", "", 0)),
    ), patch(
        "app.services.runtime_manager._container_runs_vllm_server",
        new=AsyncMock(return_value=(False, None)),
    ):
        state = await rm.verify_spark_vllm_process_started(
            "some-slug", host=None, timeout=0.05,
        )
    assert state == "absent"


@pytest.mark.asyncio
async def test_verify_vllm_process_serving():
    from app.services import runtime_manager as rm

    with patch.object(rm, "_verify_poll_interval", 0), patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=("abc123", "", 0)),
    ), patch(
        "app.services.runtime_manager._container_runs_vllm_server",
        new=AsyncMock(return_value=(True, None)),
    ):
        state = await rm.verify_spark_vllm_process_started(
            "some-slug", host=None, timeout=0.05,
        )
    assert state == "serving"
