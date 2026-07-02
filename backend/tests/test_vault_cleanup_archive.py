import pytest
import frontmatter
from pathlib import Path
from app.services.vault_cleanup import soft_archive_note


def test_soft_archive_moves_file_under_archive_root(tmp_path):
    vault = tmp_path / "vault"
    archive_root = tmp_path / "vault.archive" / "run-A"
    (vault / "memory" / "global").mkdir(parents=True)
    src = vault / "memory" / "global" / "abc.md"
    src.write_text("---\nagent: system\ntype: journal\ntags: [auto]\n---\nbody")

    result = soft_archive_note(
        vault_root=vault,
        archive_root=archive_root,
        rel_path="memory/global/abc.md",
        bucket="H1",
    )
    assert result.ok is True
    dst = archive_root / "memory" / "global" / "abc.md"
    assert dst.exists()
    assert not src.exists()
    body = dst.read_text()
    assert "archived_at:" in body
    assert "archive_bucket: H1" in body


def test_soft_archive_is_idempotent(tmp_path):
    vault = tmp_path / "vault"
    archive_root = tmp_path / "vault.archive" / "run-A"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "x.md").write_text("---\nagent: system\n---\nbody")
    r1 = soft_archive_note(vault, archive_root, "memory/x.md", "H1")
    assert r1.ok is True
    r2 = soft_archive_note(vault, archive_root, "memory/x.md", "H1")
    assert r2.ok is True
    assert r2.already_archived is True


def test_soft_archive_records_correct_bucket_reason(tmp_path):
    vault = tmp_path / "vault"
    archive_root = tmp_path / "vault.archive" / "run-A"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "y.md").write_text("---\nagent: tester\n---\nbody")
    soft_archive_note(vault, archive_root, "memory/y.md", "H3")
    dst = archive_root / "memory" / "y.md"
    fm = frontmatter.load(dst)
    assert fm.metadata["archive_bucket"] == "H3"
    assert fm.metadata["archive_reason"] == "test_or_failed"


def test_soft_archive_returns_error_when_source_missing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    archive_root = tmp_path / "archive"
    result = soft_archive_note(vault, archive_root, "memory/missing.md", "H1")
    assert result.ok is False
    assert "not found" in (result.error or "").lower()
