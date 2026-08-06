"""
API key and secrets management.

Secrets are Fernet-encrypted and stored in the DB.
The frontend only ever displays masked values (e.g. "****abcd").
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
        "description": "For cli-bridge agents with an anthropic-* runtime (claude setup-token)",
        "placeholder": "sk-ant-oat01-...",
    },
    {
        "provider": "openai",
        "key": "openai_api_key",
        "label": "OpenAI API Key",
        "description": "For GPT-4o, o1, o3 etc.",  # model-catalog: allow (UI-Text zum API-Key, kein Modell-Selektor)
        "placeholder": "sk-...",
    },
    {
        "provider": "google",
        "key": "google_api_key",
        "label": "Google AI API Key",
        "description": "For Gemini models",
        "placeholder": "AIza...",
    },
    {
        "provider": "ollama",
        "key": "ollama_api_key",
        "label": "Ollama Cloud API Key",
        "description": "For the Ollama Cloud flat rate (ollama.com)",
        "placeholder": "oll-...",
    },
    {
        "provider": "openrouter",
        "key": "openrouter_api_key",
        "label": "OpenRouter API Key",
        "description": "Multi-provider gateway (Claude, GPT-4, Llama, Mistral, ...)",  # model-catalog: allow (UI-Text zum API-Key, kein Modell-Selektor)
        "placeholder": "sk-or-v1-...",
    },
    {
        "provider": "anthropic",
        "key": "anthropic_api_key",
        "label": "Anthropic API Key",
        "description": "For Claude (Sonnet/Opus/Haiku) directly via the Anthropic API",
        "placeholder": "sk-ant-...",
    },
    {
        "provider": "discord",
        "key": "discord_bot_token",
        "label": "Discord Bot Token",
        "description": "For the per-agent Discord channels integration",
        "placeholder": "MTQ3...",
    },
    {
        "provider": "openclaw",
        "key": "openclaw_token",
        "label": "OpenClaw Gateway Token",
        "description": "Auth token for the OpenClaw gateway (legacy)",
        "placeholder": "oc-...",
    },
    {
        "provider": "github",
        "key": "github_token",
        "label": "GitHub Personal Access Token",
        "description": "For the agent git workflow (repos, branches, PRs) — can also be set via Settings → GitHub",
        "placeholder": "ghp_... / github_pat_...",
    },
    {
        "provider": "github",
        "key": "github_owner",
        "label": "GitHub Owner",
        "description": "GitHub user/org under which MC creates project repos — can also be set via Settings → GitHub",
        "placeholder": "my-github-user",
    },
    {
        "provider": "slack",
        "key": "slack_bot_token",
        "label": "Slack Bot Token",
        "description": (
            "Lets MC post as your agents in Slack and read the channels the app "
            "was invited to. From OAuth & Permissions -> Bot User OAuth Token."
        ),
        "placeholder": "xoxb-...",
    },
    {
        "provider": "slack",
        "key": "slack_app_token",
        "label": "Slack App-Level Token",
        "description": (
            "Opens the Socket Mode connection so Slack can deliver messages to a "
            "self-hosted MC without a public URL. From Basic Information -> "
            "App-Level Tokens, scope connections:write."
        ),
        "placeholder": "xapp-...",
    },
    {
        "provider": "telegram",
        "key": "telegram_bot_token",
        "label": "Telegram Bot Token (commands + approvals)",
        "description": (
            "The command bot: approval buttons and operator notifications. "
            "From @BotFather -> /newbot. Stored encrypted; applied to the "
            "running backend immediately."
        ),
        "placeholder": "1234567890:AA...",
    },
    {
        "provider": "telegram",
        "key": "telegram_reports_bot_token",
        "label": "Telegram Reports Bot Token",
        "description": (
            "The reports bot: final agent reports and deliverables. A second "
            "bot keeps info delivery out of the command chat. From @BotFather."
        ),
        "placeholder": "1234567890:AA...",
    },
    {
        "provider": "x",
        "key": "x_api_key",
        "label": "X (Twitter) API Key",
        "description": "Consumer key from the X developer portal — for the X post publisher (ADR-065)",
        "placeholder": "...",
    },
    {
        "provider": "x",
        "key": "x_api_secret",
        "label": "X (Twitter) API Key Secret",
        "description": "Consumer secret from the X developer portal — for the X post publisher (ADR-065)",
        "placeholder": "...",
    },
    {
        "provider": "x",
        "key": "x_access_token",
        "label": "X (Twitter) Access Token",
        "description": "OAuth 1.0a access token of the posting account — for the X post publisher (ADR-065)",
        "placeholder": "...",
    },
    {
        "provider": "x",
        "key": "x_access_token_secret",
        "label": "X (Twitter) Access Token Secret",
        "description": "OAuth 1.0a access token secret of the posting account — for the X post publisher (ADR-065)",
        "placeholder": "...",
    },
]


def _maybe_invalidate_github_cache(key: str) -> None:
    """github_owner/github_token edits must apply live (ADR-055)."""
    if key in ("github_owner", "github_token"):
        from app.services.github_config import invalidate_github_config_cache
        invalidate_github_config_cache()


async def _maybe_apply_channel_config(session, key: str, *, deleted: bool = False) -> None:
    """Telegram token edits must apply live (channels settings page).

    On delete, the singleton first falls back to the env default (a fresh
    Settings() re-reads .env) — apply_channel_overrides only ever SETS
    present tokens and would otherwise leave the deleted one in memory.
    Never raises: the secret write itself already succeeded.
    """
    from app.services.channel_config import TELEGRAM_TOKEN_SECRET_KEYS

    if key not in TELEGRAM_TOKEN_SECRET_KEYS:
        return
    try:
        from app.config import Settings, settings
        from app.services.channel_config import apply_channel_overrides

        if deleted:
            setattr(settings, key, getattr(Settings(), key, ""))
        await apply_channel_overrides(session)
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger("mc.channels").warning(
            "channel config apply after secret %s failed: %s", key, e
        )


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
    """Provider templates for the UI (which keys exist)."""
    return PROVIDER_TEMPLATES


@router.get("/secrets")
async def list_secrets(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """List all secrets (values masked)."""
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
    """Create a new secret (encrypted)."""
    # Check if key already exists
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
    _maybe_invalidate_github_cache(secret.key)
    await _maybe_apply_channel_config(session, secret.key)

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
    """Update a secret."""
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
    _maybe_invalidate_github_cache(secret.key)
    await _maybe_apply_channel_config(session, secret.key)

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
    """Delete a secret."""
    result = await session.exec(select(Secret).where(Secret.key == key))
    secret = result.first()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    await session.delete(secret)
    await session.commit()
    _maybe_invalidate_github_cache(key)
    await _maybe_apply_channel_config(session, key, deleted=True)
