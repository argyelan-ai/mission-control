"""Tests that restart_runtime for vllm_docker never calls `docker restart None`.

Live incident 2026-06-27: POST /runtimes/{id}/restart on a vllm_docker runtime
whose container_name was None (cleared after a recipe-switch) executed
`docker restart None` → SSH returned exit 1 / 400 Bad Request.

Fix: when container_name is unset, discover the actual running Spark container
via the label+sweep helper (_running_solo_containers) used by eviction, then
restart the discovered id. If nothing is found (or multiple), return ok=False
with a meaningful message and the launch-log path — never attempt
`docker restart None`.

All SSH is mocked — nothing touches the real Spark.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import runtime_manager


# A vllm_docker runtime whose container_name was cleared after the last switch.
# This is the exact state that triggered the `docker restart None` bug.
_SPARK_RT_NO_NAME = {
    "id": "qwen-general",
    "slug": "qwen-general",
    "display_name": "Spark Qwen vLLM",
    "runtime_type": "vllm_docker",
    "endpoint": "http://192.0.2.10:8000/v1",
    "container_name": None,
}

# Same runtime but with container_name set (happy-path regression guard).
_SPARK_RT_WITH_NAME = {
    **_SPARK_RT_NO_NAME,
    "container_name": "sparkrun_abc123_solo",
}


# ── container_name=None: no `docker restart None` ────────────────────────────


@pytest.mark.asyncio
async def test_restart_none_container_name_discovers_and_restarts():
    """When container_name is None, discover via sweep and restart the found id."""
    # _running_solo_containers returns one container → SSH is called with that id.
    ssh = AsyncMock(side_effect=[
        # _running_solo_query (docker ps call inside _running_solo_containers)
        ("sparkrun_liveone_solo", "", 0),
        # docker restart <discovered-id>
        ("", "", 0),
    ])
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.restart_runtime(_SPARK_RT_NO_NAME)

    assert result["ok"] is True
    assert "sparkrun_liveone_solo" in result["message"]

    # Verify `docker restart None` was never sent. Only the command string
    # counts — since ADR-048 every call carries a host= kwarg whose repr may
    # legitimately contain "None" (settings-fallback).
    all_cmds = [c.args[0] for c in ssh.call_args_list]
    assert not any("None" in cmd for cmd in all_cmds), (
        f"`docker restart None` was issued: {all_cmds}"
    )
    # The restart command must reference the discovered container id.
    restart_call = ssh.call_args_list[-1]
    assert "sparkrun_liveone_solo" in restart_call.args[0]
    assert "restart" in restart_call.args[0]


@pytest.mark.asyncio
async def test_restart_none_container_name_no_container_found():
    """When container_name is None and no container is running, return ok=False
    with the launch-log path — do NOT call `docker restart`."""
    # Sweep returns nothing.
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.restart_runtime(_SPARK_RT_NO_NAME)

    assert result["ok"] is False
    assert "qwen-general" in result["message"]  # slug in message
    assert "runtime-launch-qwen-general.log" in result["message"]  # log path

    # docker restart must never be issued.
    all_cmds = [str(c) for c in ssh.call_args_list]
    assert not any("docker restart" in cmd for cmd in all_cmds), (
        f"Unexpected docker restart call: {all_cmds}"
    )


@pytest.mark.asyncio
async def test_restart_none_container_name_multiple_found():
    """When container_name is None and multiple containers are running, return
    ok=False (ambiguous — refuse to guess which to restart)."""
    ssh = AsyncMock(return_value=("id_a\nid_b", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.restart_runtime(_SPARK_RT_NO_NAME)

    assert result["ok"] is False
    assert "Mehrdeutig" in result["message"] or "2" in result["message"]

    all_cmds = [str(c) for c in ssh.call_args_list]
    assert not any("docker restart" in cmd for cmd in all_cmds)


@pytest.mark.asyncio
async def test_restart_none_container_name_discovery_error():
    """SSH failure during discovery must propagate as ok=False with the log path."""
    ssh = AsyncMock(side_effect=OSError("connection refused"))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.restart_runtime(_SPARK_RT_NO_NAME)

    assert result["ok"] is False
    assert "runtime-launch-qwen-general.log" in result["message"]


# ── container_name set: happy path regression guard ──────────────────────────


@pytest.mark.asyncio
async def test_restart_with_container_name_calls_docker_restart_directly():
    """When container_name IS set, restart goes directly to docker restart <name>
    without a discovery sweep — exactly as before the fix."""
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.restart_runtime(_SPARK_RT_WITH_NAME)

    assert result["ok"] is True
    assert ssh.await_count == 1
    cmd = ssh.call_args.args[0]
    assert cmd == "docker restart sparkrun_abc123_solo"
