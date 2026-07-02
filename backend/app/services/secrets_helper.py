"""
Secrets-Lookup Helper — zentraler Zugriff auf die secrets-Tabelle mit
Fernet-Dekryption.

Nutzung:
    from app.services.secrets_helper import get_secret_plaintext_by_id
    plaintext = await get_secret_plaintext_by_id(session, agent.secret_id)

Wird von docker_agent_sync.py genutzt um den API-Key beim sync-config in
das .env File im claude-config Bind-Mount zu schreiben.
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
    """Holt einen Secret-Wert per ID und gibt den dekryptierten Plaintext zurueck.

    Returns:
        str: Plaintext-Wert wenn erfolgreich dekryptiert
        None: wenn secret_id None ist, Secret nicht gefunden, oder Dekryption fehlschlägt
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
    """Alternative Variante — Lookup per key statt ID (z.B. "ollama_api_key").

    Nutzlich fuer Callers die den Key-Namen kennen aber keine ID haben.
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
    """Legt ein Secret an oder aktualisiert den Wert (Fernet-encrypted).

    Committet die Session — Caller die eigene Transaktionen fahren rufen
    danach ggf. refresh() auf ihren eigenen Objekten auf.
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
    """Persistiert den MC_AGENT_TOKEN eines Agents als Vault-Secret.

    Key-Schema `mc_token_{agent.name.lower()}` — MUSS mit dem Lookup in
    routers/internal.py::agent_bootstrap uebereinstimmen, sonst crash-loopt
    poll.sh mit 'MC_TOKEN is not set' (Fresh-Install-Bug 2026-07-02): der
    Token wurde bisher nur als PBKDF2-Hash + einmalig im Response gespeichert,
    aber nie in den Vault geschrieben — /internal/bootstrap fand ihn nie.
    Wird bei JEDER Token-Generierung aufgerufen (create/instantiate/reset/
    provision), damit der Vault nie einen stale Token ausliefert.

    Best-effort: ein Vault-Fehler darf die Agent-Erstellung nicht killen —
    der Token ist im Response sichtbar und kann via reset-token neu in den
    Vault gebracht werden.
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
