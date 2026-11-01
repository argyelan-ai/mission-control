"""Guest card for terminal sessions (ADR-100 §6 rule 1).

The operator issues a guest card (admin only). A terminal session enrolls
with it (`POST /agent/comms/hello`) and becomes its own principal: a session
token with only comms:talk and an expiry — never a row in `agents`. The card
itself can only enroll. Revoking the card ends every session it enrolled.
"""
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.utils import utcnow
from tests.conftest import test_engine

CARDS = "/api/v1/comms/guest-cards"
HELLO = "/api/v1/agent/comms/hello"
WHO = "/api/v1/agent/comms/who"
SEND = "/api/v1/agent/comms/send"
SINBOX = "/api/v1/agent/comms/inbox"


@pytest.fixture(autouse=True)
async def warm(monkeypatch, fake_redis):
    from app.services import comms_directory
    monkeypatch.setattr(comms_directory, "_PROCESS_STARTED", 0.0)
    await fake_redis.set(comms_directory.EPOCH_KEY, "0")


async def _agent(name: str, *, scopes=None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(name=name, slug=name.lower(), role="developer",
                      scopes=["comms:talk", "tasks:read"] if scopes is None else scopes,
                      agent_token_hash=token_hash, comm_v2=True, harness="omp",
                      last_seen_at=utcnow())
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, {"Authorization": f"Bearer {raw}"}


async def _user_headers(role: str):
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"{uid.hex[:8]}@mc.local", name=role, role=role, is_active=True))
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(str(uid), role)}"}


async def _card(client, ttl_hours=12):
    admin = await _user_headers("admin")
    resp = await client.post(CARDS, headers=admin, json={"label": "desk terminals", "ttl_hours": ttl_hours})
    assert resp.status_code == 201, resp.text
    return resp.json(), admin


async def _hello(client, card_token, folder="runtimes", machine="desk", harness="claude"):
    return await client.post(HELLO, headers={"Authorization": f"Bearer {card_token}"},
                             json={"harness": harness, "machine": machine, "folder": folder})


@pytest.mark.asyncio
async def test_only_an_admin_issues_a_card(client: AsyncClient):
    operator = await _user_headers("operator")
    r = await client.post(CARDS, headers=operator, json={"label": "x"})
    assert r.status_code == 403
    _, agent_h = await _agent("Lead", scopes=[])  # empty = all scopes
    assert (await client.post(CARDS, headers=agent_h, json={"label": "x"})).status_code in (401, 403)


@pytest.mark.asyncio
async def test_card_token_is_shown_once_and_stored_hashed(client: AsyncClient):
    from app.models.comms import CommsGuestCard

    card, admin = await _card(client)
    assert card["token"].startswith("mcg_")
    listing = (await client.get(CARDS, headers=admin)).json()
    assert all("token" not in c for c in listing["cards"])
    async with AsyncSession(test_engine) as s:
        row = (await s.exec(select(CommsGuestCard))).one()
        assert card["token"] not in row.token_hash


