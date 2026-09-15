"""Worktree-Zeiger-Tests — relativer `gitdir:` (Incident 2026-09-15).

`git worktree add` schreibt einen ABSOLUTEN Zeiger in die `.git`-Datei des
Worktrees — gepraegt vom Mount-Pfad des Erstellers. Backend-Container
(`/Users/Henry/.mc/workspaces/<slug>/...`) und Agenten-Container
(`/workspace/...`) mounten denselben Workspace an unterschiedlichen
Wurzel-Pfaden; ein absoluter Zeiger loest daher nie in beiden Sichten auf
(`fatal: not a git repository` bei jedem git-Kommando im Worktree).

Alle Tests laufen am echten Git-CLI (kein Mock-Nachbau).
"""
import os
import subprocess

import pytest

from app.services.git_service import GitService


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_out(*args: str, cwd: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _read_gitfile(worktree_path: str) -> str:
    with open(os.path.join(worktree_path, ".git"), encoding="utf-8") as f:
        return f.read()


def _make_repo_with_worktree(tmp_path, slug: str) -> tuple[str, str]:
    """Real repo + real worktree via git CLI. Returns (main_repo, worktree)."""
    main_repo = str(tmp_path / "mission-control")
    os.makedirs(main_repo)
    _git("init", "-b", "main", cwd=main_repo)
    _git("config", "user.email", "t@mc.local", cwd=main_repo)
    _git("config", "user.name", "T", cwd=main_repo)
    (os.path.join(main_repo, "README.md"), )
    with open(os.path.join(main_repo, "README.md"), "w") as f:
        f.write("test\n")
    _git("add", "README.md", cwd=main_repo)
    _git("commit", "-m", "init", cwd=main_repo)
    worktree = str(tmp_path / "worktrees" / slug)
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    _git("worktree", "add", "-b", f"task/{slug}", worktree, cwd=main_repo)
    return main_repo, worktree


class TestRelativeWorktreeGitdir:
    @pytest.mark.asyncio
    async def test_setup_git_identity_heals_broken_absolute_pointer(self, tmp_path):
        """Worktree with an absolute gitdir pointing to a FOREIGN mount path:
        setup_git_identity must heal the pointer, not die with
        'fatal: not a git repository' (the four 2026-09-15 incidents)."""
        svc = GitService()
        main_repo, worktree = _make_repo_with_worktree(tmp_path, "heal-me")
        real_target = _read_gitfile(worktree).split(":", 1)[1].strip()
        # Simulate the other mount view: absolute path that does NOT exist here
        broken = "gitdir: /Users/Henry/.mc/workspaces/slug/mission-control/.git/worktrees/heal-me\n"
        with open(os.path.join(worktree, ".git"), "w") as f:
            f.write(broken)
        assert not os.path.isdir(
            broken.split(":", 1)[1].strip()
        ), "fixture broken: foreign path must not resolve locally"

        # Pre-fix this raised: RuntimeError ... fatal: not a git repository
        await svc.setup_git_identity(worktree, "alpha", main_repo=main_repo)

        content = _read_gitfile(worktree)
        assert content.startswith("gitdir: ../../"), content
        assert not os.path.isabs(content.split(":", 1)[1].strip())
        # The healed pointer resolves to the REAL gitdir, not somewhere else
        resolved = os.path.normpath(os.path.join(worktree, content.split(":", 1)[1].strip()))
        assert resolved == os.path.realpath(real_target)
        # And git actually works through it
        assert _git_out("rev-parse", "--git-dir", cwd=worktree).endswith(
            ".git/worktrees/heal-me"
        )

    @pytest.mark.asyncio
    async def test_create_task_worktree_writes_relative_pointer(self, tmp_path):
        """After create_task_worktree the .git pointer is relative
        (string check against the real file git wrote)."""
        svc = GitService()
        main_repo, worktree = _make_repo_with_worktree(tmp_path, "fresh")
        # Remove the CLI-made worktree; create_task_worktree rebuilds it
        _git("worktree", "remove", "--force", worktree, cwd=main_repo)
        _git("branch", "-D", "task/fresh", cwd=main_repo)

        path = await svc.create_task_worktree(main_repo, "fresh")

        assert path == worktree
        content = _read_gitfile(worktree)
        assert content.startswith("gitdir: ../../"), content
        assert not os.path.isabs(content.split(":", 1)[1].strip())
        # git resolves the relative pointer in THIS view already
        assert _git_out("rev-parse", "--git-dir", cwd=worktree).endswith(
            ".git/worktrees/fresh"
        )

    @pytest.mark.asyncio
    async def test_create_task_worktree_reuse_heals_absolute_pointer(self, tmp_path):
        """Existing worktree with an absolute (here: broken) pointer is
        healed on reuse instead of returned broken."""
        svc = GitService()
        main_repo, worktree = _make_repo_with_worktree(tmp_path, "reuse")
        with open(os.path.join(worktree, ".git"), "w") as f:
            f.write(
                "gitdir: /Users/Henry/.mc/workspaces/slug/mission-control/.git/worktrees/reuse\n"
            )

        path = await svc.create_task_worktree(main_repo, "reuse")

        assert path == worktree
        content = _read_gitfile(worktree)
        assert content.startswith("gitdir: ../../"), content
        assert _git_out("rev-parse", "--git-dir", cwd=worktree).endswith(
            ".git/worktrees/reuse"
        )

    @pytest.mark.asyncio
    async def test_relativize_is_idempotent_and_tolerates_full_clone(self, tmp_path):
        """Already-relative pointer: no rewrite. Full clone (.git dir): no-op."""
        svc = GitService()
        main_repo, worktree = _make_repo_with_worktree(tmp_path, "idem")
        assert svc._relativize_worktree_gitdir(worktree, main_repo) is True
        content = _read_gitfile(worktree)
        assert svc._relativize_worktree_gitdir(worktree, main_repo) is False
        assert _read_gitfile(worktree) == content
        # Full clone: .git is a directory — nothing to rewrite
        assert svc._relativize_worktree_gitdir(main_repo, main_repo) is False
