"""Login stays valid for 7 days (was 8 hours).

Operator decision (E4): longer logins only once a password change signs out
every other session — that is the brake for a lost device. The lifetime has
two sources that must agree: the Settings default and the compose default
(`${JWT_ACCESS_TOKEN_EXPIRE_MINUTES:-...}`), which wins inside the containers.
An explicit JWT_ACCESS_TOKEN_EXPIRE_MINUTES in `.env` still overrides both.
"""

import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from jose import jwt

from app.auth import JWT_ALGORITHM, create_access_token, hash_password
from app.config import Settings, settings

SEVEN_DAYS_MIN = 7 * 24 * 60
REPO = Path(__file__).resolve().parents[2]


def test_default_lifetime_is_seven_days(monkeypatch):
    monkeypatch.delenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", raising=False)
    assert Settings(_env_file=None).jwt_access_token_expire_minutes == SEVEN_DAYS_MIN


def test_explicit_override_still_wins(monkeypatch):
    monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "480")
    assert Settings(_env_file=None).jwt_access_token_expire_minutes == 480


def test_compose_default_matches_settings_default():
    compose = (REPO / "docker-compose.yml").read_text()
    defaults = re.findall(r"JWT_ACCESS_TOKEN_EXPIRE_MINUTES:\s*\$\{JWT_ACCESS_TOKEN_EXPIRE_MINUTES:-(\d+)\}", compose)
    assert defaults, "compose no longer passes JWT_ACCESS_TOKEN_EXPIRE_MINUTES"
    assert set(defaults) == {str(SEVEN_DAYS_MIN)}, defaults


def test_token_expiry_follows_setting(monkeypatch):
    monkeypatch.setattr(settings, "jwt_access_token_expire_minutes", SEVEN_DAYS_MIN)
    claims = jwt.decode(
        create_access_token(str(uuid.uuid4()), "admin"),
        settings.jwt_secret_key,
        algorithms=[JWT_ALGORITHM],
    )
    assert claims["exp"] - claims["iat"] == pytest.approx(
        timedelta(days=7).total_seconds(), abs=5
    )


@pytest.mark.asyncio
async def test_login_cookie_lives_as_long_as_the_token(client, monkeypatch):
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.user import User
    from tests.conftest import test_engine

    monkeypatch.setattr(settings, "jwt_access_token_expire_minutes", SEVEN_DAYS_MIN)
    email = f"lifetime-{uuid.uuid4().hex[:8]}@mc.local"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uuid.uuid4(), email=email, name="L", role="admin",
                   is_active=True, password_hash=hash_password("correct-horse")))
        await s.commit()
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "correct-horse"})
    assert resp.status_code == 200
    cookie = next(v for k, v in resp.headers.multi_items()
                  if k.lower() == "set-cookie" and v.startswith("mc_sse_token="))
    assert f"max-age={SEVEN_DAYS_MIN * 60}" in cookie.lower()
