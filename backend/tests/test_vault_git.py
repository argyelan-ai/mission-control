import logging
import pytest
from pathlib import Path
from app.services.vault_git import VaultGit


@pytest.fixture
def git(tmp_path):
    return VaultGit(vault_path=tmp_path, stub_mode=True)


def test_stage_logs_in_stub_mode(git, caplog):
    with caplog.at_level(logging.INFO):
        git.stage(Path("agents/sparky/lessons/x.md"))
    assert "STUB stage: agents/sparky/lessons/x.md" in caplog.text


def test_commit_batched_noop_in_stub_mode(git, caplog):
    git.stage(Path("a.md"))
    git.stage(Path("b.md"))
    with caplog.at_level(logging.INFO):
        commit_made = git.commit_batched(author_slug="sparky", message_hint="lesson")
    assert commit_made is False  # stub mode never actually commits
    assert "STUB commit_batched" in caplog.text


def test_stage_outside_vault_raises(git, tmp_path):
    outside = tmp_path.parent / "outside.md"
    with pytest.raises(ValueError, match="outside vault"):
        git.stage(outside)
