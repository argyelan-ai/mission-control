import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.services.install_executor import InstallExecutor, InstallResult


@pytest.mark.asyncio
async def test_install_skill_appends_to_cli_skills(async_session: AsyncSession):
    # Arrange: agent with initial cli_skills = ["skill-a"]
    agent = Agent(
        name="Spark",
        role="researcher",
        cli_skills=["skill-a"],
        scopes=[],
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(),
        agent_id=agent.id,
        action_type="install_skill",
        description="Install web-performance for Spark",
        payload={
            "name": "web-performance",
            "source": "github:anthropic/skill-web-performance",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
            "reason": "Agent failed 3 perf-debug tasks",
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._call_skill_install",
               new_callable=AsyncMock) as mock_install, \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock) as mock_sync:
        mock_install.return_value = {"installed_version": "1.2.0"}
        executor = InstallExecutor(async_session)
        result = await executor.execute(approval)

    # Assert: cli_skills updated, install_log written
    await async_session.refresh(agent)
    assert "web-performance" in agent.cli_skills
    assert "skill-a" in agent.cli_skills  # kept existing
    assert result.result == "success"
    assert result.installed_version == "1.2.0"
    mock_sync.assert_awaited_once_with(agent.id)


@pytest.mark.asyncio
async def test_uninstall_skill_removes_from_cli_skills(async_session: AsyncSession):
    agent = Agent(
        name="Spark",
        role="researcher",
        cli_skills=["skill-a", "web-performance"],
        scopes=[],
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(),
        agent_id=agent.id,
        action_type="uninstall_skill",
        description="Uninstall web-performance",
        payload={
            "name": "web-performance",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
            "reason": "no longer needed",
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock):
        executor = InstallExecutor(async_session)
        result = await executor.execute(approval)

    await async_session.refresh(agent)
    assert "web-performance" not in agent.cli_skills
    assert "skill-a" in agent.cli_skills
    assert result.result == "success"


@pytest.mark.asyncio
async def test_install_skill_rollback_on_sync_failure(async_session: AsyncSession):
    agent = Agent(name="Spark", role="researcher", cli_skills=["a"], scopes=[])
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    approval = Approval(
        board_id=uuid.uuid4(), agent_id=agent.id,
        action_type="install_skill", description="test",
        payload={
            "name": "web-perf",
            "source": "github:anthropic/skill-web-performance",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        },
        status="approved",
    )
    async_session.add(approval)
    await async_session.commit()

    with patch("app.services.install_executor._call_skill_install",
               new_callable=AsyncMock) as mock_install, \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock, side_effect=RuntimeError("sync fail")):
        mock_install.return_value = {"installed_version": "1.0"}
        executor = InstallExecutor(async_session)
        result = await executor.execute(approval)

    # Rollback: cli_skills back to original
    await async_session.refresh(agent)
    assert agent.cli_skills == ["a"]
    assert result.result == "rolled_back"
    assert "sync fail" in result.error


# ── Local-skill path resolution (HOME_HOST fix) ───────────────────────────


def test_call_skill_install_uses_home_host_for_local_path(tmp_path, monkeypatch):
    """Local source ~/.mc/skills/<name> must resolve HOME_HOST,
    not the container user's home.

    Bug 2026-04-23: backend container has ~ = /home/mcuser/, but the mount
    lives under /Users/testuser/.mc/. Previously os.path.expanduser('~')
    was used → FileNotFoundError despite the skill being present.
    """
    import asyncio
    from app.services.install_executor import _call_skill_install

    # Simulate: HOME_HOST points to a tmp_path, the skill lives there.
    # ~ (in this test subprocess env) points somewhere else.
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    skills_dir = tmp_path / ".mc" / "skills" / "my-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n")

    # Should resolve via HOME_HOST and find the skill
    result = asyncio.run(_call_skill_install("~/.mc/skills/my-skill", "my-skill"))
    assert result == {"installed_version": None}


def test_call_skill_install_local_missing_raises(tmp_path, monkeypatch):
    """If the skill path does NOT exist → FileNotFoundError with the resolved path."""
    import asyncio
    from app.services.install_executor import _call_skill_install

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    # Skills directory NOT created → FileNotFoundError expected
    with pytest.raises(FileNotFoundError) as exc_info:
        asyncio.run(_call_skill_install("~/.mc/skills/missing", "missing"))
    # Error text must contain the RESOLVED path (not ~)
    assert str(tmp_path) in str(exc_info.value)
    assert "missing" in str(exc_info.value)


