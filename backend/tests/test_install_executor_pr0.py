"""PR 0 of the extensions concept: the approval -> install path keeps what
the agent had, never overwrites a shared skill, installs one fixed commit and
reports per runtime honestly.

Each test here fails on the code before this change (sabotage-checked).
"""
import asyncio
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.install_log import InstallLog
from app.services.install_executor import InstallExecutor


NEEDS_RESTART = {
    "runtime": "cli-bridge", "harness": "claude",
    "status": "needs_restart", "note": "takes effect at next chat start",
}


async def _agent(session: AsyncSession, **kw) -> Agent:
    agent = Agent(name=kw.pop("name", "Spark"), role="researcher", scopes=[], **kw)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _approval(session: AsyncSession, agent: Agent, action: str, **payload) -> Approval:
    approval = Approval(
        agent_id=agent.id,
        action_type=action,
        description="test",
        payload={
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
            **payload,
        },
        status="approved",
    )
    session.add(approval)
    await session.commit()
    return approval


# ── (1) None = "all" stays None ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,action,install_fn,extra",
    [
        ("cli_skills", "install_skill", "_call_skill_install",
         {"source": "github:obra/skill-x"}),
        ("cli_plugins", "install_plugin", "_call_plugin_install",
         {"source": "claude-plugins-official"}),
        ("mcp_servers", "install_mcp", "_call_mcp_install",
         {"source": "npm:@modelcontextprotocol/server-x"}),
    ],
)
async def test_install_keeps_all_items_agent_at_all(
    async_session: AsyncSession, field, action, install_fn, extra,
):
    """An agent with allowlist None loads everything. Installing one more
    item must not shrink that to [name]."""
    agent = await _agent(async_session, **{field: None})
    approval = await _approval(async_session, agent, action, name="newthing", **extra)

    with patch(f"app.services.install_executor.{install_fn}", new_callable=AsyncMock,
               return_value={"installed_version": "1"}), \
         patch("app.services.install_executor._call_mcp_smoke_test",
               new_callable=AsyncMock, return_value=True), \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock, return_value=NEEDS_RESTART):
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert result.result == "success", result.error
    assert getattr(agent, field) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,action",
    [("cli_skills", "uninstall_skill"), ("cli_plugins", "uninstall_plugin"),
     ("mcp_servers", "uninstall_mcp")],
)
async def test_uninstall_from_all_is_refused_not_emptied(
    async_session: AsyncSession, field, action,
):
    """Removing one item from "all" used to write [] = nothing at all."""
    agent = await _agent(async_session, **{field: None})
    approval = await _approval(async_session, agent, action, name="x")

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock, return_value=NEEDS_RESTART):
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert result.result == "failed"
    assert "all" in result.error
    assert getattr(agent, field) is None


# ── (3) Existing skill name is refused, never overwritten ─────────────────


def _fake_repo(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / f"upstream-{uuid.uuid4().hex[:6]}"
    repo.mkdir()
    (repo / "SKILL.md").write_text(body)
    return repo


def test_existing_skill_in_library_is_refused(tmp_path, monkeypatch):
    from app.services.install_executor import SkillExistsError, _call_skill_install

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    existing = tmp_path / ".mc" / "skills" / "solo"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("ORIGINAL")
    repo = _fake_repo(tmp_path, "FOREIGN")

    async def fake_fetch(url, commit, dest):
        import shutil
        shutil.copytree(repo, dest)
        return "b" * 40

    monkeypatch.setattr("app.services.install_executor._fetch_commit", fake_fetch)

    with pytest.raises(SkillExistsError):
        asyncio.run(_call_skill_install("github:obra/skill-solo", "solo", "b" * 40))
    assert (existing / "SKILL.md").read_text() == "ORIGINAL"


@pytest.mark.asyncio
async def test_refused_name_leaves_agent_untouched(async_session, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    (tmp_path / ".mc" / "skills" / "solo").mkdir(parents=True)
    agent = await _agent(async_session, cli_skills=["a"])
    approval = await _approval(async_session, agent, "install_skill",
                               name="solo", source="github:obra/skill-solo")

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock, return_value=NEEDS_RESTART) as sync:
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert result.result == "failed"
    assert "already exists" in result.error
    assert agent.cli_skills == ["a"]
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_removes_the_skill_dir_it_created(async_session, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    repo = _fake_repo(tmp_path, "---\nname: fresh\n---\n")

    async def fake_fetch(url, commit, dest):
        import shutil
        shutil.copytree(repo, dest)
        return "c" * 40

    monkeypatch.setattr("app.services.install_executor._fetch_commit", fake_fetch)
    agent = await _agent(async_session, cli_skills=["a"])
    approval = await _approval(async_session, agent, "install_skill",
                               name="fresh", source="github:obra/skill-fresh",
                               commit="c" * 40)

    with patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock, side_effect=RuntimeError("sync fail")):
        result = await InstallExecutor(async_session).execute(approval)

    await async_session.refresh(agent)
    assert result.result == "rolled_back"
    assert agent.cli_skills == ["a"]
    assert not (tmp_path / ".mc" / "skills" / "fresh").exists()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "UPPER", "x" * 101])
