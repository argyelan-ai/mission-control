"""
API-Key und Secrets Verwaltung.

Secrets werden Fernet-verschlüsselt in der DB gespeichert.
Im Frontend werden Werte nur maskiert angezeigt (z.B. "****abcd").
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user, require_role, Role
from app.database import get_session
from app.models.secret import Secret
from app.services.encryption import encrypt, decrypt, safe_decrypt, mask

router = APIRouter(prefix="/api/v1", tags=["secrets"])


# ── Provider Templates ───────────────────────────────────────────────────────

PROVIDER_TEMPLATES = [
    {
        "provider": "anthropic-claude-code",
        "key": "claude_code_oauth_token",
        "label": "Claude Code OAuth Token",
        "description": "Fuer cli-bridge Agents mit anthropic-* Runtime (claude setup-token)",
        "placeholder": "sk-ant-oat01-...",
    },
    {
        "provider": "openai",
        "key": "openai_api_key",
        "label": "OpenAI API Key",
        "description": "Für GPT-4o, o1, o3 etc.",
        "placeholder": "sk-...",
    },
    {
        "provider": "google",
        "key": "google_api_key",
        "label": "Google AI API Key",
        "description": "Für Gemini Models",
        "placeholder": "AIza...",
    },
    {
        "provider": "ollama",
        "key": "ollama_api_key",
        "label": "Ollama Cloud API Key",
        "description": "Für Ollama Cloud Flatrate (ollama.com)",
        "placeholder": "oll-...",
    },
    {
        "provider": "openrouter",
        "key": "openrouter_api_key",
        "label": "OpenRouter API Key",
        "description": "Multi-Provider-Gateway (Claude, GPT-4, Llama, Mistral, ...)",
        "placeholder": "sk-or-v1-...",
    },
    {
        "provider": "anthropic",
        "key": "anthropic_api_key",
        "label": "Anthropic API Key",
        "description": "Für Claude (Sonnet/Opus/Haiku) direkt über Anthropic",
        "placeholder": "sk-ant-...",
    },
    {
        "provider": "discord",
        "key": "discord_bot_token",
        "label": "Discord Bot Token",
        "description": "Für Agent Council Discord-Integration",
        "placeholder": "MTQ3...",
    },
    {
        "provider": "openclaw",
        "key": "openclaw_token",
        "label": "OpenClaw Gateway Token",
        "description": "Auth-Token für den OpenClaw Gateway",
        "placeholder": "oc-...",
    },
]


class SecretCreate(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$")
    value: str
    provider: str | None = None
    label: str | None = None
    description: str | None = None


class SecretUpdate(BaseModel):
    value: str | None = None
    label: str | None = None
    description: str | None = None


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/secrets/providers")
async def list_provider_templates(current_user=Depends(require_user)):
    """Provider-Templates für das UI (welche Keys gibt es)."""
    return PROVIDER_TEMPLATES


@router.get("/secrets")
async def list_secrets(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Alle Secrets auflisten (Werte maskiert)."""
    result = await session.exec(select(Secret).order_by(Secret.key))
    secrets = result.all()
    items = []
    for s in secrets:
        decrypted = safe_decrypt(s.encrypted_value)
        items.append({
            "id": str(s.id),
            "key": s.key,
            "value_masked": mask(decrypted) if decrypted else "****[decrypt error]",
            "provider": s.provider,
            "label": s.label,
            "description": s.description,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        })
    return items


@router.post("/secrets", status_code=status.HTTP_201_CREATED)
async def create_secret(
    payload: SecretCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Neues Secret anlegen (verschlüsselt)."""
    # Prüfen ob Key schon existiert
    result = await session.exec(select(Secret).where(Secret.key == payload.key))
    if result.first():
        raise HTTPException(status_code=409, detail=f"Secret '{payload.key}' existiert bereits")

    secret = Secret(
        key=payload.key,
        encrypted_value=encrypt(payload.value),
        provider=payload.provider,
        label=payload.label,
        description=payload.description,
    )
    session.add(secret)
    await session.commit()
    await session.refresh(secret)

    return {
        "id": str(secret.id),
        "key": secret.key,
        "value_masked": mask(payload.value),
        "provider": secret.provider,
        "label": secret.label,
    }


@router.patch("/secrets/{key}")
async def update_secret(
    key: str,
    payload: SecretUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Secret aktualisieren."""
    result = await session.exec(select(Secret).where(Secret.key == key))
    secret = result.first()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    if payload.value is not None:
        secret.encrypted_value = encrypt(payload.value)
    if payload.label is not None:
        secret.label = payload.label
    if payload.description is not None:
        secret.description = payload.description

    secret.updated_at = datetime.now(timezone.utc)
    session.add(secret)
    await session.commit()

    decrypted = safe_decrypt(secret.encrypted_value)
    return {
        "id": str(secret.id),
        "key": secret.key,
        "value_masked": mask(decrypted) if decrypted else "****[decrypt error]",
        "provider": secret.provider,
        "label": secret.label,
    }


@router.delete("/secrets/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    key: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Secret löschen."""
    result = await session.exec(select(Secret).where(Secret.key == key))
    secret = result.first()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    await session.delete(secret)
    await session.commit()
