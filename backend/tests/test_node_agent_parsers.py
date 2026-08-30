"""Tests for scripts/mc-node-agent.py's pure parser functions (Fleet & Rezepte
v2, Phase 1). No network, no subprocess, no real /proc — every sample is a
literal string fixture, loaded via importlib like scripts/context_detect.py's
test twin (test_context_detect_python.py) since the agent lives outside the
`app` package (stdlib-only, single-file, meant to run with no venv at all).
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
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


# ── scan_model_inventory / inventory_hash (Nachtrag 30.08.2026) ─────────────


def _write_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class TestScanModelInventory:
    def test_models_local_style_with_config(self, agent, tmp_path):
        model_dir = tmp_path / "models-local" / "my-model"
        _write_file(model_dir / "weights.gguf", 1000)
        _write_file(model_dir / "tokenizer.json", 50)
        (model_dir / "config.json").write_text(json.dumps({"model_type": "llama"}))

        entries = agent.scan_model_inventory([tmp_path / "models-local"])
        assert len(entries) == 1
        entry = entries[0]
        assert entry["name"] == "my-model"
        assert entry["total_bytes"] == 1000 + 50 + len(json.dumps({"model_type": "llama"}))
        assert entry["file_count"] == 3  # weights + tokenizer + config.json itself
        assert entry["hf_repo_id"] is None
        assert entry["model_type"] == "llama"
        assert entry["mtime_max"] is not None

    def test_models_local_style_without_config(self, agent, tmp_path):
        model_dir = tmp_path / "models-local" / "no-config-model"
        _write_file(model_dir / "weights.bin", 200)

        entries = agent.scan_model_inventory([tmp_path / "models-local"])
        assert len(entries) == 1
        assert entries[0]["model_type"] is None
        assert entries[0]["hf_repo_id"] is None

    def test_hf_cache_style_decodes_repo_id_and_sizes_via_snapshots(self, agent, tmp_path):
        hub = tmp_path / "hub"
        repo_dir = hub / "models--meta-llama--Llama-3-70B"
        blobs = repo_dir / "blobs"
        _write_file(blobs / "abc123", 5000)
        snap_dir = repo_dir / "snapshots" / "main"
        snap_dir.mkdir(parents=True)
        (snap_dir / "model.safetensors").symlink_to(blobs / "abc123")
        (snap_dir / "config.json").write_text(json.dumps({"model_type": "llama"}))

        entries = agent.scan_model_inventory([hub])
        assert len(entries) == 1
        entry = entries[0]
        assert entry["hf_repo_id"] == "meta-llama/Llama-3-70B"
        assert entry["model_type"] == "llama"
        # size comes from the real blob (followed through the symlink) + config.json,
        # NOT double-counted even though blobs/ also technically holds abc123's bytes
        assert entry["total_bytes"] == 5000 + len(json.dumps({"model_type": "llama"}))

    def test_hf_cache_style_dedupes_shared_blob_across_revisions(self, agent, tmp_path):
        """Two snapshot revisions symlinking the SAME blob must count its
        bytes once, not twice — otherwise total_bytes overstates real disk usage."""
        hub = tmp_path / "hub"
        repo_dir = hub / "models--org--shared-blob-model"
        blobs = repo_dir / "blobs"
        _write_file(blobs / "sharedhash", 9000)
        for rev in ("rev1", "rev2"):
            snap_dir = repo_dir / "snapshots" / rev
            snap_dir.mkdir(parents=True)
            (snap_dir / "weights.bin").symlink_to(blobs / "sharedhash")

        entries = agent.scan_model_inventory([hub])
        assert entries[0]["total_bytes"] == 9000

    def test_hf_cache_style_dirname_without_repo_pattern_has_none_repo_id(self, agent, tmp_path):
        hub = tmp_path / "hub"
        weird_dir = hub / "models--onlyorg"  # no second '--' separator
        (weird_dir / "snapshots").mkdir(parents=True)

        entries = agent.scan_model_inventory([hub])
        assert entries[0]["hf_repo_id"] is None

    def test_mixed_roots_sorted_by_name(self, agent, tmp_path):
        local_root = tmp_path / "models-local"
        _write_file(local_root / "zzz-model" / "w.bin", 10)
        _write_file(local_root / "aaa-model" / "w.bin", 10)

        entries = agent.scan_model_inventory([local_root])
        assert [e["name"] for e in entries] == ["aaa-model", "zzz-model"]

    def test_missing_root_is_skipped_not_fatal(self, agent, tmp_path):
        assert agent.scan_model_inventory([tmp_path / "does-not-exist"]) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="posix permission bits")
    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
    def test_permission_denied_directory_is_skipped_not_fatal(self, agent, tmp_path):
        readable = tmp_path / "models-local" / "readable-model"
        _write_file(readable / "w.bin", 10)
        locked = tmp_path / "models-local" / "locked-model"
        locked.mkdir(parents=True)
        _write_file(locked / "w.bin", 10)
        os.chmod(locked, 0)  # no read/execute — os.scandir(locked) will raise

        try:
            entries = agent.scan_model_inventory([tmp_path / "models-local"])
        finally:
            os.chmod(locked, stat.S_IRWXU)  # restore so tmp_path cleanup can remove it

        names = [e["name"] for e in entries]
        # The unreadable directory is skipped entirely (its config.json stat
        # raises PermissionError) — "tolerate per directory" means the scan
        # keeps going, not that a broken entry gets faked with zero stats.
        assert names == ["readable-model"]


class TestInventoryHash:
    def test_deterministic_regardless_of_input_order(self, agent):
        a = [{"name": "a", "total_bytes": 1, "file_count": 1, "mtime_max": None,
              "hf_repo_id": None, "model_type": None}]
        b = [{"hf_repo_id": None, "model_type": None, "name": "a", "file_count": 1,
              "total_bytes": 1, "mtime_max": None}]
        assert agent.inventory_hash(a) == agent.inventory_hash(b)

    def test_different_content_different_hash(self, agent):
        a = [{"name": "a", "total_bytes": 1, "file_count": 1, "mtime_max": None,
              "hf_repo_id": None, "model_type": None}]
        b = [{"name": "a", "total_bytes": 2, "file_count": 1, "mtime_max": None,
              "hf_repo_id": None, "model_type": None}]
        assert agent.inventory_hash(a) != agent.inventory_hash(b)

    def test_empty_list_is_stable(self, agent):
        assert agent.inventory_hash([]) == agent.inventory_hash([])