def test_skill_name_cannot_leave_the_library(tmp_path, monkeypatch, bad):
    from app.services.install_allowlist import AllowlistError
    from app.services.install_executor import _call_skill_install

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    with pytest.raises(AllowlistError):
        asyncio.run(_call_skill_install("github:obra/skill-x", bad, "d" * 40))


# ── (4) Pinned commit ─────────────────────────────────────────────────────


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def two_commit_repo(tmp_path) -> tuple[Path, str, str]:
    repo = tmp_path / "origin-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=repo)
    (repo / "SKILL.md").write_text("---\nname: pin\n---\nOLD\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "old", cwd=repo)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True).stdout.strip()
    (repo / "SKILL.md").write_text("---\nname: pin\n---\nNEW\n")
    _git("commit", "-q", "-am", "new", cwd=repo)
    new = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True).stdout.strip()
    return repo, old, new


def test_install_fetches_exactly_the_approved_commit(tmp_path, monkeypatch, two_commit_repo):
    """Upstream moved on after approval: the approved (older) commit is what
    lands on disk, and its SHA is reported."""
    from app.services.install_executor import _call_skill_install

    repo, old, new = two_commit_repo
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    monkeypatch.setattr("app.services.install_executor.github_url",
                        lambda source: f"file://{repo}")

    info = asyncio.run(_call_skill_install("github:obra/skill-pin", "pin", old))

    installed = tmp_path / ".mc" / "skills" / "pin"
    assert "OLD" in (installed / "SKILL.md").read_text()
    assert info["commit"] == old
    assert not (installed / ".git").exists()


def test_resolve_ref_pins_branch_head_and_passes_full_sha(monkeypatch, two_commit_repo):
    from app.services.install_executor import resolve_skill_commit

    repo, old, new = two_commit_repo
    monkeypatch.setattr("app.services.install_executor.github_url",
                        lambda source: f"file://{repo}")
    assert asyncio.run(resolve_skill_commit("github:obra/skill-pin", None)) == new
    assert asyncio.run(resolve_skill_commit("github:obra/skill-pin", "main")) == new
    assert asyncio.run(resolve_skill_commit("github:obra/skill-pin", old.upper())) == old
    with pytest.raises(RuntimeError):
        asyncio.run(resolve_skill_commit("github:obra/skill-pin", "no-such-ref"))


@pytest.mark.asyncio
async def test_executor_passes_payload_commit_and_logs_it(async_session):
    agent = await _agent(async_session, cli_skills=[])
    sha = "e" * 40
    approval = await _approval(async_session, agent, "install_skill",
                               name="pinned", source="github:obra/skill-pinned",
                               commit=sha)

    with patch("app.services.install_executor._call_skill_install",
               new_callable=AsyncMock,
               return_value={"installed_version": sha[:12], "commit": sha,
                             "created_dir": True}) as inst, \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock, return_value=NEEDS_RESTART):
        result = await InstallExecutor(async_session).execute(approval)

    inst.assert_awaited_once_with("github:obra/skill-pinned", "pinned", sha)
    log = (await async_session.exec(
        select(InstallLog).where(InstallLog.id == result.install_log_id))).one()
    assert log.previous_state["commit"] == sha


# ── (5) Honest per-runtime result is stored ───────────────────────────────


@pytest.mark.asyncio
async def test_per_runtime_result_lands_in_install_log(async_session):
    agent = await _agent(async_session, cli_skills=["a"])
    approval = await _approval(async_session, agent, "install_skill",
                               name="b", source="~/.mc/skills/b")
    not_applied = {"runtime": "host", "harness": None, "status": "not_applied",
                   "note": "host agent config files are not written"}

    with patch("app.services.install_executor._call_skill_install",
               new_callable=AsyncMock,
               return_value={"installed_version": None, "commit": None,
                             "created_dir": False}), \
         patch("app.services.install_executor._trigger_sync_config",
               new_callable=AsyncMock, return_value=not_applied):
        result = await InstallExecutor(async_session).execute(approval)

    assert result.result == "success"
    assert result.apply == not_applied
    log = (await async_session.exec(
        select(InstallLog).where(InstallLog.id == result.install_log_id))).one()
    assert log.previous_state["apply"] == not_applied
    # rollback state is still where it was
    assert log.previous_state["cli_skills"] == ["a"]
