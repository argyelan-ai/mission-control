"""Gruppen-API (Gruppenchat V1, PR A) — CRUD, Mitglieder, Nachrichten, Dokument.

Fehler-Vokabular: 422 = ungültige Eingabe (GroupValidationError),
409 = Mitglied nicht gruppenfähig (comm_v2 fehlt — Spiegel von
input_not_supported im Sessions-Chat), 404 = gibt es nicht.
"""
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent


@pytest.fixture(autouse=True)
def _references_in_tmp(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.services.reference_ingest.references_root", lambda: str(tmp_path)
    )
    return tmp_path


async def _make_agent(
    session: AsyncSession, name: str, *, comm_v2: bool = True, archived: bool = False
) -> Agent:
    import datetime as dt

    agent = Agent(
        name=name,
        slug=name.lower(),
        agent_runtime="cli-bridge",
        comm_v2=comm_v2,
        archived_at=dt.datetime.now(tz=dt.timezone.utc) if archived else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _create_group(
    auth_client: AsyncClient, member_ids: list[uuid.UUID], **overrides
) -> dict:
    payload = {
        "goal": "DFlash2 vs vLLM entscheiden",
        "member_ids": [str(i) for i in member_ids],
        **overrides,
    }
    resp = await auth_client.post("/api/v1/groups", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_create_and_list_groups(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")

    body = await _create_group(auth_client, [a.id, b.id], name="Spark-Runde")
    assert body["name"] == "Spark-Runde"
    assert body["goal"] == "DFlash2 vs vLLM entscheiden"
    assert body["status"] == "idle"
    assert body["max_rounds"] == 3
    member_slugs = {m["slug"] for m in body["members"]}
    assert member_slugs == {"alpha", "beta"}

    listing = await auth_client.get("/api/v1/groups")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["member_count"] == 2
    assert rows[0]["status"] == "idle"


@pytest.mark.asyncio
async def test_create_group_validation_errors(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")

    resp = await auth_client.post(
        "/api/v1/groups",
        json={"goal": "   ", "member_ids": [str(a.id), str(b.id)]},
    )
    assert resp.status_code == 422

    resp = await auth_client.post(
        "/api/v1/groups", json={"goal": "Ziel", "member_ids": [str(a.id)]}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_group_rejects_non_comm_v2_with_409(
    auth_client: AsyncClient, async_session
):
    a = await _make_agent(async_session, "Alpha")
    legacy = await _make_agent(async_session, "Legacy", comm_v2=False)

    resp = await auth_client.post(
        "/api/v1/groups",
        json={"goal": "Ziel", "member_ids": [str(a.id), str(legacy.id)]},
    )
    assert resp.status_code == 409
    assert "Legacy" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_group_detail_and_document(
    auth_client: AsyncClient, async_session, _references_in_tmp: Path
):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id])
    gid = body["id"]

    detail = await auth_client.get(f"/api/v1/groups/{gid}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["goal"] == "DFlash2 vs vLLM entscheiden"
    assert d["result_doc_rel_path"].startswith("groups/")
    assert {m["slug"] for m in d["members"]} == {"alpha", "beta"}
    lead = next(m for m in d["members"] if m["role"] == "lead")
    assert lead["slug"] == "alpha"

    doc = await auth_client.get(f"/api/v1/groups/{gid}/document")
    assert doc.status_code == 200
    assert "DFlash2 vs vLLM entscheiden" in doc.json()["content"]

    missing = await auth_client.get(f"/api/v1/groups/{uuid.uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_member_add_and_remove(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    c = await _make_agent(async_session, "Gamma")
    body = await _create_group(auth_client, [a.id, b.id], lead_agent_id=str(a.id))
    gid = body["id"]

    resp = await auth_client.post(
        f"/api/v1/groups/{gid}/members", json={"agent_id": str(c.id)}
    )
    assert resp.status_code == 201

    resp = await auth_client.delete(f"/api/v1/groups/{gid}/members/{c.id}")
    assert resp.status_code == 204

    # Lead nicht entfernbar → 422 (erst neuen Lead wählen)
    resp = await auth_client.delete(f"/api/v1/groups/{gid}/members/{a.id}")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_and_read_messages(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id], lead_agent_id=str(a.id))
    gid = body["id"]

    resp = await auth_client.post(
        f"/api/v1/groups/{gid}/messages", json={"text": "@beta bitte prüfen"}
    )
    assert resp.status_code == 201
    assert resp.json()["mentions"] == ["beta"]

    resp = await auth_client.post(
        f"/api/v1/groups/{gid}/messages", json={"text": "und weiter"}
    )
    assert resp.status_code == 201
    assert resp.json()["mentions"] == ["alpha"]  # keine Mention → Lead

    listing = await auth_client.get(f"/api/v1/groups/{gid}/messages")
    assert listing.status_code == 200
    msgs = listing.json()["messages"]
    assert [m["seq"] for m in msgs] == [1, 2]
    assert msgs[0]["body"] == "@beta bitte prüfen"
    assert msgs[0]["sender_type"] == "user"


@pytest.mark.asyncio
async def test_eligible_members_only_comm_v2_unarchived(
    auth_client: AsyncClient, async_session
):
    await _make_agent(async_session, "Alpha")
    await _make_agent(async_session, "Legacy", comm_v2=False)
    await _make_agent(async_session, "Alt", archived=True)

    resp = await auth_client.get("/api/v1/groups/eligible-members")
    assert resp.status_code == 200
    slugs = {a["slug"] for a in resp.json()}
    assert slugs == {"alpha"}
