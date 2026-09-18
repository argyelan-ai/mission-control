import hashlib
import hmac
import os
import re
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
from app.database import get_session, release_session
from app.utils import utcnow

bearer_scheme = HTTPBearer(auto_error=False)


# ── JWT Config ────────────────────────────────────────────────────────────────

JWT_ALGORITHM = "HS256"

# Non-UUID `sub` values that may act as the first admin user via the legacy
# admin-role JWT fallback (see require_user / require_user_or_agent). The only
# legitimate issuer is the host-side MCP server, scripts/mc-mcp.py. Keep this
# list closed: a signed admin-role claim with an arbitrary sub must not
# resolve to a real admin account.
LEGACY_ADMIN_NON_UUID_SUBS = frozenset({"mcp-server"})


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


# ── Narrow-purpose view tokens ──────────────────────────────────────────────
#
# Short-lived JWTs bound to a single resource (e.g. one bench entry) — for
# links that are copyable/shareable by design (an <a href> a bare browser
# tab opens) where a full-lifetime session JWT would be a standing credential
# leak (link gets copied/shared/kept in history -> full admin access until
# the session token itself expires/is revoked). Same secret + algorithm as
# create_access_token, but a distinct "scope" claim that require_user
# explicitly refuses (see below) — a scoped token only ever authorizes the
# one dependency built for it, never the general session routes.

BENCH_VIEW_SCOPE = "bench_view"


def create_bench_view_token(
    user_id: str,
    challenge_id: str,
    entry_id: str,
    expires_minutes: int = 30,
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload = {
        "sub": user_id,
        "scope": BENCH_VIEW_SCOPE,
        "challenge_id": challenge_id,
        "entry_id": entry_id,
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

async def _authenticate_user(
    request: Request,
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    token: str | None = Query(None, alias="token"),
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
        if payload.get("scope"):
            # Narrow-purpose tokens (e.g. create_bench_view_token) are only
            # valid on their own dedicated dependency — never as a general
            # session token, even though they're signed with the same
            # secret. Fall through exactly like a decode failure.
            raise JWTError("scoped token not valid for session auth")
        user_id = payload.get("sub")
        if user_id:
            try:
                user = await session.get(User, uuid.UUID(user_id))
            except ValueError:
                # sub is not a UUID. Only allow-listed non-UUID service
                # identities may act as the first admin user here (the
                # host-side MCP server self-signs such a token, see
                # scripts/mc-mcp.py). Any other non-UUID sub is rejected —
                # a signed admin-role claim alone must not be a generic
                # "become the first admin account" template.
                if (
                    user_id in LEGACY_ADMIN_NON_UUID_SUBS
                    and payload.get("role") == "admin"
                ):
                    result = await session.exec(select(User).where(User.role == "admin").limit(1))
                    admin = result.first()
                    if admin:
                        return admin
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
            if not user or not user.is_active:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
            # token_version check — logout invalidates all old tokens
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
        match = _agent_route_match(request.app, suggestion)
        if match == "exact":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    f"Agent-Token darf User-Routes nicht nutzen. "
                    f"Verwende {suggestion} statt {path} (agent-scoped endpoint)."
                ),
            )
        if match == "prefix":
            # No route with this exact shape, but agent-scoped endpoints
            # live under this namespace — point at the family, not at a
            # made-up concrete path.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    f"Agent-Token darf User-Routes nicht nutzen. "
                    f"Agent-scoped endpoints für {path} liegen unter "
                    f"{suggestion}/... (agent-scoped endpoint)."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Agent-Token darf User-Routes nicht nutzen. "
                f"Für {path} gibt es keinen agent-scoped Endpoint — "
                f"das macht der Operator-Login."
            ),
        )

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    token: str | None = Query(None, alias="token"),
    # use_cache=False: this session is the auth dep's OWN, not the shared
    # one — require_user releases (closes) it, and a shared instance would
    # detach ORM objects the endpoint still re-attaches via session.add().
    session: AsyncSession = Depends(get_session, use_cache=False),
):
    """Authenticate the operator and RELEASE the DB connection before the
    endpoint runs. FastAPI unwinds ``Depends(get_session)`` only after the
    response has fully streamed (SSE/WS live for minutes), so without this
    release the auth lookup's implicit transaction pins a pool connection
    for the whole stream — on every auth-protected stream endpoint at once
    (13 pool warnings/hour, holds up to 405 s — finding 2026-09-16)."""
    user = await _authenticate_user(request, session, credentials, token)
    await release_session(session, route=request.url.path)
    return user


