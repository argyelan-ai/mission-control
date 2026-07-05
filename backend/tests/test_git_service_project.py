"""Tests for GitService project methods (subprocess mocked)."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.git_service import GitService

GITHUB_OWNER = "acme"  # nur fuer Mock-URLs — echter Owner kommt aus github_config (ADR-055)


@pytest.fixture
def git():
    return GitService()


@pytest.mark.asyncio
async def test_create_project_repo_uses_mc_prefix(git):
    """create_project_repo creates repo with mc-{slug} naming."""
    with patch.object(git, "create_repo", new_callable=AsyncMock) as mock_create, \
         patch.object(git, "init_repo_files_with_briefing", new_callable=AsyncMock) as mock_init:
        mock_create.return_value = f"https://github.com/{GITHUB_OWNER}/mc-argyelan-redesign.git"
        result = await git.create_project_repo("argyelan-redesign", "Argyelan Redesign")

    mock_create.assert_called_once_with(
        "mc-argyelan-redesign", "Argyelan Redesign"
    )
    mock_init.assert_called_once_with("mc-argyelan-redesign", "argyelan-redesign")
    assert "mc-argyelan-redesign" in result


@pytest.mark.asyncio
async def test_create_project_repo_slugifies(git):
    """Spaces and special characters in the name become hyphens."""
    with patch.object(git, "create_repo", new_callable=AsyncMock) as mock_create, \
         patch.object(git, "init_repo_files_with_briefing", new_callable=AsyncMock):
        mock_create.return_value = f"https://github.com/{GITHUB_OWNER}/mc-my-project.git"
        result = await git.create_project_repo("My Project!", "desc")

    mock_create.assert_called_once_with("mc-my-project", "desc")


@pytest.mark.asyncio
async def test_create_phase_branch_naming(git):
    """create_phase_branch creates phase/{slug} branch."""
    with patch.object(git, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = ""
        result = await git.create_phase_branch("/tmp/project", "research")

    assert result == "phase/research"
    mock_cmd.assert_any_call("git", "checkout", "-b", "phase/research", cwd="/tmp/project")


@pytest.mark.asyncio
async def test_commit_deliverable_writes_file(git, tmp_path):
    """commit_deliverable writes file and creates commit."""
    project_dir = str(tmp_path)
    phase_dir = tmp_path / "phases" / "research" / "deliverables"

    with patch.object(git, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = "abc1234"
        commit_hash = await git.commit_deliverable(
            project_dir=project_dir,
            phase_slug="research",
            filename="competitor-analysis.md",
            content="# Competitor Analysis\n\nContent here.",
            task_id="task-123",
            title="Competitor Analysis",
        )

    # File was written
    written_file = tmp_path / "phases" / "research" / "deliverables" / "competitor-analysis.md"
    assert written_file.exists()
    assert "Competitor Analysis" in written_file.read_text()

    # git add + commit was called
    add_calls = [c for c in mock_cmd.call_args_list if "add" in c.args]
    commit_calls = [c for c in mock_cmd.call_args_list if "commit" in c.args]
    assert len(add_calls) >= 1
    assert len(commit_calls) >= 1


@pytest.mark.asyncio
async def test_create_phase_pr_uses_correct_base(git):
    """create_phase_pr opens PR from phase/{slug} to main."""
    with patch.object(git, "create_pr", new_callable=AsyncMock) as mock_pr, \
         patch.object(git, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_pr.return_value = f"https://github.com/{GITHUB_OWNER}/mc-project/pull/1"
        mock_cmd.return_value = ""
        result = await git.create_phase_pr(
            project_dir="/tmp/project",
            phase_slug="research",
            title="Phase: Research Complete",
        )

    assert mock_pr.called
    call_kwargs = mock_pr.call_args
    # base="main" must be passed
    assert call_kwargs.kwargs.get("base") == "main" or (len(call_kwargs.args) >= 4 and call_kwargs.args[3] == "main")
    assert "github.com" in result


@pytest.mark.asyncio
async def test_create_git_tag_pushes(git):
    """create_git_tag creates and pushes tag."""
    with patch.object(git, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = ""
        await git.create_git_tag("/tmp/project", "project/argyelan/phase-1-done")

    all_args = [list(c.args) for c in mock_cmd.call_args_list]
    assert any("tag" in args for args in all_args)
    assert any("push" in args for args in all_args)


@pytest.mark.asyncio
async def test_get_resume_briefing_returns_string(git):
    """get_resume_briefing reads git log and returns a summary."""
    fake_log = "abc1234 deliverable: Font Research [task/abc12345]\ndef5678 deliverable: Colors [task/def56789]"
    with patch.object(git, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = fake_log
        result = await git.get_resume_briefing("/tmp/project")

    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_get_task_git_info_uses_branch_name(tmp_path):
    """get_task_git_info passes branch_name to git log when provided."""
    from app.services.git_service import GitService
    service = GitService.__new__(GitService)

    calls = []

    async def fake_run(*args, cwd=None):
        calls.append(list(args))
        cmd = list(args)
        if "branch" in cmd and "--show-current" in cmd:
            return "main"
        if "log" in cmd:
            return "abc1234\x1ffix: something\x1fAuthor\x1f2 hours ago"
        if "status" in cmd:
            return ""
        if "rev-list" in cmd:
            return "0"
        if "gh" in cmd:
            raise Exception("no PR")
        return ""

    service._run_cmd = fake_run

    result = await service.get_task_git_info("/fake/path", branch_name="task/my-branch")

    # Find the git log call and check it includes the branch name
    log_calls = [c for c in calls if "log" in c]
    assert len(log_calls) == 1
    assert "task/my-branch" in log_calls[0]
