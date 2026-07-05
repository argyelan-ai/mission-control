"""GitHub connection config (ADR-055) — single resolver for owner + token.

Sources in priority order:
  1. Secrets vault (keys ``github_owner`` / ``github_token``) — set via
     Settings → GitHub or the setup wizard; applies live, no restart.
  2. Backend env (``GITHUB_OWNER`` / ``GH_TOKEN`` from .env) — the
     CLI-first path seeded by install.sh.

Every consumer (git_service, repos router, visibility monitor, template
renderer) goes through this module. Nobody reads os.environ["GITHUB_OWNER"]
or the vault keys directly anymore — that was the pre-ADR-055 state where
the owner was frozen at import time and UI edits needed a restart.

The resolver must never crash callers: vault errors degrade to the env
fallback. A short TTL cache keeps hot paths (every git subprocess) cheap;
secrets writes invalidate explicitly.
"""

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger("mc.github_config")

OWNER_SECRET_KEY = "github_owner"
TOKEN_SECRET_KEY = "github_token"

_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class GithubConfig:
    owner: str
    owner_source: str | None  # "vault" | "env" | None
    token: str
    token_source: str | None  # "vault" | "env" | None

    @property
    def configured(self) -> bool:
        return bool(self.owner and self.token)


_cache: GithubConfig | None = None
_cache_ts: float = 0.0


def invalidate_github_config_cache() -> None:
    """Called by the secrets router after github_* vault writes."""
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0


def get_cached_owner() -> str:
    """Sync accessor for sync contexts (template rendering).

    Returns the last resolved owner, falling back to the env var when no
    async resolution has happened yet (e.g. very early in startup).
    """
    if _cache is not None:
        return _cache.owner
    return os.environ.get("GITHUB_OWNER", "")


async def _read_vault(session, key: str) -> str | None:
    from app.services.secrets_helper import get_secret_plaintext_by_key

    value = await get_secret_plaintext_by_key(session, key)
    return value.strip() if value else None


async def resolve_github_config(session=None, *, fresh: bool = False) -> GithubConfig:
    """Resolve owner + token (vault > env). Pass a session where you have one;
    without one the resolver opens its own (background services)."""
    global _cache, _cache_ts

    if not fresh and _cache is not None and time.monotonic() - _cache_ts < _CACHE_TTL_SECONDS:
        return _cache

    vault_owner: str | None = None
    vault_token: str | None = None
    try:
        if session is not None:
            vault_owner = await _read_vault(session, OWNER_SECRET_KEY)
            vault_token = await _read_vault(session, TOKEN_SECRET_KEY)
        else:
            from sqlmodel.ext.asyncio.session import AsyncSession

            from app.database import engine

            async with AsyncSession(engine, expire_on_commit=False) as own:
                vault_owner = await _read_vault(own, OWNER_SECRET_KEY)
                vault_token = await _read_vault(own, TOKEN_SECRET_KEY)
    except Exception as e:  # vault down ≠ git down — degrade to env
        logger.warning("resolve_github_config: vault lookup failed (%s) — env fallback", e)

    env_owner = os.environ.get("GITHUB_OWNER", "").strip()
    env_token = os.environ.get("GH_TOKEN", "").strip()

    config = GithubConfig(
        owner=vault_owner or env_owner,
        owner_source="vault" if vault_owner else ("env" if env_owner else None),
        token=vault_token or env_token,
        token_source="vault" if vault_token else ("env" if env_token else None),
    )
    _cache = config
    _cache_ts = time.monotonic()
    return config


async def get_github_owner(session=None) -> str:
    return (await resolve_github_config(session)).owner


async def get_github_token(session=None) -> str:
    return (await resolve_github_config(session)).token


async def require_github_owner(session=None) -> str:
    """Fail fast with a clear message instead of building '/repo' slugs."""
    owner = await get_github_owner(session)
    if not owner:
        raise RuntimeError(
            "GITHUB_OWNER is not configured — set it in Settings → GitHub "
            "(or GITHUB_OWNER in .env): the GitHub user/org under which MC "
            "creates project repos."
        )
    return owner
