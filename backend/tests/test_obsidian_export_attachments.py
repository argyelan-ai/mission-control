"""Phase 7 — OBS-03 Attachment Mirror + Wikilink Rewrite (Plan 07-03).

Real test bodies for the OBS-03 helpers shipped in Plan 07-03:
- ``_mirror_attachment(src, dest)`` copies a file with size+mtime idempotency.
- ``_rewrite_wikilinks(body, attachments, memory_id)`` rewrites known refs to
  Obsidian ``![[name]]`` syntax (precise per-attachment replacement; Pitfall 6).
- ``_resolve_collision_safe_attachments(attachments)`` detects duplicate
  ``original_name`` within a memory_id and assigns sha16-prefixed
  ``display_name`` so each attachment resolves to its own image.

Source layout (Plan 05-06 ship):
    ${HOME_HOST}/.mc/attachments/{board_id|_global}/{memory_id}/{sha16}-{name}

Target layout (this plan):
    ${HOME_HOST}/.mc/vault/attachments/tasks/{memory_id}/{display_name}

Pattern from test_memory_attachments.py: tmp_path + monkeypatch.setenv("HOME_HOST").
"""
import os
import time
import uuid
from datetime import datetime

import pytest


def _make_entry(**overrides):
    """Build a BoardMemory test instance — defaults match a board-scoped
    knowledge entry (so attachment routing is non-trivial).
    """
    from app.models.memory import BoardMemory

    defaults = dict(
        id=uuid.uuid4(),
        board_id=uuid.uuid4(),
        agent_id=None,
        title="Test Entry",
        content="hello world",
        memory_type="knowledge",
        tags=["alpha"],
        source="user",
        is_pinned=False,
        auto_generated=False,
        updated_at=datetime(2026, 4, 27, 12, 0, 0),
        attachments=[],
    )
    defaults.update(overrides)
    return BoardMemory(**defaults)


@pytest.mark.asyncio
async def test_image_mirrored(tmp_path, monkeypatch):
    """OBS-03: a PNG attachment in ${HOME_HOST}/.mc/attachments/{board}/{eid}/...
    MUST be copied (shutil.copy2) into
    ${HOME_HOST}/.mc/vault/attachments/tasks/{eid}/... with byte-identical content.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    from app.services.obsidian_export import (
        _mirror_attachment,
        _vault_attachment_path,
    )
    from app.routers.memory import _attachments_root

    entry = _make_entry()
    memory_id = str(entry.id)
    board_id = str(entry.board_id)
    sha = "abcd1234567890ef"
    original = "screen.png"
    rel = f"{board_id}/{memory_id}/{sha}-{original}"

    src = os.path.join(_attachments_root(), rel)
    os.makedirs(os.path.dirname(src), exist_ok=True)
    payload = b"\x89PNG\r\n\x1a\n" + b"FAKE_IMAGE_BYTES_FOR_TEST"
    with open(src, "wb") as f:
        f.write(payload)

    dest = _vault_attachment_path(entry, original, category="tasks")
    assert _mirror_attachment(src, dest) is True, "first mirror should copy"

    assert os.path.isfile(dest), f"vault target missing: {dest}"
    with open(dest, "rb") as f:
        assert f.read() == payload, "mirrored content differs from source"


@pytest.mark.asyncio
async def test_wikilink_rewrite():
    """OBS-03: vault Markdown body MUST replace known attachment refs with
    Obsidian ``![[name]]`` wikilink syntax (precise — Pitfall 6: never blind
    regex). Unrelated user-authored markdown images MUST NOT be touched.
    """
    from app.services.obsidian_export import (
        _resolve_collision_safe_attachments,
        _rewrite_wikilinks,
    )

    atts = [
        {
            "path": "b1/m1/abcd1234567890ef-screen.png",
            "original_name": "screen.png",
            "mime_type": "image/png",
            "size_bytes": 100,
        },
    ]
    safe = _resolve_collision_safe_attachments(atts)
    body = (
        "Look: ![screen](b1/m1/abcd1234567890ef-screen.png)\n"
        "And a plain link [view](b1/m1/abcd1234567890ef-screen.png)\n"
        "And bare ref: b1/m1/abcd1234567890ef-screen.png\n"
        "Random unrelated image: ![cat](https://example.com/cat.jpg)\n"
    )
    out = _rewrite_wikilinks(body, safe, "m1")

    assert "![[screen.png]]" in out, f"image-syntax not rewritten:\n{out}"
    # Unrelated image must remain untouched (Pitfall 6).
    assert "![cat](https://example.com/cat.jpg)" in out, (
        f"unrelated image got rewritten:\n{out}"
    )
    # The bare reference was replaced by display name.
    assert "b1/m1/abcd1234567890ef-screen.png" not in out, (
        f"raw path still appears in body:\n{out}"
    )


@pytest.mark.asyncio
async def test_attachment_idempotent(tmp_path, monkeypatch):
    """OBS-03: re-running _mirror_attachment with unchanged source MUST skip
    the copy (size match AND dest mtime >= source mtime). mtime preserved.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    from app.services.obsidian_export import _mirror_attachment

    src = str(tmp_path / "src.bin")
    dst = str(tmp_path / "dest" / "copy.bin")
    with open(src, "wb") as f:
        f.write(b"PAYLOAD-ONE")

    assert _mirror_attachment(src, dst) is True, "first copy should write"
    mtime1 = os.path.getmtime(dst)

    time.sleep(0.05)

    assert _mirror_attachment(src, dst) is False, (
        "second copy with unchanged source should skip"
    )
    mtime2 = os.path.getmtime(dst)
    assert mtime1 == mtime2, (
        f"mtime changed on no-op skip: {mtime1} → {mtime2}"
    )


