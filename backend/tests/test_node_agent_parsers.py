"""Tests for scripts/mc-node-agent.py's pure parser functions (Fleet & Rezepte
v2, Phase 1). No network, no subprocess, no real /proc — every sample is a
literal string fixture, loaded via importlib like scripts/context_detect.py's
test twin (test_context_detect_python.py) since the agent lives outside the
`app` package (stdlib-only, single-file, meant to run with no venv at all).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "mc-node-agent.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("mc_node_agent_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def agent():
    return _load_agent()


# ── read_proc_stat_cpu_pct ───────────────────────────────────────────────────


class TestReadProcStatCpuPct:
    def test_half_busy(self, agent):
        # user nice system idle iowait irq softirq steal guest guest_nice
        a = "cpu  100 0 100 800 0 0 0 0 0 0"
        b = "cpu  200 0 200 1000 0 0 0 0 0 0"
        # total_delta = (200+200+1000) - (100+100+800) = 1400 - 1000 = 400
        # idle_delta = 1000 - 800 = 200 -> busy = 1 - 200/400 = 50%
        assert agent.read_proc_stat_cpu_pct(a, b) == 50.0

    def test_fully_idle(self, agent):
        a = "cpu  0 0 0 1000 0 0 0 0 0 0"
        b = "cpu  0 0 0 2000 0 0 0 0 0 0"
        assert agent.read_proc_stat_cpu_pct(a, b) == 0.0

    def test_fully_busy(self, agent):
        a = "cpu  0 0 0 1000 0 0 0 0 0 0"
        b = "cpu  1000 0 0 1000 0 0 0 0 0 0"
        assert agent.read_proc_stat_cpu_pct(a, b) == 100.0

    def test_iowait_counts_as_idle(self, agent):
        a = "cpu  0 0 0 500 500 0 0 0 0 0"
        b = "cpu  0 0 0 500 1500 0 0 0 0 0"
        # total_delta = 1000, idle_delta (idle+iowait) = 1000 -> 0% busy
        assert agent.read_proc_stat_cpu_pct(a, b) == 0.0

    def test_malformed_line_returns_none(self, agent):
        assert agent.read_proc_stat_cpu_pct("not cpu data", "cpu  1 2 3 4") is None
        assert agent.read_proc_stat_cpu_pct("", "cpu  1 2 3 4 5") is None

    def test_no_time_elapsed_returns_none(self, agent):
        a = "cpu  100 0 100 800 0 0 0 0 0 0"
        assert agent.read_proc_stat_cpu_pct(a, a) is None


# ── read_meminfo ─────────────────────────────────────────────────────────────


class TestReadMeminfo:
    def test_full_meminfo(self, agent):
        text = (
            "MemTotal:       131072000 kB\n"
            "MemFree:         10000000 kB\n"
            "MemAvailable:   100000000 kB\n"
            "SwapTotal:        8000000 kB\n"
            "SwapFree:         6000000 kB\n"
        )
        result = agent.read_meminfo(text)
        assert result["mem_total_mb"] == round(131072000 / 1024)
        assert result["mem_available_mb"] == round(100000000 / 1024)
        assert result["mem_used_mb"] == round((131072000 - 100000000) / 1024)
        assert result["swap_used_mb"] == round((8000000 - 6000000) / 1024)

    def test_no_swap_lines(self, agent):
        text = "MemTotal:       1000000 kB\nMemAvailable:    500000 kB\n"
        result = agent.read_meminfo(text)
        assert result["swap_used_mb"] is None
        assert result["mem_used_mb"] == round((1000000 - 500000) / 1024)

    def test_missing_mem_total_all_none(self, agent):
        assert agent.read_meminfo("garbage: not kb\n") == {
            "mem_total_mb": None,
            "mem_used_mb": None,
            "mem_available_mb": None,
            "swap_used_mb": None,
        }


# ── read_loadavg ─────────────────────────────────────────────────────────────


class TestReadLoadavg:
    def test_parses_first_field(self, agent):
        assert agent.read_loadavg("0.52 0.58 0.59 1/823 12345\n") == 0.52

    def test_malformed_returns_none(self, agent):
        assert agent.read_loadavg("") is None
        assert agent.read_loadavg("not-a-number 0.5 0.5 1/1 1") is None


# ── parse_nvidia_smi ─────────────────────────────────────────────────────────


class TestParseNvidiaSmi:
    def test_normal_gpu(self, agent):
        result = agent.parse_nvidia_smi("35, 8806, 131072, 61\n")
        assert result == {
            "gpu_util_pct": 35,
            "vram_used_mb": 8806,
            "vram_total_mb": 131072,
            "gpu_temp_c": 61,
        }

    def test_unified_memory_na_fields(self, agent):
        """GB10-style unified memory box — nvidia-smi prints [N/A] for
        fields that don't apply (same tolerance as backend's SSH parser)."""
        result = agent.parse_nvidia_smi("[N/A], [N/A], [N/A], 55\n")
        assert result == {
            "gpu_util_pct": None,
            "vram_used_mb": None,
            "vram_total_mb": None,
            "gpu_temp_c": 55,
        }

    def test_multi_gpu_uses_first_line(self, agent):
        result = agent.parse_nvidia_smi("10, 100, 1000, 40\n90, 900, 9000, 80\n")
        assert result["gpu_util_pct"] == 10
        assert result["vram_total_mb"] == 1000

    def test_empty_stdout_binary_missing(self, agent):
        assert agent.parse_nvidia_smi("") == {
            "gpu_util_pct": None,
            "vram_used_mb": None,
            "vram_total_mb": None,
            "gpu_temp_c": None,
        }

    def test_malformed_line_all_none(self, agent):
        assert agent.parse_nvidia_smi("not,enough\n") == {
            "gpu_util_pct": None,
            "vram_used_mb": None,
            "vram_total_mb": None,
            "gpu_temp_c": None,
        }


# ── collect_telemetry — smoke test on the real host (no mocking) ────────────


class TestCollectTelemetry:
    def test_returns_all_expected_keys(self, agent):
        """Runs for real on whatever box executes the test suite (macOS/CI
        Linux) — every collector must degrade to None instead of raising, so
        this must succeed everywhere regardless of nvidia-smi/proc availability."""
        telemetry = agent.collect_telemetry()
        expected_keys = {
            "ts", "cpu_pct", "load1", "mem_total_mb", "mem_used_mb",
            "mem_available_mb", "swap_used_mb", "disk_used_gb", "disk_total_gb",
            "gpu_util_pct", "vram_used_mb", "vram_total_mb", "gpu_temp_c",
        }
        assert expected_keys <= set(telemetry.keys())
        assert telemetry["ts"]  # non-empty ISO timestamp
        assert telemetry["disk_used_gb"] is not None  # shutil.disk_usage('/') always works
