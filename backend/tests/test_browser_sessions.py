"""Browser sessions (ADR-088): one browser area per agent session or head run.

PR S1 is the invisible foundation: the `browser_sessions` row, the session's
gateway address `/s/<token>/`, registering it with cdp-gateway and ending it
there. Nothing in MC opens a session on its own yet (harness wiring is a later ADR-088 step).
The gateway is faked with an `httpx.MockTransport`.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.browser_session import BrowserSession
from app.services import browser_sessions as svc
from tests.conftest import test_engine

# The gateway's own token rule (docker/cdp-browser/gateway/cdp_gateway.py `_TOKEN_RE`).
GATEWAY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
RUN_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"


class FakeGateway:
    """Records every request; answers like cdp-gateway's `/mc/sessions`."""

    def __init__(self, *, reachable: bool = True):
        self.reachable = reachable
        self.requests: list[httpx.Request] = []
        self.registered: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("gateway down", request=request)
        self.requests.append(request)
        token = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            created = token not in self.registered
            self.registered[token] = request.url.params["session"]
            return httpx.Response(201 if created else 200, json={"sessionId": self.registered[token]})
        if request.method == "DELETE":
            if token not in self.registered:
                return httpx.Response(404, text="cdp-gateway: unknown browser session")
            sid = self.registered.pop(token)
            return httpx.Response(200, json={
                "sessionId": sid, "closedTargets": 2, "disposedContexts": 1, "closedConnections": 1, "errors": [],
            })
        return httpx.Response(405)


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(svc, "_transport", httpx.MockTransport(fake.handler))
    return fake


# ── token ──────────────────────────────────────────────────────────────────

def test_token_is_deterministic_url_safe_and_per_session():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert svc.session_token(a) == svc.session_token(a)
    assert svc.session_token(a) != svc.session_token(b)
    assert GATEWAY_TOKEN_RE.match(svc.session_token(a))
    assert svc.endpoint_path(a) == f"/s/{svc.session_token(a)}/"


def test_token_depends_on_the_server_secret(monkeypatch):
    sid = uuid.uuid4()
    before = svc.session_token(sid)
    monkeypatch.setattr(svc.settings, "secrets_encryption_key", "another-key")
    assert svc.session_token(sid) != before


def test_rotating_the_jwt_secret_does_not_strand_open_sessions(monkeypatch):
    """Review L4: the token used to hang off the JWT secret — rotating it (to
    log every user out) changed every token, so MC could neither re-register
    (409, the gateway refuses to re-point) nor end (404) its open sessions.
    It now derives from the encryption key, which must stay stable anyway
    (rotating it already breaks every stored secret)."""
    sid = uuid.uuid4()
    before = svc.session_token(sid)
    monkeypatch.setattr(svc.settings, "jwt_secret_key", "rotated-jwt-secret")
    assert svc.session_token(sid) == before


def test_a_dedicated_browser_session_secret_wins(monkeypatch):
    sid = uuid.uuid4()
    before = svc.session_token(sid)
    monkeypatch.setattr(svc.settings, "browser_session_secret", "dedicated")
    assert svc.session_token(sid) != before


def test_the_token_is_never_stored():
    """The row carries no token column: the token is derived on demand, so a
    database dump does not hand out live browser addresses."""
    assert not any("token" in c.name for c in BrowserSession.__table__.columns)


# ── open ───────────────────────────────────────────────────────────────────

async def test_open_for_agent_registers_with_gateway(session: AsyncSession, make_agent, gateway):
    agent = await make_agent(name="Alpha", slug="alpha")
    row, registered = await svc.open_session(session, agent=agent)

    assert registered is True
    assert row.owner_kind == "agent" and row.agent_id == agent.id and row.status == "open"
    [req] = gateway.requests
    assert req.method == "PUT"
    assert req.url.path == f"/mc/sessions/{svc.session_token(row.id)}"
    assert req.url.params["session"] == str(row.id)
    assert req.url.params["agent"] == "alpha"


async def test_open_for_head_run_is_idempotent(session: AsyncSession, gateway):
    first, _ = await svc.open_session(session, head_run_id=RUN_ID)
    second, registered = await svc.open_session(session, head_run_id=RUN_ID)

    assert second.id == first.id
    assert registered is True
    assert first.owner_kind == "head" and first.agent_id is None
    # Re-registering is safe (gateway PUT is idempotent) and heals a gateway
    # that lost its register in a restart.
    assert [r.method for r in gateway.requests] == ["PUT", "PUT"]
    assert "agent" not in gateway.requests[0].url.params


async def test_open_needs_exactly_one_owner(session: AsyncSession, make_agent, gateway):
    agent = await make_agent(name="Alpha", slug="alpha")
    with pytest.raises(ValueError):
        await svc.open_session(session)
    with pytest.raises(ValueError):
        await svc.open_session(session, agent=agent, head_run_id=RUN_ID)
    with pytest.raises(ValueError):
        await svc.open_session(session, head_run_id="../../etc")


async def test_open_survives_an_unreachable_gateway(session: AsyncSession, monkeypatch):
    """Opening is cheap and must not fail a head start: the row exists, the
    gateway learns about it on the next register (ADR-088 lifecycle step)."""
    fake = FakeGateway(reachable=False)
    monkeypatch.setattr(svc, "_transport", httpx.MockTransport(fake.handler))
    row, registered = await svc.open_session(session, head_run_id=RUN_ID)
    assert registered is False
    assert row.status == "open"


