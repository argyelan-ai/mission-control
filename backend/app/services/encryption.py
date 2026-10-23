"""
Fernet-based encryption for MC's own secrets.

Secrets are stored symmetrically encrypted in the DB.
The encryption key comes from the ENV variable SECRETS_ENCRYPTION_KEY.

Generate a key:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import base64
import hashlib
import logging
import re

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger("encryption")

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Lazy-init Fernet instance."""
    global _fernet
    if _fernet is not None:
        return _fernet

    key = settings.secrets_encryption_key
    if not key:
        raise RuntimeError(
            "SECRETS_ENCRYPTION_KEY is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and add it to your .env file."
        )

    # Key must be a valid Fernet key (URL-safe base64, 32 bytes)
    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        # App-store installs (CasaOS, Runtipi, ...) can only supply an
        # arbitrary random string, not a Fernet-formatted key. Derive a
        # proper key from the passphrase instead of refusing to boot. A
        # value that already IS a valid Fernet key never reaches this path,
        # so existing installs are unaffected.
        raw = key.encode() if isinstance(key, str) else key
        _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))
        logger.info(
            "SECRETS_ENCRYPTION_KEY is not a Fernet-formatted key - "
            "derived one from it (sha256). Keep the original value stable."
        )

    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypts plaintext with Fernet and returns a base64 ciphertext.

    Args:
        plaintext: The plaintext string to encrypt.

    Returns:
        A Fernet-generated, base64-encoded ciphertext as a string.
    """
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext → plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def safe_decrypt(ciphertext: str) -> str | None:
    """Decrypt with error handling — returns None on errors."""
    try:
        return decrypt(ciphertext)
    except (InvalidToken, RuntimeError, Exception) as e:
        logger.warning("Failed to decrypt secret: %s", type(e).__name__)
        return None


# Every masked value starts with this fixed run, whatever the real length —
# the length of a secret is information too (round-3 user tests R1,
# 23.10.2026: a 5-character password used to come back as "*" + its last
# 4 characters, i.e. 4 of 5 characters and the exact length in clear).
# rule: R-mask-no-length-no-password - a 5-char password showed 4 of 5 chars
MASK = "••••••••"

# A tail (last 4 characters) is only shown for values at least this long.
# Long random tokens (API keys, OAuth tokens) stay recognisable that way
# ("which key is this?"), while 4 characters of a 20+ character random
# string give nothing away. Anything shorter is a password-sized value.
TAIL_MIN_LENGTH = 20


def mask(value: str, visible_chars: int = 4, *, reveal_tail: bool = True) -> str:
    """Mask a secret for the frontend.

    Always the fixed ``MASK`` — never the real length. Only a value of at
    least ``TAIL_MIN_LENGTH`` characters keeps its last ``visible_chars``
    characters, and only when ``reveal_tail`` is set. Callers masking a
    password-like field pass ``reveal_tail=False`` (see
    ``mask_credential_field``)."""
    if reveal_tail and len(value) >= TAIL_MIN_LENGTH:
        return MASK + value[-visible_chars:]
    return MASK


# Field-name rule shared by EVERY masked response — the credentials Vault
# (routers/credentials.py._mask_data) and the API-key list
# (routers/secrets.py). Round-4 user tests (K5, r1 N1, 23.10.2026) found the
# old whole-word/suffix match let "dbPassword", "clientSecret", "secret_key"
# and "totp_seed" show their last 4 characters, and secrets.py had no field
# rule at all ("x_api_secret" showed a tail).
#
# The name is normalised first — lower case, every separator and the
# camelCase hump folded away ("dbPassword" -> "dbpassword", "Client-Secret"
# -> "clientsecret") — and then matched as a SUBSTRING. That over-hides a
# few harmless names ("shipping_token" contains "pin"); hiding too much is
# the safe way to be wrong.
#
# rule: R-mask-no-length-no-password - camelCase/prefix secret names showed 4 chars
_NO_TAIL_NAME_PARTS = (
    "password", "passwd", "pwd", "passphrase", "pin",
    "secret", "seed", "private", "otp",  # "otp" also covers "totp"
    "content",  # free text: its last characters may be a secret too
)

# Only these names may keep a tail (and only for TAIL_MIN_LENGTH+ values):
# long random tokens and API keys, where 4 characters answer "which key is
# this?" without giving anything away. Every other name — known or not —
# gets the fixed mask.
_TAIL_NAME_PARTS = ("token", "key")


def _normalise_field_name(field: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(field).lower())


def _is_no_tail_field(field: str) -> bool:
    name = _normalise_field_name(field)
    return any(part in name for part in _NO_TAIL_NAME_PARTS)


def _may_show_tail(field: str) -> bool:
    name = _normalise_field_name(field)
    if not name or _is_no_tail_field(field):
        return False
    return any(part in name for part in _TAIL_NAME_PARTS)


def mask_credential_field(field: str, value) -> str:
    """Mask one stored value for the UI under the shared field rule.

    Password-like names, free text and unknown names are fully hidden;
    token/key-like names follow ``mask`` (tail only for long values). A
    nested value (dict, list, tuple, set) is hidden in full — its text form
    would leak closing characters, and its inner key names may be secret
    too — so the response stays one string per field."""
    if isinstance(value, (dict, list, tuple, set)):
        return MASK
    return mask(str(value), reveal_tail=_may_show_tail(field))


# Fields whose value must NEVER leave the backend, in ANY credential
# response — not the admin Vault UI (routers/credentials.py._mask_data),
# and not the agent-scoped endpoints (routers/agent_scoped.py), even the one
# that otherwise hands back real, unmasked secrets on purpose (login/token
# credentials an agent needs to actually use). A private key is not like a
# password: routers/agent_scoped.py.agent_get_credential was found (Phase 2
# security review, 30.08.2026) returning ssh_key credentials fully
# decrypted — every credential redaction point must import this constant
# instead of keeping its own list, or the next new field type drifts the
# same way this one did.
NEVER_EXPOSE_CREDENTIAL_FIELDS = ("private_key_pem",)


def redact_never_exposed_fields(data: dict) -> dict:
    """Replaces NEVER_EXPOSE_CREDENTIAL_FIELDS values with "[hidden]" —
    everything else passes through untouched. Apply this to EVERY
    credential response, including ones that otherwise return real secrets."""
    return {
        k: ("[hidden]" if k in NEVER_EXPOSE_CREDENTIAL_FIELDS else v)
        for k, v in data.items()
    }
