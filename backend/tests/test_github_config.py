"""ADR-055: GitHub connection config — resolver (vault > env), status + config API."""
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.secret import Secret
from app.services.encryption import encrypt
from app.services.github_config import (
    OWNER_SECRET_KEY,
    TOKEN_SECRET_KEY,
    get_cached_owner,
    invalidate_github_config_cache,
    resolve_github_config,
)

from tests.conftest import test_engine


async def _put_vault(key: str, value: str) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Secret(key=key, encrypted_value=encrypt(value), provider="github"))
        await s.commit()


# ── Resolver ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_env_fallback(monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "env-owner")
    monkeypatch.setenv("GH_TOKEN", "env-token")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        cfg = await resolve_github_config(s, fresh=True)
    assert (cfg.owner, cfg.owner_source) == ("env-owner", "env")
    assert (cfg.token, cfg.token_source) == ("env-token", "env")
    assert cfg.configured


@pytest.mark.asyncio
async def test_resolver_vault_wins_over_env(monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "env-owner")
    monkeypatch.setenv("GH_TOKEN", "env-token")
    await _put_vault(OWNER_SECRET_KEY, "vault-owner")
    await _put_vault(TOKEN_SECRET_KEY, "vault-token")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        cfg = await resolve_github_config(s, fresh=True)
    assert (cfg.owner, cfg.owner_source) == ("vault-owner", "vault")
    assert (cfg.token, cfg.token_source) == ("vault-token", "vault")
    # Sync accessor (template rendering) sees the resolved owner.
    assert get_cached_owner() == "vault-owner"


@pytest.mark.asyncio
async def test_resolver_unconfigured(monkeypatch):
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        cfg = await resolve_github_config(s, fresh=True)
    assert cfg.owner == "" and cfg.owner_source is None
    assert cfg.token == "" and cfg.token_source is None
    assert not cfg.configured


@pytest.mark.asyncio
async def test_secrets_api_write_invalidates_cache(auth_client: AsyncClient, monkeypatch):
    """A github_token write via the generic secrets API must apply live —
    without waiting out the TTL cache."""
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await resolve_github_config(s)).token == ""  # cache primed: empty

    r = await auth_client.post("/api/v1/secrets", json={
        "key": TOKEN_SECRET_KEY, "value": "ghp_fresh", "provider": "github",
    })
    assert r.status_code == 201

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        cfg = await resolve_github_config(s)  # NOT fresh — must still see the write
    assert cfg.token == "ghp_fresh" and cfg.token_source == "vault"


# ── GET /repos/github-status ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_without_probe(auth_client: AsyncClient, monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    r = await auth_client.get("/api/v1/repos/github-status")
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == "acme" and body["owner_source"] == "env"
    assert body["token_set"] is False and body["configured"] is False
    assert body["connected"] is None  # kein Probe → keine Aussage


@pytest.mark.asyncio
async def test_status_probe_unconfigured(auth_client: AsyncClient, monkeypatch):
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    r = await auth_client.get("/api/v1/repos/github-status?probe=true")
    body = r.json()
    assert body["connected"] is False
    assert body["error"]


@pytest.mark.asyncio
async def test_status_probe_success(auth_client: AsyncClient, monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.setenv("GH_TOKEN", "ghp_x")

    outputs = {
        ("gh", "api", "user"): json.dumps({"login": "acme-bot"}),
        ("gh", "api", "users/acme"): json.dumps({"type": "Organization"}),
        ("gh", "api", "rate_limit"): json.dumps(
            {"resources": {"core": {"remaining": 4990, "limit": 5000}}}
        ),
    }

    async def fake_run(self, *args, cwd=None):
        return outputs[args]

    with patch("app.services.git_service.GitService._run_cmd", new=fake_run):
        r = await auth_client.get("/api/v1/repos/github-status?probe=true")
    body = r.json()
    assert body["connected"] is True
    assert body["login"] == "acme-bot"
    assert body["owner_type"] == "Organization"
    assert body["rate_limit_remaining"] == 4990
    assert body["error"] is None


@pytest.mark.asyncio
async def test_status_probe_bad_token(auth_client: AsyncClient, monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.setenv("GH_TOKEN", "ghp_expired")
    with patch(
        "app.services.git_service.GitService._run_cmd",
        new=AsyncMock(side_effect=RuntimeError("Git command failed: gh api user → HTTP 401")),
    ):
        r = await auth_client.get("/api/v1/repos/github-status?probe=true")
    body = r.json()
    assert body["connected"] is False
    assert "401" in body["error"]


# ── PUT /repos/github-config ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_config_sets_vault(auth_client: AsyncClient, monkeypatch):
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    r = await auth_client.put("/api/v1/repos/github-config", json={
        "owner": "acme-org", "token": "ghp_new",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner"] == "acme-org" and body["owner_source"] == "vault"
    assert body["token_set"] is True and body["token_source"] == "vault"
    assert body["configured"] is True

    # Idempotentes Update (upsert, kein 409 wie beim rohen POST /secrets)
    r2 = await auth_client.put("/api/v1/repos/github-config", json={"owner": "acme-two"})
    assert r2.json()["owner"] == "acme-two"


@pytest.mark.asyncio
async def test_put_config_invalid_owner(auth_client: AsyncClient):
    r = await auth_client.put("/api/v1/repos/github-config", json={"owner": "evil/../path"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_config_empty_string_clears_vault(auth_client: AsyncClient, monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "env-owner")
    await _put_vault(OWNER_SECRET_KEY, "vault-owner")
    r = await auth_client.put("/api/v1/repos/github-config", json={"owner": ""})
    body = r.json()
    # Vault-Row weg → .env-Fallback greift wieder
    assert body["owner"] == "env-owner" and body["owner_source"] == "env"


# ── GitService auth follows token rotation ────────────────────────────


@pytest.mark.asyncio
async def test_git_auth_rewrites_credentials_on_token_change(tmp_path, monkeypatch):
    from app.services.git_service import GitService

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    git = GitService()
    with patch.object(git, "_run_cmd", new=AsyncMock()):
        monkeypatch.setenv("GH_TOKEN", "tok-one")
        invalidate_github_config_cache()
        await git._ensure_git_auth()
        assert "tok-one" in (tmp_path / ".git-credentials").read_text()

        monkeypatch.setenv("GH_TOKEN", "tok-two")
        invalidate_github_config_cache()
        await git._ensure_git_auth()
        assert "tok-two" in (tmp_path / ".git-credentials").read_text()
