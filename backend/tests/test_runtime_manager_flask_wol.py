"""Tests for the flask_wol runtime_type branch in runtime_manager.

A flask_wol box is a power-managed Windows host: it sleeps when idle, is woken
via Wake-on-LAN, and is controlled via a Flask :5555 server (PowerShell) instead
of the DGX SSH/tmux path. These tests pin the lifecycle + wake behaviour.
"""
import json
import pytest
from unittest.mock import AsyncMock, patch

from app.services import runtime_manager
from app.services.agent_runtime_switch import _PROBEABLE_RUNTIME_TYPES


WOL_RT = {
    "id": "wake-box-runtime",
    "slug": "wake-box-runtime",
    "display_name": "Unsloth (wake box)",
    "runtime_type": "flask_wol",
    "endpoint": "http://192.0.2.20:8000/v1",
    "healthcheck_path": "/v1/models",
    "control_url": "http://192.0.2.20:5555",
    "wol_mac_address": "00:11:22:33:44:55",
    "host": "192.0.2.20",
    "power_managed": True,
    "launch_command": "Start-Process unsloth-server",
}


# ── get_runtime_state ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_asleep_when_control_unreachable():
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=False)):
        state = await runtime_manager.get_runtime_state(WOL_RT)
    assert state["state"] == "stopped"
    assert state["container_status"] == "asleep"
    assert state["http_reachable"] is False


@pytest.mark.asyncio
async def test_state_booted_no_model_when_awake_but_no_http():
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=False)):
        state = await runtime_manager.get_runtime_state(WOL_RT)
    # Box awake but server not serving → stopped so the UI shows the Start button.
    assert state["state"] == "stopped"
    assert state["container_status"] == "booted_no_model"


@pytest.mark.asyncio
async def test_state_probe_avoids_double_v1():
    """Regression (review finding A): endpoint '.../v1' + healthcheck '/v1/models'
    must NOT probe '.../v1/v1/models'.

    Asserted on the URL that actually goes out, not on the arguments handed to
    ``_probe_http``. The normalization moved into ``join_probe_url`` so every
    runtime type benefits (the earlier per-branch guard left the others broken);
    an argument-level assertion would pin the old layering and pass even if the
    resulting URL were wrong.
    """
    rt = {**WOL_RT, "endpoint": "http://192.0.2.20:8000/v1", "healthcheck_path": "/v1/models"}
    assert runtime_manager.join_probe_url(rt["endpoint"], rt["healthcheck_path"]) == (
        "http://192.0.2.20:8000/v1/models"
    )
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=True)):
        state = await runtime_manager.get_runtime_state(rt)
    assert state["state"] == "ready"


@pytest.mark.asyncio
async def test_state_serving_when_awake_and_http():
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_probe_http", new=AsyncMock(return_value=True)):
        state = await runtime_manager.get_runtime_state(WOL_RT)
    assert state["state"] == "ready"
    assert state["container_status"] == "serving"
    assert state["http_reachable"] is True


# ── start_runtime ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_blocks_when_launch_command_placeholder():
    rt = {**WOL_RT, "launch_command": "TODO: fill in"}
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=True)):
        result = await runtime_manager.start_runtime(rt)
    assert result["ok"] is False
    assert "launch_command" in result["message"]


@pytest.mark.asyncio
async def test_start_blocks_when_box_asleep():
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=False)):
        result = await runtime_manager.start_runtime(WOL_RT)
    assert result["ok"] is False
    assert "wecken" in result["message"].lower()


@pytest.mark.asyncio
async def test_start_ok_runs_powershell():
    ps = AsyncMock(return_value=("started", "", 0))
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_control_server_powershell", new=ps):
        result = await runtime_manager.start_runtime(WOL_RT)
    assert result["ok"] is True
    ps.assert_awaited_once()
    # the configured launch_command is what gets executed
    assert ps.await_args.args[1] == WOL_RT["launch_command"]


# ── stop_runtime ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_when_asleep_is_idempotent_ok():
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=False)):
        result = await runtime_manager.stop_runtime(WOL_RT)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_stop_without_port_fails_loudly():
    """Regression (review finding D): a port-less endpoint must NOT report a false
    'VRAM freed' success — it returns ok=False instead of running a no-op."""
    rt = {**WOL_RT, "endpoint": "https://wake-box/v1"}  # no :port
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_control_server_powershell", new=AsyncMock(side_effect=AssertionError("must not run a no-op stop"))):
        result = await runtime_manager.stop_runtime(rt)
    assert result["ok"] is False
    assert "Port" in result["message"]


@pytest.mark.asyncio
async def test_stop_kills_process_on_port():
    ps = AsyncMock(return_value=("stopped", "", 0))
    with patch.object(runtime_manager, "_control_server_reachable", new=AsyncMock(return_value=True)), \
         patch.object(runtime_manager, "_control_server_powershell", new=ps):
        result = await runtime_manager.stop_runtime(WOL_RT)
    assert result["ok"] is True
    # the stop command targets the OpenAI port from the endpoint (8000)
    assert "8000" in ps.await_args.args[1]


# ── restart_runtime ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restart_delegates_stop_then_start():
    with patch.object(runtime_manager, "stop_runtime", new=AsyncMock(return_value={"ok": True, "message": "s"})) as stop_mock, \
         patch.object(runtime_manager, "start_runtime", new=AsyncMock(return_value={"ok": True, "message": "g"})) as start_mock:
        result = await runtime_manager.restart_runtime(WOL_RT)
    assert result["ok"] is True
    stop_mock.assert_awaited_once()
    start_mock.assert_awaited_once()


# ── wake_runtime ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_rejects_non_power_managed():
    rt = {**WOL_RT, "power_managed": False}
    result = await runtime_manager.wake_runtime(rt)
    assert result["ok"] is False
    assert "power_managed" in result["message"]


@pytest.mark.asyncio
async def test_wake_writes_trigger_file(tmp_path):
    # The host-side watcher's heartbeat (round-7 L5): without it no wake is queued.
    (tmp_path / runtime_manager.WAKE_HELPER_HEARTBEAT_NAME).write_text("{}")
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_runtime(WOL_RT)
    assert result["ok"] is True
    trigger = tmp_path / "wake-box-runtime.request.json"
    assert trigger.exists()
    payload = json.loads(trigger.read_text())
    assert payload["mac"] == "00:11:22:33:44:55"
    assert payload["slug"] == "wake-box-runtime"
    assert "requested_at" in payload


@pytest.mark.asyncio
async def test_wake_fails_without_mac():
    rt = {k: v for k, v in WOL_RT.items() if k != "wol_mac_address"}
    # ensure the settings fallback is also empty so the guard triggers
    with patch.object(runtime_manager.settings, "wol_mac", ""):
        result = await runtime_manager.wake_runtime(rt)
    assert result["ok"] is False


# ── probe-model wiring ───────────────────────────────────────────────────────

def test_flask_wol_is_probeable():
    assert "flask_wol" in _PROBEABLE_RUNTIME_TYPES
