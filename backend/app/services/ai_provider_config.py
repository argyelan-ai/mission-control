"""Runtime provider configuration for MC's OWN AI functions.

Same three layers, same order, same mechanics as ``channel_config`` — that
module is the template, this is deliberately not a second pattern:

  1. **pydantic env defaults** (``app/config.py``) — a plain .env install keeps
     working exactly as before; env is the default, never the master.
  2. **app_settings rows** (operator decisions from Settings -> AI providers),
     applied onto the live ``settings`` singleton so every read site sees them.
  3. **secrets-stored keys** (Fernet, ADR-033) for the auth material.

Only keys in ``AI_PROVIDER_SETTING_FIELDS`` may be overridden — the allowlist
is the security boundary, exactly as in ``channel_config``.

── Warum die Keys NICHT auf das Settings-Singleton wandern ────────────────
Channel config applies Telegram tokens onto ``settings`` because dozens of
call sites read ``settings.telegram_bot_token``. The two keys here
(``hf_token``, ``ollama_api_key``) must NOT get that treatment: ADR-056
Finding 5 removed the global ``ollama_api_key`` fallback because ANY
openai-protocol runtime — including keyless local vLLM/LM Studio — silently
inherited a paid cloud key as its Bearer token. Putting the key on a global
object re-opens exactly that door. So the keys are read through the two
named accessors below, and only the MC-function consumers call them:

  * ``get_hf_token``        — model catalog search, repo files, GGUF download
  * ``get_ollama_api_key``  — the ollama_cloud arm of embeddings/insights

``harness_compat.resolve_provider_credentials`` (the agent-runtime path) does
not import this module, and ``tests/test_provider_credentials.py`` pins that.

── Provider-Auflösung ────────────────────────────────────────────────────
An empty ``ai_embeddings_url`` / ``_model`` / ``ai_insights_model`` means
"inherit the function's legacy source" (spark_embedding_url, the runtime
model resolver). That is what keeps the defaults identical to today's
behaviour while making every deviation an explicit operator decision.
"""
from __future__ import annotations

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.app_setting import AppSetting

logger = logging.getLogger("mc.ai_provider_config")

# Providers per function. "off" exists only for insights: embeddings without a
# provider is not a mode, it is a broken memory system — the caller already
# degrades fail-soft when the endpoint is unreachable.
EMBEDDING_PROVIDERS: tuple[str, ...] = ("spark", "ollama_cloud")
INSIGHTS_PROVIDERS: tuple[str, ...] = ("spark", "ollama_cloud", "off")

# key -> allowed values (None = free text). Every key must exist on Settings.
AI_PROVIDER_SETTING_FIELDS: dict[str, tuple[str, ...] | None] = {
    "ai_embeddings_provider": EMBEDDING_PROVIDERS,
    "ai_embeddings_url": None,
    "ai_embeddings_model": None,
    "ai_insights_provider": INSIGHTS_PROVIDERS,
    "ai_insights_model": None,
}

# Secrets read by the named MC-function consumers below — never by a runtime.
HF_TOKEN_SECRET_KEY = "hf_token"
OLLAMA_API_KEY_SECRET_KEY = "ollama_api_key"


async def stored_overrides(session: AsyncSession) -> dict[str, str]:
    """The operator's saved decisions. Unknown keys are skipped with a warning
    (an old row must never break startup)."""
    rows = (await session.exec(select(AppSetting))).all()
    out: dict[str, str] = {}
    for row in rows:
        if row.key not in AI_PROVIDER_SETTING_FIELDS:
            continue
        out[row.key] = row.value
    return out


async def apply_ai_provider_overrides(session: AsyncSession) -> dict[str, str]:
    """DB state -> live ``settings`` singleton. Returns what was applied.

    Never raises: a broken row degrades to "env value stays", logged — provider
    config must not take the backend down.
    """
    applied: dict[str, str] = {}
    try:
        for key, value in (await stored_overrides(session)).items():
            allowed = AI_PROVIDER_SETTING_FIELDS[key]
            if allowed is not None and value not in allowed:
                logger.warning(
                    "app_settings: %r=%r ist kein gueltiger Wert (%s) — ignoriert",
                    key, value, ", ".join(allowed),
                )
                continue
            setattr(settings, key, value)
            applied[key] = value
    except Exception as e:  # noqa: BLE001 — degrade to env defaults
        logger.warning("ai provider overrides nicht anwendbar: %s", e)
    return applied


