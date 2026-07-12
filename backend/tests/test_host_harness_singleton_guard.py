"""Singleton host-bridge guard + launchctl-over-SSH (2026-07-12 incident).

Root cause: the hermes/grok host harnesses hardcode their config dir
(``~/.mc/agents/<slug>``) and their single plist to ONE slug. When a
wizard-created agent "Dev" picked harness=hermes, ``bootstrap_hermes_agent``
wrote a fresh token into the REAL Hermes's ``agent.env`` (clobbering a live
agent) and then crashed at ``launchctl`` — which does not exist inside the
Linux backend container.

Two fixes, verified here:
  1. Singleton guard — bootstrap_hermes_agent/bootstrap_grok_agent refuse any
     agent whose slug isn't the singleton slug, BEFORE writing any file. The
     provision router rejects it earlier with a 422.
  2. launchctl-over-SSH — when launchctl is absent (the container), the argv
     SSHes to the host instead of dying with Errno 2.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


def _mk_openai_runtime() -> Runtime:
    return Runtime(
        slug="qwen-general",
        display_name="DGX Spark vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="Qwen/Qwen3.6-35B-A3B-FP8",
        enabled=True,
    )


# ── 1. Singleton guard: service layer (defense in depth) ─────────────────────

@pytest.mark.asyncio
async def test_bootstrap_hermes_refuses_foreign_slug_and_writes_nothing(
    async_session, tmp_path, monkeypatch
):
    """A non-'hermes' agent must never reach the env-write — that write lands in
    ~/.mc/agents/hermes and would clobber the live Hermes agent's token."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    from app.services.agent_bootstrap import bootstrap_hermes_agent

    rt = _mk_openai_runtime()
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    dev = Agent(name="Dev", agent_runtime="host", harness="hermes", runtime_id=rt.id)
    async_session.add(dev)
    await async_session.commit()
    await async_session.refresh(dev)
    assert dev.slug == "dev"

    with pytest.raises(ValueError, match="singleton"):
        await bootstrap_hermes_agent(async_session, dev, rt)

    # The Hermes config dir must not have been created/touched at all.
    assert not (tmp_path / ".mc" / "agents" / "hermes" / "agent.env").exists()


