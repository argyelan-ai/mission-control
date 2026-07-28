"""fix/host-launcher-from-template — generated start-claude.sh for native-claude
host agents (Boss today).

Bug this closes: docker/boss-host/start-claude.sh hardcoded
CONFIG_DIR=~/.mc/agents/boss-host/claude-config, while
sync_host_agent_files() writes SOUL.md/CARD.md to
agent.workspace_path/claude-config (~/.mc/workspaces/boss for Boss). The two
paths diverged silently for three months (Boss read a 29 Apr System-Prompt
while MC "successfully" synced a file nobody read from 28 Jul).

render_host_launcher_script() renders backend/templates/start-agent.sh.j2
with CONFIG_DIR derived straight from agent.workspace_path — the same field
sync_host_agent_files() uses — so the two locations can no longer diverge by
construction.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.services.docker_agent_sync import (
    _agent_slug,
    render_host_launcher_script,
    sync_host_agent_files,
)
from tests.conftest import test_engine


def _make_claude_host_agent(*, workspace_path: str | None, name: str = "Boss") -> Agent:
    return Agent(
        id=uuid.uuid4(),
        name=name,
        role="orchestrator",
        emoji="🧠",
        agent_runtime="host",
        harness="claude",
        model="claude-opus-4-8",
        workspace_path=workspace_path,
    )


@pytest.fixture(autouse=True)
def _isolate_agents_dir(tmp_path, monkeypatch):
    """render_host_launcher_script writes under AGENTS_DIR (~/.mc/agents) by
    convention — redirect it into tmp_path so tests never touch the real
    ~/.mc/agents/ tree (task instruction: nothing written outside tmp_path)."""
    fake_agents_dir = tmp_path / "agents-dir"
    monkeypatch.setattr(
        "app.services.docker_agent_sync.AGENTS_DIR", fake_agents_dir
    )
    return fake_agents_dir


def test_launcher_config_dir_derives_from_workspace_path(tmp_path):
    """The single-source-of-truth test: compute the expected CONFIG_DIR from
    agent.workspace_path (never hardcode it a second time), then assert the
    rendered script points exactly there — the same directory
    sync_host_agent_files() writes SOUL.md/CARD.md into. A test that
    hardcodes the path twice would pass even if the bug were reproduced."""
    workspace = tmp_path / "workspaces" / "boss"
    workspace.mkdir(parents=True)
    agent = _make_claude_host_agent(workspace_path=str(workspace))

    expected_config_dir = str(Path(agent.workspace_path) / "claude-config")

    result = render_host_launcher_script(agent)
    assert "start-claude.sh" in result, result

    from app.services import docker_agent_sync as das
    launcher_path = das.AGENTS_DIR / f"{_agent_slug(agent)}-host" / "start-claude.sh"
    assert launcher_path.exists()
    content = launcher_path.read_text()
    assert f'CONFIG_DIR="{expected_config_dir}"' in content


@pytest.mark.asyncio
async def test_launcher_config_dir_matches_sync_host_agent_files_target(tmp_path):
    """End-to-end: sync_host_agent_files() writes SOUL.md into
    workspace_path/claude-config; the generated launcher's CONFIG_DIR must
    point at that exact directory."""
    workspace = tmp_path / "workspaces" / "boss"
    workspace.mkdir(parents=True)
    agent = _make_claude_host_agent(workspace_path=str(workspace))

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_host_agent_files(s, agent)

    assert results.get("SOUL.md") == "written"
    soul_path = workspace / "claude-config" / "SOUL.md"
    assert soul_path.exists()

    from app.services import docker_agent_sync as das
    launcher_path = das.AGENTS_DIR / f"{_agent_slug(agent)}-host" / "start-claude.sh"
    content = launcher_path.read_text()
    # The launcher's CONFIG_DIR must be the parent of the file sync actually wrote.
    assert str(soul_path.parent) in content
    assert f'CONFIG_DIR="{soul_path.parent}"' in content


def test_launcher_preserves_card_soul_fallback(tmp_path):
    workspace = tmp_path / "boss"
    workspace.mkdir()
    agent = _make_claude_host_agent(workspace_path=str(workspace))

    render_host_launcher_script(agent)

    from app.services import docker_agent_sync as das
    launcher_path = das.AGENTS_DIR / f"{_agent_slug(agent)}-host" / "start-claude.sh"
    content = launcher_path.read_text()
    assert '[ -s "$CARD_FILE" ] || CARD_FILE="$SOUL_FILE"' in content
    assert "--append-system-prompt" in content


def test_launcher_missing_workspace_path_returns_clean_error_no_crash(tmp_path):
    """Grok + Jarvis have agent_runtime=host with workspace_path=NULL in
    prod. Must not raise, must not write a broken script."""
    agent = _make_claude_host_agent(workspace_path=None, name="NoWorkspaceHost")

    result = render_host_launcher_script(agent)
    assert "_error" in result
    assert "workspace_path" in result["_error"]

    from app.services import docker_agent_sync as das
    launcher_path = das.AGENTS_DIR / f"{_agent_slug(agent)}-host" / "start-claude.sh"
    assert not launcher_path.exists()


def test_launcher_non_claude_harness_is_noop(tmp_path):
    """Hermes/Grok don't run the native-claude launcher pattern at all
    (verified live: Hermes starts via launchd -> python -m hermes_cli, no
    start-claude.sh anywhere under ~/.mc). Skip cleanly, no file written."""
    workspace = tmp_path / "hermes"
    workspace.mkdir()
    agent = _make_claude_host_agent(workspace_path=str(workspace), name="Hermes")
    agent.harness = "hermes"

    result = render_host_launcher_script(agent)
    assert result.get("_skipped")

    from app.services import docker_agent_sync as das
    launcher_path = das.AGENTS_DIR / f"{_agent_slug(agent)}-host" / "start-claude.sh"
    assert not launcher_path.exists()


def test_launcher_is_valid_shell_syntax(tmp_path):
    workspace = tmp_path / "boss"
    workspace.mkdir()
    agent = _make_claude_host_agent(workspace_path=str(workspace))

    render_host_launcher_script(agent)

    from app.services import docker_agent_sync as das
    launcher_path = das.AGENTS_DIR / f"{_agent_slug(agent)}-host" / "start-claude.sh"
    proc = subprocess.run(
        ["bash", "-n", str(launcher_path)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_launcher_backs_up_existing_script_before_overwrite(tmp_path):
    workspace = tmp_path / "boss"
    workspace.mkdir()
    agent = _make_claude_host_agent(workspace_path=str(workspace))

    from app.services import docker_agent_sync as das
    launcher_dir = das.AGENTS_DIR / f"{_agent_slug(agent)}-host"
    launcher_dir.mkdir(parents=True)
    launcher_path = launcher_dir / "start-claude.sh"
    launcher_path.write_text("#!/bin/bash\necho old-hardcoded-script\n")

    render_host_launcher_script(agent)

    bak_path = launcher_dir / "start-claude.sh.bak"
    assert bak_path.exists()
    assert "old-hardcoded-script" in bak_path.read_text()
    assert "old-hardcoded-script" not in launcher_path.read_text()


@pytest.mark.asyncio
async def test_launcher_sync_host_agent_files_wires_it_in(tmp_path):
    """sync_host_agent_files() (the function sync-config actually calls for
    host agents) must generate the launcher as part of its normal sync, not
    require a separate manual call."""
    workspace = tmp_path / "boss"
    workspace.mkdir()
    agent = _make_claude_host_agent(workspace_path=str(workspace))

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_host_agent_files(s, agent)

    assert "start-claude.sh" in results

    from app.services import docker_agent_sync as das
    launcher_path = das.AGENTS_DIR / f"{_agent_slug(agent)}-host" / "start-claude.sh"
    assert launcher_path.exists()
