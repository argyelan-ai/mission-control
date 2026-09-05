"""Live-Befund 05.09.2026 — der Drift-Waechter erkannte den Anker einer
``ssh_process``-Runtime nicht, wenn dahinter ein Docker-Container steckt.

Seit #396 darf ``process_name`` auch ein CONTAINERNAME sein: viele
Rezept-Startskripte starten in Wahrheit einen Container und legen sich
schlafen. Der Waechter fragte aber nur ``pgrep -x`` — fuer einen Container
blind. Folge: die Antwort der eigenen Engine galt als „fremd", ``model_identifier``
blieb auf dem HuggingFace-Pfad statt dem Servier-Namen stehen, und ein Agent
bekam darauf 404.

``runtime_manager.anchor_running`` kennt beide Faelle bereits (pgrep ODER
docker inspect). Der Waechter benutzt jetzt dieselbe Regel.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.runtime import Runtime
from app.services import sse as sse_mod
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_watcher import RuntimeWatcher


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


def _ssh_host():
    return SimpleNamespace(kind="ssh", ssh_host="192.0.2.10", tailscale_host=None)


def _shell_like_ssh(calls: list[str]):
    """Ein Fake, der sich wie die Box verhaelt: KEIN Prozess dieses Namens,
    aber ein LAUFENDER Container dieses Namens."""
    async def _run(cmd, *a, **kw):
        calls.append(cmd)
        if "docker inspect" in cmd:
            # pgrep faellt durch, der docker-Teil des Befehls trifft.
            return ("", "", 0)
        return ("", "", 1)
    return AsyncMock(side_effect=_run)


async def _mk_rt(session, **kw):
    defaults = dict(
        slug="recipe-x", display_name="recipe-x", runtime_type="ssh_process",
        endpoint="http://box-a:8000/v1", model_identifier="/models/hf/path",
        enabled=True,
    )
    defaults.update(kw)
    rt = Runtime(**defaults)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _tick_twice(watcher, session, fake_redis, served, ssh):
    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel(served, None)),
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
        await watcher.tick(session=session)
        await watcher.tick(session=session)


@pytest.mark.asyncio
async def test_ssh_process_anchor_accepts_container_handle(async_session, fake_redis):
    """``process_name`` ist ein Containername → der Anker laeuft, Drift greift."""
    rt = await _mk_rt(async_session, slug="recipe-x-container", process_name="box-engine")
    calls: list[str] = []

    await _tick_twice(
        RuntimeWatcher(interval=90), async_session, fake_redis,
        "served-name", _shell_like_ssh(calls),
    )

    await async_session.refresh(rt)
    assert rt.model_identifier == "served-name"
    assert any("docker inspect" in c for c in calls), (
        "Der Anker-Check muss auch die Container-Art pruefen, nicht nur pgrep"
    )


@pytest.mark.asyncio
async def test_ssh_process_anchor_blocked_when_neither_process_nor_container(
    async_session, fake_redis,
):
    """Gegenprobe: weder Prozess noch Container → fremde Antwort, kein Write."""
    rt = await _mk_rt(async_session, slug="recipe-x-down", process_name="box-engine")
    ssh = AsyncMock(return_value=("", "", 1))

    await _tick_twice(
        RuntimeWatcher(interval=90), async_session, fake_redis, "foreign-model", ssh,
    )

    await async_session.refresh(rt)
    assert rt.model_identifier == "/models/hf/path"


@pytest.mark.asyncio
async def test_ssh_process_without_handle_stays_not_own(async_session, fake_redis):
    """Unveraendert (#415): ohne Handle gibt es keinen Beleg — kein Write."""
    rt = await _mk_rt(async_session, slug="recipe-x-nohandle")
    ssh = AsyncMock(return_value=("", "", 0))

    await _tick_twice(
        RuntimeWatcher(interval=90), async_session, fake_redis, "foreign-model", ssh,
    )

    await async_session.refresh(rt)
    assert rt.model_identifier == "/models/hf/path"


@pytest.mark.asyncio
async def test_docker_engine_anchor_behaviour_unchanged(async_session, fake_redis):
    """Docker-Engines pruefen weiter ihren Containernamen — laeuft er nicht,
    bleibt die Identitaet stehen."""
    rt = await _mk_rt(
        async_session, slug="docker-down", runtime_type="vllm_docker",
        container_name="box-engine", process_name=None,
    )
    ssh = AsyncMock(return_value=("exited", "", 1))

    await _tick_twice(
        RuntimeWatcher(interval=90), async_session, fake_redis, "foreign-model", ssh,
    )

    await async_session.refresh(rt)
    assert rt.model_identifier == "/models/hf/path"
