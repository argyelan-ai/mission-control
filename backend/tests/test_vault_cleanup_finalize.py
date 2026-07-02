import pytest
import tarfile
from pathlib import Path
from app.services.vault_cleanup import finalize_cleanup
from app.services.vault_cleanup_state import VaultCleanupState


@pytest.mark.asyncio
async def test_finalize_creates_tarball_and_removes_archive(tmp_path):
    vault = tmp_path / "vault"
    backups = tmp_path / "backups"
    archive = tmp_path / "vault.archive" / "run-X"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "keep.md").write_text("---\nid: keep\n---\nkeep")
    (archive / "memory").mkdir(parents=True)
    (archive / "memory" / "gone.md").write_text("---\nid: gone\n---\nbye")

    state = VaultCleanupState(root=tmp_path / "state")
    state.ensure()

    result = await finalize_cleanup(
        state=state,
        vault_root=vault,
        archive_root=archive,
        backups_root=backups,
        skip_git=True,
    )

    assert result.ok is True
    assert result.tarball_path is not None
    assert result.tarball_path.exists()
    with tarfile.open(result.tarball_path) as tar:
        names = tar.getnames()
    assert any("memory/keep.md" in n for n in names)
    assert not archive.exists()
    assert (vault / "memory" / "keep.md").exists()


@pytest.mark.asyncio
async def test_finalize_is_safe_when_no_archive(tmp_path):
    state = VaultCleanupState(root=tmp_path / "state")
    state.ensure()
    result = await finalize_cleanup(
        state=state,
        vault_root=tmp_path / "vault",
        archive_root=tmp_path / "nonexistent",
        backups_root=tmp_path / "backups",
        skip_git=True,
    )
    assert result.ok is True
    assert result.archive_removed is False
