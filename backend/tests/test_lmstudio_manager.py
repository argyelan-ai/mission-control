import pytest
from unittest.mock import AsyncMock, patch
from app.services.runtime_manager import _parse_lms_ls, list_lms_models, lms_download_model, lms_delete_model

LMS_LS_OUTPUT = """You have 5 models, taking up 210.97 GB of disk space.

LLM                                                                             PARAMS    ARCH              SIZE        DEVICE
google/gemma-3-4b (1 variant)                                                   4B        gemma3            3.34 GB     Local
lmstudio-community/qwen3-coder-next-q4_k_m-gguf/qwen3-coder-next-q4_k_m.gguf    80B       qwen3next         48.49 GB    Local
nvidia/nemotron-3-super (1 variant)                                             120B      nemotron_h_moe    86.05 GB    Local     ✓ LOADED
qwen/qwen3-coder-next (1 variant)                                               80B       qwen3next         48.49 GB    Local

EMBEDDING                               PARAMS    ARCH          SIZE        DEVICE
text-embedding-nomic-embed-text-v1.5              Nomic BERT    84.11 MB    Local     ✓ LOADED
"""


def test_parse_lms_ls_count():
    models = _parse_lms_ls(LMS_LS_OUTPUT)
    assert len(models) == 5  # 4 LLMs + 1 Embedding


def test_parse_lms_ls_embedding():
    models = _parse_lms_ls(LMS_LS_OUTPUT)
    embeddings = [m for m in models if m["is_embedding"]]
    assert len(embeddings) == 1
    assert "nomic" in embeddings[0]["id"]
    assert embeddings[0]["size_gb"] == pytest.approx(84.11 / 1024, rel=1e-3)


def test_parse_lms_ls_loaded():
    models = _parse_lms_ls(LMS_LS_OUTPUT)
    loaded = [m for m in models if m["is_loaded"]]
    assert len(loaded) == 2  # nemotron + embedding both LOADED
    llm_loaded = [m for m in loaded if not m["is_embedding"]]
    assert llm_loaded[0]["id"] == "nvidia/nemotron-3-super"


def test_parse_lms_ls_size():
    models = _parse_lms_ls(LMS_LS_OUTPUT)
    gemma = next(m for m in models if "gemma" in m["id"])
    assert gemma["size_gb"] == pytest.approx(3.34)


def test_parse_lms_ls_strips_variant_suffix():
    models = _parse_lms_ls(LMS_LS_OUTPUT)
    ids = [m["id"] for m in models]
    assert "nvidia/nemotron-3-super" in ids
    assert "google/gemma-3-4b" in ids
    # Full path variant (no suffix to strip)
    assert any("qwen3-coder-next-q4_k_m.gguf" in i for i in ids)


def test_parse_lms_ls_not_loaded_by_default():
    models = _parse_lms_ls(LMS_LS_OUTPUT)
    not_loaded = [m for m in models if not m["is_loaded"]]
    assert len(not_loaded) == 3  # 3 LLMs not loaded


@pytest.mark.asyncio
async def test_list_lms_models_calls_ssh():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = (LMS_LS_OUTPUT, "", 0)
        models = await list_lms_models()
        mock_ssh.assert_called_once()
        call_args = mock_ssh.call_args[0][0]
        assert "lms ls" in call_args


@pytest.mark.asyncio
async def test_lms_download_model_success():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = ("", "", 0)
        result = await lms_download_model("Qwen/Qwen3-8B-GGUF")
        assert result["ok"] is True
        call_args = mock_ssh.call_args[0][0]
        assert "lms get" in call_args
        assert "Qwen3-8B" in call_args  # Implementation strips vendor prefix and -GGUF suffix


@pytest.mark.asyncio
async def test_lms_download_model_ssh_error():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.side_effect = Exception("SSH timeout")
        result = await lms_download_model("Qwen/Qwen3-8B-GGUF")
        assert result["ok"] is False
        assert "SSH" in result["message"]


@pytest.mark.asyncio
async def test_lms_delete_model_success():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        # First call: find returns a directory path; second call: rm -rf succeeds
        mock_ssh.side_effect = [
            ("/Users/testuser/.lmstudio/models/nvidia/nemotron-3-super", "", 0),
            ("", "", 0),
        ]
        result = await lms_delete_model("nvidia/nemotron-3-super")
        assert result["ok"] is True
        find_call = mock_ssh.call_args_list[0][0][0]
        assert "find" in find_call
        assert "nemotron-3-super" in find_call


@pytest.mark.asyncio
async def test_lms_delete_model_failure():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        # find returns empty → model directory not found
        mock_ssh.return_value = ("", "", 0)
        result = await lms_delete_model("nvidia/not-existing")
        assert result["ok"] is False
        assert "nicht gefunden" in result["message"]


@pytest.mark.asyncio
async def test_lms_unload_all_success():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = ("", "", 0)
        from app.services.runtime_manager import lms_unload_all
        result = await lms_unload_all()
    assert result["ok"] is True
    mock_ssh.assert_called_once()
    called_cmd = mock_ssh.call_args[0][0]
    assert "unload" in called_cmd
    assert "--all" in called_cmd


@pytest.mark.asyncio
async def test_lms_unload_all_failure():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = ("", "error: no models loaded", 1)
        from app.services.runtime_manager import lms_unload_all
        result = await lms_unload_all()
    assert result["ok"] is False
