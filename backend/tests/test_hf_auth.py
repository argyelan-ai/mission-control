"""HuggingFace as a first-class integration — the token reaches all three
call sites, and its absence changes nothing.

The three sites are catalog search, repo file listing (both HTTPS GETs) and
the GGUF download (a curl executed over SSH on the GPU box). Gated repos need
the header on all three: finding a repo you then cannot download is worse than
not finding it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.runtime_manager import (
    download_hf_file,
    get_hf_repo_files,
    search_lmstudio_catalog,
)
from tests.conftest import test_engine

_CATALOG_RESPONSE = [
    {
        "modelId": "lmstudio-community/Qwen3-8B-GGUF",
        "tags": ["gguf", "8B"],
        "siblings": [{"rfilename": "Qwen3-8B-Q4_K_M.gguf", "size": 5200000000}],
    }
]
_REPO_RESPONSE = {
    "modelId": "SomeOrg/Gated-GGUF",
    "siblings": [{"rfilename": "Q4_K_M.gguf", "size": 16500000000}],
}


@pytest.fixture(autouse=True)
def _own_session_goes_to_the_test_db(monkeypatch):
    """``ai_provider_config._secret`` opens its own session — keep it off the
    developer's real Postgres."""
    monkeypatch.setattr("app.database.engine", test_engine)


async def _store_token(value: str = "hf_TESTONLY") -> None:
    from app.services.secrets_helper import upsert_secret_by_key

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await upsert_secret_by_key(s, key="hf_token", value=value)


def _mock_httpx_get(json_data, capture: dict, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()

    async def get(url, **kwargs):
        capture["url"] = url
        capture.update(kwargs)
        return resp

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = get
    return client


# ── Site 1: catalog search ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_search_is_anonymous_without_a_token():
    capture: dict = {}
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(_CATALOG_RESPONSE, capture)):
        results = await search_lmstudio_catalog("qwen")
    assert results and results[0]["model_id"] == "lmstudio-community/Qwen3-8B-GGUF"
    assert capture["headers"] == {}


@pytest.mark.asyncio
async def test_catalog_search_sends_the_token():
    await _store_token()
    capture: dict = {}
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(_CATALOG_RESPONSE, capture)):
        await search_lmstudio_catalog("qwen")
    assert capture["headers"]["Authorization"] == "Bearer hf_TESTONLY"


# ── Site 2: repo files ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repo_files_is_anonymous_without_a_token():
    capture: dict = {}
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(_REPO_RESPONSE, capture)):
        result = await get_hf_repo_files("SomeOrg/Gated-GGUF")
    assert [f["filename"] for f in result["files"]] == ["Q4_K_M.gguf"]
    assert capture["headers"] == {}


@pytest.mark.asyncio
async def test_repo_files_sends_the_token():
    await _store_token()
    capture: dict = {}
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(_REPO_RESPONSE, capture)):
        await get_hf_repo_files("SomeOrg/Gated-GGUF")
    assert capture["headers"]["Authorization"] == "Bearer hf_TESTONLY"


# ── Site 3: the download curl ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_command_is_byte_identical_without_a_token():
    """An install that never set a token must gain no new failure mode."""
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as ssh:
        ssh.return_value = ("", "", 0)
        result = await download_hf_file("SomeOrg/Gated-GGUF", "Q4_K_M.gguf")

    assert result["ok"] is True
    cmd = ssh.call_args[0][0]
    assert "Authorization" not in cmd
    assert (
        "nohup curl -L 'https://huggingface.co/SomeOrg/Gated-GGUF/resolve/main/Q4_K_M.gguf'"
        in cmd
    )


@pytest.mark.asyncio
async def test_download_command_carries_the_token_header():
    await _store_token()
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as ssh:
        ssh.return_value = ("", "", 0)
        result = await download_hf_file("SomeOrg/Gated-GGUF", "Q4_K_M.gguf")

    assert result["ok"] is True
    cmd = ssh.call_args[0][0]
    assert "-H 'Authorization: Bearer hf_TESTONLY'" in cmd
    # The header goes BEFORE the URL — curl would treat it as a second URL
    # otherwise and silently download the wrong thing.
    assert cmd.index("Authorization") < cmd.index("huggingface.co")
    assert "~/.lmstudio/models/SomeOrg/Gated-GGUF" in cmd


@pytest.mark.asyncio
async def test_a_vault_outage_degrades_to_anonymous(monkeypatch):
    """A broken vault must not stop the model browser — it falls back to what
    an install without a token does anyway."""
    from app.services import ai_provider_config, secrets_helper

    await _store_token()

    async def boom(session, key):
        raise RuntimeError("vault down")

    monkeypatch.setattr(secrets_helper, "get_secret_plaintext_by_key", boom)
    assert await ai_provider_config.hf_auth_headers() == {}

    capture: dict = {}
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(_REPO_RESPONSE, capture)):
        result = await get_hf_repo_files("SomeOrg/Gated-GGUF")
    assert result["files"]  # the browser still works, just anonymously
    assert capture["headers"] == {}
