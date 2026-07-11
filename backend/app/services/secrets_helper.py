"""
Secrets lookup helper — central access to the secrets table with
Fernet decryption.

Usage:
    from app.services.secrets_helper import get_secret_plaintext_by_id
    plaintext = await get_secret_plaintext_by_id(session, agent.secret_id)

Used by docker_agent_sync.py to write the API key into the .env file in
the claude-config bind mount during sync-config.
"""
import logging
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.secret import Secret
from app.services.encryption import encrypt, safe_decrypt

logger = logging.getLogger("mc.secrets_helper")


async def get_secret_plaintext_by_id(
    session: AsyncSession,
    secret_id: uuid.UUID | str | None,
) -> str | None:
    """Fetches a secret value by ID and returns the decrypted plaintext.

    Returns:
        str: plaintext value if decrypted successfully
        None: if secret_id is None, the secret was not found, or decryption fails
    """
    if not secret_id:
        return None

    try:
        secret = await session.get(Secret, secret_id)
    except Exception as e:
        logger.warning("get_secret_plaintext_by_id(%s): DB lookup failed: %s", secret_id, e)
        return None

    if not secret:
        logger.warning("get_secret_plaintext_by_id(%s): not found", secret_id)
        return None

    plaintext = safe_decrypt(secret.encrypted_value)
    if plaintext is None:
        logger.error(
            "get_secret_plaintext_by_id(%s, key=%s): decryption failed (Fernet InvalidToken)",
            secret_id,
            secret.key,
        )
        return None

    return plaintext


async def get_secret_plaintext_by_key(
    session: AsyncSession,
    key: str,
) -> str | None:
    """Alternative variant — lookup by key instead of ID (e.g. "ollama_api_key").

    Useful for callers that know the key name but don't have an ID.
    """
    try:
        result = await session.exec(select(Secret).where(Secret.key == key))
        secret = result.first()
    except Exception as e:
        logger.warning("get_secret_plaintext_by_key(%s): DB lookup failed: %s", key, e)
        return None

    if not secret:
        return None

    return safe_decrypt(secret.encrypted_value)


async def upsert_secret_by_key(
    session: AsyncSession,
    key: str,
    value: str,
    *,
    provider: str | None = None,
    label: str | None = None,
    description: str | None = None,
) -> Secret:
    """Creates a secret or updates its value (Fernet-encrypted).

    Commits the session — callers running their own transactions should
    call refresh() on their own objects afterwards if needed.
    """
    result = await session.exec(select(Secret).where(Secret.key == key))
    secret = result.first()
    encrypted = encrypt(value)
    if secret:
        secret.encrypted_value = encrypted
    else:
        secret = Secret(
            key=key,
            encrypted_value=encrypted,
            provider=provider,
            label=label,
            description=description,
        )
    session.add(secret)
    await session.commit()
    await session.refresh(secret)
    return secret


async def upsert_agent_token_secret(
    session: AsyncSession,
    agent_name: str,
    raw_token: str,
) -> None:
    """Persists an agent's MC_AGENT_TOKEN as a vault secret.

    Key schema `mc_token_{agent.name.lower()}` — MUST match the lookup in
    routers/internal.py::agent_bootstrap, otherwise poll.sh crash-loops with
    'MC_TOKEN is not set' (fresh-install bug 2026-07-02): the token used to be
    stored only as a PBKDF2 hash + once in the response, but never written to
    the vault — /internal/bootstrap never found it. Called on EVERY token
    generation (create/instantiate/reset/provision) so the vault never
    serves a stale token.

    Best-effort: a vault error must not kill agent creation — the token is
    visible in the response and can be brought back into the vault via
    reset-token.
    """
    slug = agent_name.lower()
    try:
        await upsert_secret_by_key(
            session,
            f"mc_token_{slug}",
            raw_token,
            provider="mc-agent",
            label=f"Agent Token: {agent_name}",
            description=f"PBKDF2-Auth Token fuer Agent {agent_name} (auto-managed)",
        )
    except Exception as e:
        logger.error("upsert_agent_token_secret(%s): Vault-Write fehlgeschlagen: %s", slug, e)


async def delete_agent_token_secret(
    session: AsyncSession,
    agent_slug: str,
) -> bool:
    """Removes an agent's MC_AGENT_TOKEN vault secret on agent deletion.

    Without this, a deleted agent leaves an orphaned `mc-agent` secret behind
    that (a) never gets cleaned up and (b) still lands in docker/.env.agents
    on the next start-all.sh token generation — a stale token for a
    non-existent agent (found 2026-07-11: a leftover `mc_token_host testpilot`
    even broke .env.agents parsing).

    Keyed on the agent's STABLE slug (set on insert, never on rename — see
    Agent._agent_fill_slug), not its current name: a plain PATCH rename does
    not rotate the token, so the vault key still reflects the ORIGINAL name.
    upsert_agent_token_secret writes `mc_token_{name.lower()}` (spaces
    preserved) while the slug uses dashes, so we clear BOTH derivations —
    `mc_token_{slug}` and `mc_token_{slug-with-dashes-as-spaces}` — to catch a
    multi-word agent whichever scheme its key was written under.

    Does NOT commit — runs inside the delete_agent transaction so it is
    rolled back atomically if the agent delete fails. Returns True when at
    least one secret was found and staged for deletion.
    """
    candidate_keys = {
        f"mc_token_{agent_slug}",
        f"mc_token_{agent_slug.replace('-', ' ')}",
    }
    result = await session.exec(select(Secret).where(Secret.key.in_(candidate_keys)))
    secrets = list(result.all())
    for secret in secrets:
        await session.delete(secret)
    return bool(secrets)
