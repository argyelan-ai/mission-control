import pytest
from unittest.mock import AsyncMock, patch
from app.services.runtime_manager import _parse_spark_metrics, get_spark_metrics

NVIDIA_SMI_OUTPUT = "47, 88064, 131072, 62"
FREE_OUTPUT = """               total        used        free      shared  buff/cache   available
Mem:          128000       24000       80000        1000       24000      102000
Swap:              0           0           0"""
COMBINED_OUTPUT = f"{NVIDIA_SMI_OUTPUT}\n---\n{FREE_OUTPUT}"

UNREACHABLE = {
    "reachable": False,
    "gpu_util_pct": None,
    "vram_used_mb": None,
    "vram_total_mb": None,
    "gpu_temp_c": None,
    "ram_used_mb": None,
    "ram_total_mb": None,
}


def test_parse_spark_metrics_gpu():
    result = _parse_spark_metrics(COMBINED_OUTPUT)
    assert result["gpu_util_pct"] == 47
    assert result["gpu_temp_c"] == 62


def test_parse_spark_metrics_vram():
    result = _parse_spark_metrics(COMBINED_OUTPUT)
    assert result["vram_used_mb"] == 88064
    assert result["vram_total_mb"] == 131072


def test_parse_spark_metrics_ram():
    result = _parse_spark_metrics(COMBINED_OUTPUT)
    assert result["ram_total_mb"] == 128000
    assert result["ram_used_mb"] == 24000


def test_parse_spark_metrics_reachable():
    result = _parse_spark_metrics(COMBINED_OUTPUT)
    assert result["reachable"] is True


def test_parse_spark_metrics_invalid_returns_unreachable():
    result = _parse_spark_metrics("unexpected output")
    assert result["reachable"] is False
    assert result["gpu_util_pct"] is None


@pytest.mark.asyncio
async def test_get_spark_metrics_calls_ssh():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = (COMBINED_OUTPUT, "", 0)
        result = await get_spark_metrics()
        mock_ssh.assert_called_once()
        assert "nvidia-smi" in mock_ssh.call_args[0][0]
        assert "free -m" in mock_ssh.call_args[0][0]
        assert result["reachable"] is True


@pytest.mark.asyncio
async def test_get_spark_metrics_ssh_error_returns_unreachable():
    with patch("app.services.runtime_manager._ssh_run", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.side_effect = Exception("SSH connection refused")
        result = await get_spark_metrics()
        assert result == UNREACHABLE
