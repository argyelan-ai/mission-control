"""One field rule for every masked secret (round-4 user tests K5, r1 N1).

Round 3 (#962) stopped masks from leaking a password, but the field rule
only matched a whole name or a ``_``/``-`` suffix. Round 4 found three gaps:

1. ``dbPassword``, ``clientSecret``, ``secret_key`` and ``totp_seed`` still
   showed their last 4 characters once the value was 20+ characters long
   (camelCase and prefix names slipped past the suffix match).
2. A nested value (dict/list) was masked as ``str(v)``, so the closing
   characters of the text form leaked: ``{'pw': 'hunter2'}`` came back as
   ``••••••••r2'}`` — 2 characters of the inner password.
3. ``routers/secrets.py`` masked API keys without any field rule:
   ``x_api_secret`` showed its last 4 although "secret" never shows a tail
   in the credentials Vault.

The rule now: a field whose normalised name (lower case, separators and
camelCase folded) contains a password-like word is always the fixed mask;
only token / key-like names keep a tail, and only for 20+ characters;
anything nested is hidden in full.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

import app.services.encryption as enc
from app.routers.credentials import _mask_data

LONG = "S" * 40 + "WXYZ"  # 44 characters — long enough for a tail


@pytest.mark.parametrize(
    "field",
    [
        "dbPassword",
        "DB_PASSWORD",
        "clientSecret",
        "Client-Secret",
        "secret_key",
        "secretKey",
        "totp_seed",
        "totpSeed",
        "seed_phrase",
        "otpCode",
        "privateKey",
        "private_token",
        "passPhrase",
        "user.pin",
        "PIN",
        "x_api_secret",
        "x_access_token_secret",
        "apiSecretKey",
        "content",
    ],
)
def test_password_like_names_in_any_spelling_never_show_a_tail(field):
    masked = enc.mask_credential_field(field, LONG)
    assert masked == enc.MASK, (field, masked)


@pytest.mark.parametrize(
    "field",
    ["token", "api_key", "apiKey", "GITHUB_TOKEN", "access_token", "claude_code_oauth_token", "OPENAI_API_KEY"],
)
def test_token_like_names_keep_the_fixed_length_tail(field):
    assert enc.mask_credential_field(field, LONG) == enc.MASK + "WXYZ"
    assert enc.mask_credential_field(field, "short-token-123") == enc.MASK


@pytest.mark.parametrize("field", ["value", "host", "account_id", "url_path", "anything"])
def test_unknown_names_never_show_a_tail(field):
    # Only a field we KNOW is a random token may show 4 characters.
    assert enc.mask_credential_field(field, LONG) == enc.MASK


@pytest.mark.parametrize(
    "value",
    [
        {"pw": "hunter2-hunter2-hunter2-hunter2"},
        {"token": "ghp_" + "x" * 30 + "9f3a"},
        ["ghp_" + "x" * 30 + "9f3a", "second"],
        {"outer": {"inner": ["deep-secret-value-0123456789"]}},
    ],
)
def test_nested_values_are_hidden_in_full(value):
    masked = _mask_data({"token": value, "extra": value}, "custom")
    for field in ("token", "extra"):
        assert masked[field] == enc.MASK, masked
    # Nothing from the inner text form survives: no tail, no brace, no key.
    text = masked["token"] + masked["extra"]
    for needle in ("r2", "9f3a", "}", "]", "pw", "inner", "second", "6789"):
        assert needle not in text, (needle, text)


def test_non_string_scalars_follow_the_field_rule():
    masked = _mask_data({"pin": 1234, "port": 22, "enabled": True}, "custom")
    assert masked["pin"] == enc.MASK
    assert masked["port"] == enc.MASK
    assert masked["enabled"] == enc.MASK


def test_flat_fields_still_mask_as_before():
    masked = _mask_data(
        {"username": "mark", "password": "hunter2", "token": LONG, "private_key_pem": "pem"},
        "login",
    )
    assert masked == {
        "username": "mark",
        "password": enc.MASK,
        "token": enc.MASK + "WXYZ",
        "private_key_pem": "[hidden]",
    }


# ── Secrets list (API keys) uses the same field rule ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,expect_tail",
    [("k5_x_api_secret", False), ("k5_db_password", False), ("k5_totp_seed", False), ("k5_service_token", True), ("k5_api_key", True)],
)
async def test_secret_routes_mask_with_the_field_rule(auth_client: AsyncClient, key, expect_tail):
    want = enc.MASK + "WXYZ" if expect_tail else enc.MASK

    created = await auth_client.post("/api/v1/secrets", json={"key": key, "value": LONG})
    assert created.status_code == 201, created.text
    assert created.json()["value_masked"] == want

    listed = await auth_client.get("/api/v1/secrets")
    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json() if r["key"] == key)
    assert row["value_masked"] == want

    patched = await auth_client.patch(f"/api/v1/secrets/{key}", json={"value": LONG})
    assert patched.status_code == 200, patched.text
    assert patched.json()["value_masked"] == want
