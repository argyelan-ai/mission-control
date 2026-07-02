import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from fastapi import Depends, HTTPException, Query, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import bcrypt
from jose import JWTError, jwt
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import get_session
from app.utils import utcnow

bearer_scheme = HTTPBearer(auto_error=False)

# ── JWT Config ────────────────────────────────────────────────────────────────

JWT_ALGORITHM = "HS256"


def create_access_token(
    user_id: str,
    role: str,
    token_version: int = 0,
    expires_delta: timedelta | None = None,
) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    payload = {
        "sub": user_id,
        "role": role,
        "tv": token_version,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=JWT_ALGORITHM)


# ── Password Hashing (bcrypt) ────────────────────────────────────────────────

def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except Exception:
        return False


# ── Roles ─────────────────────────────────────────────────────────────────────

class Role(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


ROLE_HIERARCHY = {Role.ADMIN: 3, Role.OPERATOR: 2, Role.VIEWER: 1}


def require_role(minimum_role: Role):
    """FastAPI dependency factory — checks that the user has at least the given role."""
    async def _check(current_user=Depends(require_user)):
        user_level = ROLE_HIERARCHY.get(current_user.role, 0)
        required_level = ROLE_HIERARCHY[minimum_role]
        if user_level < required_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum_role} role or higher",
            )
        return current_user
    return _check


# ── User Auth ────────────────────────────────────────────────────────────────

async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    token: str | None = Query(None, alias="token"),
    session: AsyncSession = Depends(get_session),
):
    from app.models.user import User

    # Get token from header, query param, or HttpOnly cookie (SSE fallback)
    raw_token: str | None = None
    if credentials:
        raw_token = credentials.credentials
    elif token:
        raw_token = token
    else:
        raw_token = request.cookies.get("mc_sse_token")

    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    # 1. Try JWT decode
    try:
        payload = jwt.decode(raw_token, settings.jwt_secret_key, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if user_id:
            try:
                user = await session.get(User, uuid.UUID(user_id))
            except ValueError:
                # sub is not a UUID (e.g. "mcp-server") — check for admin role JWT
                if payload.get("role") == "admin":
                    result = await session.exec(select(User).where(User.role == "admin").limit(1))
                    admin = result.first()
                    if admin:
                        return admin
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
            if not user or not user.is_active:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
            # token_version Check — Logout invalidiert alle alten Tokens
            token_tv = payload.get("tv", 0)
            if token_tv != user.token_version:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revoked")
            return user
    except JWTError:
        pass

    # 2. Fallback: legacy LOCAL_AUTH_TOKEN (only if configured and non-empty)
    if (
        settings.local_auth_token
        and settings.local_auth_token not in ("", "dev-token", "change-me")
        and secrets.compare_digest(raw_token, settings.local_auth_token)
    ):
        # Return first admin user, or synthetic admin if none exist
        result = await session.exec(select(User).where(User.role == "admin").limit(1))
        admin = result.first()
        if admin:
            return admin
        # Synthetic admin for transition period (no users created yet)
        return User(
            id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            email="admin@local",
            name="Local Admin",
            role="admin",
            is_active=True,
        )

    # If the caller passed an Agent Token (64-char hex) to a User-only route,
    # give a precise hint instead of a generic 401. Agents repeatedly stumble
    # on this — Davinci self-reflection 2026-05-10 cf319ff1.
    is_agent_token_shape = (
        len(raw_token) == 64 and all(c in "0123456789abcdef" for c in raw_token.lower())
    )
    if is_agent_token_shape:
        path = request.url.path
        suggestion = path.replace("/api/v1/", "/api/v1/agent/", 1) if path.startswith("/api/v1/") else path
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Agent-Token darf User-Routes nicht nutzen. "
                f"Verwende {suggestion} statt {path} (agent-scoped endpoint)."
            ),
        )

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ── Agent Auth ────────────────────────────────────────────────────────────────

def hash_agent_token(token: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        token.encode(),
        salt,
        settings.agent_token_iterations,
        dklen=32,
    )
    return salt.hex() + ":" + dk.hex()


def generate_agent_token() -> tuple[str, str]:
    token = secrets.token_hex(32)
    salt = os.urandom(16)
    token_hash = hash_agent_token(token, salt)
    return token, token_hash


def verify_agent_token(token: str, token_hash: str) -> bool:
    try:
        salt_hex, _ = token_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = hash_agent_token(token, salt)
        return hmac.compare_digest(expected, token_hash)
    except Exception:
        return False