@pytest.mark.asyncio
async def test_ttl_is_capped(client: AsyncClient):
    admin = await _user_headers("admin")
    r = await client.post(CARDS, headers=admin, json={"label": "x", "ttl_hours": 24 * 30})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_hello_makes_its_own_principal_not_an_agent_row(client: AsyncClient):
    from app.models.agent import Agent
    from app.models.comms import CommsSession

    card, _ = await _card(client)
    async with AsyncSession(test_engine) as s:
        agents_before = len((await s.exec(select(Agent))).all())
    r = await _hello(client, card["token"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["address"] == "claude@desk:runtimes"
    assert out["token"].startswith("mcs_")
    assert out["scopes"] == ["comms:talk"]
    async with AsyncSession(test_engine) as s:
        assert len((await s.exec(select(Agent))).all()) == agents_before
        sess = (await s.exec(select(CommsSession))).one()
        assert sess.address == "claude@desk:runtimes"
        assert sess.expires_at is not None


@pytest.mark.asyncio
async def test_session_token_reaches_only_comms_routes(client: AsyncClient):
    card, _ = await _card(client)
    tok = (await _hello(client, card["token"])).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.get(WHO, headers=h)).status_code == 200
    for path in ("/api/v1/agent/me", "/api/v1/agent/boxes/status", "/api/v1/agents", "/api/v1/approvals"):
        assert (await client.get(path, headers=h)).status_code == 401, path


@pytest.mark.asyncio
async def test_card_token_only_enrolls(client: AsyncClient):
    card, _ = await _card(client)
    h = {"Authorization": f"Bearer {card['token']}"}
    assert (await client.get(WHO, headers=h)).status_code == 401
    assert (await client.post(SEND, headers=h, json={"to": "x", "body": "y"})).status_code == 401
    assert (await client.get("/api/v1/agent/me", headers=h)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("folder", "../etc"), ("machine", "a b"), ("harness", "x" * 41), ("folder", ""),
])
async def test_hello_rejects_bad_name_parts(client: AsyncClient, field, value):
    card, _ = await _card(client)
    body = {"harness": "claude", "machine": "desk", "folder": "runtimes", field: value}
    r = await client.post(HELLO, headers={"Authorization": f"Bearer {card['token']}"}, json=body)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_an_address_held_by_another_card_is_refused(client: AsyncClient):
    card1, _ = await _card(client)
    card2, _ = await _card(client)
    assert (await _hello(client, card1["token"])).status_code == 201
    r = await _hello(client, card2["token"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "address_taken"


@pytest.mark.asyncio
async def test_hello_again_from_the_same_card_replaces_the_old_session(client: AsyncClient):
    card, _ = await _card(client)
    old = (await _hello(client, card["token"])).json()["token"]
    new = (await _hello(client, card["token"])).json()["token"]
    assert (await client.get(WHO, headers={"Authorization": f"Bearer {old}"})).status_code == 401
    assert (await client.get(WHO, headers={"Authorization": f"Bearer {new}"})).status_code == 200


@pytest.mark.asyncio
async def test_expired_session_is_refused(client: AsyncClient):
    from app.models.comms import CommsSession
    card, _ = await _card(client)
    tok = (await _hello(client, card["token"])).json()["token"]
    async with AsyncSession(test_engine) as s:
        row = (await s.exec(select(CommsSession))).one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        s.add(row)
        await s.commit()
    assert (await client.get(WHO, headers={"Authorization": f"Bearer {tok}"})).status_code == 401


@pytest.mark.asyncio
async def test_revoking_the_card_ends_its_sessions_and_its_sending(client: AsyncClient):
    card, admin = await _card(client)
    await _agent("Worker")
    tok = (await _hello(client, card["token"])).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.post(SEND, headers=h, json={"to": "worker", "body": "hi"})).status_code == 201
    r = await client.post(f"{CARDS}/{card['id']}/revoke", headers=admin)
    assert r.status_code == 200
    assert (await client.post(SEND, headers=h, json={"to": "worker", "body": "again"})).status_code == 401
    assert (await _hello(client, card["token"])).status_code == 401


@pytest.mark.asyncio
async def test_revoke_all_switches_every_guest_off(client: AsyncClient):
    c1, admin = await _card(client)
    c2, _ = await _card(client)
    t1 = (await _hello(client, c1["token"], folder="a")).json()["token"]
    t2 = (await _hello(client, c2["token"], folder="b")).json()["token"]
    assert (await client.post(f"{CARDS}/revoke-all", headers=admin)).status_code == 200
    for t in (t1, t2):
        assert (await client.get(WHO, headers={"Authorization": f"Bearer {t}"})).status_code == 401


@pytest.mark.asyncio
async def test_operator_sees_sessions_and_can_revoke_one(client: AsyncClient):
    card, admin = await _card(client)
    tok = (await _hello(client, card["token"])).json()["token"]
    rows = (await client.get("/api/v1/comms/sessions", headers=admin)).json()["sessions"]
    [row] = rows
    assert row["address"] == "claude@desk:runtimes"
    assert "token" not in row and "token_hash" not in row
    assert (await client.post(f"/api/v1/comms/sessions/{row['id']}/revoke", headers=admin)).status_code == 200
    assert (await client.get(WHO, headers={"Authorization": f"Bearer {tok}"})).status_code == 401


@pytest.mark.asyncio
async def test_round_trip_agent_and_terminal_session(client: AsyncClient):
    """The operator's example: an agent asks a terminal session, the session
    reads it at rest (its own inbox), answers, the agent reads the answer."""
    card, _ = await _card(client)
    hello = (await _hello(client, card["token"])).json()
    s_h = {"Authorization": f"Bearer {hello['token']}"}
    agent, a_h = await _agent("Asker")

    who = (await client.get(WHO, headers=a_h)).json()
    [row] = [r for r in who["sessions"] if r["address"] == "claude@desk:runtimes"]
    assert row["kind"] == "session" and row["alive"] == "yes" and row["listening"] is True

    r = await client.post(SEND, headers=a_h, json={"to": "runtimes", "body": "When is your test done?"})
    assert r.status_code == 201, r.text

    inbox = (await client.get(SINBOX, headers=s_h)).json()
    [m] = inbox["messages"]
    assert m["from"] == "asker"
    assert m["body"].startswith("[Message from asker (omp)")
    await client.post(f"{SINBOX}/ack", headers=s_h, json={"thread_id": m["thread_id"], "seq": m["seq"]})
    assert (await client.get(SINBOX, headers=s_h)).json()["messages"] == []

    r = await client.post(SEND, headers=s_h, json={"to": "asker", "body": "Done at about 15:30."})
    assert r.status_code == 201, r.text
    [back] = (await client.get("/api/v1/agent/me/inbox", headers=a_h)).json()["messages"]
    assert back["body"].startswith("[Message from claude@desk:runtimes (claude)")


@pytest.mark.asyncio
async def test_session_cannot_ack_a_foreign_thread(client: AsyncClient):
    card, _ = await _card(client)
    s_h = {"Authorization": f"Bearer {(await _hello(client, card['token'])).json()['token']}"}
    r = await client.post(f"{SINBOX}/ack", headers=s_h, json={"thread_id": str(uuid.uuid4()), "seq": 5})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_agents_use_their_ordinary_inbox(client: AsyncClient):
    _, a_h = await _agent("Asker")
    assert (await client.get(SINBOX, headers=a_h)).status_code == 409


@pytest.mark.asyncio
async def test_session_presence_is_its_own_and_expires(client: AsyncClient):
    card, _ = await _card(client)
    s_h = {"Authorization": f"Bearer {(await _hello(client, card['token'])).json()['token']}"}
    _, a_h = await _agent("Asker")
    await client.post("/api/v1/agent/comms/presence", headers=s_h, json={"busy": True, "task": "bench"})
    [row] = [r for r in (await client.get(WHO, headers=a_h)).json()["sessions"] if r["kind"] == "session"]
    assert row["busy"] is True and row["task"] == "bench"


@pytest.mark.asyncio
async def test_an_expired_card_ends_its_sessions_too(client: AsyncClient):
    """A session never outlives its card — checked on every call, not only
    when the card is revoked through the route."""
    from app.models.comms import CommsGuestCard
    card, _ = await _card(client)
    tok = (await _hello(client, card["token"])).json()["token"]
    async with AsyncSession(test_engine) as s:
        row = await s.get(CommsGuestCard, uuid.UUID(card["id"]))
        row.expires_at = utcnow() - timedelta(seconds=1)
        s.add(row)
        await s.commit()
    assert (await client.get(WHO, headers={"Authorization": f"Bearer {tok}"})).status_code == 401


@pytest.mark.asyncio
async def test_card_token_on_a_comms_route_says_it_only_enrolls(client: AsyncClient):
    card, _ = await _card(client)
    r = await client.get(WHO, headers={"Authorization": f"Bearer {card['token']}"})
    assert r.status_code == 401
    assert "only enrolls" in r.json()["detail"]