# ── Multi-skill repo handling (live bug 2026-04-24 stitch-skills) ──────────


def test_github_clone_extracts_from_multi_skill_monorepo(tmp_path, monkeypatch):
    """Monorepo with skills/<name>/SKILL.md → only the sub-skill is extracted,
    not the whole repo."""
    import asyncio
    from app.services.install_executor import _call_skill_install

    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Fake git clone: mock subprocess_exec to create a multi-skill repo layout
    # locally instead of actually cloning.
    fake_repo = tmp_path / "fake_upstream_repo"
    (fake_repo / "skills" / "shadcn-ui").mkdir(parents=True)
    (fake_repo / "skills" / "shadcn-ui" / "SKILL.md").write_text(
        "---\nname: shadcn-ui\n---\n# shadcn-ui\nComponent-design patterns.\n"
    )
    (fake_repo / "skills" / "other-skill").mkdir(parents=True)
    (fake_repo / "skills" / "other-skill" / "SKILL.md").write_text("---\nname: other\n---\n")
    (fake_repo / "README.md").write_text("# Repo readme")
    (fake_repo / "LICENSE").write_text("MIT")

    async def fake_exec(*args, **kwargs):
        # args are ("git", "clone", "--depth", "1", url, target_dir)
        target = args[5]
        import shutil as _sh
        _sh.copytree(str(fake_repo), target)

        class _Proc:
            returncode = 0
            async def communicate(self):
                return (b"", b"")
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = asyncio.run(_call_skill_install(
        "github:fake-org/multi-skill-repo", "shadcn-ui",
    ))
    assert result == {"installed_version": None}

    installed = tmp_path / ".mc" / "skills" / "shadcn-ui"
    assert installed.exists()
    # ONLY shadcn-ui content, NOT the whole repo (no README, no LICENSE)
    assert (installed / "SKILL.md").exists()
    assert "shadcn-ui" in (installed / "SKILL.md").read_text()
    assert not (installed / "README.md").exists(), (
        "README vom Repo-Root darf NICHT im Skill-Dir landen"
    )
    assert not (installed / "skills").exists(), (
        "Monorepo skills/ Unterverzeichnis darf nicht durchreichen"
    )


def test_github_clone_single_skill_repo(tmp_path, monkeypatch):
    """Single-skill repo (SKILL.md at root) → the whole content gets installed."""
    import asyncio
    from app.services.install_executor import _call_skill_install

    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    fake_repo = tmp_path / "fake_single_repo"
    fake_repo.mkdir(parents=True)
    (fake_repo / "SKILL.md").write_text("---\nname: solo\nversion: 1.2.3\n---\n# Solo\n")
    (fake_repo / "examples").mkdir()
    (fake_repo / "examples" / "demo.md").write_text("demo")

    async def fake_exec(*args, **kwargs):
        target = args[5]
        import shutil as _sh
        _sh.copytree(str(fake_repo), target)

        class _Proc:
            returncode = 0
            async def communicate(self):
                return (b"", b"")
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = asyncio.run(_call_skill_install("github:obra/skill-solo", "solo"))
    assert result == {"installed_version": "1.2.3"}

    installed = tmp_path / ".mc" / "skills" / "solo"
    assert (installed / "SKILL.md").exists()
    assert (installed / "examples" / "demo.md").exists()


def test_github_clone_unknown_layout_raises(tmp_path, monkeypatch):
    """Repo without a recognizable skill layout → RuntimeError with a helpful hint."""
    import asyncio
    from app.services.install_executor import _call_skill_install

    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    fake_repo = tmp_path / "fake_weird_repo"
    fake_repo.mkdir(parents=True)
    (fake_repo / "src").mkdir()
    (fake_repo / "src" / "app.py").write_text("# not a skill")

    async def fake_exec(*args, **kwargs):
        target = args[5]
        import shutil as _sh
        _sh.copytree(str(fake_repo), target)

        class _Proc:
            returncode = 0
            async def communicate(self):
                return (b"", b"")
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(_call_skill_install("github:fake/weird", "weird"))
    # Error should show top-level content for debugging
    assert "kein erkennbares Skill-Layout" in str(exc_info.value)
    assert "src" in str(exc_info.value)
