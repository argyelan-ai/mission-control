"""Phase 4 — GET /vault/attachment/{deliverable_id} serves wrapper binaries.

The frontend Reading-Panel needs to render PDFs/images inline; agents
without the docker bind-mount need an HTTP path to the binary. Both
admin (Role.ADMIN) and agent-scoped (vault:read) variants exist; both
resolve deliverable_id → on-disk file under vault/attachments/ and
return FileResponse with the wrapper's attachment_mime.

Path-traversal defenses are inherited from the resolver — there's no
caller-supplied filename anywhere.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import frontmatter as fm_lib
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.models.agent import Agent
from app.scopes import Scope
from tests.conftest import test_engine


# ── App + fixtures ────────────────────────────────────────────────────────────


def _make_app(vault_index) -> FastAPI:
    from app.database import get_session
    from app.routers.vault import agent_router, router

    fa = FastAPI()
    fa.include_router(router)
    fa.include_router(agent_router)
    fa.state.vault_index = vault_index

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    fa.dependency_overrides[get_session] = override_get_session
    return fa


@pytest.fixture
def vault_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "vault"
    p.mkdir()
    for kind in ("files", "images", "audio"):
        (p / "attachments" / kind).mkdir(parents=True)
    monkeypatch.setattr(app.config.settings, "vault_path", p)
    return p


@pytest.fixture
def vault_index(vault_path: Path):
    from app.services.vault_index import VaultIndex
    idx = VaultIndex(db_path=vault_path / ".mc_index.db", vault_path=vault_path)
    yield idx
    idx.close()


def _make_wrapper_with_pdf(vault_path: Path, *, pdf_size: int = 200) -> tuple[str, Path]:
    """Build a wrapper + hardlinked PDF. Returns (deliverable_id, pdf_abs_path)."""
    pdf_id = "abcd0001-0000-0000-0000-000000000001"
    pdf_abs = vault_path / "attachments" / "files" / f"{pdf_id}.pdf"
    pdf_abs.write_bytes(b"%PDF-1.4 " + b"X" * (pdf_size - 9))

    wrapper_abs = (
        vault_path / "agents" / "researcher" / "deliverables" / f"weather-{pdf_id}.md"
    )
    wrapper_abs.parent.mkdir(parents=True)
    rel = f"../../../attachments/files/{pdf_id}.pdf"
    post = fm_lib.Post(
        f"# Weather\n\n![[{rel}]]\n",
        id=f"deliverable-{pdf_id}",
        title="Weather Report",
        agent="researcher",
        type="deliverable",
        deliverable_kind="file",
        deliverable_id=pdf_id,
        date="2026-05-15T13:00:00+00:00",
        attachment_path=rel,
        attachment_mime="application/pdf",
        attachment_size=pdf_size,
    )
    wrapper_abs.write_text(fm_lib.dumps(post))
    return pdf_id, pdf_abs


def _make_image_attachment(vault_path: Path) -> str:
    """Image attachment without a wrapper — mime falls back to octet-stream."""
    img_id = "1234abcd-0000-0000-0000-000000000002"
    img_abs = vault_path / "attachments" / "images" / f"{img_id}.png"
    # Minimal valid PNG: 8-byte signature + IHDR + IEND.
    img_abs.write_bytes(b"\x89PNG\r\n\x1a\n" + b"X" * 50)
    return img_id


@pytest.fixture
async def admin_client(vault_index, vault_path):
    from app.auth import create_access_token

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    from app.models.user import User
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email="admin@test.local", name="Admin", role="admin", is_active=True))
        await s.commit()

    fa = _make_app(vault_index)
    transport = ASGITransport(app=fa)
    token = create_access_token(str(user_id), "admin")
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac, vault_path


@pytest.fixture
async def agent_vault_read_client(vault_index, vault_path):
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(
            id=uuid.uuid4(),
            name="VaultReader",
            role="developer",
            agent_token_hash=token_hash,
            scopes=[Scope.VAULT_READ.value],
            provision_status="provisioned",
        ))
        await s.commit()

    fa = _make_app(vault_index)
    transport = ASGITransport(app=fa)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {raw_token}"},
    ) as ac:
        yield ac, vault_path


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_fetch_pdf_with_correct_mime(admin_client):
    client, vault_path = admin_client
    pdf_id, pdf_abs = _make_wrapper_with_pdf(vault_path)
    r = await client.get(f"/api/v1/vault/attachment/{pdf_id}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content == pdf_abs.read_bytes()


@pytest.mark.asyncio
async def test_admin_can_fetch_image_without_wrapper(admin_client):
    """Attachment with no matching wrapper → mime falls back to octet-stream."""
    client, vault_path = admin_client
    img_id = _make_image_attachment(vault_path)
    r = await client.get(f"/api/v1/vault/attachment/{img_id}")
    assert r.status_code == 200, r.text
    # No wrapper means no mime hint; FileResponse uses octet-stream.
    assert r.headers["content-type"].startswith(("application/octet-stream", "image/"))


@pytest.mark.asyncio
async def test_admin_404_for_unknown_deliverable(admin_client):
    client, _ = admin_client
    r = await client.get(f"/api/v1/vault/attachment/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_agent_with_vault_read_can_fetch(agent_vault_read_client):
    client, vault_path = agent_vault_read_client
    pdf_id, _ = _make_wrapper_with_pdf(vault_path)
    r = await client.get(f"/api/v1/agent/vault/attachment/{pdf_id}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")


@pytest.mark.asyncio
async def test_agent_without_scope_gets_403(vault_index, vault_path):
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(
            id=uuid.uuid4(),
            name="NoScope",
            role="developer",
            agent_token_hash=token_hash,
            scopes=[Scope.CHAT_WRITE.value],  # no vault:read
            provision_status="provisioned",
        ))
        await s.commit()

    pdf_id, _ = _make_wrapper_with_pdf(vault_path)
    fa = _make_app(vault_index)
    transport = ASGITransport(app=fa)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {raw_token}"},
    ) as ac:
        r = await ac.get(f"/api/v1/agent/vault/attachment/{pdf_id}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_rejects_path_traversal_via_id(admin_client):
    """deliverable_id containing path metacharacters cannot escape the dirs.

    The id is interpolated into a glob pattern (``{id}.*``) inside known
    kind-dirs; a traversal attempt either matches nothing (→404) or stays
    contained because we re-resolve and check relative_to(attachments_root).
    """
    client, _ = admin_client
    # FastAPI itself rejects "/" in path components → 404 before our code runs.
    r = await client.get("/api/v1/vault/attachment/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (404, 422), r.text
