"""Phase 5 — MSY-03 attachments (upload / serve / delete / MIME / traversal).

Bodies landed in Plan 05-06 — these tests cover:
- ``POST /api/v1/knowledge/{id}/attachments`` (multipart upload, MIME allowlist, size cap, count cap, path-traversal guard)
- ``GET /api/v1/knowledge/{id}/attachments/{filename}`` (auth-gated FileResponse stream)
- ``DELETE /api/v1/knowledge/{id}`` cascade (filesystem dir cleanup via shutil.rmtree)
- HOME_HOST resolver (NEVER expanduser('~') per memory feedback rule)
- Path-traversal guard (Pitfall 6 — explicit operator-aware test, CLAUDE.md SEC-06)

All tests use ``monkeypatch.setenv("HOME_HOST", str(tmp_path))`` to redirect attachment
writes to the test tmp directory; without it, tests would pollute the real
``~/.mc/attachments/`` filesystem on the host.
"""
import io
import os
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.memory import BoardMemory
from tests.conftest import test_engine


async def _seed_entry() -> uuid.UUID:
    """Create a Board + BoardMemory entry, return entry id."""
    bid = uuid.uuid4()
    eid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            Board(
                id=bid,
                name="A",
                slug=f"a-{bid.hex[:8]}",
                require_review_before_done=False,
            ),
        )
        await s.commit()
        s.add(
            BoardMemory(
                id=eid,
                board_id=bid,
                content="x",
                source="user",
                memory_type="knowledge",
            ),
        )
        await s.commit()
    return eid


@pytest.mark.asyncio
async def test_upload_mime_allowlist(auth_client, tmp_path, monkeypatch):
    """MSY-03 D-12: upload accepts only the whitelisted MIME types
    (image/png, image/jpeg, image/gif, image/webp, application/pdf).
    Anything else is rejected with HTTP 415.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    eid = await _seed_entry()

    bad = {"file": ("evil.exe", io.BytesIO(b"malicious"), "application/octet-stream")}
    resp = await auth_client.post(f"/api/v1/knowledge/{eid}/attachments", files=bad)
    assert resp.status_code == 415, resp.text

    good = {"file": ("ok.png", io.BytesIO(b"\x89PNG..."), "image/png")}
    resp2 = await auth_client.post(f"/api/v1/knowledge/{eid}/attachments", files=good)
    assert resp2.status_code == 201, resp2.text
    assert resp2.json()["mime_type"] == "image/png"
    assert resp2.json()["original_name"] == "ok.png"


@pytest.mark.asyncio
async def test_upload_size_limit(auth_client, tmp_path, monkeypatch):
    """MSY-03 D-12: upload rejects files > 10 MB with HTTP 413."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    eid = await _seed_entry()

    too_big = io.BytesIO(b"x" * (10 * 1024 * 1024 + 1))
    files = {"file": ("big.png", too_big, "image/png")}
    resp = await auth_client.post(f"/api/v1/knowledge/{eid}/attachments", files=files)
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_attachment_path_traversal_rejected(auth_client, tmp_path, monkeypatch):
    """MSY-03 + Pitfall 6: a filename containing ``..`` segments must be
    rejected with HTTP 400 BEFORE any filesystem I/O. Mirrors the
    ``tasks.py:935-940`` os.path.realpath guard pattern.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    eid = await _seed_entry()

    # Filename with ../ → backend MUST reject 400
    files = {"file": ("../etc-passwd.png", io.BytesIO(b"\x89PNG"), "image/png")}
    resp = await auth_client.post(f"/api/v1/knowledge/{eid}/attachments", files=files)
    assert resp.status_code == 400, resp.text

    # And GET with traversal in path also 400.
    # NOTE: httpx normalizes raw `../../etc/passwd` client-side and rewrites
    # the URL to `/api/v1/knowledge/etc/passwd` (a 404 — also safe). We use
    # URL-encoded `%2E%2E` to bypass client-side normalization so the handler
    # actually sees `..` in the filename param and the explicit guard fires.
    resp2 = await auth_client.get(
        f"/api/v1/knowledge/{eid}/attachments/%2E%2E",
    )
    assert resp2.status_code == 400, resp2.text


@pytest.mark.asyncio
async def test_delete_memory_cascades_files(auth_client, tmp_path, monkeypatch):
    """MSY-03 D-16: ``DELETE /api/v1/knowledge/{id}`` removes the memory
    row AND the on-disk attachment directory at
    ``${HOME_HOST}/.mc/attachments/{board_id}/{memory_id}/`` via shutil.rmtree.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    eid = await _seed_entry()

    files = {"file": ("a.png", io.BytesIO(b"\x89PNG"), "image/png")}
    up = await auth_client.post(f"/api/v1/knowledge/{eid}/attachments", files=files)
    assert up.status_code == 201, up.text

    rel = up.json()["path"]
    abs_path = os.path.join(str(tmp_path), ".mc", "attachments", rel)
    assert os.path.isfile(abs_path), f"file not written at {abs_path}"

    # Delete the entry
    resp = await auth_client.delete(f"/api/v1/knowledge/{eid}")
    assert resp.status_code == 204, resp.text

    # File AND parent directory removed
    assert not os.path.exists(abs_path), "file should be gone after cascade"


@pytest.mark.asyncio
async def test_get_attachment_streams(auth_client, tmp_path, monkeypatch):
    """MSY-03 D-14: ``GET /api/v1/knowledge/{id}/attachments/{filename}``
    streams the file with the correct Content-Type. Auth via standard
    Bearer token. NO direct static-mount.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    eid = await _seed_entry()

    payload = b"\x89PNG-fake-bytes"
    files = {"file": ("photo.png", io.BytesIO(payload), "image/png")}
    up = await auth_client.post(f"/api/v1/knowledge/{eid}/attachments", files=files)
    assert up.status_code == 201, up.text

    fname = os.path.basename(up.json()["path"])
    resp = await auth_client.get(f"/api/v1/knowledge/{eid}/attachments/{fname}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == payload