async def save_ai_provider_settings(
    session: AsyncSession, updates: dict[str, str]
) -> dict[str, str]:
    """Validate against the allowlist, upsert, apply. Returns applied values.

    Raises ValueError on an unknown key or an invalid provider value — the
    router turns that into a 422; nothing is written then (all-or-nothing).
    """
    unknown = sorted(set(updates) - set(AI_PROVIDER_SETTING_FIELDS))
    if unknown:
        raise ValueError(f"Unbekannte AI-Provider-Settings: {', '.join(unknown)}")

    for key, value in updates.items():
        allowed = AI_PROVIDER_SETTING_FIELDS[key]
        if allowed is not None and str(value) not in allowed:
            raise ValueError(
                f"{key}: '{value}' ist kein gueltiger Wert ({', '.join(allowed)})"
            )

    for key, value in updates.items():
        serialized = str(value)
        row = (
            await session.exec(select(AppSetting).where(AppSetting.key == key))
        ).one_or_none()
        if row is None:
            session.add(AppSetting(key=key, value=serialized))
        else:
            row.value = serialized
            session.add(row)
    await session.commit()
    return await apply_ai_provider_overrides(session)


# ── Named secret consumers (ADR-056 boundary) ─────────────────────────────


async def _secret(key: str) -> str | None:
    """Read one secret in its own session. Never raises — a vault hiccup makes
    the caller fall back to anonymous/keyless, which is the pre-PR behaviour."""
    try:
        from app.database import async_session_maker
        from app.services.secrets_helper import get_secret_plaintext_by_key

        async with async_session_maker() as session:
            return await get_secret_plaintext_by_key(session, key)
    except Exception as e:  # noqa: BLE001
        logger.warning("secret %s nicht lesbar: %s", key, e)
        return None


async def get_hf_token() -> str | None:
    """The HuggingFace token, for the model catalog + download call sites only."""
    return await _secret(HF_TOKEN_SECRET_KEY)


async def hf_auth_headers() -> dict[str, str]:
    """``Authorization`` header for huggingface.co, or ``{}`` when no token is
    stored. No token = today's behaviour exactly: anonymous, public repos only
    — a degradation, never an error."""
    token = await get_hf_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def get_ollama_api_key() -> str | None:
    """The Ollama Cloud key, for the ollama_cloud arm of embeddings/insights.

    This is the explicit consumer ADR-056 Finding 5 left room for: a named
    function asks for the key by name. It is never handed to a runtime.
    """
    return await _secret(OLLAMA_API_KEY_SECRET_KEY)


# ── Resolution: which endpoint does a function actually talk to? ──────────


def embeddings_provider_key() -> str:
    key = (getattr(settings, "ai_embeddings_provider", "") or "spark").strip()
    return key if key in EMBEDDING_PROVIDERS else "spark"


def insights_provider_key() -> str:
    key = (getattr(settings, "ai_insights_provider", "") or "spark").strip()
    return key if key in INSIGHTS_PROVIDERS else "spark"


def embeddings_url() -> str:
    """Full POST URL for the embeddings call of the ACTIVE provider.

    Override wins; otherwise the provider's own default — for spark that is
    ``spark_embedding_url``, the value this function used to read directly.
    """
    override = (getattr(settings, "ai_embeddings_url", "") or "").strip()
    if override:
        return override
    if embeddings_provider_key() == "ollama_cloud":
        return f"{settings.ollama_cloud_url.rstrip('/')}/v1/embeddings"
    return settings.spark_embedding_url


def embeddings_model() -> str:
    override = (getattr(settings, "ai_embeddings_model", "") or "").strip()
    if override:
        return override
    if embeddings_provider_key() == "ollama_cloud":
        return settings.ollama_cloud_embedding_model
    return settings.spark_embedding_model


def insights_model_override() -> str:
    """Operator-pinned insights model, or "" for "let the provider decide"
    (spark: the DB-driven runtime resolver — the good path this PR copies)."""
    return (getattr(settings, "ai_insights_model", "") or "").strip()
