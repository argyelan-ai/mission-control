"""Tests for docker_agent_sync .env rendering with runtime injection."""
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from app.models.agent import Agent
from app.models.runtime import Runtime


@pytest.fixture
def tmp_agent_dir(tmp_path, monkeypatch):
    """Patches AGENTS_DIR so the sync writes into a pytest tmp_path."""
    import app.services.docker_agent_sync as sync_mod

    monkeypatch.setattr(sync_mod, "AGENTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
async def runtime_qwen(async_session):
    rt = Runtime(
        slug="qwen-coder-lms",
        display_name="Qwen3 Coder",
        runtime_type="lmstudio",
        endpoint="http://192.0.2.10:1234/v1",
        model_identifier="qwen3-coder-next",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    return rt


async def _make_agent(session, *, name, runtime_id=None, agent_runtime="cli-bridge"):
    agent = Agent(
        name=name,
        agent_runtime=agent_runtime,
        runtime_id=runtime_id,
        tools_md="tools content",
        soul_md="soul content",
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_env_written_when_runtime_set(async_session, tmp_agent_dir, runtime_qwen):
    from app.services.docker_agent_sync import sync_docker_agent_files

    agent = await _make_agent(async_session, name="Sparky", runtime_id=runtime_qwen.id)
    claude_dir = tmp_agent_dir / "sparky" / "claude-config"
    claude_dir.mkdir(parents=True)

    with patch("app.services.docker_agent_sync.render_agent_file", return_value="rendered"):
        results = await sync_docker_agent_files(async_session, agent)

    env_file = claude_dir / ".env"
    assert env_file.exists(), f"results={results}"
    content = env_file.read_text()
    assert "OPENAI_BASE_URL=http://192.0.2.10:1234/v1" in content
    assert "OPENAI_MODEL=qwen3-coder-next" in content
    assert "runtime=qwen-coder-lms" in results[".env"]


@pytest.mark.asyncio
async def test_env_recycler_only_when_no_runtime_no_secret(async_session, tmp_agent_dir):
    """No runtime + no secret -> .env contains ONLY the recycler kill-switch line.

    Phase 3 (Plan 03-04, Caveat 1): the previous behavior of skipping/removing
    .env was replaced by an unconditional minimum write so the recycler in
    Window 2 always sees AGENT_RECYCLER_ENABLED. No OPENAI_* keys land here.
    """
    from app.services.docker_agent_sync import sync_docker_agent_files

    agent = await _make_agent(async_session, name="Freecode")
    claude_dir = tmp_agent_dir / "freecode" / "claude-config"
    claude_dir.mkdir(parents=True)

    with patch("app.services.docker_agent_sync.render_agent_file", return_value="rendered"):
        results = await sync_docker_agent_files(async_session, agent)

    env_file = claude_dir / ".env"
    assert env_file.exists(), f"results={results}"
    content = env_file.read_text()
    assert "AGENT_RECYCLER_ENABLED=true" in content
    assert "OPENAI_BASE_URL" not in content
    assert "OPENAI_MODEL" not in content
    assert "OPENAI_API_KEY" not in content
    assert "written" in results[".env"]
    assert "recycler=on" in results[".env"]


@pytest.mark.asyncio
async def test_env_recycler_only_when_runtime_disabled(async_session, tmp_agent_dir):
    """Runtime exists but enabled=False -> .env contains ONLY the recycler line.

    Phase 3 (Plan 03-04, Caveat 1): no OPENAI_* keys are rendered for a
    disabled runtime, but the recycler line still lands so the watchdog
    in Window 2 knows whether to spawn or no-op.
    """
    from app.services.docker_agent_sync import sync_docker_agent_files

    rt = Runtime(
        slug="disabled-rt",
        display_name="Disabled Runtime",
        runtime_type="lmstudio",
        endpoint="http://example.com/v1",
        model_identifier="some-model",
        enabled=False,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    agent = await _make_agent(async_session, name="Rex", runtime_id=rt.id)
    claude_dir = tmp_agent_dir / "rex" / "claude-config"
    claude_dir.mkdir(parents=True)

    with patch("app.services.docker_agent_sync.render_agent_file", return_value="rendered"):
        results = await sync_docker_agent_files(async_session, agent)

    env_file = claude_dir / ".env"
    assert env_file.exists(), f"results={results}"
    content = env_file.read_text()
    assert "AGENT_RECYCLER_ENABLED=true" in content
    assert "OPENAI_BASE_URL" not in content
    assert "OPENAI_MODEL" not in content


@pytest.mark.asyncio
async def test_env_recycler_disabled_per_agent(async_session, tmp_agent_dir):
    """Per-agent recycler_enabled=False -> .env contains AGENT_RECYCLER_ENABLED=false.

    Phase 3 (Plan 03-04): two-tier kill-switch resolution via
    get_effective_recycler_enabled — per-agent disable wins over global True.
    """
    from app.services.docker_agent_sync import sync_docker_agent_files

    agent = await _make_agent(async_session, name="Sparky")
    agent.recycler_enabled = False
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    claude_dir = tmp_agent_dir / "sparky" / "claude-config"
    claude_dir.mkdir(parents=True)

    with patch("app.services.docker_agent_sync.render_agent_file", return_value="rendered"):
        results = await sync_docker_agent_files(async_session, agent)

    env_file = claude_dir / ".env"
    assert env_file.exists(), f"results={results}"
    content = env_file.read_text()
    assert "AGENT_RECYCLER_ENABLED=false" in content
    assert "recycler=off" in results[".env"]


@pytest.mark.asyncio
async def test_no_secret_error_for_anthropic_runtime(async_session, tmp_agent_dir):
    """ADR-056: an agent bound to an anthropic-protocol runtime uses OAuth and
    has no OPENAI_API_KEY by design. Even with a secret_id set (whose lookup
    yields no OPENAI key), sync must NOT record `.env_secret_error` — that would
    be a false alarm.

    Uses a *disabled* anthropic runtime so `is_anthropic` is False and the
    non-anthropic else-branch is actually reached, exercising the protocol
    guard on the `.env_secret_error` line (regression: pre-fix this recorded
    the error because only `agent.secret_id` was checked).
    """
    import uuid as _uuid

    from app.services.docker_agent_sync import sync_docker_agent_files

    rt = Runtime(
        slug="anthropic-claude-opus",
        display_name="Claude Opus",
        runtime_type="anthropic_oauth",
        endpoint="https://api.anthropic.com",
        model_identifier="claude-opus",
        enabled=False,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    agent = await _make_agent(async_session, name="Boss2", runtime_id=rt.id)
    # secret_id points at a non-existent secret → no OPENAI key resolves.
    agent.secret_id = _uuid.uuid4()
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    claude_dir = tmp_agent_dir / "boss2" / "claude-config"
    claude_dir.mkdir(parents=True)

    with patch("app.services.docker_agent_sync.render_agent_file", return_value="rendered"):
        results = await sync_docker_agent_files(async_session, agent)

    assert ".env_secret_error" not in results, f"results={results}"


@pytest.mark.asyncio
async def test_host_runtime_skipped(async_session, tmp_agent_dir):
    from app.services.docker_agent_sync import sync_docker_agent_files

    agent = await _make_agent(async_session, name="Boss", agent_runtime="host")
    results = await sync_docker_agent_files(async_session, agent)
    assert results.get("_skipped") == "host runtime"


# ── restart_docker_agent_container ─────────────────────────────────────────


def test_restart_default_uses_docker_restart():
    """Default path: `docker restart -t 5 mc-agent-<slug>` — preserves image."""
    from app.services.docker_agent_sync import restart_docker_agent_container

    agent = Agent(name="Davinci", agent_runtime="cli-bridge")

    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        result = restart_docker_agent_container(agent, force_recreate=False)

    assert result["status"] == "restarted"
    assert result["container"] == "mc-agent-davinci"
    assert result["mode"] == "restart"
    cmd = run_mock.call_args.args[0]
    assert cmd[:2] == ["docker", "restart"]
    assert "mc-agent-davinci" in cmd
    # Ensure no `compose ... up --force-recreate` slipped into the default path
    assert "compose" not in cmd
    assert "--force-recreate" not in cmd


def test_restart_force_recreate_runs_docker_compose_up():
    """force_recreate=True: docker compose -f ... -f ... up -d --force-recreate <svc>."""
    from app.services.docker_agent_sync import restart_docker_agent_container

    agent = Agent(name="Sparky", agent_runtime="cli-bridge")

    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        result = restart_docker_agent_container(agent, force_recreate=True)

    assert result["status"] == "recreated"
    assert result["container"] == "mc-agent-sparky"
    assert result["mode"] == "recreate"
    cmd = run_mock.call_args.args[0]
    assert cmd[0] == "docker"
    assert cmd[1] == "compose"
    assert "-f" in cmd
    assert "up" in cmd
    assert "-d" in cmd
    assert "--force-recreate" in cmd
    assert cmd[-1] == "mc-agent-sparky"
    # 90s timeout (Phase 15 contract)
    assert run_mock.call_args.kwargs.get("timeout") == 90


def test_restart_host_runtime_skipped():
    """Host agents (Boss) have no docker container — skip without invoking subprocess."""
    from app.services.docker_agent_sync import restart_docker_agent_container

    agent = Agent(name="Boss", agent_runtime="host")
    with patch("subprocess.run") as run_mock:
        result = restart_docker_agent_container(agent, force_recreate=True)
    assert "skipped" in result["status"]
    assert result["mode"] == "skip"
    run_mock.assert_not_called()


def test_restart_default_no_container_returns_skipped():
    """When `docker restart` reports no such container, surface that as skip."""
    from app.services.docker_agent_sync import restart_docker_agent_container

    agent = Agent(name="Ghost", agent_runtime="cli-bridge")
    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 1
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = "Error: No such container: mc-agent-ghost"
        result = restart_docker_agent_container(agent, force_recreate=False)
    assert "skipped" in result["status"]
    assert result["mode"] == "restart"


# ── _sanitize_env_val — newline injection defense (HIGH-2 fix) ─────────────


def test_env_value_with_newline_rejected():
    """_sanitize_env_val must raise ValueError for values containing LF or CR,
    preventing a runtime.endpoint like 'host\\nOPENAI_API_KEY=evil' from
    injecting extra lines into the .env file written per agent."""
    from app.services.docker_agent_sync import _sanitize_env_val

    # LF injection
    with pytest.raises(ValueError, match="injection rejected"):
        _sanitize_env_val("http://attacker.com\nOPENAI_API_KEY=injected")

    # CR injection
    with pytest.raises(ValueError, match="injection rejected"):
        _sanitize_env_val("http://attacker.com\rX-Header=evil")

    # CRLF injection
    with pytest.raises(ValueError, match="injection rejected"):
        _sanitize_env_val("http://attacker.com\r\nX-Header=evil")


def test_env_value_clean_passes_through():
    """_sanitize_env_val returns the value unchanged when it contains no CR/LF."""
    from app.services.docker_agent_sync import _sanitize_env_val

    assert _sanitize_env_val("http://192.0.2.10:8000/v1") == "http://192.0.2.10:8000/v1"
    assert _sanitize_env_val("qwen3-coder-next") == "qwen3-coder-next"
    assert _sanitize_env_val("sk-abc123") == "sk-abc123"


# ── Task 2: restart_docker_agent_container respawn_window_only branch ─────


def test_restart_respawn_window_only_calls_helper_not_restart():
    """respawn_window_only=True → _respawn_agent_window is called, no
    docker restart, no docker compose up."""
    from app.services.docker_agent_sync import restart_docker_agent_container

    agent = Agent(name="Sparky", agent_runtime="cli-bridge")
    helper_result = {
        "status": "respawned",
        "container": "mc-agent-sparky",
        "mode": "respawn",
    }
    with patch(
        "app.services.docker_agent_sync._respawn_agent_window",
        return_value=helper_result,
    ) as helper_mock, patch("subprocess.run") as run_mock:
        result = restart_docker_agent_container(agent, respawn_window_only=True)

    helper_mock.assert_called_once_with(agent)
    run_mock.assert_not_called()
    assert result == helper_result


def test_restart_respawn_window_only_wins_over_force_recreate():
    """force_recreate=True AND respawn_window_only=True → the respawn path wins."""
    from app.services.docker_agent_sync import restart_docker_agent_container

    agent = Agent(name="Davinci", agent_runtime="cli-bridge")
    helper_result = {
        "status": "respawned",
        "container": "mc-agent-davinci",
        "mode": "respawn",
    }
    with patch(
        "app.services.docker_agent_sync._respawn_agent_window",
        return_value=helper_result,
    ) as helper_mock, patch("subprocess.run") as run_mock:
        result = restart_docker_agent_container(
            agent, force_recreate=True, respawn_window_only=True
        )

    helper_mock.assert_called_once_with(agent)
    # docker compose up --force-recreate is never reached
    run_mock.assert_not_called()
    assert result["mode"] == "respawn"


def test_restart_default_unchanged_backward_compat():
    """Without the new flags: docker restart path as before (backward compat)."""
    from app.services.docker_agent_sync import restart_docker_agent_container

    agent = Agent(name="Rex", agent_runtime="cli-bridge")
    with patch(
        "app.services.docker_agent_sync._respawn_agent_window"
    ) as helper_mock, patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        result = restart_docker_agent_container(agent)

    helper_mock.assert_not_called()
    cmd = run_mock.call_args.args[0]
    assert cmd[:2] == ["docker", "restart"]
    assert result["mode"] == "restart"


@pytest.mark.asyncio
async def test_wait_for_agent_healthy_respawn_mode_uses_window_ready():
    """respawn_mode=True → _wait_for_window_ready is called, docker inspect skipped."""
    from app.services.docker_agent_sync import wait_for_agent_healthy

    agent = Agent(name="Neo", agent_runtime="cli-bridge")
    helper_result = {"healthy": True, "reason": "tmux window ready"}

    async def _fake_helper(*args, **kwargs):
        return helper_result

    with patch(
        "app.services.docker_agent_sync._wait_for_window_ready",
        side_effect=_fake_helper,
    ) as helper_mock, patch("subprocess.run") as run_mock:
        result = await wait_for_agent_healthy(
            agent, timeout=5, poll_interval=1.0, respawn_mode=True
        )

    helper_mock.assert_called_once()
    run_mock.assert_not_called()
    assert result == helper_result


@pytest.mark.asyncio
async def test_wait_for_agent_healthy_default_unchanged_backward_compat():
    """Without respawn_mode: docker inspect loop (backward compat)."""
    from app.services.docker_agent_sync import wait_for_agent_healthy

    agent = Agent(name="Tester", agent_runtime="cli-bridge")
    with patch(
        "app.services.docker_agent_sync._wait_for_window_ready"
    ) as helper_mock, patch("subprocess.run") as run_mock, patch(
        "asyncio.sleep", new_callable=AsyncMock
    ):
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = "running\n"
        run_mock.return_value.stderr = ""
        result = await wait_for_agent_healthy(
            agent, timeout=5, poll_interval=0.1
        )

    helper_mock.assert_not_called()
    cmd = run_mock.call_args.args[0]
    assert cmd[:2] == ["docker", "inspect"]
    assert result["healthy"] is True


# ── Task 1: _respawn_agent_window + _wait_for_window_ready helpers ─────────


def test_respawn_agent_window_uses_slug_as_session():
    """_respawn_agent_window calls `docker exec mc-agent-{slug} tmux respawn-window
    -k -t {slug}:0`. The entrypoint.sh sets SESSION="${AGENT_NAME}", and
    docker-compose.agents.yml sets AGENT_NAME=<lowercase-slug>. Live-verified
    2026-04-29 (Phase 16 D-13): RESEARCH.md Pitfall-3 was wrong — slug, not
    agent.name."""
    from app.services.docker_agent_sync import _respawn_agent_window

    # name has uppercase + space → slug differs from name
    agent = Agent(name="Free Code", agent_runtime="cli-bridge")
    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        result = _respawn_agent_window(agent)

    cmd = run_mock.call_args.args[0]
    assert cmd[:2] == ["docker", "exec"]
    # container name = mc-agent-{slug} (lowercase slug)
    assert cmd[2] == "mc-agent-free-code"
    assert cmd[3] == "tmux"
    assert cmd[4] == "respawn-window"
    assert "-k" in cmd
    # target = {slug}:0 (entrypoint sets SESSION=$AGENT_NAME = slug)
    assert "free-code:0" in cmd
    assert result["status"] == "respawned"
    assert result["container"] == "mc-agent-free-code"
    assert result["mode"] == "respawn"


def test_respawn_agent_window_timeout_returns_error():
    """subprocess.TimeoutExpired → dict with mode='respawn' and status startswith
    'error: tmux respawn-window timed out'."""
    import subprocess

    from app.services.docker_agent_sync import _respawn_agent_window

    agent = Agent(name="Davinci", agent_runtime="cli-bridge")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10),
    ):
        result = _respawn_agent_window(agent)

    assert result["mode"] == "respawn"
    assert result["status"].startswith("error: tmux respawn-window timed out")
    assert result["container"] == "mc-agent-davinci"


def test_respawn_agent_window_success_returncode_zero():
    """returncode=0 → {'status':'respawned','container':'mc-agent-{slug}','mode':'respawn'}."""
    from app.services.docker_agent_sync import _respawn_agent_window

    agent = Agent(name="Sparky", agent_runtime="cli-bridge")
    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        result = _respawn_agent_window(agent)

    assert result == {
        "status": "respawned",
        "container": "mc-agent-sparky",
        "mode": "respawn",
    }


@pytest.mark.asyncio
async def test_wait_for_window_ready_detects_ready_signal():
    """capture-pane output containing '╭─' header → healthy."""
    from app.services.docker_agent_sync import _wait_for_window_ready

    agent = Agent(name="Rex", agent_runtime="cli-bridge")
    with patch("subprocess.run") as run_mock, patch(
        "asyncio.sleep", new_callable=AsyncMock
    ):
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = "╭─ openclaude ─╮\n│ ready        │\n"
        run_mock.return_value.stderr = ""
        result = await _wait_for_window_ready(
            agent, timeout=10, poll_interval=1.0
        )

    assert result["healthy"] is True
    assert "ready" in result["reason"].lower() or "mc-agent-rex" in result["reason"]


@pytest.mark.asyncio
async def test_wait_for_window_ready_timeout_returns_unhealthy():
    """Empty capture-pane forever → timeout result {'healthy': False, 'reason': 'timeout...'}."""
    from app.services.docker_agent_sync import _wait_for_window_ready

    agent = Agent(name="Neo", agent_runtime="cli-bridge")
    with patch("subprocess.run") as run_mock, patch(
        "asyncio.sleep", new_callable=AsyncMock
    ):
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""  # never matches a ready-signal
        run_mock.return_value.stderr = ""
        result = await _wait_for_window_ready(
            agent, timeout=1, poll_interval=0.01
        )

    assert result["healthy"] is False
    assert "timeout" in result["reason"].lower()


@pytest.mark.asyncio
async def test_wait_for_window_ready_skips_for_host_runtime():
    """Host runtime → _wait_for_window_ready returns healthy without subprocess."""
    from app.services.docker_agent_sync import _wait_for_window_ready

    agent = Agent(name="Boss", agent_runtime="host")
    with patch("subprocess.run") as run_mock:
        result = await _wait_for_window_ready(agent, timeout=5, poll_interval=0.1)

    assert result["healthy"] is True
    run_mock.assert_not_called()


# ── Bug 5 permanent fix: settings.json systemPrompt sync ──────────────────


@pytest.mark.asyncio
async def test_settings_json_renders_via_plugin_manager_when_soul_md_populated(
    async_session, tmp_agent_dir, runtime_qwen
):
    """Bug 5 permanent fix: when settings.json exists + runtime is enabled +
    agent.soul_md is populated, sync_docker_agent_files MUST delegate the
    settings.json write to plugin_manager.sync_agent_plugins_to_disk so the
    full template is re-rendered (systemPrompt + model + enabledPlugins).

    Pre-fix behavior: only the `model` key was merged into the existing JSON,
    so a stale/empty systemPrompt persisted forever.
    """
    from unittest.mock import patch as _patch

    from app.services.docker_agent_sync import sync_docker_agent_files

    # The SOUL.md render step inside sync_docker_agent_files overwrites
    # agent.soul_md with whatever render_agent_file returns (Template -> DB).
    # So we mock render_agent_file to return a >1000-char string — that's
    # what the production code sees too (real SOUL.md.j2 renders ~30k chars).
    long_soul = "soul " * 300  # ~1500 chars, well above the 1000-char self-check
    agent = await _make_agent(
        async_session, name="Sparky", runtime_id=runtime_qwen.id
    )
    agent.cli_plugins = ["superpowers@claude-plugins-official"]
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    claude_dir = tmp_agent_dir / "sparky" / "claude-config"
    claude_dir.mkdir(parents=True)
    # Existing settings.json with EMPTY systemPrompt — exactly Sparky's state
    # before the fix. Includes a stale model so we can prove the new code path
    # took over (the previous code would have only updated `model`).
    (claude_dir / "settings.json").write_text(
        '{"model": "stale-model", "systemPrompt": ""}'
    )

    with _patch(
        "app.services.docker_agent_sync.render_agent_file",
        return_value=long_soul,
    ), _patch(
        "app.services.plugin_manager.sync_agent_plugins_to_disk",
        return_value={"settings.json": True},
    ) as plugin_mock:
        results = await sync_docker_agent_files(async_session, agent)

    plugin_mock.assert_called_once()
    args, kwargs = plugin_mock.call_args
    # Accept either positional or keyword argument styles.
    if kwargs:
        passed_slug = kwargs.get("agent_slug", args[0] if args else None)
        passed_prompt = kwargs.get("system_prompt", args[1] if len(args) > 1 else None)
        passed_model = kwargs.get("model", args[2] if len(args) > 2 else None)
        passed_plugins = kwargs.get("cli_plugins", args[3] if len(args) > 3 else None)
    else:
        passed_slug, passed_prompt, passed_model, passed_plugins = args[:4]

    assert passed_slug == "sparky"
    assert passed_prompt == long_soul, "systemPrompt must be the full agent.soul_md"
    assert passed_model == "qwen3-coder-next"
    assert passed_plugins == ["superpowers@claude-plugins-official"]
    assert "written" in results["settings.json"]


@pytest.mark.asyncio
async def test_settings_json_skipped_with_warning_when_soul_md_too_short(
    async_session, tmp_agent_dir, runtime_qwen, caplog
):
    """Self-check: if agent.soul_md is shorter than 1000 chars (i.e. the DB
    row is unpopulated or stub-state), do NOT call sync_agent_plugins_to_disk —
    that would overwrite a previously-good settings.json with an empty
    systemPrompt and silently re-introduce the bug. Instead, skip + warn.
    """
    import logging
    from unittest.mock import patch as _patch

    from app.services.docker_agent_sync import sync_docker_agent_files

    agent = await _make_agent(
        async_session, name="Sparky", runtime_id=runtime_qwen.id
    )
    # _make_agent already sets soul_md="soul content" (12 chars) — well below 1000.
    claude_dir = tmp_agent_dir / "sparky" / "claude-config"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(
        '{"model": "x", "systemPrompt": "previously good prompt"}'
    )

    with _patch(
        "app.services.docker_agent_sync.render_agent_file",
        return_value="rendered",
    ), _patch(
        "app.services.plugin_manager.sync_agent_plugins_to_disk"
    ) as plugin_mock, caplog.at_level(logging.WARNING, logger="mc.docker_agent_sync"):
        results = await sync_docker_agent_files(async_session, agent)

    plugin_mock.assert_not_called()
    assert "skipped" in results["settings.json"].lower()
    assert any(
        "soul_md" in rec.message and "short" in rec.message.lower()
        for rec in caplog.records
    ), f"expected warn-log about short soul_md, got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_settings_json_unchanged_when_no_runtime(
    async_session, tmp_agent_dir
):
    """Backward compat: no runtime → don't touch settings.json (no model to set,
    no point re-rendering). plugin_manager must NOT be called."""
    from unittest.mock import patch as _patch

    from app.services.docker_agent_sync import sync_docker_agent_files

    agent = await _make_agent(async_session, name="Freecode")
    claude_dir = tmp_agent_dir / "freecode" / "claude-config"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text('{"model": "x"}')

    with _patch(
        "app.services.docker_agent_sync.render_agent_file",
        return_value="rendered",
    ), _patch(
        "app.services.plugin_manager.sync_agent_plugins_to_disk"
    ) as plugin_mock:
        results = await sync_docker_agent_files(async_session, agent)

    plugin_mock.assert_not_called()
    assert "unchanged" in results["settings.json"]


@pytest.mark.asyncio
async def test_settings_json_skipped_when_file_does_not_exist(
    async_session, tmp_agent_dir, runtime_qwen
):
    """Backward compat: claude-config exists but no settings.json yet → skip
    (provisioning must create the initial file). plugin_manager NOT called."""
    from unittest.mock import patch as _patch

    from app.services.docker_agent_sync import sync_docker_agent_files

    long_soul = "soul " * 300
    agent = await _make_agent(
        async_session, name="Sparky", runtime_id=runtime_qwen.id
    )
    agent.soul_md = long_soul
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    claude_dir = tmp_agent_dir / "sparky" / "claude-config"
    claude_dir.mkdir(parents=True)
    # No settings.json on disk.

    with _patch(
        "app.services.docker_agent_sync.render_agent_file",
        return_value="rendered",
    ), _patch(
        "app.services.plugin_manager.sync_agent_plugins_to_disk"
    ) as plugin_mock:
        results = await sync_docker_agent_files(async_session, agent)

    plugin_mock.assert_not_called()
    assert "skipped" in results["settings.json"]


@pytest.mark.asyncio
async def test_runtime_endpoint_with_newline_rejected_during_sync(
    async_session, tmp_agent_dir
):
    """sync_docker_agent_files must propagate ValueError (not silently write
    the injected content) when runtime.endpoint contains a newline."""
    from app.services.docker_agent_sync import sync_docker_agent_files

    rt = Runtime(
        slug="evil-rt",
        display_name="Evil Runtime",
        runtime_type="lmstudio",
        endpoint="http://attacker.com\nOPENAI_API_KEY=injected",
        model_identifier="safe-model",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    agent = await _make_agent(async_session, name="Victim", runtime_id=rt.id)
    claude_dir = tmp_agent_dir / "victim" / "claude-config"
    claude_dir.mkdir(parents=True)

    with patch("app.services.docker_agent_sync.render_agent_file", return_value="rendered"):
        results = await sync_docker_agent_files(async_session, agent)

    # The .env write should fail and the result should surface the error, not
    # silently write the injected content.
    env_file = claude_dir / ".env"
    if env_file.exists():
        content = env_file.read_text()
        assert "injected" not in content, (
            "Newline injection was NOT blocked — _sanitize_env_val not applied"
        )
    # Result key should indicate an error.
    assert "error" in results.get(".env", "").lower() or not env_file.exists()
