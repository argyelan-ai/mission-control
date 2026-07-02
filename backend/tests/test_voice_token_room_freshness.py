"""Tests fuer voice.py — Room-Naming pro Token-Request.

Bug 2026-05-14 abends: Der Operator connectet, beendet Call, neuer Call → kein Audio.
Root-Cause: voice.py nutzte fixen Room-Namen `voice-{user_id}` pro User.
LiveKit dispatched seinen Worker-Job aber nur EINMAL pro Room (beim
CreateRoom-Event). Reconnect mit gleichem Room → Browser joint, aber kein
Worker-Dispatch → keine Stimme. Plus xAI Realtime-Session-State Korruption
(`failed to insert item already exists`) weil derselbe Worker-Subprocess
mehrere Calls geteilt hat.

Fix: pro Token-Request frischer Room `voice-{user_id}-{ts}-{rand}`.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_voice_token_room_unique_per_request(client):
    """Zwei aufeinanderfolgende /voice/token Requests geben verschiedene Rooms."""
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

    # LiveKit Keys mocken damit kein 503 kommt
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
    """Room-Name muss user_id enthalten (für Multi-User-Isolation in Zukunft)."""
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
    """JWT-Payload muss die richtige Room im video.room Claim haben — sonst
    rejected LiveKit den Join."""
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
        # JWT enthält keine 'aud' Claim aber default-Decoder erwartet keine
        options={"verify_aud": False},
    )
    assert decoded["video"]["room"] == body["room"]