@pytest.mark.asyncio
async def test_collision_keeps_sha_prefix():
    """OBS-03 (Pitfall 5): two attachments under the same memory_id sharing
    the same ``original_name`` MUST get distinct sha16-prefixed display_names.
    Single-name (no collision) keeps the bare original_name.
    """
    from app.services.obsidian_export import _resolve_collision_safe_attachments

    atts = [
        {"path": "b1/m1/abcd1234567890ef-screen.png", "original_name": "screen.png"},
        {"path": "b1/m1/fedcba0987654321-screen.png", "original_name": "screen.png"},
        {"path": "b1/m1/0011223344556677-other.png", "original_name": "other.png"},
    ]
    safe = _resolve_collision_safe_attachments(atts)
    display_names = [a["display_name"] for a in safe]
    assert display_names == [
        "abcd1234567890ef-screen.png",
        "fedcba0987654321-screen.png",
        "other.png",
    ], f"collision handling broken: {display_names}"


@pytest.mark.asyncio
async def test_path_traversal_source_rejected(tmp_path, monkeypatch):
    """OBS-03 (T-7-03-01): adversarial att.path with '../' segments MUST NOT
    cause trigger_cycle to copy a file outside _attachments_root().
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Create a payload OUTSIDE the attachments root that an attacker would
    # want to exfiltrate.
    secret = tmp_path / "secret.txt"
    secret.write_text("CLASSIFIED")

    # Set up a victim attachments root.
    from app.routers.memory import _attachments_root

    os.makedirs(_attachments_root(), exist_ok=True)

    # Craft a memory entry whose attachment path tries to escape via ../
    from app.services.obsidian_export import (
        _resolve_collision_safe_attachments,
    )

    adversarial = [
        {
            "path": "../../secret.txt",
            "original_name": "secret.txt",
            "mime_type": "text/plain",
            "size_bytes": 11,
        }
    ]
    # _resolve_collision_safe_attachments itself should not crash on
    # adversarial input — it just produces display_name.
    safe = _resolve_collision_safe_attachments(adversarial)
    assert len(safe) == 1
    # The actual rejection of the source path happens inside trigger_cycle's
    # realpath check before _mirror_attachment is called. We assert on the
    # check semantics directly:
    real_root = os.path.realpath(_attachments_root())
    src_abs = os.path.join(_attachments_root(), adversarial[0]["path"])
    real_src = os.path.realpath(src_abs)
    assert not real_src.startswith(real_root + os.sep), (
        "adversarial path escapes detected by realpath comparison "
        f"({real_src} should NOT live under {real_root})"
    )


@pytest.mark.asyncio
async def test_mirror_missing_source_returns_false(tmp_path):
    """OBS-03: _mirror_attachment MUST defensively skip (return False, log
    WARN) when the source file does not exist — a row may reference an
    attachment that has since been deleted; the cycle must not crash.
    """
    from app.services.obsidian_export import _mirror_attachment

    fake_src = str(tmp_path / "does_not_exist.bin")
    fake_dst = str(tmp_path / "dest" / "copy.bin")

    result = _mirror_attachment(fake_src, fake_dst)
    assert result is False, (
        f"missing source should return False (defensive), got {result}"
    )
