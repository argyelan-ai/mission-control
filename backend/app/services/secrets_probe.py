"""Startup probe: can secrets stored in the DB actually be decrypted?

`safe_decrypt` (app.services.encryption) swallows decryption errors and
returns None — correct for every call site that treats "no value" as a
soft miss, but it means a wrong `SECRETS_ENCRYPTION_KEY` (e.g. after moving
the database to a new host without its matching .env) goes completely
unnoticed. Agents then just fail with no clear reason.

This module probe-decrypts ONE existing secret once at startup and caches
the result in-process. `/health` reads that cache to report "degraded"
instead of staying silently "ok". Never logs the key or any decrypted
value — only a boolean outcome.
"""

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.secret import Secret
from app.services.encryption import safe_decrypt

logger = logging.getLogger("secrets_probe")

# None = not probed yet (treated as healthy so a slow/failed probe never
# blocks boot or paints the app degraded before it had a chance to run).
_decryptable: bool | None = None


def secrets_decryptable() -> bool:
    """Result of the last startup probe. True until a probe has run or ran ok."""
    return _decryptable is not False


async def probe_secrets_decryptable(session: AsyncSession) -> bool:
    """Try to decrypt one existing secret. Caches + returns the outcome.

    No secrets in the DB at all -> nothing to prove wrong -> ok.
    Never logs the encryption key or any decrypted value.
    """
    global _decryptable

    secret = (await session.exec(select(Secret).limit(1))).first()
    if secret is None:
        _decryptable = True
        return True

    plaintext = safe_decrypt(secret.encrypted_value)
    _decryptable = plaintext is not None

    if not _decryptable:
        logger.error(
            "Secrets decrypt probe failed: SECRETS_ENCRYPTION_KEY does not "
            "match the key this database's secrets were encrypted with."
        )

    return _decryptable


def reset_for_tests() -> None:
    """Clear the cached probe result between tests (mirrors mc_home_preflight)."""
    global _decryptable
    _decryptable = None
