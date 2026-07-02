"""Tests for the Unsloth runtime_type branch in runtime_manager."""
import pytest
from unittest.mock import AsyncMock, patch

from app.services import runtime_manager


UNSLOTH_RT = {
    "id": "unsloth-studio",
    "display_name": "Unsloth Studio",
    "runtime_type": "unsloth",
    "endpoint": "http://192.0.2.10:8888",
    "healthcheck_path": "/api/health",
    "tmux_session": "unsloth-studio",
}


@pytest.mark.asyncio
async def test_unsloth_state_stopped_when_no_tmux():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 1))):
        state = await runtime_manager.get_runtime_state(UNSLOTH_RT)
    assert state["state"] == "stopped"
    assert state["container_status"] == "no_session"


@pytest.mark.asyncio
async def test_unsloth_state_warming_when_tmux_but_no_http():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=False)):
        state = await runtime_manager.get_runtime_state(UNSLOTH_RT)
    assert state["state"] == "warming"
    assert state["container_status"] == "tmux_running"


@pytest.mark.asyncio
async def test_unsloth_state_ready_when_tmux_and_http():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=True)):
        state = await runtime_manager.get_runtime_state(UNSLOTH_RT)
    assert state["state"] == "ready"
    assert state["http_reachable"] is True


@pytest.mark.asyncio
async def test_unsloth_start_ok():
    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(return_value=("", "", 0))):
        result = await runtime_manager.start_runtime(UNSLOTH_RT)
    assert result["ok"] is True
    assert "Unsloth Studio" in result["message"]


@pytest.mark.asyncio
async def test_unsloth_stop_handles_missing_session():
    mock_ssh = AsyncMock(return_value=("", "can't find session: unsloth-studio", 1))
    with patch.object(runtime_manager, "_ssh_run", new=mock_ssh):
        result = await runtime_manager.stop_runtime(UNSLOTH_RT)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_unsloth_restart_calls_stop_then_start():
    """Ensure restart delegates to stop + start for the unsloth branch."""
    with patch.object(
        runtime_manager, "stop_runtime", new=AsyncMock(return_value={"ok": True, "message": "stopped"})
    ) as stop_mock, patch.object(
        runtime_manager, "start_runtime", new=AsyncMock(return_value={"ok": True, "message": "started"})
    ) as start_mock:
        result = await runtime_manager.restart_runtime(UNSLOTH_RT)
    assert result["ok"] is True
    stop_mock.assert_awaited_once()
    start_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsloth_restart_aborts_on_stop_failure():
    stop_result = {"ok": False, "message": "stop failed"}
    with patch.object(runtime_manager, "stop_runtime", new=AsyncMock(return_value=stop_result)):
        result = await runtime_manager.restart_runtime(UNSLOTH_RT)
    assert result == stop_result


@pytest.mark.asyncio
async def test_openai_compatible_probe_only():
    rt = {
        "id": "external-openai",
        "display_name": "External",
        "runtime_type": "openai_compatible",
        "endpoint": "https://api.example.com",
        "healthcheck_path": "/v1/models",
    }
    with patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=True)):
        state = await runtime_manager.get_runtime_state(rt)
    assert state["state"] == "ready"
    assert state["http_reachable"] is True
