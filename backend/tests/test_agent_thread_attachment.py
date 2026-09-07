"""Agenten hängen Dateien an Thread-Nachrichten (Gruppenchat, 07.09.2026).

Live-Befund (Gruppe „Nav-Pattern", 07.09.): Der Operator bat um Screenshots,
beide Agenten antworteten „technisch nicht möglich" — zu Recht: `mc msg
--vault-path` reist nur über den Chat-Spiegel (Slack/Telegram), und
Gruppen spiegeln nicht (ADR-075); `mc report --photo` braucht einen Task,
den eine Gruppe nicht hat; `~/.mc/references` ist im Container read-only.

Der Weg hier ist derselbe wie beim Operator-Upload im Sessions-Chat (#330):
Multipart hoch, Ablage als AGENTEN-Referenz, absoluter Pfad zurück — den
hängt die CLI als `[Anhang: <pfad>]`-Zeile an die Nachricht, und das
Frontend macht daraus die bekannte Kachel. Kein neues Protokoll.
"""
import os

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.reference_file import ReferenceFile
from tests.test_group_agent_post import _make_group, _make_member


@pytest.fixture
def references_root(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    return tmp_path / ".mc" / "references"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_member_uploads_an_attachment_for_a_group_thread(
    client: AsyncClient, async_session: AsyncSession, references_root
):
    alpha, _ = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/attachment",
        headers=_auth(beta_token),
        files={"file": ("mockup.png", b"\x89PNG-bytes", "image/png")},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "mockup.png"
    assert body["isImage"] is True
    assert body["bytes"] == len(b"\x89PNG-bytes")
    assert body["root"] == "references"
    # Absoluter Pfad auf eine echte Datei — genau dieser String landet als
    # `[Anhang: …]`-Zeile in der Nachricht, und die Kachel im Frontend holt
    # die Bytes über den Files-Endpunkt (root/subpath).
    assert body["path"].startswith(str(references_root))
    assert os.path.isfile(body["path"])
    assert open(body["path"], "rb").read() == b"\x89PNG-bytes"

    rows = (
        await async_session.exec(
            select(ReferenceFile).where(ReferenceFile.agent_id == beta.id)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].uploaded_by == "agent"
    assert body["subpath"] == rows[0].rel_path


@pytest.mark.asyncio
async def test_non_member_gets_404_and_nothing_is_stored(
    client: AsyncClient, async_session: AsyncSession, references_root
):
    """Wie beim Nachrichten-Endpunkt: 404 für „gibt es nicht" UND „nicht
    deiner" — ein Agent darf fremde Gespräche nicht ertasten."""
    alpha, _ = await _make_member(async_session, "Alpha", "alpha")
    _stranger, stranger_token = await _make_member(async_session, "Gamma", "gamma")
    _group, thread = await _make_group(async_session, [alpha])

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/attachment",
        headers=_auth(stranger_token),
        files={"file": ("mockup.png", b"bytes", "image/png")},
    )

    assert resp.status_code == 404, resp.text
    rows = (await async_session.exec(select(ReferenceFile))).all()
    assert rows == []
    assert not references_root.exists() or not any(references_root.rglob("*"))


@pytest.mark.asyncio
async def test_empty_file_is_rejected(
    client: AsyncClient, async_session: AsyncSession, references_root
):
    alpha, alpha_token = await _make_member(async_session, "Alpha", "alpha")
    _group, thread = await _make_group(async_session, [alpha])

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/attachment",
        headers=_auth(alpha_token),
        files={"file": ("leer.png", b"", "image/png")},
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_cli_multipart_body_is_accepted_by_the_endpoint(
    client: AsyncClient, async_session: AsyncSession, references_root
):
    """Kontrakt CLI ↔ Backend: `mc msg --attach` baut den Multipart-Körper
    von Hand (stdlib, kein requests). Hier geht genau dieser Körper an den
    echten Endpunkt — sonst wäre der CLI-Unit-Test nur selbstkonsistent."""
    import sys
    from pathlib import Path

    mc_cli_path = Path(__file__).resolve().parents[2] / "scripts" / "mc-cli"
    if str(mc_cli_path) not in sys.path:
        sys.path.insert(0, str(mc_cli_path))
    from mc_cli.client import encode_multipart

    alpha, _ = await _make_member(async_session, "Alpha", "alpha")
    beta, beta_token = await _make_member(async_session, "Beta", "beta")
    _group, thread = await _make_group(async_session, [alpha, beta])

    content_type, body = encode_multipart("file", "shot.png", b"\x89PNG-cli", "image/png")
    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/attachment",
        headers={**_auth(beta_token), "Content-Type": content_type},
        content=body,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "shot.png"
    assert data["bytes"] == len(b"\x89PNG-cli")
    with open(data["path"], "rb") as fh:
        assert fh.read() == b"\x89PNG-cli"
