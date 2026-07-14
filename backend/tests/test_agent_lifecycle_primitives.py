import subprocess
import pytest
from unittest.mock import patch, MagicMock

from app.services import agent_bootstrap
from app.services import docker_agent_sync
from app.models.agent import Agent


def test_bootout_argv_local_when_launchctl_present():
    with patch.object(agent_bootstrap.shutil, "which", return_value="/bin/launchctl"), \
         patch.object(agent_bootstrap.os, "getuid", return_value=501):
        argv = agent_bootstrap._launchctl_bootout_argv("com.mc.agent.dev")
    assert argv == ["launchctl", "bootout", "gui/501/com.mc.agent.dev"]


def test_bootout_argv_ssh_when_launchctl_absent():
    with patch.object(agent_bootstrap.shutil, "which", return_value=None):
        argv = agent_bootstrap._launchctl_bootout_argv("com.mc.agent.dev")
    assert argv[0] == "ssh"
    assert "launchctl bootout gui/$(id -u)/com.mc.agent.dev" in argv[-1]


def test_run_bootout_tolerates_not_loaded():
    proc = MagicMock(returncode=3, stdout="", stderr="Boot-out failed: 3: No such process")
    with patch.object(agent_bootstrap.subprocess, "run", return_value=proc), \
         patch.object(agent_bootstrap, "_launchctl_bootout_argv", return_value=["launchctl", "bootout", "gui/501/com.mc.agent.dev"]):
        result = agent_bootstrap._run_launchctl_bootout("com.mc.agent.dev")
    assert result["already_gone"] is True


def test_run_bootout_success():
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch.object(agent_bootstrap.subprocess, "run", return_value=proc), \
         patch.object(agent_bootstrap, "_launchctl_bootout_argv", return_value=["launchctl", "bootout", "gui/501/com.mc.agent.dev"]):
        result = agent_bootstrap._run_launchctl_bootout("com.mc.agent.dev")
    assert result["unloaded"] is True


def test_stop_container_targets_mc_agent_prefix_only():
    agent = Agent(name="Dev", slug="dev", agent_runtime="cli-bridge")
    with patch.object(docker_agent_sync.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="dev", stderr="")
        docker_agent_sync.stop_docker_agent_container(agent)
    called_argv = run.call_args[0][0]
    assert called_argv[:2] == ["docker", "stop"]
    assert any(a == "mc-agent-dev" for a in called_argv)


def test_remove_container_uses_rm_force():
    agent = Agent(name="Dev", slug="dev", agent_runtime="cli-bridge")
    with patch.object(docker_agent_sync.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="dev", stderr="")
        docker_agent_sync.remove_docker_agent_container(agent)
    called_argv = run.call_args[0][0]
    assert called_argv[:3] == ["docker", "rm", "-f"]
    assert any(a == "mc-agent-dev" for a in called_argv)


def test_remove_container_accepts_raw_slug():
    with patch.object(docker_agent_sync.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="dev", stderr="")
        docker_agent_sync.remove_docker_agent_container("dev")  # raw slug string
    called_argv = run.call_args[0][0]
    assert any(a == "mc-agent-dev" for a in called_argv)
