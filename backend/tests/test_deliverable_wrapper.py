"""Tests for the Phase A vault-as-brain wrapper sync service.

Covers:
- File deliverable: hardlink + wrapper write, frontmatter shape, atomic write.
- Markdown document deliverable: inline content embedded in wrapper body.
- URL deliverable: source_url in frontmatter, link in body, no attachment.
- Screenshot deliverable: image attachment in attachments/images/.
- Idempotency: re-sync skips when wrapper exists.
- Force: re-sync overrides.
- Missing source file: graceful skip with reason.
- Hardlink fallback to copy when cross-FS.
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import frontmatter as fm_lib
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.models.deliverable import TaskDeliverable
from app.services.deliverable_wrapper import (
    SyncResult,
    hardlink_or_copy,
    sync_deliverable_to_vault,
    wrapper_relpath,
)
from tests.conftest import test_engine


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault_root(tmp_path: Path, monkeypatch) -> Path:
    """Tmp vault with the attachment subtree pre-created (mirrors lifespan)."""
    root = tmp_path / "vault"
    root.mkdir()
    for kind in ("files", "images", "audio"):
        (root / "attachments" / kind).mkdir(parents=True)
    monkeypatch.setattr(app.config.settings, "vault_path", root)
    return root


@pytest.fixture
def deliverables_root(tmp_path: Path, monkeypatch) -> Path:
    """Tmp dir that stands in for ~/.mc/deliverables. We point HOME_HOST at
    the same tmp tree so resolve_deliverable_fs_path can find sources."""
    d = tmp_path / "mc"
    (d / "deliverables").mkdir(parents=True)
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    return d / "deliverables"


@pytest.fixture
async def session():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


def _mk_deliverable(
    deliverable_type: str = "file",
    title: str = "Test Deliverable",
    path: str | None = None,
    description: str | None = None,
    content: str | None = None,
    agent_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    tags: list[str] | None = None,
) -> TaskDeliverable:
    return TaskDeliverable(
        id=uuid.uuid4(),
        task_id=task_id or uuid.uuid4(),
        agent_id=agent_id,
        deliverable_type=deliverable_type,
        title=title,
        path=path,
        description=description,
        content=content,
        tags=tags,
        created_at=datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_deliverable_inlines_content(vault_root, session):
    deliverable = _mk_deliverable(
        deliverable_type="document",
        title="Research Result Brazilian Food",
        description="Quick summary.",
        content="## Findings\n\nLine 1\nLine 2",
    )

    res = await sync_deliverable_to_vault(deliverable, session)

    assert res.error is None
    assert not res.skipped
    assert res.wrapper_path is not None
    assert res.wrapper_path.exists()

    post = fm_lib.load(str(res.wrapper_path))
    assert post["type"] == "deliverable"
    assert post["deliverable_kind"] == "document"
    assert post["title"] == "Research Result Brazilian Food"
    assert "## Content" in post.content
    assert "Line 1" in post.content
    # No attachment for document kind
    assert "attachment_path" not in post.metadata


@pytest.mark.asyncio
async def test_url_deliverable_carries_source_url(vault_root, session):
    deliverable = _mk_deliverable(
        deliverable_type="url",
        title="GitHub Repo",
        path="https://github.com/test-owner/mc-workspace",
    )

    res = await sync_deliverable_to_vault(deliverable, session)

    post = fm_lib.load(str(res.wrapper_path))
    assert post["source_url"] == "https://github.com/test-owner/mc-workspace"
    assert "github.com/test-owner" in post.content
    assert "attachment_path" not in post.metadata


@pytest.mark.asyncio
async def test_file_deliverable_hardlinks_into_attachments(vault_root, deliverables_root, session):
    # Stage the source PDF + use absolute legacy path — the resolver's
    # fallback branch returns it as-is when it exists, which avoids the
    # docker-mount translation that wouldn't work in the test environment.
    task_id = uuid.uuid4()
    src = deliverables_root / str(task_id) / "report.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-1.4 fake pdf bytes")

    deliverable = _mk_deliverable(
        deliverable_type="file",
        title="Quarterly Report",
        path=str(src),  # absolute path — hits the resolver's legacy branch
        task_id=task_id,
    )

    res = await sync_deliverable_to_vault(deliverable, session)

    assert res.error is None
    assert res.attachment_path is not None
    assert res.attachment_path.exists()
    assert res.attachment_path.parent == vault_root / "attachments" / "files"

    # Hardlink semantics: same inode (only true on same FS — fine for tmp_path)
    assert os.stat(src).st_ino == os.stat(res.attachment_path).st_ino

    post = fm_lib.load(str(res.wrapper_path))
    assert post["attachment_mime"] == "application/pdf"
    assert post["attachment_size"] == src.stat().st_size
    assert "../../../attachments/files/" in post["attachment_path"]
    assert "![[../../../attachments/files/" in post.content
    # Auto-extracted slot reserved for Phase B.1 PDF text extraction
    assert "## Auto-extracted" in post.content


@pytest.mark.asyncio
async def test_screenshot_lands_in_images_subdir(vault_root, deliverables_root, session):
    task_id = uuid.uuid4()
    src = deliverables_root / str(task_id) / "screenshot.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\x89PNG\r\n\x1a\nfake png bytes")

    deliverable = _mk_deliverable(
        deliverable_type="screenshot",
        title="Test Run Failure",
        path=str(src),  # absolute → resolver's legacy branch
        task_id=task_id,
    )

    res = await sync_deliverable_to_vault(deliverable, session)

    assert res.attachment_path is not None
    assert res.attachment_path.parent == vault_root / "attachments" / "images"
    assert res.attachment_path.suffix == ".png"


@pytest.mark.asyncio
async def test_idempotent_resync_skips(vault_root, session):
    deliverable = _mk_deliverable(
        deliverable_type="document",
        title="Once",
        content="Body",
    )

    first = await sync_deliverable_to_vault(deliverable, session)
    assert not first.skipped

    second = await sync_deliverable_to_vault(deliverable, session)
    assert second.skipped is True
    assert second.reason == "already-exists"
    assert second.wrapper_path == first.wrapper_path


@pytest.mark.asyncio
async def test_force_resync_overwrites(vault_root, session):
    deliverable = _mk_deliverable(
        deliverable_type="document",
        title="Force Test",
        content="Original body",
    )

    await sync_deliverable_to_vault(deliverable, session)

    # Mutate the deliverable's content + force re-sync
    deliverable.content = "Updated body"
    res = await sync_deliverable_to_vault(deliverable, session, force=True)
    assert not res.skipped

    post = fm_lib.load(str(res.wrapper_path))
    assert "Updated body" in post.content


@pytest.mark.asyncio
async def test_missing_source_file_skips_gracefully(vault_root, session):
    deliverable = _mk_deliverable(
        deliverable_type="file",
        title="Ghost File",
        path="~/.mc/deliverables/nonexistent-task/ghost.pdf",
    )

    res = await sync_deliverable_to_vault(deliverable, session)

    assert res.skipped
    assert res.reason and res.reason.startswith("source-missing:")
    assert res.error is None


@pytest.mark.asyncio
async def test_hardlink_or_copy_falls_back_to_copy(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    dst = tmp_path / "out" / "dst.bin"

    with patch("os.link", side_effect=OSError("EXDEV cross-filesystem")):
        mode = hardlink_or_copy(src, dst)

    assert mode == "copy"
    assert dst.exists()
    assert dst.read_bytes() == b"hello"


def test_wrapper_relpath_is_deterministic():
    deliverable = _mk_deliverable(title="My Awesome Note!")
    deliverable.id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    rel = wrapper_relpath(deliverable, agent_slug="researcher")
    assert rel == (
        "agents/researcher/deliverables/"
        "my-awesome-note-11111111-2222-3333-4444-555555555555.md"
    )