@pytest.mark.asyncio
async def test_bootstrap_grok_refuses_foreign_slug(async_session, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    from app.services.agent_bootstrap import bootstrap_grok_agent

    rt = Runtime(slug="grok-cloud", display_name="Grok", runtime_type="grok",
                 endpoint="https://x", model_identifier="grok-4.5", enabled=True)
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    other = Agent(name="Not Grok", agent_runtime="host", harness="grok", runtime_id=rt.id)
    async_session.add(other)
    await async_session.commit()
    await async_session.refresh(other)

    with pytest.raises(ValueError, match="singleton"):
        await bootstrap_grok_agent(async_session, other, rt)
    assert not (tmp_path / ".mc" / "agents" / "grok" / "agent.env").exists()


@pytest.mark.asyncio
async def test_bootstrap_hermes_allows_real_hermes_slug(async_session, tmp_path, monkeypatch, fake_redis):
    """The guard must NOT block the real Hermes (slug 'hermes') — it still provisions."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    from unittest.mock import MagicMock

    async def _get():
        return fake_redis

    rt = _mk_openai_runtime()
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    hermes = Agent(name="Hermes", agent_runtime="host", harness="hermes", runtime_id=rt.id)
    async_session.add(hermes)
    await async_session.commit()
    await async_session.refresh(hermes)
    assert hermes.slug == "hermes"

    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("app.services.sse.get_redis", _get), \
         patch("app.redis_client.get_redis", _get), \
         patch("app.services.agent_bootstrap.subprocess.run", return_value=proc):
        from app.services.agent_bootstrap import bootstrap_hermes_agent
        result = await bootstrap_hermes_agent(async_session, hermes, rt)

    assert result["token"]
    assert (tmp_path / ".mc" / "agents" / "hermes" / "agent.env").exists()


# ── 2. Singleton guard: provision router (nice 422) ──────────────────────────

@pytest.mark.asyncio
async def test_provision_router_rejects_foreign_hermes_with_422(auth_client, async_session, tmp_path, monkeypatch):
    """POST /agents/{id}/provision on a hermes-harness agent whose slug != 'hermes'
    returns 422 and leaves provision_status untouched — no clobber."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    rt = _mk_openai_runtime()
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    dev = Agent(name="Dev", agent_runtime="host", harness="hermes",
                runtime_id=rt.id, provision_status="local")
    async_session.add(dev)
    await async_session.commit()
    await async_session.refresh(dev)

    resp = await auth_client.post(f"/api/v1/agents/{dev.id}/provision")
    assert resp.status_code == 422, resp.text
    assert "singleton" in resp.text.lower() or "openclaude" in resp.text.lower()

    await async_session.refresh(dev)
    assert dev.provision_status == "local"
    assert not (tmp_path / ".mc" / "agents" / "hermes" / "agent.env").exists()


# ── 3. launchctl-over-SSH argv ───────────────────────────────────────────────

def test_launchctl_argv_local_when_launchctl_present():
    from app.services.agent_bootstrap import _launchctl_bootstrap_argv
    with patch("app.services.agent_bootstrap.shutil.which", return_value="/bin/launchctl"):
        argv = _launchctl_bootstrap_argv(Path("/x/com.mc.hermes-bridge.plist"))
    assert argv[0] == "launchctl"
    assert argv[1] == "bootstrap"
    assert argv[-1] == "/x/com.mc.hermes-bridge.plist"


def test_run_launchctl_tolerates_already_loaded_eio():
    """macOS returns rc=5 'Input/output error' when bootstrapping an already-loaded
    service (not always rc=37) — idempotent re-provision must tolerate it."""
    from unittest.mock import MagicMock
    from app.services import agent_bootstrap

    proc = MagicMock(returncode=5, stdout="", stderr="Bootstrap failed: 5: Input/output error")
    with patch("app.services.agent_bootstrap.shutil.which", return_value="/bin/launchctl"), \
         patch("app.services.agent_bootstrap.subprocess.run", return_value=proc):
        result = agent_bootstrap._run_launchctl_bootstrap(Path("/x/com.mc.hermes-bridge.plist"))
    assert result["loaded"] is True
    assert result["already"] is True


def test_run_launchctl_still_raises_on_real_failure():
    """A non-already-loaded hard failure (rc=5 without the EIO string, or other rc)
    must still raise so the caller rolls back."""
    from unittest.mock import MagicMock
    from app.services import agent_bootstrap

    proc = MagicMock(returncode=1, stdout="", stderr="Load failed: no such file")
    with patch("app.services.agent_bootstrap.shutil.which", return_value="/bin/launchctl"), \
         patch("app.services.agent_bootstrap.subprocess.run", return_value=proc):
        with pytest.raises(RuntimeError):
            agent_bootstrap._run_launchctl_bootstrap(Path("/x/com.mc.foo.plist"))


def test_launchctl_argv_ssh_when_launchctl_absent():
    """In the Linux container launchctl is absent → run it on the host via SSH,
    with $(id -u) evaluated remotely (host login uid, not the container user)."""
    from app.services.agent_bootstrap import _launchctl_bootstrap_argv
    with patch("app.services.agent_bootstrap.shutil.which", return_value=None):
        argv = _launchctl_bootstrap_argv(Path("/x/com.mc.hermes-bridge.plist"))
    assert argv[0] == "ssh"
    assert "host.docker.internal" in argv[-2]
    remote = argv[-1]
    assert remote.startswith("launchctl bootstrap gui/$(id -u) ")
    assert "com.mc.hermes-bridge.plist" in remote
