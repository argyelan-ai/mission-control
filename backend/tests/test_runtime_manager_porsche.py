"""Tests for the unsloth_porsche runtime_type branch in runtime_manager.

The PORSCHE box is a power-managed Windows host: it sleeps when idle, is woken
via Wake-on-LAN, and is controlled via a Flask :5555 server (PowerShell) instead
of the DGX SSH/tmux path. These tests pin the lifecycle + wake behaviour.
"""
import json
import pytest
from unittest.mock import AsyncMock, patch

from app.services import runtime_manager
from app.services.agent_runtime_switch import _PROBEABLE_RUNTIME_TYPES


PORSCHE_RT = {
    "id": "unsloth-porsche",
    "slug": "unsloth-porsche",
    "display_name": "Unsloth (PORSCHE)",
    "runtime_type": "unsloth_porsche",
    "endpoint": "http://192.0.2.100:8000/v1",
    "healthcheck_path": "/v1/models",
    "control_url": "http://192.0.2.100:5555",
    "wol_mac_address": "00:11:22:33:44:55",
    "host": "192.0.2.100",
    "power_managed": True,
    "launch_command": "Start-Process unsloth-server",
}


# ── get_runtime_state ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_asleep_when_control_unreachable():
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=False)):
        state = await runtime_manager.get_runtime_state(PORSCHE_RT)
    assert state["state"] == "stopped"
    assert state["container_status"] == "asleep"
    assert state["http_reachable"] is False


@pytest.mark.asyncio
async def test_state_booted_no_model_when_awake_but_no_http():
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=False)):
        state = await runtime_manager.get_runtime_state(PORSCHE_RT)
    # Box awake but server not serving → stopped so the UI shows the Start button.
    assert state["state"] == "stopped"
    assert state["container_status"] == "booted_no_model"


@pytest.mark.asyncio
async def test_state_probe_avoids_double_v1():
    """Regression (review finding A): endpoint '.../v1' + healthcheck '/v1/models'
    must NOT probe '.../v1/v1/models'. The branch strips the redundant /v1."""
    rt = {**PORSCHE_RT, "endpoint": "http://192.0.2.100:8000/v1", "healthcheck_path": "/v1/models"}
    probe = AsyncMock(return_value=True)
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_probe_http", new=probe):
        state = await runtime_manager.get_runtime_state(rt)
    # normalized: probed path is /models (→ final URL .../v1/models), not /v1/models
    assert probe.await_args.args == ("http://192.0.2.100:8000/v1", "/models")
    assert state["state"] == "ready"


@pytest.mark.asyncio
async def test_state_serving_when_awake_and_http():
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=True)):
        state = await runtime_manager.get_runtime_state(PORSCHE_RT)
    assert state["state"] == "ready"
    assert state["container_status"] == "serving"
    assert state["http_reachable"] is True


# ── start_runtime ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_blocks_when_launch_command_placeholder():
    rt = {**PORSCHE_RT, "launch_command": "TODO: fill in"}
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=True)):
        result = await runtime_manager.start_runtime(rt)
    assert result["ok"] is False
    assert "launch_command" in result["message"]


@pytest.mark.asyncio
async def test_start_blocks_when_box_asleep():
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=False)):
        result = await runtime_manager.start_runtime(PORSCHE_RT)
    assert result["ok"] is False
    assert "wecken" in result["message"].lower()


@pytest.mark.asyncio
async def test_start_ok_runs_powershell():
    ps = AsyncMock(return_value=("started", "", 0))
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_porsche_powershell", new=ps):
        result = await runtime_manager.start_runtime(PORSCHE_RT)
    assert result["ok"] is True
    ps.assert_awaited_once()
    # the configured launch_command is what gets executed
    assert ps.await_args.args[1] == PORSCHE_RT["launch_command"]


# ── stop_runtime ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_when_asleep_is_idempotent_ok():
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=False)):
        result = await runtime_manager.stop_runtime(PORSCHE_RT)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_stop_without_port_fails_loudly():
    """Regression (review finding D): a port-less endpoint must NOT report a false
    'VRAM freed' success — it returns ok=False instead of running a no-op."""
    rt = {**PORSCHE_RT, "endpoint": "https://porsche/v1"}  # no :port
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_porsche_powershell", new=AsyncMock(side_effect=AssertionError("must not run a no-op stop"))):
        result = await runtime_manager.stop_runtime(rt)
    assert result["ok"] is False
    assert "Port" in result["message"]


@pytest.mark.asyncio
async def test_stop_kills_process_on_port():
    ps = AsyncMock(return_value=("stopped", "", 0))
    with patch.object(runtime_manager, "_porsche_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_porsche_powershell", new=ps):
        result = await runtime_manager.stop_runtime(PORSCHE_RT)
    assert result["ok"] is True
    # the stop command targets the OpenAI port from the endpoint (8000)
    assert "8000" in ps.await_args.args[1]


# ── restart_runtime ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restart_delegates_stop_then_start():
    with patch.object(runtime_manager, "stop_runtime", new=AsyncMock(return_value={"ok": True, "message": "s"})) as stop_mock, \
         patch.object(runtime_manager, "start_runtime", new=AsyncMock(return_value={"ok": True, "message": "g"})) as start_mock:
        result = await runtime_manager.restart_runtime(PORSCHE_RT)
    assert result["ok"] is True
    stop_mock.assert_awaited_once()
    start_mock.assert_awaited_once()


# ── wake_runtime ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_rejects_non_power_managed():
    rt = {**PORSCHE_RT, "power_managed": False}
    result = await runtime_manager.wake_runtime(rt)
    assert result["ok"] is False
    assert "power_managed" in result["message"]


@pytest.mark.asyncio
async def test_wake_writes_trigger_file(tmp_path):
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_runtime(PORSCHE_RT)
    assert result["ok"] is True
    trigger = tmp_path / "unsloth-porsche.request.json"
    assert trigger.exists()
    payload = json.loads(trigger.read_text())
    assert payload["mac"] == "00:11:22:33:44:55"
    assert payload["slug"] == "unsloth-porsche"
    assert "requested_at" in payload


@pytest.mark.asyncio
async def test_wake_fails_without_mac():
    rt = {k: v for k, v in PORSCHE_RT.items() if k != "wol_mac_address"}
    # ensure the settings fallback is also empty so the guard triggers
    with patch.object(runtime_manager.settings, "porsche_mac", ""):
        result = await runtime_manager.wake_runtime(rt)
    assert result["ok"] is False


# ── probe-model wiring ───────────────────────────────────────────────────────

def test_unsloth_porsche_is_probeable():
    assert "unsloth_porsche" in _PROBEABLE_RUNTIME_TYPES
