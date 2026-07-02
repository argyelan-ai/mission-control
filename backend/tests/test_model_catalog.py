import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.runtime_manager import (
    search_lmstudio_catalog,
    get_hf_repo_files,
    download_hf_file,
)

_HF_CATALOG_RESPONSE = [
    {
        "modelId": "lmstudio-community/Qwen3-8B-GGUF",
        "tags": ["gguf", "8B", "transformers"],
        "siblings": [
            {"rfilename": "Qwen3-8B-Q4_K_M.gguf", "size": 5200000000},
            {"rfilename": "README.md", "size": 4000},
        ],
    }
]

_HF_REPO_RESPONSE = {
    "modelId": "Jackrong/Qwen3.5-27B-GGUF",
    "siblings": [
        {"rfilename": "Q4_K_M.gguf", "size": 16500000000},
        {"rfilename": "Q8_0.gguf", "size": 28900000000},
        {"rfilename": "README.md", "size": 1000},
    ],
}


def _mock_httpx_get(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_search_lmstudio_catalog_returns_list():
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(_HF_CATALOG_RESPONSE)):
        result = await search_lmstudio_catalog("qwen")

    assert len(result) == 1
    assert result[0]["model_id"] == "lmstudio-community/Qwen3-8B-GGUF"
    assert result[0]["name"] == "Qwen3-8B-GGUF"
    assert result[0]["params"] == "8B"
    assert result[0]["size_gb"] == pytest.approx(4.8, abs=0.5)


@pytest.mark.asyncio
async def test_search_lmstudio_catalog_empty_query():
    result = await search_lmstudio_catalog("")
    assert result == []


@pytest.mark.asyncio
async def test_get_hf_repo_files_filters_gguf():
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(_HF_REPO_RESPONSE)):
        result = await get_hf_repo_files("Jackrong/Qwen3.5-27B-GGUF")

    assert "error" not in result
    assert len(result["files"]) == 2
    filenames = [f["filename"] for f in result["files"]]
    assert "Q4_K_M.gguf" in filenames
    assert "Q8_0.gguf" in filenames
    assert "README.md" not in filenames


@pytest.mark.asyncio
async def test_get_hf_repo_files_not_found():
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get({}, status_code=404)):
        result = await get_hf_repo_files("nonexistent/model")

    assert result.get("error") == "Repo nicht gefunden"


@pytest.mark.asyncio
async def test_download_hf_file_correct_command():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = ("", "", 0)
        result = await download_hf_file("Jackrong/MyModel", "Q4_K_M.gguf")

    assert result["ok"] is True
    assert "Download gestartet" in result["message"]
    cmd = mock_ssh.call_args[0][0]
    assert "~/.lmstudio/models/Jackrong/MyModel" in cmd
    assert "Q4_K_M.gguf" in cmd
    assert "huggingface.co/Jackrong/MyModel/resolve/main/Q4_K_M.gguf" in cmd


@pytest.mark.asyncio
async def test_download_hf_file_ssh_error():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.side_effect = Exception("Connection refused")
        result = await download_hf_file("Jackrong/MyModel", "Q4_K_M.gguf")

    assert result["ok"] is False
