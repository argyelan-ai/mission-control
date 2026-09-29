"""POST /api/v1/webhooks/github/{id} must never accept an unsigned event.

The endpoint has no user auth; the webhook secret (HMAC-SHA256 in
X-Hub-Signature-256) is its only authentication. It used to be checked only
*if* a secret was configured, so a webhook row without one accepted events
from anyone who knew (or guessed from a log line) its UUID. A missing secret
now means "not usable" — fail closed — instead of "no check".
"""
import hashlib
import hmac
import json
import uuid

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.webhook import Webhook, WebhookPayload
from tests.conftest import test_engine

SECRET = "test-webhook-secret"
BODY = json.dumps(
    {
        "ref": "refs/heads/main",
        "repository": {"full_name": "example/repo"},
        "commits": [{"id": "abc"}],
        "head_commit": {"id": "abcdef0123", "message": "msg", "author": {"name": "dev"}},
    }
).encode()


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _make_webhook(secret: str | None) -> uuid.UUID:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        webhook = Webhook(board_id=board.id, name="gh", secret=secret)
        s.add(webhook)
        await s.commit()
        return webhook.id


async def _stored_payloads(webhook_id: uuid.UUID) -> int:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = await s.exec(select(WebhookPayload).where(WebhookPayload.webhook_id == webhook_id))
        return len(rows.all())


async def _post(client, webhook_id, headers=None):
    return await client.post(
        f"/api/v1/webhooks/github/{webhook_id}",
        content=BODY,
        headers={"Content-Type": "application/json", "X-GitHub-Event": "push", **(headers or {})},
    )


# ── rejected ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, ""])
async def test_webhook_without_secret_rejects_unsigned_event(client, secret):
    webhook_id = await _make_webhook(secret)
    resp = await _post(client, webhook_id)
    assert resp.status_code == 403
    assert "secret" in resp.json()["detail"].lower()
    assert await _stored_payloads(webhook_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, ""])
async def test_webhook_without_secret_rejects_even_a_signed_looking_event(client, secret):
    # A signature made with the empty key must not count as authentication.
    webhook_id = await _make_webhook(secret)
    resp = await _post(client, webhook_id, {"X-Hub-Signature-256": _sign(BODY, "")})
    assert resp.status_code == 403
    assert await _stored_payloads(webhook_id) == 0


@pytest.mark.asyncio
async def test_unsigned_event_is_rejected(client):
    webhook_id = await _make_webhook(SECRET)
    resp = await _post(client, webhook_id)
    assert resp.status_code == 403
    assert await _stored_payloads(webhook_id) == 0


@pytest.mark.asyncio
async def test_wrong_signature_is_rejected(client):
    webhook_id = await _make_webhook(SECRET)
    resp = await _post(client, webhook_id, {"X-Hub-Signature-256": _sign(BODY, "other")})
    assert resp.status_code == 403
    assert await _stored_payloads(webhook_id) == 0


# ── accepted ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correctly_signed_event_is_accepted(client):
    webhook_id = await _make_webhook(SECRET)
    resp = await _post(client, webhook_id, {"X-Hub-Signature-256": _sign(BODY)})
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    assert await _stored_payloads(webhook_id) == 1