def _agent_route_match(app: object, path: str) -> str | None:
    """How the app's registered /api/v1/agent routes relate to `path`.

    The 401 hint for agent tokens must only ever point at routes that
    actually exist — a suggestion to a 404 route sent agents hunting for
    alternate ways in (scoping finding 2026-09-15). Returns:
      "exact"  — a registered route matches the path shape
                 (concrete segments vs {param} templates)
      "prefix" — no exact match, but registered agent routes start with
                 this path + "/" (an endpoint family exists here)
      None     — nothing agent-scoped anywhere near this path
    """
    best: str | None = None
    for route in getattr(app, "routes", []):
        candidates = [getattr(route, "path", None)]
        # Routers are included as _IncludedRouter wrappers; their real
        # paths live on the wrapped APIRouter.
        original = getattr(route, "original_router", None)
        if original is not None:
            candidates = [getattr(sub, "path", None) for sub in getattr(original, "routes", [])]
        for route_path in candidates:
            if not route_path or not route_path.startswith("/api/v1/agent"):
                continue
            parts = re.split(r"(\{[^}]+\})", route_path)
            pattern = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
            if re.fullmatch(pattern, path):
                return "exact"
            if best is None and route_path.startswith(path + "/"):
                best = "prefix"
    return best


async def require_bench_view(
    challenge_id: uuid.UUID,
    entry_id: uuid.UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    token: str | None = Query(None, alias="token"),
    session: AsyncSession = Depends(get_session),
):
    """Auth for the bench entry HTML view route only: accepts EITHER a
    normal operator session (JWT/legacy token, via require_user) OR a
    create_bench_view_token() scoped to this exact challenge_id/entry_id.
    challenge_id/entry_id are bound in the token itself, so a leaked view
    link only ever exposes the one artifact it was minted for."""
    raw_token = credentials.credentials if credentials else token
    if raw_token:
        try:
            payload = jwt.decode(raw_token, settings.jwt_secret_key, algorithms=[JWT_ALGORITHM])
            if (
                payload.get("scope") == BENCH_VIEW_SCOPE
                and payload.get("challenge_id") == str(challenge_id)
                and payload.get("entry_id") == str(entry_id)
            ):
                return payload
        except JWTError:
            pass
    user = await require_user(request, credentials, token, session)
    # require_user already released; this covers the bench-token path where
    # this dependency is the only DB user (release is idempotent).
    await release_session(session, route=request.url.path)
    return user


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
    """SHA256 of the raw token as cache key — never store the token itself."""
    sha = hashlib.sha256(raw_token.encode()).hexdigest()
    return f"mc:agent-auth:{sha}"


async def _resolve_agent_from_token(token: str, session: AsyncSession) -> "Agent | None":
    """
    Resolve agent via token — Redis cache first, then PBKDF2 fallback.

    Cache: SHA256(token) → agent_id (TTL 5min)
    This means: only one PBKDF2 call needed instead of N (for N agents) per request.
    """
    from app.models.agent import Agent
    from app.redis_client import get_redis

    cache_key = _agent_token_cache_key(token)

    # 1. Check Redis cache (fast, no PBKDF2)
    try:
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached:
            agent_id = uuid.UUID(cached.decode() if isinstance(cached, bytes) else cached)
            agent = await session.get(Agent, agent_id)
            if agent and agent.agent_token_hash:
                return agent
            # Cache entry invalid (agent deleted or token changed)
            await redis.delete(cache_key)
    except Exception:
        pass  # Redis unavailable — fall back to PBKDF2

    # 2. PBKDF2 verification (expensive, but only once per token)
    result = await session.exec(
        select(Agent).where(Agent.agent_token_hash.isnot(None))  # type: ignore[arg-type]
    )
    agents = result.all()

    for agent in agents:
        if agent.agent_token_hash and verify_agent_token(token, agent.agent_token_hash):
            # Cache the result (5min TTL)
            try:
                redis = await get_redis()
                await redis.set(cache_key, str(agent.id), ex=300)
            except Exception:
                pass
            return agent

    return None


async def _authenticate_agent(
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
):
    from app.models.agent import Agent  # noqa: F401 (for type hints)

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


async def require_agent(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    session: AsyncSession = Depends(get_session),
):
    """Agent-token auth. NOTE: deliberately NO release_session here —
    no agent-authenticated stream endpoint exists (nothing to heal), and
    agent endpoints legitimately re-attach the returned Agent row via
    session.add(agent); closing the shared session would detach it and
    break them (InvalidRequestError)."""
    return await _authenticate_agent(session, credentials)


async def _authenticate_user_or_agent(
    request: Request,
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    token: str | None = Query(None, alias="token"),
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
                # sub is not a UUID. Same allowlist as require_user: only
                # known non-UUID service identities may resolve to the first
                # admin user (scripts/mc-mcp.py), nothing else.
                if (
                    user_id in LEGACY_ADMIN_NON_UUID_SUBS
                    and payload.get("role") == "admin"
                ):
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

    # 3. Agent token (with Redis cache)
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


async def require_user_or_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    token: str | None = Query(None, alias="token"),
    # use_cache=False — see require_user.
    session: AsyncSession = Depends(get_session, use_cache=False),
):
    """Dual auth with early connection release (see require_user)."""
    result = await _authenticate_user_or_agent(request, session, credentials, token)
    await release_session(session, route=request.url.path)
    return result


# ── Aliases for semantic clarity ─────────────────────────────────────────
# "Control Plane" = internal control (Board Lead agent or user).
# Semantically identical to require_user_or_agent, but a more explicit name.
require_user_or_control_plane = require_user_or_agent
require_control_plane = require_agent  # Agents only (internal control)
