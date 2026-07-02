"""Authentication endpoints: login, register, user management."""

import time
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import (
    Role,
    create_access_token,
    hash_password,
    require_role,
    require_user,
    verify_password,
)
from app.config import settings
from app.database import get_session
from app.models.user import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# ── Rate Limiting ────────────────────────────────────────────────────────────
# Simple in-memory rate limiter for login attempts.
# Tracks failed attempts per IP. After 5 failures within 15 minutes, blocks for 15 min.

_login_attempts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW = 900  # 15 minutes


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    # Clean old attempts
    _login_attempts[client_ip] = [
        t for t in _login_attempts[client_ip] if now - t < _RATE_LIMIT_WINDOW
    ]
    if len(_login_attempts[client_ip]) >= _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Zu viele Login-Versuche. Bitte 15 Minuten warten.",
        )


def _record_failed_attempt(client_ip: str) -> None:
    _login_attempts[client_ip].append(time.time())


def _clear_attempts(client_ip: str) -> None:
    _login_attempts.pop(client_ip, None)


# ── Payloads ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str = Field(min_length=6)


class CreateUserRequest(BaseModel):
    email: str
    name: str
    password: str = Field(min_length=6)
    role: str = "operator"


class UpdateProfileRequest(BaseModel):
    """Self-update: users can change their own profile fields."""
    name: str | None = None
    preferred_name: str | None = None
    timezone: str | None = None
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=6)


class UpdateUserRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


def _user_dict(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "preferred_name": user.preferred_name,
        "role": user.role,
        "avatar_url": user.avatar_url,
        "timezone": user.timezone,
    }


# ── Public Endpoints ──────────────────────────────────────────────────────────

@router.get("/setup-required")
async def check_setup_required(session: AsyncSession = Depends(get_session)):
    """Check if any users with passwords exist. If not, frontend shows registration."""
    count = (
        await session.exec(
            select(func.count(User.id)).where(User.password_hash.isnot(None))  # type: ignore[arg-type]
        )
    ).one()
    return {"setup_required": count == 0}


@router.post("/register", response_model=TokenResponse)
async def register(payload: RegisterRequest, session: AsyncSession = Depends(get_session)):
    """Create first admin user. Only works when no users with passwords exist."""
    user_count = (
        await session.exec(
            select(func.count(User.id)).where(User.password_hash.isnot(None))  # type: ignore[arg-type]
        )
    ).one()

    if user_count > 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration geschlossen. Bitte einen Admin kontaktieren.",
        )

    # Check for existing user record without password (e.g. from settings)
    existing = (await session.exec(select(User).where(User.email == payload.email))).first()

    if existing and existing.password_hash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User existiert bereits.")

    if existing:
        # Activate existing user record
        existing.password_hash = hash_password(payload.password)
        existing.name = payload.name
        existing.role = "admin"
        existing.is_active = True
        session.add(existing)
        user = existing
    else:
        user = User(
            email=payload.email,
            name=payload.name,
            password_hash=hash_password(payload.password),
            role="admin",
        )
        session.add(user)

    await session.commit()
    await session.refresh(user)

    token = create_access_token(str(user.id), user.role, user.token_version)
    return TokenResponse(access_token=token, user=_user_dict(user))


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Authenticate with email and password, returns JWT."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    result = await session.exec(select(User).where(User.email == payload.email))
    user = result.first()

    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        _record_failed_attempt(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falsche E-Mail oder Passwort.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account deaktiviert.",
        )

    _clear_attempts(client_ip)
    token = create_access_token(str(user.id), user.role, user.token_version)

    # SSE-Auth Cookie setzen (HttpOnly — kein JS-Zugriff, kein Token in URL-Logs)
    response = JSONResponse(
        content=TokenResponse(access_token=token, user=_user_dict(user)).model_dump(),
    )
    response.set_cookie(
        key="mc_sse_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_access_token_expire_minutes * 60,
        path="/",
    )
    return response


# ── Authenticated Endpoints ──────────────────────────────────────────────────


@router.post("/logout")
async def logout(
    current_user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Logout — invalidiert alle bestehenden JWTs via token_version increment."""
    current_user.token_version += 1
    session.add(current_user)
    await session.commit()

    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie("mc_sse_token", path="/")
    return response


@router.get("/me")
async def get_me(current_user: User = Depends(require_user)):
    """Get current authenticated user info."""
    return _user_dict(current_user)


@router.patch("/me")
async def update_me(
    payload: UpdateProfileRequest,
    current_user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Update own profile (name, preferred_name, timezone, password)."""
    if payload.name is not None:
        current_user.name = payload.name.strip()
    if payload.preferred_name is not None:
        current_user.preferred_name = payload.preferred_name.strip() or None
    if payload.timezone is not None:
        current_user.timezone = payload.timezone

    # Password change requires current password verification
    if payload.new_password is not None:
        if not payload.current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Aktuelles Passwort ist erforderlich.",
            )
        if not current_user.password_hash or not verify_password(
            payload.current_password, current_user.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Aktuelles Passwort ist falsch.",
            )
        current_user.password_hash = hash_password(payload.new_password)

    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)
    return _user_dict(current_user)


# ── Admin: User Management ───────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_role(Role.ADMIN)),
):
    """List all users (admin only)."""
    result = await session.exec(select(User).order_by(User.created_at))
    users = result.all()
    return [
        {
            **_user_dict(u),
            "is_active": u.is_active,
            "has_password": u.password_hash is not None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: CreateUserRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_role(Role.ADMIN)),
):
    """Create a new user (admin only)."""
    existing = (await session.exec(select(User).where(User.email == payload.email))).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="E-Mail bereits vergeben.")

    if payload.role not in ("admin", "operator", "viewer"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ungültige Rolle.")

    user = User(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {**_user_dict(user), "is_active": user.is_active}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    payload: UpdateUserRequest,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_role(Role.ADMIN)),
):
    """Update a user's role, active status, or password (admin only)."""
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User nicht gefunden.")

    if payload.name is not None:
        user.name = payload.name
    if payload.role is not None:
        if payload.role not in ("admin", "operator", "viewer"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ungültige Rolle.")
        # Prevent removing the last admin
        if user.role == "admin" and payload.role != "admin":
            admin_count = (
                await session.exec(
                    select(func.count(User.id)).where(User.role == "admin", User.is_active == True)  # noqa: E712
                )
            ).one()
            if admin_count <= 1:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Letzten Admin kann man nicht degradieren.")
        user.role = payload.role
    if payload.is_active is not None:
        # Prevent deactivating the last admin
        if user.role == "admin" and not payload.is_active:
            admin_count = (
                await session.exec(
                    select(func.count(User.id)).where(User.role == "admin", User.is_active == True)  # noqa: E712
                )
            ).one()
            if admin_count <= 1:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Letzten Admin kann man nicht deaktivieren.")
        user.is_active = payload.is_active
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)

    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {**_user_dict(user), "is_active": user.is_active}
