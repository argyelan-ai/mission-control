"""B6 (Rex-Review PR #500): the lifespan-shutdown cancel for jarvis_briefing_task.

On main, cancelling ``app.state.jarvis_briefing_task`` was the FIRST shutdown
step after the yield. The lifespan split (4674502) lost the call — the task
was created in ``app.main.lifespan`` (main.py:207-212) but never cancelled.
Rex's AST-lifecycle diff (main-lifespan vs PR-lifespan plus all
``app.background`` entrypoints) confirmed this is the only actually-lost call.

The test drives the REAL lifespan shutdown path with everything before the
jarvis cancel mocked away, plants a real (hanging) background task at
``app.state.jarvis_briefing_task`` and asserts that shutdown cancels it.
"""
import asyncio
import contextlib
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import app.main as main
import app.background as bg_mod


_THE_17 = [
    "scheduler", "watchdog", "task_runner", "loop_runner", "group_runner",
    "intelligence", "file_indexer", "embedding_retry", "obsidian_export",
    "runtime_schedule_service", "runtime_watcher", "runtime_pulse",
    "cli_update_checker", "model_catalog_checker", "local_registry_checker",
    "telegram_bot", "slack_socket",
]


async def _hang() -> None:
    """Loop that behaves like jarvis_briefing_loop: sleeps until cancelled."""
    while True:
        await asyncio.sleep(3600)


async def _run_lifespan_shutdown(monkeypatch) -> asyncio.Task:
    """Run lifespan() startup + shutdown with external effects stubbed."""
    monkeypatch.setattr(main, "prepare_process", AsyncMock())
    monkeypatch.setattr("app.services.fs_roots.mc_home", lambda: Path("/tmp"))
    # W2 (Rex-Review PR #500): auf bg_mod patchen greift NICHT — app.main
    # haelt eigene Bindungen (from app.background import start_/stop_…,
    # main.py:116-127), und lifespan() loest die Namen in seinem eigenen
    # Modul-Namespace auf. Gemessen: 'start/stop_vault_services mock
    # called: False' bei bg_mod-Patch. Deshalb: auf app.main patchen.
    monkeypatch.setattr(main, "start_vault_services", AsyncMock(return_value={}))
    monkeypatch.setattr(main, "stop_vault_services", AsyncMock())

    # The 17 singleton services: not the subject, keep them inert on both sides.
    start_mocks = {name: AsyncMock() for name in _THE_17}
    stop_mocks = {name: AsyncMock() for name in _THE_17}

    # Plant a hanging task under the same key lifespan uses. We bypass the
    # real scheduling (feature-gated on JARVIS_BRIEFING_ENABLED + API key) —
    # what B6 protects is the CANCEL, and the cancel must not care whether
    # the feature was on when the task got created.
    task: asyncio.Task | None = None

    def _fake_create(coro, name=None):
        nonlocal task
        if name == "jarvis_briefing_loop":
            task = asyncio.create_task(_hang(), name=name)
            coro.close()  # never schedule the real loop coroutine
            return task
        return asyncio.create_task(coro, name=name)

    monkeypatch.setattr(main, "_create_background_task", _fake_create)

    with contextlib.ExitStack() as stack:
        for name, mock in start_mocks.items():
            stack.enter_context(patch.object(getattr(bg_mod, name), "start", mock))
        for name, mock in stop_mocks.items():
            stack.enter_context(patch.object(getattr(bg_mod, name), "stop", mock))
        async with main.lifespan(main.app):
            assert task is not None, "jarvis_briefing_loop task was not scheduled"
            assert not task.done()
        # lifespan exit == shutdown tail has fully run
    return task


@pytest.mark.asyncio
async def test_lifespan_shutdown_cancels_jarvis_briefing_task(monkeypatch):
    task = await _run_lifespan_shutdown(monkeypatch)
    # The cancel fired and was awaited: the task is done and CANCELLED.
    assert task.cancelled(), (
        "jarvis_briefing_task survived the lifespan shutdown — the B6 cancel "
        "is missing (regress out of 4674502)"
    )


def test_lifespan_source_cancels_jarvis_briefing_task():
    # Structural guard mirroring test_lifespan_wires_the_guard: the cancel
    # must live in app.main.lifespan (it is not delegated to app.background).
    src = inspect.getsource(main.lifespan)
    assert "jarvis_briefing_task" in src.split("yield")[1], (
        "lifespan shutdown tail no longer cancels jarvis_briefing_task"
    )
