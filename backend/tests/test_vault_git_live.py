"""Tests for VaultGit — real git operations with 30s batching."""
import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.vault_git import VaultGit


@pytest.fixture
def git_vault(tmp_path):
    """Create a tmp dir with git init."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@mc.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    # Initial commit so HEAD exists
    (tmp_path / ".gitignore").write_text("_inbox/\n_rejected/\n_conflicts/\n_trash/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    return tmp_path


class TestVaultGitStage:
    def test_stage_adds_file_to_git_index(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)
        note = git_vault / "agents" / "researcher" / "lessons" / "test.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# Test note")

        vg.stage(note)

        result = subprocess.run(
            ["git", "-C", str(git_vault), "diff", "--cached", "--name-only"],
            capture_output=True, text=True,
        )
        assert "agents/researcher/lessons/test.md" in result.stdout

    def test_stage_rejects_path_outside_vault(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)
        # Use a completely separate directory (not a child of git_vault)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            outside = Path(td) / "evil.md"
            outside.write_text("evil")

            with pytest.raises(ValueError, match="outside vault"):
                vg.stage(outside)

    def test_stage_relative_path(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)
        note = git_vault / "test-relative.md"
        note.write_text("relative path test")

        vg.stage(Path("test-relative.md"))

        result = subprocess.run(
            ["git", "-C", str(git_vault), "diff", "--cached", "--name-only"],
            capture_output=True, text=True,
        )
        assert "test-relative.md" in result.stdout


class TestVaultGitCommitBatched:
    def test_commit_batched_creates_git_commit(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)
        note = git_vault / "test-commit.md"
        note.write_text("# Commit test")

        vg.stage(note)
        committed = vg.commit_batched("researcher", "compacted 1 note")

        assert committed is True
        result = subprocess.run(
            ["git", "-C", str(git_vault), "log", "--oneline", "-1"],
            capture_output=True, text=True,
        )
        assert "vault:" in result.stdout
        assert "researcher" in result.stdout

    def test_commit_batched_no_staged_files_returns_false(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)

        committed = vg.commit_batched("system", "nothing to commit")

        assert committed is False

    def test_commit_batched_uses_vault_author(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)
        note = git_vault / "author-test.md"
        note.write_text("# Author test")
        vg.stage(note)
        vg.commit_batched("sparky", "test author")

        result = subprocess.run(
            ["git", "-C", str(git_vault), "log", "--format=%an <%ae>", "-1"],
            capture_output=True, text=True,
        )
        assert "MC Vault" in result.stdout
        assert "vault@mc.local" in result.stdout


class TestVaultGitAutoInit:
    def test_ensure_git_inits_when_no_dotgit(self, tmp_path):
        """VaultGit.ensure_git() creates .git/ if it doesn't exist."""
        vg = VaultGit(tmp_path, stub_mode=False)
        vg.ensure_git()

        assert (tmp_path / ".git").exists()

    def test_ensure_git_idempotent_when_already_init(self, git_vault):
        """ensure_git() on an already-initialized vault is a no-op."""
        vg = VaultGit(git_vault, stub_mode=False)
        vg.ensure_git()

        assert (git_vault / ".git").exists()


class TestVaultGitBatchTimer:
    def test_pending_changes_accumulate(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)
        n1 = git_vault / "note1.md"
        n2 = git_vault / "note2.md"
        n1.write_text("one")
        n2.write_text("two")

        vg.stage(n1)
        vg.stage(n2)

        assert len(vg._staged) == 2

    def test_commit_batched_clears_staged(self, git_vault):
        vg = VaultGit(git_vault, stub_mode=False)
        note = git_vault / "clear-test.md"
        note.write_text("clear")
        vg.stage(note)

        vg.commit_batched("system", "test")

        assert len(vg._staged) == 0
