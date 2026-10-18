"""AI providers settings — the operator's switchboard for MC's OWN AI functions.

The twin of ``routers/channels.py``, for the other half of the question "what
does MC talk to": channels covers who MC talks to (Slack, Telegram), this
covers what does its thinking (embeddings for memory search, the daily
insights distillation, the HuggingFace model catalog).

Admin-only, like every endpoint that touches System-Token state (ADR-033).
GET returns the EFFECTIVE runtime values (env default + DB override already
applied) plus provenance; PUT saves operator decisions into ``app_settings``
and applies them to the running process immediately (no restart).

Both connection tests mirror the channels contract exactly: always HTTP 200,
a failure is a reportable state, and no response ever contains key material.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.config import settings
from app.database import get_session
from app.services.ai_provider_config import (
    AI_PROVIDER_SETTING_FIELDS,
    EMBEDDING_PROVIDERS,
    HF_TOKEN_SECRET_KEY,
    INSIGHTS_PROVIDERS,
    OLLAMA_API_KEY_SECRET_KEY,
    embeddings_provider_key,
    get_embeddings_api_key,
    get_embeddings_cloud_api_key,
    get_hf_token,
    get_ollama_api_key,
    insights_provider_key,
    save_ai_provider_settings,
    stored_overrides,
)
from app.services.embedding_provider import embedding_provider_catalog

logger = logging.getLogger("mc.ai_providers")

router = APIRouter(prefix="/api/v1/ai-providers", tags=["ai-providers"])


class AiProviderSettingsUpdate(BaseModel):
    """Partial update; only allowlisted keys, enforced in the service."""

    settings: dict[str, str]


@router.get("/settings")
async def get_ai_provider_settings(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Effective values + override provenance + which keys are stored.

    ``values`` are what the system ACTS on right now; ``overridden`` names the
    keys the operator has pinned via this page (everything else is env/default).
    Keys never appear here — they live behind the secrets API, masked.
    """
    from app.services.intelligence import effective_insights_model

    values = {key: getattr(settings, key, None) for key in AI_PROVIDER_SETTING_FIELDS}
    overridden = sorted((await stored_overrides(session)).keys())
    return {
        "values": values,
        "overridden": overridden,
        # What the next distillation would ACTUALLY use — includes the legacy
        # Redis model field the Intelligence tab no longer edits. null =
        # resolved at call time from the box (spark auto) / provider off.
        "insights_effective_model": await effective_insights_model(),
        "choices": {
            "ai_embeddings_provider": list(EMBEDDING_PROVIDERS),
            "ai_insights_provider": list(INSIGHTS_PROVIDERS),
        },
        "embedding_providers": embedding_provider_catalog(),
        "state": {
            "hf_token_set": bool(await get_hf_token()),
            "ollama_api_key_set": bool(await get_ollama_api_key()),
            "embeddings_api_key_set": bool(await get_embeddings_api_key()),
            "embeddings_cloud_api_key_set": bool(await get_embeddings_cloud_api_key()),
            # Combinations that silently cannot work, surfaced as state, not
            # as an error: Ollama Cloud (insights-only arm) without its key,
            # and the cloud embeddings arm without its key.
            "ollama_key_required": insights_provider_key() == "ollama_cloud",
            "embeddings_cloud_key_required": embeddings_provider_key() == "cloud",
        },
    }


@router.put("/settings")
async def put_ai_provider_settings(
    payload: AiProviderSettingsUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Save + apply immediately. 422 on an unknown key or an invalid value."""
    try:
        applied = await save_ai_provider_settings(session, dict(payload.settings))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    logger.info("ai provider settings updated: %s", sorted(payload.settings.keys()))
    return {
        "ok": True,
        "applied": sorted(payload.settings.keys()),
        "effective": {k: v for k, v in applied.items() if k in payload.settings},
    }


@router.post("/huggingface/test-connection")
async def huggingface_test_connection(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Check the stored HF token. Always 200; no token material in the response.

    "No token stored" is a first-class OK state, not an error: MC works
    anonymously against public repos, which is what most installs need.
    """
    token = await get_hf_token()
    result: dict = {
        "token_set": bool(token),
        "connected": False,
        "username": None,
        "error": None,
        "anonymous_ok": True,
    }
    if not token:
        return result
    # The whoami procedure lives in the check registry (kind ``secret``,
    # subject ``hf_token``), which also stores the result with its age.
    from app.services import config_checks

    try:
        run = await config_checks.run_check(session, "secret", HF_TOKEN_SECRET_KEY)
    except (config_checks.CheckNotRunnable, config_checks.UnknownCheck):
        return result
    return {**result, **{k: run.outcome.detail.get(k) for k in ("connected", "username", "error")}}


@router.post("/embeddings/test-connection")
async def embeddings_test_connection(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Embed one short string through the ACTIVE provider. Always 200.

    Reports the real vector dimension: a provider that answers but returns
    e.g. 1024 dims would silently poison the 768-dim Qdrant collections, so
    "reachable" alone is not the interesting answer.
    """
    from app.services import config_checks
    from app.services.embedding_provider import active_embedding_provider

    # Procedure + stored result: check registry, kind ``embeddings``.
    run = await config_checks.run_check(session, "embeddings", active_embedding_provider().key)
    return run.outcome.detail
