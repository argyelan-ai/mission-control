"""The "add a key" list offers no retired provider (pruefteam 21.11).

The OpenClaw gateway was retired (ADR-039); its token still showed up as an
offered provider in Settings → API keys. It must not be offered anymore —
a value someone stored earlier stays readable, because ``GET /secrets`` lists
the stored rows themselves, not the templates.
"""

from __future__ import annotations

import inspect

from app.routers import secrets


def test_openclaw_gateway_token_is_not_offered():
    keys = {t["key"] for t in secrets.PROVIDER_TEMPLATES}
    labels = " ".join(t["label"] for t in secrets.PROVIDER_TEMPLATES)
    assert "openclaw_token" not in keys
    assert "OpenClaw" not in labels


def test_stored_secrets_are_listed_from_rows_not_templates():
    # A stored legacy value stays readable: list_secrets reads the Secret rows.
    src = inspect.getsource(secrets.list_secrets)
    assert "select(Secret)" in src
    assert "PROVIDER_TEMPLATES" not in src
