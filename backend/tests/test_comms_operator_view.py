"""The operator's read-only view of talking across harnesses (ADR-100 §4, 2d).

The phone shows the directory as a sheet from the chat header and every
session conversation in the inbox tab "Between agents". These routes only
read: the directory comes from the one presence source
(`comms_directory.directory`, §6 rule 2), conversations and their messages
from the same `threads`/`messages` rows the send path writes (§6 rule 3).
A signed-in operator reads them; an agent or session token never does.
"""
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.utils import utcnow
from tests.conftest import test_engine

SEND = "/api/v1/agent/comms/send"
DIRECTORY = "/api/v1/comms/directory"
CONVERSATIONS = "/api/v1/comms/conversations"


@pytest.fixture(autouse=True)
async def warm(monkeypatch, fake_redis):
    from app.services import comms_directory
    monkeypatch.setattr(comms_directory, "_PROCESS_STARTED", 0.0)
    await fake_redis.set(comms_directory.EPOCH_KEY, "0")


async def _agent(name: str, *, scopes=None, operational_mode="active"):
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(name=name, slug=name.lower(), role="developer",
                      scopes=["comms:talk", "chat:write"] if scopes is None else scopes,
                      agent_token_hash=token_hash, comm_v2=True, harness="omp",
                      operational_mode=operational_mode,
                      last_seen_at=utcnow() - timedelta(seconds=5))
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, {"Authorization": f"Bearer {raw}"}


async def _user(role: str):
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"{uid.hex[:8]}@mc.local", name=role, role=role, is_active=True))
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(str(uid), role)}"}


async def _send(client, headers, to, body):
    r = await client.post(SEND, headers=headers, json={"to": to, "body": body})
    assert r.status_code == 201, r.text
    return r.json()


# ── directory ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_directory_is_the_one_presence_source(client: AsyncClient):
    """Same rows and words as `GET /agent/comms/who` — not a second computation."""
    _, lead_h = await _agent("Lead")
    await _agent("Worker", operational_mode="paused")
    op = await _user("operator")

    r = await client.get(DIRECTORY, headers=op)
    assert r.status_code == 200, r.text
    mine = r.json()
    who = (await client.get("/api/v1/agent/comms/who", headers=lead_h)).json()
    assert mine["sessions"] == who["sessions"]
    assert mine["warming_up"] == who["warming_up"]

    by = {row["address"]: row for row in mine["sessions"]}
    assert by["lead"]["alive"] == "yes" and by["lead"]["listening"] is True
    # alive but not listening: the paused agent keeps both words apart
    assert by["worker"]["alive"] == "yes" and by["worker"]["listening"] is False


@pytest.mark.asyncio
async def test_directory_uses_the_presence_service(client: AsyncClient, monkeypatch):
    from app.services import comms_directory

    async def fake(session):
        return {"sessions": [{"address": "x"}], "warming_up": True}

    monkeypatch.setattr(comms_directory, "directory", fake)
    r = await client.get(DIRECTORY, headers=await _user("operator"))
    assert r.json() == {"sessions": [{"address": "x"}], "warming_up": True}


# ── conversations ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conversations_list_who_with_whom_last_message_and_brake(client: AsyncClient):
    _, lead_h = await _agent("Lead")
    _, worker_h = await _agent("Worker")
    await _agent("Helper")
    await _send(client, lead_h, "worker", "Is box one free?")
    await _send(client, worker_h, "lead", "Measuring, done at 15:30.")
    await _send(client, lead_h, "helper", "ping")

    r = await client.get(CONVERSATIONS, headers=await _user("operator"))
    assert r.status_code == 200, r.text
    convs = r.json()["conversations"]
    assert len(convs) == 2
    lw = next(c for c in convs if {p["address"] for p in c["participants"]} == {"lead", "worker"})
    assert lw["message_count"] == 2
    assert lw["state"] == "running"
    assert lw["last_message"]["from"] == "worker"
    # the stored frame and quote marks are stripped for the phone
    assert lw["last_message"]["text"] == "Measuring, done at 15:30."
    assert lw["last_message_at"]
    # newest conversation first
    assert convs[0]["last_message_at"] >= convs[1]["last_message_at"]


