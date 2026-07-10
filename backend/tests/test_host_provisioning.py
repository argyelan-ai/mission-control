"""Host-agent file staging for the onboarding wizard (2026-07-10).

Renders plist + run.sh + agent.env into ~/.mc/agents/<slug>/ so an
operator can review and launchctl-load them. SAFETY: launchctl is never
run in tests, and autoload is gated behind a feature flag (default off).
"""
import os

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
import app.services.host_provisioning as hp


@pytest.fixture
def home_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_stage_writes_files(home_host, async_session, monkeypatch):
    async def _fake_env(runtime, session):
        return {"OPENAI_BASE_URL": "http://x/v1", "OPENAI_MODEL": "m"}

    monkeypatch.setattr(hp, "build_runtime_env", _fake_env)

    rt = Runtime(
        slug="host-rt", display_name="Host RT", runtime_type="lmstudio",
        endpoint="http://x/v1", model_identifier="m", enabled=True,
    )
    agent = Agent(name="Nova Host", emoji="🛰️", agent_runtime="host", harness="openclaude")

    result = await hp.stage_host_agent_files(agent, rt, "tok-abc", session=async_session)

    assert os.path.isfile(result.plist_staged_path)
    assert os.path.isfile(result.run_script_path)
    assert os.path.isfile(result.env_path)
    assert result.plist_label == "com.mc.agent.nova-host"
    # env file has 600 perms and holds the token
    assert (os.stat(result.env_path).st_mode & 0o777) == 0o600
    assert "tok-abc" in open(result.env_path).read()
    # plist references the staged run script and the label
    plist = open(result.plist_staged_path).read()
    assert "com.mc.agent.nova-host" in plist
    assert result.run_script_path in plist
    # launchctl command is offered but NOT executed
    assert "launchctl bootstrap" in result.launchctl_command


@pytest.mark.asyncio
async def test_maybe_load_disabled_does_not_run_launchctl(home_host, async_session, monkeypatch):
    async def _fake_env(runtime, session):
        return {}

    monkeypatch.setattr(hp, "build_runtime_env", _fake_env)
    monkeypatch.setattr(hp.settings, "host_agent_autoload_enabled", False)

    called = {"launchctl": False}

    def _boom(*a, **k):
        called["launchctl"] = True
        raise AssertionError("launchctl must not run when autoload is disabled")

    monkeypatch.setattr(hp.subprocess, "run", _boom)

    rt = Runtime(slug="r", display_name="R", runtime_type="lmstudio", endpoint="http://x/v1", model_identifier="m", enabled=True)
    agent = Agent(name="No Load", agent_runtime="host")
    result = await hp.stage_host_agent_files(agent, rt, "t", session=async_session)

    load = hp.maybe_load_plist(result)
    assert load["loaded"] is False
    assert called["launchctl"] is False


@pytest.mark.asyncio
async def test_maybe_load_enabled_invokes_launchctl(home_host, async_session, monkeypatch):
    async def _fake_env(runtime, session):
        return {}

    monkeypatch.setattr(hp, "build_runtime_env", _fake_env)
    monkeypatch.setattr(hp.settings, "host_agent_autoload_enabled", True)

    calls = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(hp.subprocess, "run", _fake_run)

    rt = Runtime(slug="r2", display_name="R2", runtime_type="lmstudio", endpoint="http://x/v1", model_identifier="m", enabled=True)
    agent = Agent(name="Do Load", agent_runtime="host")
    result = await hp.stage_host_agent_files(agent, rt, "t", session=async_session)

    load = hp.maybe_load_plist(result)
    assert load["loaded"] is True
    # a launchctl bootstrap command was invoked
    assert any("launchctl" in c[0] for c in calls)