def _agent_token_cache_key(raw_token: str) -> str:
    """SHA256 des raw tokens als Cache-Key — nie das Token selbst speichern."""
    sha = hashlib.sha256(raw_token.encode()).hexdigest()
    return f"mc:agent-auth:{sha}"


async def _resolve_agent_from_token(token: str, session: AsyncSession) -> "Agent | None":
    """
    Agent via Token ermitteln — Redis-Cache zuerst, dann PBKDF2-Fallback.

    Cache: SHA256(token) → agent_id (TTL 5min)
    Dadurch: nur ein PBKDF2-Aufruf noetig statt N (bei N Agents) bei jedem Request.
    """
    from app.models.agent import Agent
    from app.redis_client import get_redis

    cache_key = _agent_token_cache_key(token)

    # 1. Redis-Cache pruefen (schnell, kein PBKDF2)
    try:
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached:
            agent_id = uuid.UUID(cached.decode() if isinstance(cached, bytes) else cached)
            agent = await session.get(Agent, agent_id)
            if agent and agent.agent_token_hash:
                return agent
            # Cache-Eintrag ungueltig (Agent geloescht oder Token geaendert)
            await redis.delete(cache_key)
    except Exception:
        pass  # Redis nicht verfuegbar — Fallback zu PBKDF2

    # 2. PBKDF2-Verifikation (teuer, aber einmalig pro Token)
    result = await session.exec(
        select(Agent).where(Agent.agent_token_hash.isnot(None))  # type: ignore[arg-type]
    )
    agents = result.all()

    for agent in agents:
        if agent.agent_token_hash and verify_agent_token(token, agent.agent_token_hash):
            # Ergebnis cachen (5min TTL)
            try:
                redis = await get_redis()
                await redis.set(cache_key, str(agent.id), ex=300)
            except Exception:
                pass
            return agent

    return None


async def require_agent(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    session: AsyncSession = Depends(get_session),
):
    from app.models.agent import Agent  # noqa: F401 (für Type-Hints)

    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    agent = await _resolve_agent_from_token(credentials.credentials, session)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent token")

    now = datetime.now(tz=timezone.utc)
    last_seen = agent.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_seen is None or (now - last_seen).total_seconds() > 30:
        agent.last_seen_at = now
        session.add(agent)
        await session.commit()

    return agent


async def require_user_or_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    token: str | None = Query(None, alias="token"),
    session: AsyncSession = Depends(get_session),
):
    from app.models.user import User

    raw_token: str | None = None
    if credentials:
        raw_token = credentials.credentials
    elif token:
        raw_token = token

    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    # 1. Try JWT
    try:
        payload = jwt.decode(raw_token, settings.jwt_secret_key, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if user_id:
            try:
                user = await session.get(User, uuid.UUID(user_id))
            except ValueError:
                # sub is not a UUID (e.g. "mcp-server") — check for admin role JWT
                if payload.get("role") == "admin":
                    result = await session.exec(select(User).where(User.role == "admin").limit(1))
                    admin = result.first()
                    if admin:
                        return {"type": "user", "user": admin}
                user = None
            if user and user.is_active:
                token_tv = payload.get("tv", 0)
                if token_tv == user.token_version:
                    return {"type": "user", "user": user}
    except JWTError:
        pass

    # 2. Legacy token (only if configured and non-empty)
    if (
        settings.local_auth_token
        and settings.local_auth_token not in ("", "dev-token", "change-me")
        and secrets.compare_digest(raw_token, settings.local_auth_token)
    ):
        return {"type": "user"}

    # 3. Agent token (mit Redis-Cache)
    agent = await _resolve_agent_from_token(raw_token, session)
    if agent is not None:
        now = utcnow()
        last_seen = agent.last_seen_at
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen is None or (now - last_seen).total_seconds() > 30:
            agent.last_seen_at = now
            session.add(agent)
            await session.commit()
        return {"type": "agent", "agent": agent}

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ── Aliases fuer semantische Klarheit ─────────────────────────────────────
# "Control Plane" = interne Steuerung (Board Lead Agent oder User).
# Semantisch identisch mit require_user_or_agent, aber expliziter Name.
require_user_or_control_plane = require_user_or_agent
require_control_plane = require_agent  # Nur Agents (interne Steuerung)