@pytest.mark.asyncio
async def test_brake_paused_and_stopped_states(client: AsyncClient, auth_client):
    from app.models.approval import Approval

    _, a_h = await _agent("Ping")
    _, b_h = await _agent("Pong")
    for i in range(6):
        h, to = (a_h, "pong") if i % 2 == 0 else (b_h, "ping")
        await _send(client, h, to, f"turn {i}")

    convs = (await client.get(CONVERSATIONS)).json()["conversations"]
    [c] = convs
    assert c["state"] == "paused"
    async with AsyncSession(test_engine) as s:
        [card] = (await s.exec(select(Approval).where(Approval.action_type == "comms_continue"))).all()
    assert c["brake_approval_id"] == str(card.id)

    await client.patch(f"/api/v1/approvals/{card.id}", json={"status": "rejected"})
    [c] = (await client.get(CONVERSATIONS)).json()["conversations"]
    assert c["state"] == "stopped"


@pytest.mark.asyncio
async def test_messages_of_one_conversation_in_order_with_sender(client: AsyncClient):
    _, lead_h = await _agent("Lead")
    _, worker_h = await _agent("Worker")
    tid = (await _send(client, lead_h, "worker", "first line\nsecond line"))["thread_id"]
    await _send(client, worker_h, "lead", "answer")

    r = await client.get(f"{CONVERSATIONS}/{tid}/messages", headers=await _user("operator"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert {p["address"] for p in out["conversation"]["participants"]} == {"lead", "worker"}
    msgs = out["messages"]
    assert [m["from"] for m in msgs] == ["lead", "worker"]
    assert msgs[0]["text"] == "first line\nsecond line"
    assert msgs[0]["seq"] < msgs[1]["seq"]
    assert msgs[0]["created_at"]


@pytest.mark.asyncio
async def test_messages_of_a_non_session_thread_are_not_served(client: AsyncClient):
    from app.models.thread import Thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = Thread(kind="dm", title="operator and an agent")
        s.add(t)
        await s.commit()
    op = await _user("operator")
    assert (await client.get(f"{CONVERSATIONS}/{t.id}/messages", headers=op)).status_code == 404
    assert (await client.get(f"{CONVERSATIONS}/{uuid.uuid4()}/messages", headers=op)).status_code == 404


@pytest.mark.asyncio
async def test_a_terminal_session_appears_with_its_address(client: AsyncClient):
    admin = await _user("admin")
    card = (await client.post("/api/v1/comms/guest-cards", headers=admin, json={"label": "desk"})).json()
    hello = await client.post("/api/v1/agent/comms/hello", headers={"Authorization": f"Bearer {card['token']}"},
                              json={"harness": "claude", "machine": "desk", "folder": "runtimes"})
    s_h = {"Authorization": f"Bearer {hello.json()['token']}"}
    await _agent("Lead")
    await _send(client, s_h, "lead", "hello from the terminal")

    [c] = (await client.get(CONVERSATIONS, headers=admin)).json()["conversations"]
    by = {p["address"]: p for p in c["participants"]}
    assert by["claude@desk:runtimes"]["kind"] == "session"
    assert by["claude@desk:runtimes"]["harness"] == "claude"
    assert c["last_message"]["from"] == "claude@desk:runtimes"


# ── who may read ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_and_session_tokens_are_refused(client: AsyncClient):
    _, lead_h = await _agent("Lead", scopes=[])  # empty = all agent scopes
    _, talk_h = await _agent("Talker")
    await _agent("Other")
    tid = (await _send(client, talk_h, "other", "hi"))["thread_id"]
    admin = await _user("admin")
    card = (await client.post("/api/v1/comms/guest-cards", headers=admin, json={"label": "desk"})).json()
    hello = await client.post("/api/v1/agent/comms/hello", headers={"Authorization": f"Bearer {card['token']}"},
                              json={"harness": "claude", "machine": "desk", "folder": "x"})
    sess_h = {"Authorization": f"Bearer {hello.json()['token']}"}
    card_h = {"Authorization": f"Bearer {card['token']}"}

    for h in (lead_h, talk_h, sess_h, card_h):
        for path in (DIRECTORY, CONVERSATIONS, f"{CONVERSATIONS}/{tid}/messages"):
            assert (await client.get(path, headers=h)).status_code in (401, 403), path


@pytest.mark.asyncio
async def test_a_viewer_is_refused_an_operator_reads(client: AsyncClient):
    viewer = await _user("viewer")
    operator = await _user("operator")
    assert (await client.get(CONVERSATIONS, headers=viewer)).status_code == 403
    assert (await client.get(DIRECTORY, headers=viewer)).status_code == 403
    assert (await client.get(CONVERSATIONS, headers=operator)).status_code == 200


@pytest.mark.asyncio
async def test_the_routes_only_read(client: AsyncClient):
    op = await _user("operator")
    for path in (DIRECTORY, CONVERSATIONS):
        assert (await client.post(path, headers=op, json={})).status_code == 405
