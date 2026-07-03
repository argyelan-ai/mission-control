"""Tests for voice.py — room naming per token request.

Bug 2026-05-14 evening: operator connects, ends call, new call → no audio.
Root cause: voice.py used a fixed room name `voice-{user_id}` per user.
LiveKit only dispatches its worker job ONCE per room (on the CreateRoom
event). Reconnecting with the same room → browser joins, but no worker
dispatch → no voice. Plus xAI realtime session-state corruption
(`failed to insert item already exists`) because the same worker subprocess
shared multiple calls.

Fix: a fresh room per token request, `voice-{user_id}-{ts}-{rand}`.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_voice_token_room_unique_per_request(client):
    """Two consecutive /voice/token requests return different rooms."""
    from app.auth import create_access_token
    from app.models.user import User
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email="markvoice@mc.local", name="Operator",
                   role="admin", is_active=True))
        await s.commit()
    jwt = create_access_token(str(user_id), "admin")

    # Mock LiveKit keys so we don't get a 503
    with patch("app.routers.voice.LIVEKIT_API_KEY", "test-key"), \
         patch("app.routers.voice.LIVEKIT_API_SECRET", "test-secret-for-jwt-signing"):
        r1 = await client.post("/api/v1/voice/token",
                               headers={"Authorization": f"Bearer {jwt}"})
        r2 = await client.post("/api/v1/voice/token",
                               headers={"Authorization": f"Bearer {jwt}"})

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    room1 = r1.json()["room"]
    room2 = r2.json()["room"]
    assert room1 != room2, (
        f"Bug: Zwei Token-Requests geben gleichen Room → LiveKit dispatched "
        f"keinen neuen Worker-Job beim Reconnect. Got room1={room1} room2={room2}"
    )


@pytest.mark.asyncio
async def test_voice_token_room_contains_user_id(client):
    """Room name must contain user_id (for future multi-user isolation)."""
    from app.auth import create_access_token
    from app.models.user import User
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000098")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email="user98@mc.local", name="U98",
                   role="admin", is_active=True))
        await s.commit()
    jwt = create_access_token(str(user_id), "admin")

    with patch("app.routers.voice.LIVEKIT_API_KEY", "test-key"), \
         patch("app.routers.voice.LIVEKIT_API_SECRET", "test-secret-for-jwt"):
        r = await client.post("/api/v1/voice/token",
                              headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200
    room = r.json()["room"]
    assert str(user_id) in room
    assert room.startswith("voice-")


@pytest.mark.asyncio
async def test_voice_token_token_includes_correct_room_claim(client):
    """JWT payload must have the correct room in the video.room claim — otherwise
    LiveKit rejects the join."""
    from app.auth import create_access_token
    from app.models.user import User
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine
    from jose import jwt as jose_jwt

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000097")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email="user97@mc.local", name="U97",
                   role="admin", is_active=True))
        await s.commit()
    jwt_auth = create_access_token(str(user_id), "admin")

    with patch("app.routers.voice.LIVEKIT_API_KEY", "test-key"), \
         patch("app.routers.voice.LIVEKIT_API_SECRET", "test-secret-for-jwt-signing"):
        r = await client.post("/api/v1/voice/token",
                              headers={"Authorization": f"Bearer {jwt_auth}"})
    assert r.status_code == 200
    body = r.json()
    decoded = jose_jwt.decode(
        body["token"], "test-secret-for-jwt-signing",
        algorithms=["HS256"],
        # JWT contains no 'aud' claim, but the default decoder doesn't expect one
        options={"verify_aud": False},
    )
    assert decoded["video"]["room"] == body["room"]
