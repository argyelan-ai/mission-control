"""Tests for the launch_command-aware path in runtime_manager.start_runtime().

Regression guard for the 2026-05-15 incident where a sparkrun-launched
vllm container was auto-removed (--rm default) after `docker stop`, and
all subsequent /runtimes/{id}/start clicks 404'd because docker_start
couldn't find a container that didn't exist anymore.

Coverage:
- Path A: container exists → docker start
- Path B: container missing + launch_command set → SSH execute via nohup
- Path B: missing container_name (recipe-only runtime) → still uses launch_command
- Path C: container missing + no launch_command → user-friendly 400-style error
- launch_command shell-injection prevention (shlex_quote)
- Failure surface bubbles up with stderr
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services import runtime_manager


def _runtime(**overrides) -> dict:
    base = {
        "id": "qwen-general",
        "slug": "qwen-general",
        "display_name": "Qwen 3.6 35B A3B FP8",
        "runtime_type": "vllm_docker",
        "container_name": "sparkrun_abc123_solo",
        "launch_command": None,
        "endpoint": "http://192.0.2.10:8000/v1",
    }
    base.update(overrides)
    return base


# ── Path A: container exists → docker start ───────────────────────────────


@pytest.mark.asyncio
async def test_path_a_existing_container_uses_docker_start():
    """When `docker inspect` succeeds, `docker start <name>` is the only call —
    launch_command (if any) is NOT executed."""
    rt = _runtime(launch_command="echo should-not-run")
    calls: list[str] = []

    async def fake_ssh(cmd: str, **kwargs):
        calls.append(cmd)
        if "docker inspect" in cmd:
            return ("running\n", "", 0)
        if cmd.startswith("docker start"):
            return ("", "", 0)
        raise AssertionError(f"unexpected SSH command: {cmd}")

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is True
    assert "sparkrun_abc123_solo" in result["message"]
    assert any("docker inspect" in c for c in calls)
    assert any(c.startswith("docker start") for c in calls)
    # launch_command must NOT have been executed
    assert not any("echo should-not-run" in c for c in calls)
    assert not any("nohup" in c for c in calls)


@pytest.mark.asyncio
async def test_path_a_docker_start_failure_surfaces_stderr():
    rt = _runtime()

    async def fake_ssh(cmd: str, **kwargs):
        if "docker inspect" in cmd:
            return ("exited\n", "", 0)
        if cmd.startswith("docker start"):
            return ("", "Error response from daemon: ...", 1)
        raise AssertionError(cmd)

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is False
    assert "Error response from daemon" in result["message"]


# ── Path B: container missing + launch_command → SSH execute ──────────────


@pytest.mark.asyncio
async def test_path_b_missing_container_runs_launch_command():
    """`docker inspect` 404s → falls through to launch_command via SSH."""
    rt = _runtime(
        launch_command=(
            "uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm "
            "--solo --no-rm --ensure --no-follow "
            "--label mc.runtime.slug=qwen-general"
        ),
    )
    calls: list[str] = []

    async def fake_ssh(cmd: str, **kwargs):
        calls.append(cmd)
        if "docker inspect" in cmd:
            # inspect failed — container does not exist
            return ("", "Error: No such object: sparkrun_abc123_solo", 1)
        if "nohup" in cmd:
            return ("", "", 0)
        if "docker top" in cmd:
            # ADR-059 process-liveness check: a real vllm serve process is
            # running inside the labelled container.
            return ("root 1 vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --port 8000", "", 0)
        if "docker ps" in cmd:
            # P2 start-verification poll: labelled container appeared.
            return ("sparkrun_new_solo", "", 0)
        raise AssertionError(cmd)

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is True, result
    # The detached invocation must mention the launch_command and a log file.
    assert any("nohup" in c for c in calls)
    assert any("sparkrun run" in c for c in calls)
    assert any("runtime-launch-qwen-general.log" in c for c in calls)


@pytest.mark.asyncio
async def test_path_b_works_without_container_name():
    """Recipe-only runtime (no container_name yet) still launches via launch_command."""
    rt = _runtime(container_name=None, launch_command="echo go")

    async def fake_ssh(cmd: str, **kwargs):
        # docker inspect path should be skipped entirely
        if "docker inspect" in cmd:
            raise AssertionError("docker inspect must not run when container_name is empty")
        if "nohup" in cmd:
            return ("", "", 0)
        if "docker top" in cmd:
            # ADR-059 process-liveness check: a real vllm serve process is
            # running inside the labelled container.
            return ("root 1 vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --port 8000", "", 0)
        if "docker ps" in cmd:
            # P2 start-verification poll: labelled container appeared.
            return ("sparkrun_new_solo", "", 0)
        raise AssertionError(cmd)

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_path_b_launch_command_failure_surfaces_stderr():
    rt = _runtime(container_name=None, launch_command="false")

    async def fake_ssh(cmd: str, **kwargs):
        if "nohup" in cmd:
            return ("", "permission denied", 1)
        raise AssertionError(cmd)

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is False
    assert "permission denied" in result["message"]


@pytest.mark.asyncio
async def test_path_b_quotes_launch_command_against_shell_injection():
    """Even if a user types a launch_command with a stray `;` or `$(...)`,
    shlex_quote must wrap it so the SSH host shell treats it as a single arg
    to `bash -lc`."""
    nasty = "echo hello; rm -rf /tmp/should-not-happen"
    rt = _runtime(container_name=None, launch_command=nasty)
    captured = []

    async def fake_ssh(cmd: str, **kwargs):
        captured.append(cmd)
        if "nohup" in cmd:
            return ("", "", 0)
        if "docker top" in cmd:
            return ("root 1 vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --port 8000", "", 0)
        if "docker ps" in cmd:
            # P2 start-verification poll: pretend the container appeared.
            return ("sparkrun_new_solo", "", 0)
        raise AssertionError(cmd)

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        await runtime_manager.start_runtime(rt)

    # The whole nasty string must appear inside single quotes — never as bare tokens.
    full_cmd = " ".join(captured)
    assert "'echo hello; rm -rf /tmp/should-not-happen'" in full_cmd, full_cmd


# ── Path C: nothing to start ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_c_missing_container_and_no_launch_command_returns_error():
    rt = _runtime(launch_command=None)

    async def fake_ssh(cmd: str, **kwargs):
        if "docker inspect" in cmd:
            return ("", "No such object", 1)
        raise AssertionError(cmd)

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is False
    assert "keine launch_command" in result["message"]


@pytest.mark.asyncio
async def test_path_c_no_container_name_and_no_launch_command():
    rt = _runtime(container_name=None, launch_command=None)

    async def fake_ssh(cmd: str, **kwargs):
        raise AssertionError(f"no SSH call expected, got {cmd}")

    with patch.object(runtime_manager, "_ssh_run", new=AsyncMock(side_effect=fake_ssh)):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is False
    assert "keine launch_command" in result["message"]