async def test_database_allows_one_open_session_per_head_run(gateway):
    """Guard in the schema, not only in the service (two starts at once)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(BrowserSession(owner_kind="head", head_run_id=RUN_ID))
        s.add(BrowserSession(owner_kind="head", head_run_id=RUN_ID))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(BrowserSession(owner_kind="head", head_run_id=RUN_ID, status="ended"))
        s.add(BrowserSession(owner_kind="head", head_run_id=RUN_ID, status="ended"))
        s.add(BrowserSession(owner_kind="head", head_run_id=RUN_ID))
        await s.commit()  # ended sessions don't count


async def test_open_that_loses_a_race_returns_the_winning_row(session: AsyncSession, gateway, monkeypatch):
    """Two opens for one head run at the same moment: both SELECT nothing,
    the second INSERT hits the partial unique index. The loser must get the
    winner's row back, not a 500."""
    async with AsyncSession(test_engine, expire_on_commit=False) as other:
        winner = BrowserSession(owner_kind="head", head_run_id=RUN_ID)
        other.add(winner)
        await other.commit()

    real_find = svc._find_open
    calls = {"n": 0}

    async def stale_first_lookup(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_find(*args, **kwargs)

    monkeypatch.setattr(svc, "_find_open", stale_first_lookup)
    row, registered = await svc.open_session(session, head_run_id=RUN_ID)
    assert row.id == winner.id
    assert registered is True


# ── end ────────────────────────────────────────────────────────────────────

async def test_end_session_cleans_up_at_gateway_and_is_idempotent(session: AsyncSession, gateway):
    row, _ = await svc.open_session(session, head_run_id=RUN_ID)
    result = await svc.end_session(session, row, reason="run_exited")

    assert row.status == "ended" and row.ended_at is not None and row.end_reason == "run_exited"
    assert result["closedTargets"] == 2
    assert gateway.requests[-1].method == "DELETE"
    assert gateway.requests[-1].url.path == f"/mc/sessions/{svc.session_token(row.id)}"

    count = len(gateway.requests)
    assert await svc.end_session(session, row, reason="again") is None
    assert len(gateway.requests) == count
    assert row.end_reason == "run_exited"


async def test_end_session_ends_the_row_even_if_the_gateway_forgot_it(session: AsyncSession, gateway):
    row, _ = await svc.open_session(session, head_run_id=RUN_ID)
    gateway.registered.clear()  # gateway restarted: nothing to clean up there
    assert await svc.end_session(session, row, reason="operator") is None
    assert row.status == "ended"


# ── API ────────────────────────────────────────────────────────────────────

async def test_api_open_list_end(auth_client, make_agent, gateway):
    agent = await make_agent(name="Alpha", slug="alpha")
    resp = await auth_client.post("/api/v1/browser-sessions", json={"agent_id": str(agent.id)})
    assert resp.status_code == 201, resp.text
    opened = resp.json()
    token = svc.session_token(uuid.UUID(opened["id"]))
    # Only the open call returns the address (a repeat open of the same
    # session returns the same, deterministic address)...
    assert opened["endpoint_path"] == f"/s/{token}/"
    assert opened["registered"] is True and opened["status"] == "open"

    listing = await auth_client.get("/api/v1/browser-sessions")
    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()] == [opened["id"]]
    # ...the listing and the end call never do.
    assert token not in listing.text
    assert "endpoint_path" not in listing.json()[0]

    ended = await auth_client.post(f"/api/v1/browser-sessions/{opened['id']}/end", json={"reason": "operator"})
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    assert ended.json()["gateway"]["closedTargets"] == 2
    assert token not in ended.text


async def test_api_rejects_bad_owner(auth_client, gateway):
    assert (await auth_client.post("/api/v1/browser-sessions", json={})).status_code == 422
    assert (await auth_client.post(
        "/api/v1/browser-sessions", json={"agent_id": str(uuid.uuid4()), "head_run_id": RUN_ID},
    )).status_code == 422
    assert (await auth_client.post("/api/v1/browser-sessions", json={"head_run_id": "x; rm -rf"})).status_code == 422
    assert (await auth_client.post("/api/v1/browser-sessions", json={"agent_id": str(uuid.uuid4())})).status_code == 404
    assert (await auth_client.post(f"/api/v1/browser-sessions/{uuid.uuid4()}/end", json={})).status_code == 404


async def test_api_open_and_end_need_operator(client, gateway):
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"v-{uid.hex[:6]}@mc.local", name="V", role="viewer", is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), 'viewer')}"

    assert (await client.get("/api/v1/browser-sessions")).status_code == 200
    assert (await client.post("/api/v1/browser-sessions", json={"head_run_id": RUN_ID})).status_code == 403
    assert gateway.requests == []
    body = json.dumps({})
    assert (await client.post(f"/api/v1/browser-sessions/{uuid.uuid4()}/end", content=body)).status_code == 403



def test_ending_waits_longer_than_the_gateways_cleanup_deadline():
    """Review L5: the gateway answers DELETE within its cleanup deadline
    (8 s); the backend must not give up ("unreachable") before that."""
    assert svc._END_TIMEOUT > 8.0
