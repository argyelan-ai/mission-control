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
import socket
import shutil
import stat
import subprocess
import sys
import threading
import time
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

    def test_symlink_cycle_does_not_recurse_infinitely(self, agent, tmp_path):
        """Review finding #2 (30.08.2026): a directory symlink cycle must not
        blow the recursion limit — RecursionError isn't an OSError, so the
        per-entry try/except inside _walk_dir_stats wouldn't catch it."""
        model_dir = tmp_path / "models-local" / "cyclic-model"
        model_dir.mkdir(parents=True)
        _write_file(model_dir / "real.bin", 10)
        (model_dir / "self-loop").symlink_to(model_dir)  # symlink back to itself

        entries = agent.scan_model_inventory([tmp_path / "models-local"])
        assert len(entries) == 1
        # Only the real file is counted — the symlinked directory is never
        # descended into (directories only ever traverse real paths now).
        assert entries[0]["file_count"] == 1
        assert entries[0]["total_bytes"] == 10

    def test_mutual_symlink_cycle_between_two_dirs(self, agent, tmp_path):
        """Two directories symlinking into each other — a longer cycle than
        the direct self-loop above, still must not recurse forever."""
        root = tmp_path / "models-local"
        dir_a = root / "model-a"
        dir_b = root / "model-a" / "sub"
        dir_b.mkdir(parents=True)
        _write_file(dir_a / "real.bin", 5)
        (dir_b / "back-to-a").symlink_to(dir_a)

        entries = agent.scan_model_inventory([root])
        assert len(entries) == 1
        assert entries[0]["total_bytes"] == 5

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


def _neutralise_device_state(agent, monkeypatch):
    """Hält die Geräte-Steuerung aus den älteren run_loop-Tests heraus: der
    Test-Mac hat weder /proc noch nvidia-smi noch systemctl, und diese Tests
    prüfen ausschliesslich das Inventar-Verhalten."""
    monkeypatch.setattr(agent, "_run_nvidia_smi", lambda: "")
    monkeypatch.setattr(agent, "read_oom_guard", lambda: "missing")
    monkeypatch.setattr(agent, "collect_device_state", lambda **_kwargs: {"gpu_mode": "eco"})


# ── run_loop's inventory caching (review finding #7, 30.08.2026) ────────────


class TestRunLoopInventoryCaching:
    def test_scan_cached_across_retry_and_hash_only_commits_after_send_succeeds(self, agent, monkeypatch):
        """A failed heartbeat send must not force a re-scan on the immediate
        retry (the filesystem walk is the expensive part), and the
        "last sent" hash must only advance once a send actually went
        through — not the moment the scan finished."""
        one_entry = [{"name": "m", "total_bytes": 1, "file_count": 1,
                      "mtime_max": None, "hf_repo_id": None, "model_type": None}]
        scan_calls = []

        def fake_scan(paths):
            scan_calls.append(paths)
            return one_entry

        monkeypatch.setattr(agent, "scan_model_inventory", fake_scan)
        monkeypatch.setattr(agent, "collect_telemetry", lambda **_kwargs: {"ts": "now"})
        _neutralise_device_state(agent, monkeypatch)

        send_calls = []

        def fake_send(mc_url, token, telemetry, inventory=None, device_state=None):
            send_calls.append(inventory)
            if len(send_calls) == 1:
                raise RuntimeError("network blip")
            return {"ok": True}

        monkeypatch.setattr(agent, "send_heartbeat", fake_send)

        sleep_calls = {"n": 0}

        def fake_sleep(_seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 3:
                raise KeyboardInterrupt()

        monkeypatch.setattr(agent.time, "sleep", fake_sleep)

        agent.run_loop("http://mc.test", "tok")

        assert len(scan_calls) == 1  # NOT re-scanned on the retry
        assert send_calls[0] == one_entry  # first (failing) attempt still carries the fresh scan
        assert send_calls[1] == one_entry  # retry reuses the SAME cached scan, not a fresh one
        assert send_calls[2] is None  # steady state: hash unchanged, nothing to attach anymore

    def test_broken_scan_does_not_prevent_the_heartbeat_from_sending(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "collect_telemetry", lambda **_kwargs: {"ts": "now"})
        _neutralise_device_state(agent, monkeypatch)

        def fake_scan(paths):
            raise RuntimeError("permission denied somewhere deep")

        monkeypatch.setattr(agent, "scan_model_inventory", fake_scan)

        send_calls = []
        monkeypatch.setattr(
            agent, "send_heartbeat",
            lambda mc_url, token, telemetry, inventory=None, device_state=None: send_calls.append(inventory),
        )
        monkeypatch.setattr(agent.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))

        agent.run_loop("http://mc.test", "tok")

        assert send_calls == [None]  # heartbeat still went out, just without inventory


# ── install_systemd_unit's token migration (review finding #9, 30.08.2026) ──


class TestInstallSystemdUnitTokenMigration:
    def test_migrates_legacy_user_token_when_system_token_missing(self, agent, tmp_path, monkeypatch):
        """A plain `--pair CODE` run WITHOUT sudo saves the token under the
        operator's own home; a later `sudo ... --install` must pick it up
        from there instead of failing just because /etc has nothing yet."""
        system_token = tmp_path / "etc" / "mc-node-agent" / "token"
        legacy_token = tmp_path / "home" / "mark" / ".config" / "mc-node-agent" / "token"
        legacy_token.parent.mkdir(parents=True)
        legacy_token.write_text("secret-token-value\n")

        monkeypatch.setattr(agent, "SYSTEM_TOKEN_PATH", system_token)
        monkeypatch.setattr(agent, "SYSTEMD_UNIT_PATH", tmp_path / "mc-node-agent.service")
        monkeypatch.setattr(agent, "_user_token_path_for", lambda user: legacy_token)
        monkeypatch.setattr(agent, "_chown_to_user", lambda path, user: None)
        monkeypatch.setattr(agent.os, "geteuid", lambda: 0)
        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: None)

        agent.install_systemd_unit("http://mc.test", "mark")

        assert system_token.read_text(encoding="utf-8").strip() == "secret-token-value"
        assert oct(system_token.stat().st_mode & 0o777) == oct(0o600)

    def test_fails_cleanly_when_no_token_anywhere(self, agent, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "SYSTEM_TOKEN_PATH", tmp_path / "etc" / "token")
        monkeypatch.setattr(agent, "_user_token_path_for", lambda user: tmp_path / "nope" / "token")
        monkeypatch.setattr(agent.os, "geteuid", lambda: 0)

        with pytest.raises(SystemExit):
            agent.install_systemd_unit("http://mc.test", "mark")

    def test_does_not_touch_legacy_path_when_system_token_already_present(
        self, agent, tmp_path, monkeypatch
    ):
        system_token = tmp_path / "etc" / "mc-node-agent" / "token"
        system_token.parent.mkdir(parents=True)
        system_token.write_text("already-there\n")

        def _boom(_user):
            raise AssertionError("must not consult the legacy path when /etc already has a token")

        monkeypatch.setattr(agent, "SYSTEM_TOKEN_PATH", system_token)
        monkeypatch.setattr(agent, "SYSTEMD_UNIT_PATH", tmp_path / "mc-node-agent.service")
        monkeypatch.setattr(agent, "_user_token_path_for", _boom)
        monkeypatch.setattr(agent, "_chown_to_user", lambda path, user: None)
        monkeypatch.setattr(agent.os, "geteuid", lambda: 0)
        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: None)

        agent.install_systemd_unit("http://mc.test", "mark")
        assert system_token.read_text(encoding="utf-8").strip() == "already-there"


# ── _http_post_json — raw-socket HTTP client (Speicher-Diät round, 30.08.2026,
#    replaces urllib.request/http.client — see the module docstring). This is
#    the one item the task explicitly called out as needing thorough testing,
#    so every response shape it has to handle gets its own real TCP server. ──


class _OneShotHTTPServer:
    """Listens once on 127.0.0.1, hands the accepted connection to `handler`
    in a background thread, then closes. A real socket (not a mock) is the
    only faithful way to exercise hand-rolled HTTP parsing — chunked
    encoding, timeouts, and a mid-response disconnect are all about actual
    TCP behaviour, not something a mock can stand in for."""

    def __init__(self, handler):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, args=(handler,), daemon=True)
        self._thread.start()

    def _serve(self, handler):
        conn, _ = self._sock.accept()
        try:
            conn.settimeout(5)
            handler(conn)
        finally:
            conn.close()
            self._sock.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def join(self) -> None:
        self._thread.join(timeout=5)


def _read_request_headers(conn: socket.socket) -> bytes:
    """Drains the request up to the end of its headers — good enough for a
    test server that doesn't need to inspect the request body."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


class TestHttpPostJson:
    def test_success_content_length(self, agent):
        def handler(conn):
            _read_request_headers(conn)
            body = b'{"node_token": "abc123"}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
            )

        server = _OneShotHTTPServer(handler)
        result = agent._http_post_json(server.url + "/x", {"a": 1}, "tok", 5)
        server.join()
        assert result == {"node_token": "abc123"}

    def test_success_chunked_transfer_encoding(self, agent):
        def handler(conn):
            _read_request_headers(conn)
            pieces = [b'{"ok"', b": true, ", b'"n": 42}']
            chunked = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in pieces) + b"0\r\n\r\n"
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked)

        server = _OneShotHTTPServer(handler)
        result = agent._http_post_json(server.url + "/x", {}, None, 5)
        server.join()
        assert result == {"ok": True, "n": 42}

    def test_4xx_raises_http_error_with_status_and_body(self, agent):
        def handler(conn):
            _read_request_headers(conn)
            body = b'{"detail": "unbekannter Pairing-Code"}'
            conn.sendall(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)
            )

        server = _OneShotHTTPServer(handler)
        with pytest.raises(agent._HTTPError) as exc_info:
            agent._http_post_json(server.url + "/x", {}, None, 5)
        server.join()
        assert exc_info.value.status == 404
        assert b"unbekannter Pairing-Code" in exc_info.value.body

    def test_5xx_raises_http_error(self, agent):
        def handler(conn):
            _read_request_headers(conn)
            body = b'{"detail": "boom"}'
            conn.sendall(
                b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)
            )

        server = _OneShotHTTPServer(handler)
        with pytest.raises(agent._HTTPError) as exc_info:
            agent._http_post_json(server.url + "/x", {}, None, 5)
        server.join()
        assert exc_info.value.status == 500

    def test_redirect_is_surfaced_as_error_not_followed(self, agent):
        """A 3xx must never trigger a second connection to Location — this
        client has no redirect-following logic at all, so a 301 is just
        another non-2xx failure like any other."""
        second_connection_made = threading.Event()

        def handler(conn):
            _read_request_headers(conn)
            conn.sendall(
                b"HTTP/1.1 301 Moved Permanently\r\n"
                b"Location: http://example.invalid/elsewhere\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

        server = _OneShotHTTPServer(handler)
        with pytest.raises(agent._HTTPError) as exc_info:
            agent._http_post_json(server.url + "/x", {}, None, 5)
        server.join()
        assert exc_info.value.status == 301
        assert not second_connection_made.is_set()

    def test_timeout_raises_os_error(self, agent):
        def handler(conn):
            _read_request_headers(conn)
            time.sleep(2)  # never responds within the client's timeout

        server = _OneShotHTTPServer(handler)
        start = time.monotonic()
        with pytest.raises(OSError):
            agent._http_post_json(server.url + "/x", {}, None, 0.3)
        elapsed = time.monotonic() - start
        server.join()
        assert elapsed < 1.5  # actually timed out, didn't wait for the 2s sleep

    def test_connection_dropped_mid_response_raises_connection_error(self, agent):
        def handler(conn):
            _read_request_headers(conn)
            # Claims a 100-byte body, sends 7, then closes — client must not
            # hang waiting for bytes that will never arrive, nor silently
            # accept a truncated body as if it were complete.
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial")

        server = _OneShotHTTPServer(handler)
        with pytest.raises(ConnectionError):
            agent._http_post_json(server.url + "/x", {}, None, 5)
        server.join()

    def test_empty_body_2xx_returns_empty_dict(self, agent):
        def handler(conn):
            _read_request_headers(conn)
            conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

        server = _OneShotHTTPServer(handler)
        result = agent._http_post_json(server.url + "/x", {}, None, 5)
        server.join()
        assert result == {}

    def test_token_sent_as_bearer_authorization_header(self, agent):
        received = {}

        def handler(conn):
            received["headers"] = _read_request_headers(conn)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

        server = _OneShotHTTPServer(handler)
        agent._http_post_json(server.url + "/x", {}, "secret-tok", 5)
        server.join()
        assert b"Authorization: Bearer secret-tok\r\n" in received["headers"]

    def test_https_url_imports_ssl_only_lazily_not_for_http(self, agent, tmp_path):
        """Speicher-Diät requirement: an http:// deployment must never pay
        for `ssl` (~4 MB). Run in a FRESH subprocess (this test file's own
        process may already have ssl imported transitively via other
        libraries) so `'ssl' in sys.modules` is a meaningful signal."""
        script = tmp_path / "probe.py"
        module_path = Path(__file__).resolve().parents[2] / "scripts" / "mc-node-agent.py"
        script.write_text(
            "import sys, socket, threading, importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('agent', {str(module_path)!r})\n"
            "agent = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(agent)\n"
            "assert 'ssl' not in sys.modules, 'ssl must not be imported yet for a plain http:// URL'\n"
            "srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "srv.bind(('127.0.0.1', 0))\n"
            "srv.listen(1)\n"
            "port = srv.getsockname()[1]\n"
            "def serve():\n"
            "    conn, _ = srv.accept()\n"
            "    buf = b''\n"
            "    while b'\\r\\n\\r\\n' not in buf:\n"
            "        buf += conn.recv(4096)\n"
            "    conn.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\n{}')\n"
            "    conn.close()\n"
            "    srv.close()\n"
            "threading.Thread(target=serve, daemon=True).start()\n"
            "agent._http_post_json(f'http://127.0.0.1:{port}/x', {}, None, 5)\n"
            "assert 'ssl' not in sys.modules, 'ssl must stay lazy for an http:// URL'\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert result.stdout.strip() == "OK"


# ── Geräte-Steuerung: Ist-Zustand lesen (Vertrag 01.09.2026, Gewerk A) ──────


class TestParseNvidiaSmiDevice:
    def test_full_line(self, agent):
        out = agent.parse_nvidia_smi_device("42, 1024, 2048, 63, 1989, 33.12\n")
        assert out == {"gpu_clock_mhz": 1989, "gpu_power_w": 33.1, "gpu_temp_c": 63}

    def test_short_line_from_an_older_query_yields_none_not_an_error(self, agent):
        out = agent.parse_nvidia_smi_device("42, 1024, 2048, 63")
        assert out == {"gpu_clock_mhz": None, "gpu_power_w": None, "gpu_temp_c": 63}

    def test_na_fields(self, agent):
        out = agent.parse_nvidia_smi_device("[N/A], [N/A], [N/A], [N/A], [N/A], [N/A]")
        assert out == {"gpu_clock_mhz": None, "gpu_power_w": None, "gpu_temp_c": None}

    def test_empty_stdout(self, agent):
        assert agent.parse_nvidia_smi_device("") == {
            "gpu_clock_mhz": None, "gpu_power_w": None, "gpu_temp_c": None,
        }

    def test_telemetry_parser_still_reads_the_extended_line(self, agent):
        """Die zwei neuen Felder hängen hinten dran — der alte Parser muss
        unverändert weiterlesen (sonst hätte die Erweiterung die Telemetrie
        still kaputtgemacht)."""
        out = agent.parse_nvidia_smi("42, 1024, 2048, 63, 1989, 33.12")
        assert out == {
            "gpu_util_pct": 42, "vram_used_mb": 1024,
            "vram_total_mb": 2048, "gpu_temp_c": 63,
        }


class TestParseGpuMode:
    @pytest.mark.parametrize("raw,expected", [
        ("boost\n", "boost"), ("normal", "normal"), ("eco\n", "eco"),
        (" ECO+ \n", "eco+"),
    ])
    def test_known_modes(self, agent, raw, expected):
        assert agent.parse_gpu_mode(raw) == expected

    @pytest.mark.parametrize("raw", ["", "turbo", "eco; rm -rf /", "eco eco"])
    def test_unknown_is_unknown_not_guessed(self, agent, raw):
        assert agent.parse_gpu_mode(raw) == "unknown"


class TestParseOomGuard:
    def test_enabled_and_running(self, agent):
        assert agent.parse_oom_guard("enabled\n", "active\n") == "active"

    def test_enabled_but_dead(self, agent):
        assert agent.parse_oom_guard("enabled\n", "failed\n") == "inactive"

    def test_disabled(self, agent):
        assert agent.parse_oom_guard("disabled\n", "inactive\n") == "inactive"

    def test_unit_not_installed_is_missing_not_inactive(self, agent):
        """Der teuer bezahlte Unterschied: auf Marks zweiter Box war earlyoom
        gar nicht installiert — das muss anders aussehen als 'aus'."""
        assert agent.parse_oom_guard("not-found\n", "inactive\n") == "missing"

    def test_empty_output_is_missing(self, agent):
        assert agent.parse_oom_guard("", "") == "missing"


class TestParseAspmPolicy:
    def test_performance_active(self, agent):
        assert agent.parse_aspm_policy_performance("default [performance] powersave") is True

    def test_other_policy_active(self, agent):
        assert agent.parse_aspm_policy_performance("default performance [powersave]") is False

    def test_empty(self, agent):
        assert agent.parse_aspm_policy_performance("") is False


class TestParseDefaultIface:
    ROUTE = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
    )

    def test_picks_the_default_route_interface(self, agent):
        assert agent.parse_default_iface(self.ROUTE) == "eth0"

    def test_lowest_metric_wins(self, agent):
        text = self.ROUTE + "tailscale0\t00000000\t00000000\t0003\t0\t0\t50\t00000000\t0\t0\t0\n"
        assert agent.parse_default_iface(text) == "tailscale0"

    def test_no_default_route(self, agent):
        header = self.ROUTE.splitlines()[0] + "\n"
        assert agent.parse_default_iface(header) is None

    def test_garbage_is_not_fatal(self, agent):
        assert agent.parse_default_iface("kaputt") is None


class TestCollectDeviceState:
    def test_reads_every_contract_field_from_fake_system_files(self, agent, tmp_path, monkeypatch):
        (tmp_path / "mc-gpu-mode").write_text("eco\n")
        (tmp_path / "min_free_kbytes").write_text("5242880\n")
        (tmp_path / "policy").write_text("default [performance] powersave\n")
        monkeypatch.setattr(agent, "GPU_MODE_FILE", tmp_path / "mc-gpu-mode")
        monkeypatch.setattr(agent, "MIN_FREE_KBYTES_FILE", tmp_path / "min_free_kbytes")
        monkeypatch.setattr(agent, "ASPM_POLICY_FILE", tmp_path / "policy")
        monkeypatch.setattr(agent, "_holder_process_running", lambda name: True)
        monkeypatch.setattr(agent, "read_mtu", lambda: {"iface": "eth0", "value": 9000})

        state = agent.collect_device_state(
            gpu_fields={"gpu_clock_mhz": 1989, "gpu_power_w": 33.0, "gpu_temp_c": 63},
            oom_guard="active",
            apply_status={"applied_at": "2026-09-01T00:12:00+00:00", "last_error": None},
        )

        assert state == {
            "gpu_mode": "eco", "gpu_clock_mhz": 1989, "gpu_power_w": 33.0,
            "gpu_temp_c": 63, "min_free_kbytes": 5242880, "oom_guard": "active",
            "latency_tune": True, "mtu": {"iface": "eth0", "value": 9000},
            "applied_at": "2026-09-01T00:12:00+00:00", "last_error": None,
        }

    def test_missing_system_files_leave_safe_defaults(self, agent, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "GPU_MODE_FILE", tmp_path / "weg")
        monkeypatch.setattr(agent, "MIN_FREE_KBYTES_FILE", tmp_path / "weg")
        monkeypatch.setattr(agent, "ASPM_POLICY_FILE", tmp_path / "weg")
        monkeypatch.setattr(agent, "NET_ROUTE_FILE", tmp_path / "weg")

        state = agent.collect_device_state(gpu_fields={}, oom_guard="missing")

        assert state["gpu_mode"] == "unknown"
        assert state["min_free_kbytes"] is None
        assert state["latency_tune"] is False
        assert state["mtu"] is None

    def test_latency_tune_false_when_holder_process_is_gone(self, agent, tmp_path, monkeypatch):
        """ASPM allein reicht nicht — stirbt der Halte-Prozess, ist die
        Einstellung faktisch weg (genau der stille Rückfall nach Neustart)."""
        (tmp_path / "policy").write_text("default [performance] powersave\n")
        monkeypatch.setattr(agent, "ASPM_POLICY_FILE", tmp_path / "policy")
        monkeypatch.setattr(agent, "_holder_process_running", lambda name: False)
        assert agent.read_latency_tune() is False


class TestReadOomGuard:
    def test_no_systemctl_binary_is_missing_not_a_crash(self, agent, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("systemctl")

        monkeypatch.setattr(agent.subprocess, "run", boom)
        assert agent.read_oom_guard() == "missing"


# ── Geräte-Steuerung: Soll-Zustand anwenden ─────────────────────────────────


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def cmds(agent, monkeypatch):
    """Fängt jeden Setz-Befehl ab und merkt sich argv + kwargs, damit die
    Tests sowohl das WAS (Argumentliste) als auch das WIE (nie shell=True)
    prüfen können."""
    class _Calls(list):
        """Liste der Aufrufe; `.result["proc"]` erlaubt einem Test, den
        Rückgabewert des nächsten Befehls zu setzen (z.B. Exit 1)."""
        result = {"proc": _FakeProc(0)}

    calls = _Calls()
    calls.result = {"proc": _FakeProc(0)}

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return calls.result["proc"]

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    return calls


def _expected(agent, argv):
    """Erwartete Argumentliste inkl. sudo-Schicht — der Testrechner ist nicht
    root, das Geraet evtl. schon; beides muss derselbe Test abdecken."""
    return agent._privileged(argv)


def _bin(agent, name):
    return agent.control_binary(name)


CURRENT = {
    "gpu_mode": "eco",
    "min_free_kbytes": 5242880,
    "oom_guard": "active",
    "latency_tune": True,
    "mtu": {"iface": "eth0", "value": 9000},
}


class TestApplyDesiredState:
    def test_empty_desired_does_nothing(self, agent, cmds):
        assert agent.apply_desired_state({}, CURRENT) == (False, [])
        assert cmds == []

    def test_identical_desired_is_idempotent(self, agent, cmds, monkeypatch):
        """Die wichtigste Eigenschaft: der Abgleich läuft alle 15 s. Wäre er
        nicht idempotent, liefe alle 15 s ein root-Befehl auf dem Gerät."""
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        desired = {
            "gpu_mode": "eco", "min_free_kbytes": 5242880,
            "oom_guard": True, "latency_tune": True, "mtu": 9000,
        }
        changed, errors = agent.apply_desired_state(desired, CURRENT)
        assert (changed, errors) == (False, [])
        assert cmds == []

    def test_gpu_mode_change_calls_the_hardcoded_script_without_a_shell(self, agent, cmds):
        changed, errors = agent.apply_desired_state({"gpu_mode": "eco+"}, CURRENT)
        assert (changed, errors) == (True, [])
        argv, kwargs = cmds[0]
        assert argv == _expected(agent, [agent.GPU_MODE_SCRIPT, "eco+"])
        assert kwargs.get("shell") is not True

    @pytest.mark.parametrize("evil", [
        "eco; rm -rf /", "eco && reboot", "$(id)", "`id`", "eco\nrm -rf /",
        "../../bin/sh", 42, None, True, ["eco"],
    ])
    def test_malicious_or_unknown_gpu_mode_is_refused(self, agent, cmds, evil):
        changed, errors = agent.apply_desired_state({"gpu_mode": evil}, CURRENT)
        assert changed is False
        assert cmds == []  # NICHTS ausgeführt
        assert errors and "gpu_mode" in errors[0]

    def test_min_free_kbytes_change(self, agent, cmds):
        changed, errors = agent.apply_desired_state({"min_free_kbytes": 1048576}, CURRENT)
        assert (changed, errors) == (True, [])
        assert cmds[0][0] == _expected(agent, [_bin(agent, "sysctl"), "-w", "vm.min_free_kbytes=1048576"])

    @pytest.mark.parametrize("bad", ["1048576", 0, -5, True, 1024, 99999999999, 1.5, None])
    def test_min_free_kbytes_rejects_wrong_type_or_range(self, agent, cmds, bad):
        changed, errors = agent.apply_desired_state({"min_free_kbytes": bad}, CURRENT)
        assert (changed, cmds) == (False, [])
        assert errors and "min_free_kbytes" in errors[0]

    def test_oom_guard_enable_when_unit_missing(self, agent, cmds):
        current = dict(CURRENT, oom_guard="missing")
        changed, errors = agent.apply_desired_state({"oom_guard": True}, current)
        assert (changed, errors) == (True, [])
        assert cmds[0][0] == _expected(agent, [_bin(agent, "systemctl"), "enable", "--now", "earlyoom"])

    def test_oom_guard_disable(self, agent, cmds):
        changed, errors = agent.apply_desired_state({"oom_guard": False}, CURRENT)
        assert (changed, errors) == (True, [])
        assert cmds[0][0] == _expected(agent, [_bin(agent, "systemctl"), "disable", "--now", "earlyoom"])

    def test_oom_guard_rejects_non_boolean(self, agent, cmds):
        changed, errors = agent.apply_desired_state({"oom_guard": "yes"}, CURRENT)
        assert (changed, cmds) == (False, [])
        assert errors and "oom_guard" in errors[0]

    def test_latency_tune_on(self, agent, cmds):
        current = dict(CURRENT, latency_tune=False)
        changed, errors = agent.apply_desired_state({"latency_tune": True}, current)
        assert (changed, errors) == (True, [])
        assert cmds[0][0] == _expected(agent, [_bin(agent, "bash"), agent.LATENCY_TUNE_SCRIPT])

    def test_latency_tune_off_is_reported_not_invented(self, agent, cmds):
        changed, errors = agent.apply_desired_state({"latency_tune": False}, CURRENT)
        assert (changed, cmds) == (False, [])
        assert errors and "latency_tune" in errors[0]

    def test_mtu_change_uses_the_interface_from_the_ist_zustand(self, agent, cmds, monkeypatch):
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        changed, errors = agent.apply_desired_state({"mtu": 1500}, CURRENT)
        assert (changed, errors) == (True, [])
        assert cmds[0][0] == _expected(agent, [_bin(agent, "ip"), "link", "set", "eth0", "mtu", "1500"])

    @pytest.mark.parametrize("bad", [99, 576, 9216, 99999, "9000", True, None])
    def test_mtu_rejects_implausible_values(self, agent, cmds, monkeypatch, bad):
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        changed, errors = agent.apply_desired_state({"mtu": bad}, CURRENT)
        assert (changed, cmds) == (False, [])
        assert errors and "mtu" in errors[0]

    def test_mtu_without_a_known_interface_is_refused(self, agent, cmds):
        current = dict(CURRENT, mtu=None)
        changed, errors = agent.apply_desired_state({"mtu": 9000}, current)
        assert (changed, cmds) == (False, [])
        assert errors and "Schnittstelle" in errors[0]

    def test_permission_denied_lands_in_last_error_without_crashing(self, agent, cmds):
        cmds.result["proc"] = _FakeProc(1, stderr="sysctl: permission denied\n")
        changed, errors = agent.apply_desired_state({"min_free_kbytes": 1048576}, CURRENT)
        assert changed is False
        assert errors and "permission denied" in errors[0]

    def test_missing_script_lands_in_last_error_without_crashing(self, agent, monkeypatch):
        def boom(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(agent.subprocess, "run", boom)
        changed, errors = agent.apply_desired_state({"gpu_mode": "boost"}, CURRENT)
        assert changed is False
        assert errors and "nicht vorhanden" in errors[0]  # sudo ODER das Skript

    def test_timeout_lands_in_last_error_without_crashing(self, agent, monkeypatch):
        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 20)

        monkeypatch.setattr(agent.subprocess, "run", boom)
        changed, errors = agent.apply_desired_state({"latency_tune": True},
                                                   dict(CURRENT, latency_tune=False))
        assert changed is False
        assert errors and "fehlgeschlagen" in errors[0]

    def test_one_bad_field_does_not_block_the_others(self, agent, cmds):
        desired = {"gpu_mode": "unsinn", "min_free_kbytes": 1048576}
        changed, errors = agent.apply_desired_state(desired, CURRENT)
        assert changed is True
        assert len(errors) == 1
        assert cmds[0][0] == _expected(agent, [_bin(agent, "sysctl"), "-w", "vm.min_free_kbytes=1048576"])

    def test_non_dict_desired_state_is_ignored(self, agent, cmds):
        changed, errors = agent.apply_desired_state("gib mir root", CURRENT)
        assert (changed, cmds) == (False, [])
        assert errors


# ── Geräte-Steuerung im Heartbeat-Kreislauf ─────────────────────────────────


class TestHeartbeatDeviceState:
    def test_device_state_is_attached_to_the_payload(self, agent, monkeypatch):
        sent = {}
        monkeypatch.setattr(
            agent, "_http_post_json",
            lambda url, payload, token, timeout: sent.update(payload) or {},
        )
        agent.send_heartbeat("http://mc.test", "tok", {"ts": "now"},
                             device_state={"gpu_mode": "eco"})
        assert sent["device_state"] == {"gpu_mode": "eco"}

    def test_payload_stays_unchanged_when_no_device_state(self, agent, monkeypatch):
        sent = {}
        monkeypatch.setattr(
            agent, "_http_post_json",
            lambda url, payload, token, timeout: sent.update(payload) or {},
        )
        agent.send_heartbeat("http://mc.test", "tok", {"ts": "now"})
        assert "device_state" not in sent


class TestRunLoopDesiredState:
    """run_loop mit echter Antwortverarbeitung — collect_device_state und
    send_heartbeat sind gefälscht, apply_desired_state ist es NICHT."""

    def _drive(self, agent, monkeypatch, responses, current=None, device_state_fn=None):
        monkeypatch.setattr(agent, "_run_nvidia_smi", lambda: "")
        monkeypatch.setattr(agent, "read_oom_guard", lambda: "missing")
        monkeypatch.setattr(agent, "collect_telemetry", lambda **_k: {"ts": "now"})
        monkeypatch.setattr(agent, "scan_model_inventory", lambda paths: [])
        monkeypatch.setattr(
            agent, "collect_device_state",
            device_state_fn or (lambda **_k: dict(current if current is not None else CURRENT)),
        )
        seq = list(responses)

        def fake_send(mc_url, token, telemetry, inventory=None, device_state=None):
            sends.append(device_state)
            return seq.pop(0) if seq else {}

        sends: list = []
        monkeypatch.setattr(agent, "send_heartbeat", fake_send)
        n = {"i": 0}

        def fake_sleep(_s):
            n["i"] += 1
            if n["i"] >= len(responses):
                raise KeyboardInterrupt()

        monkeypatch.setattr(agent.time, "sleep", fake_sleep)
        agent.run_loop("http://mc.test", "tok")
        return sends

    def test_response_without_desired_state_changes_nothing(self, agent, monkeypatch, cmds):
        sends = self._drive(agent, monkeypatch, [{"ok": True}])
        assert sends == [CURRENT]  # Ist-Zustand ging mit raus
        assert cmds == []  # aber nichts wurde gesetzt

    def test_old_backend_empty_response_changes_nothing(self, agent, monkeypatch, cmds):
        self._drive(agent, monkeypatch, [{}])
        assert cmds == []

    def test_desired_state_equal_to_ist_changes_nothing(self, agent, monkeypatch, cmds):
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        self._drive(agent, monkeypatch, [{"desired_state": {"gpu_mode": "eco", "oom_guard": True}}])
        assert cmds == []

    def test_deviating_desired_state_is_applied(self, agent, monkeypatch, cmds):
        self._drive(agent, monkeypatch, [{"desired_state": {"gpu_mode": "eco+"}}])
        assert cmds[0][0] == _expected(agent, [agent.GPU_MODE_SCRIPT, "eco+"])

    def test_failure_is_reported_in_the_next_heartbeat_and_the_loop_lives(
        self, agent, monkeypatch, cmds
    ):
        cmds.result["proc"] = _FakeProc(1, stderr="Operation not permitted")
        def device_state(gpu_fields=None, oom_guard=None, apply_status=None):
            return dict(
                CURRENT,
                applied_at=(apply_status or {}).get("applied_at"),
                last_error=(apply_status or {}).get("last_error"),
            )

        sends = self._drive(
            agent, monkeypatch,
            [{"desired_state": {"gpu_mode": "boost"}}, {"ok": True}],
            device_state_fn=device_state,
        )
        assert len(sends) == 2  # Schleife lebt weiter
        assert sends[0]["last_error"] is None  # erste Runde: noch kein Versuch
        assert "not permitted" in sends[1]["last_error"]


# ── Formatvertrag Agent <-> Backend (Präzisierung 01.09.2026) ───────────────
#
# Zwei bewusste Formatbrüche: `mtu` ist im Soll eine nackte Zahl, im Ist
# {"iface": …, "value": …}; `oom_guard` ist im Soll bool, im Ist dreiwertig.
# Passt eine der beiden Seiten nicht zusammen, stünde die Ampel dauerhaft auf
# gelb, OHNE dass irgendetwas kaputt wäre — genau deshalb hier festgenagelt.


class TestFormatContractWithBackend:
    def test_mtu_ist_is_a_dict_soll_is_a_bare_number(self, agent, cmds, monkeypatch):
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        # Soll = blanke Zahl, Ist = {"iface", "value"} -> erfüllt, nichts tun
        changed, errors = agent.apply_desired_state({"mtu": 9000}, CURRENT)
        assert (changed, errors, list(cmds)) == (False, [], [])

    def test_mtu_compares_only_the_value_never_the_iface(self, agent, cmds, monkeypatch):
        """MC kennt den Namen der Netzwerkkarte nicht. Ein anderer iface-Name
        darf deshalb NIE als Abweichung zählen."""
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        current = dict(CURRENT, mtu={"iface": "ganz-anders0", "value": 9000})
        changed, errors = agent.apply_desired_state({"mtu": 9000}, current)
        assert (changed, errors, list(cmds)) == (False, [], [])

    @pytest.mark.parametrize("ist,soll,erfuellt", [
        ("active", True, True),
        ("inactive", True, False),
        ("missing", True, False),
        ("active", False, False),
        ("inactive", False, True),
        ("missing", False, True),   # nicht installiert = "aus" ist erfüllt
    ])
    def test_oom_guard_bool_vs_three_state_matrix(self, agent, cmds, ist, soll, erfuellt):
        current = dict(CURRENT, oom_guard=ist)
        changed, errors = agent.apply_desired_state({"oom_guard": soll}, current)
        assert errors == []
        assert changed is not erfuellt
        assert (cmds == []) is erfuellt

    def test_gpu_mode_unknown_is_an_ist_value_never_a_command(self, agent, cmds):
        """'unknown' heisst "konnte ich nicht feststellen" — als Befehl ist es
        sinnlos und muss abgelehnt werden, sonst liefe mc-gpu-mode.sh unknown."""
        assert agent.parse_gpu_mode("") == "unknown"  # im Ist erlaubt
        changed, errors = agent.apply_desired_state(
            {"gpu_mode": "unknown"}, dict(CURRENT, gpu_mode="unknown")
        )
        assert (changed, list(cmds)) == (False, [])
        assert errors and "gpu_mode" in errors[0]

    def test_real_backend_response_shape_without_desired_state(self, agent, monkeypatch, cmds):
        """Exakt die Antwort, die nodes.py heute liefert, wenn kein Soll
        gesetzt ist (desired_state fällt via exclude_none ganz weg)."""
        sends = TestRunLoopDesiredState()._drive(
            agent, monkeypatch,
            [{"ok": True, "heartbeat_interval_s": 15, "commands": []}],
        )
        assert len(sends) == 1
        assert cmds == []

    def test_real_backend_response_shape_with_desired_state(self, agent, monkeypatch, cmds):
        sends = TestRunLoopDesiredState()._drive(
            agent, monkeypatch,
            [{"ok": True, "heartbeat_interval_s": 15, "commands": [],
              "desired_state": {"gpu_mode": "normal", "oom_guard": True, "mtu": 9000}}],
        )
        assert len(sends) == 1
        assert [c[0] for c in cmds] == [_expected(agent, [agent.GPU_MODE_SCRIPT, "normal"])]  # nur die Abweichung

    def test_limits_match_the_backends_limits_exactly(self, agent):
        """Drift-Wächter: wäre der Agent lockerer als das Backend, gäbe es
        einen Wertebereich, den nur eine Seite kennt."""
        svc = pytest.importorskip("app.services.device_state")
        assert (agent.MIN_FREE_KBYTES_MIN, agent.MIN_FREE_KBYTES_MAX) == svc.MIN_FREE_KBYTES_RANGE
        assert (agent.MTU_MIN, agent.MTU_MAX) == svc.MTU_RANGE
        assert agent.GPU_MODES == svc.GPU_MODES

    def test_reported_ist_validates_against_the_backends_pydantic_model(self, agent, tmp_path, monkeypatch):
        """E2E-Absicherung ohne Box: der Ist-Zustand, den der Agent baut, muss
        durch das DeviceState-Modell des echten Endpunkts gehen — sonst
        antwortet das Backend im Feldversuch mit 422 statt mit dem Soll."""
        nodes = pytest.importorskip("app.routers.nodes")
        (tmp_path / "mc-gpu-mode").write_text("eco\n")
        monkeypatch.setattr(agent, "GPU_MODE_FILE", tmp_path / "mc-gpu-mode")
        monkeypatch.setattr(agent, "MIN_FREE_KBYTES_FILE", tmp_path / "weg")
        monkeypatch.setattr(agent, "ASPM_POLICY_FILE", tmp_path / "weg")
        monkeypatch.setattr(agent, "read_mtu", lambda: {"iface": "eth0", "value": 9000})

        state = agent.collect_device_state(
            gpu_fields={"gpu_clock_mhz": 1989, "gpu_power_w": 33.1, "gpu_temp_c": 63},
            oom_guard="missing",
            apply_status={
                "applied_at": "2026-09-01T00:12:00+00:00",
                "last_error": "sysctl -> Exit 1: permission denied",
            },
        )
        parsed = nodes.DeviceState.model_validate(state)
        assert parsed.gpu_mode == "eco"
        assert parsed.oom_guard == "missing"
        assert parsed.mtu.value == 9000
        assert parsed.applied_at is not None
        # Und der ganze Heartbeat-Body, so wie er über die Leitung geht:
        nodes.HeartbeatRequest.model_validate(
            {"telemetry": {"ts": "2026-09-01T00:12:00+00:00"},
             "agent_version": agent.AGENT_VERSION, "device_state": state}
        )


# ── Rechte: sudo -n + --allow-control (Nachtrag 01.09.2026) ─────────────────
#
# Der Live-Test auf einer echten Box zeigte, was kein Testdaten-Lauf zeigen konnte: der
# Dienst läuft als gpuops, "ip link set" antwortet mit "Operation not
# permitted". Melden ging, Setzen nicht.


class TestSudoLayer:
    def test_non_root_gets_a_sudo_n_prefix(self, agent, monkeypatch):
        monkeypatch.setattr(agent.os, "geteuid", lambda: 1000)
        assert agent._privileged(["ip", "link"]) == ["sudo", "-n", "ip", "link"]

    def test_root_calls_directly_without_sudo(self, agent, monkeypatch):
        monkeypatch.setattr(agent.os, "geteuid", lambda: 0)
        assert agent._privileged(["ip", "link"]) == ["ip", "link"]

    def test_control_binaries_are_absolute_paths_never_bare_names(self, agent, cmds, monkeypatch):
        """sudo löst über secure_path auf, nicht über den PATH des Dienstes.
        Ein nackter Name könnte deshalb auf einen anderen Pfad zeigen als die
        sudoers-Regel — die Regel griffe dann nie, ohne dass jemand sieht warum."""
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        agent.apply_desired_state(
            {"gpu_mode": "boost", "min_free_kbytes": 1048576,
             "oom_guard": False, "mtu": 1500}, CURRENT,
        )
        for argv, _kwargs in cmds:
            binary = argv[2] if argv[0] == "sudo" else argv[0]
            assert binary.startswith("/"), f"kein absoluter Pfad: {argv}"

    @pytest.mark.parametrize("stderr", [
        "sudo: a password is required",
        "sudo: no tty present and no askpass program specified",
        "Sorry, user gpuops is not allowed to execute '/usr/sbin/ip link set eth0 mtu 9000' as root.",
        "gpuops is not in the sudoers file.  This incident will be reported.",
    ])
    def test_missing_sudoers_rule_becomes_a_readable_message(self, agent, cmds, monkeypatch, stderr):
        """Nicht der rohe Exit-Code, sondern ein Satz, der sagt was zu tun ist."""
        monkeypatch.setattr(agent.os, "geteuid", lambda: 1000)
        cmds.result["proc"] = _FakeProc(1, stderr=stderr)
        changed, errors = agent.apply_desired_state({"gpu_mode": "boost"}, CURRENT)
        assert changed is False
        assert len(errors) == 1
        assert "--allow-control" in errors[0]
        assert "keine Berechtigung" in errors[0]

    def test_a_real_command_failure_is_still_reported_verbatim(self, agent, cmds, monkeypatch):
        """Gegenprobe: scheitert der Befehl SELBST (nicht sudo), darf die
        sudoers-Meldung nicht darüberbügeln — sonst sucht der Betreiber an
        der falschen Stelle."""
        monkeypatch.setattr(agent.os, "geteuid", lambda: 1000)
        cmds.result["proc"] = _FakeProc(2, stderr="RTNETLINK answers: Invalid argument")
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        changed, errors = agent.apply_desired_state({"mtu": 1500}, CURRENT)
        assert changed is False
        assert "Invalid argument" in errors[0]
        assert "--allow-control" not in errors[0]

    def test_missing_sudo_binary_does_not_crash(self, agent, monkeypatch):
        monkeypatch.setattr(agent.os, "geteuid", lambda: 1000)

        def boom(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(agent.subprocess, "run", boom)
        changed, errors = agent.apply_desired_state({"gpu_mode": "boost"}, CURRENT)
        assert changed is False
        assert errors and "sudo nicht vorhanden" in errors[0]


class TestRenderSudoers:
    def _rule(self, agent):
        return agent.render_sudoers("gpuops", exists=lambda p: True)

    def test_covers_exactly_the_five_actions(self, agent):
        text = self._rule(agent)
        assert text.count("NOPASSWD:") == 4 + 1 + 6 + 1 + 1  # Modi + sysctl + systemctl + bash + ip
        for mode in agent.GPU_MODES:
            assert f"{agent.GPU_MODE_SCRIPT} {mode}" in text
        assert "-w vm.min_free_kbytes=*" in text
        assert "enable --now earlyoom" in text
        assert f"bash {agent.LATENCY_TUNE_SCRIPT}" in text
        assert "link set * mtu *" in text

    @pytest.mark.parametrize("verboten", [
        "tee", " sh ", "/bin/sh", "sed", "ALL\n", "NOPASSWD: ALL",
        "systemctl *", "sysctl -w *", "mc-gpu-mode.sh *",
    ])
    def test_never_grants_a_backdoor_to_full_root(self, agent, verboten):
        """tee/sh/sed oder ein Platzhalter am falschen Ort wären faktisch
        volles root durch die Hintertür. Geprüft werden NUR die Regelzeilen —
        im Kopfkommentar dürfen diese Wörter stehen (dort steht ja, dass sie
        verboten sind)."""
        rules = "\n".join(
            line for line in self._rule(agent).splitlines() if "NOPASSWD:" in line
        )
        assert verboten not in rules

    def test_bash_only_ever_with_the_one_fixed_script(self, agent):
        for line in self._rule(agent).splitlines():
            if "bash" in line and line.startswith("gpuops"):
                assert line.endswith(agent.LATENCY_TUNE_SCRIPT)

    def test_paths_come_from_the_same_resolution_as_the_real_calls(self, agent, cmds, monkeypatch):
        """Drift-Wächter: Regel und Aufruf müssen denselben Pfad meinen."""
        monkeypatch.setattr(agent, "_iface_exists", lambda iface: True)
        monkeypatch.setattr(agent.os, "geteuid", lambda: 1000)
        agent.apply_desired_state(
            {"gpu_mode": "boost", "min_free_kbytes": 1048576,
             "oom_guard": False, "mtu": 1500}, CURRENT,
        )
        text = agent.render_sudoers("gpuops")
        for argv, _kwargs in cmds:
            assert argv[2] in text, f"Pfad {argv[2]} steht in keiner sudoers-Regel"

    def test_real_visudo_accepts_the_generated_file(self, agent, tmp_path):
        """Kein Ersatz-Check: das echte visudo liest die echte Datei."""
        if shutil.which("visudo") is None:
            pytest.skip("visudo nicht vorhanden")
        f = tmp_path / "mc-node-agent"
        f.write_text(self._rule(agent), encoding="utf-8")
        proc = subprocess.run(["visudo", "-c", "-f", str(f)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestInstallSudoers:
    def _prepare(self, agent, tmp_path, monkeypatch):
        monkeypatch.setattr(agent.os, "geteuid", lambda: 0)
        monkeypatch.setattr(agent, "SUDOERS_PATH", tmp_path / "sudoers.d" / "mc-node-agent")
        monkeypatch.setattr(agent, "SYSTEM_TOKEN_PATH", tmp_path / "etc" / "token")
        return tmp_path / "sudoers.d" / "mc-node-agent"

    def test_writes_the_file_when_visudo_is_happy(self, agent, tmp_path, monkeypatch):
        target = self._prepare(agent, tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "_visudo_check", lambda path: None)
        agent.install_sudoers("gpuops")
        assert target.exists()
        assert "mc-gpu-mode.sh eco" in target.read_text()
        assert stat.S_IMODE(target.stat().st_mode) == 0o440

    def test_invalid_syntax_leaves_nothing_behind(self, agent, tmp_path, monkeypatch):
        """Eine kaputte Datei in sudoers.d legt JEDES sudo lahm — der Nutzer
        könnte sich aussperren. Es darf nichts zurückbleiben."""
        target = self._prepare(agent, tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "_visudo_check", lambda path: ">>> syntax error near line 4 <<<")
        with pytest.raises(SystemExit):
            agent.install_sudoers("gpuops")
        assert not target.exists()
        assert not (tmp_path / "etc" / "sudoers.staged").exists()

    def test_syntax_is_checked_before_the_file_reaches_sudoers_d(self, agent, tmp_path, monkeypatch):
        """Der Prüfling darf NICHT in sudoers.d liegen — sonst gäbe es ein
        Zeitfenster, in dem sudo bereits kaputt ist."""
        target = self._prepare(agent, tmp_path, monkeypatch)
        checked: list[Path] = []

        def fake_check(path):
            checked.append(Path(path))
            assert not target.exists() or Path(path) == target
            return None

        monkeypatch.setattr(agent, "_visudo_check", fake_check)
        agent.install_sudoers("gpuops")
        assert checked[0].parent != target.parent  # erst ausserhalb geprüft
        assert checked[-1] == target  # und danach am echten Platz noch einmal

    def test_a_complaint_after_the_move_removes_the_file_again(self, agent, tmp_path, monkeypatch):
        target = self._prepare(agent, tmp_path, monkeypatch)
        calls = {"n": 0}

        def fake_check(path):
            calls["n"] += 1
            return None if calls["n"] == 1 else "Kollision mit einer anderen Regel"

        monkeypatch.setattr(agent, "_visudo_check", fake_check)
        with pytest.raises(SystemExit):
            agent.install_sudoers("gpuops")
        assert not target.exists()

    def test_needs_root(self, agent, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "SUDOERS_PATH", tmp_path / "mc-node-agent")
        monkeypatch.setattr(agent.os, "geteuid", lambda: 1000)
        with pytest.raises(SystemExit):
            agent.install_sudoers("gpuops")
        assert not (tmp_path / "mc-node-agent").exists()

    def test_missing_visudo_refuses_to_install_unchecked(self, agent, tmp_path, monkeypatch):
        target = self._prepare(agent, tmp_path, monkeypatch)

        def boom(argv, **kwargs):
            raise FileNotFoundError("visudo")

        monkeypatch.setattr(agent.subprocess, "run", boom)
        with pytest.raises(SystemExit):
            agent.install_sudoers("gpuops")
        assert not target.exists()


class TestAllowControlFlag:
    def test_flag_is_off_by_default(self, agent):
        assert agent.parse_args(["--mc-url", "http://x", "--install"]).allow_control is False

    def test_flag_is_parsed(self, agent):
        args = agent.parse_args(["--mc-url", "http://x", "--install", "--allow-control"])
        assert (args.install, args.allow_control) == (True, True)

    def test_install_without_the_flag_writes_no_sudoers_file(self, agent, monkeypatch):
        """Standard bleibt: melden ja, setzen nein."""
        monkeypatch.setattr(agent, "install_systemd_unit", lambda mc_url, user, allow_control=False: None)
        called = []
        monkeypatch.setattr(agent, "install_sudoers", lambda user: called.append(user))
        assert agent.main(["--mc-url", "http://x", "--install"]) == 0
        assert called == []

    def test_install_with_the_flag_writes_the_sudoers_file(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "install_systemd_unit", lambda mc_url, user, allow_control=False: None)
        called = []
        monkeypatch.setattr(agent, "install_sudoers", lambda user: called.append(user))
        monkeypatch.setattr(agent, "_default_install_user", lambda: "gpuops")
        assert agent.main(["--mc-url", "http://x", "--install", "--allow-control"]) == 0
        assert called == ["gpuops"]

    def test_allow_control_alone_does_not_touch_the_service(self, agent, monkeypatch):
        """Nachträglich freischalten, ohne den laufenden Dienst anzufassen."""
        unit = []
        monkeypatch.setattr(agent, "install_systemd_unit", lambda mc_url, user, allow_control=False: unit.append(user))
        monkeypatch.setattr(agent, "install_sudoers", lambda user: None)
        monkeypatch.setattr(agent, "_default_install_user", lambda: "gpuops")
        assert agent.main(["--mc-url", "http://x", "--allow-control"]) == 0
        assert unit == []


# ── Unit-Härtung vs. Steuerung (Live-Fund, 01.09.2026) ────────────────
#
# "The 'no new privileges' flag is set, which prevents sudo from running as
# root" — die eigene Härtung blockierte den sudo-Weg. Kein Testdaten-Lauf
# konnte das zeigen, nur die echte Box.


class TestUnitHardening:
    def _unit(self, agent, allow_control):
        return agent.render_unit(
            "https://mc.test", "gpuops", "/usr/bin/python3", "/opt/mc-node-agent.py",
            allow_control=allow_control,
        )

    def test_report_only_keeps_no_new_privileges_on(self, agent):
        assert "NoNewPrivileges=true" in self._unit(agent, False)

    def test_allow_control_turns_no_new_privileges_off(self, agent):
        unit = self._unit(agent, True)
        assert "NoNewPrivileges=no" in unit
        assert "NoNewPrivileges=true" not in unit

    def test_memory_cap_leaves_room_for_the_sudo_child(self, agent):
        """sudo und sein Kind laufen im SELBEN cgroup und zählen auf dieselbe
        Grenze. Bei 32 MB hart würde der erste systemctl-Aufruf den OOM-Killer
        auslösen — ohne Meldung, also genau der stille Ausfall."""
        report = self._unit(agent, False)
        control = self._unit(agent, True)
        assert "MemoryMax=32M" in report and "TasksMax=8" in report
        assert "MemoryMax=96M" in control and "TasksMax=16" in control

    def test_the_rest_of_the_straightjacket_is_untouched(self, agent):
        """Gelockert wird NUR, was den Steuer-Weg blockiert — der Rest bleibt."""
        for allow in (False, True):
            unit = self._unit(agent, allow)
            assert "Nice=10" in unit
            assert "IOSchedulingClass=idle" in unit
            assert "CPUQuota=10%" in unit
            assert "Restart=always" in unit
            assert "User=gpuops" in unit

    @pytest.mark.parametrize("blocker", [
        "RestrictSUIDSGID",       # blockiert sudo (setuid)
        "ProtectKernelTunables",  # /proc/sys nur-lesbar -> sysctl -w scheitert
        "ProtectSystem",          # /etc + /usr dicht
        "PrivateDevices",         # /dev/cpu_dma_latency weg -> latency-tune
        "CapabilityBoundingSet",  # ohne CAP_NET_ADMIN kein ip link set
    ])
    def test_no_other_flag_of_the_same_class_is_set(self, agent, blocker):
        """Wächter: jedes dieser Flags würde eine der fünf Aktionen brechen —
        jeweils mit einer anderen, schwer zuzuordnenden Meldung. Fügt sie
        jemand später hinzu, wird dieser Test rot statt der Box."""
        assert blocker not in self._unit(agent, True)

    def test_install_passes_the_flag_through_to_the_unit(self, agent, tmp_path, monkeypatch):
        """Gegenprobe bis zur Datei auf der Platte — nicht nur die reine
        Funktion, sondern der Weg, den --install wirklich geht."""
        system_token = tmp_path / "etc" / "token"
        system_token.parent.mkdir(parents=True)
        system_token.write_text("tok\n")
        unit_path = tmp_path / "mc-node-agent.service"
        monkeypatch.setattr(agent, "SYSTEM_TOKEN_PATH", system_token)
        monkeypatch.setattr(agent, "SYSTEMD_UNIT_PATH", unit_path)
        monkeypatch.setattr(agent, "_chown_to_user", lambda path, user: None)
        monkeypatch.setattr(agent.os, "geteuid", lambda: 0)
        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: None)

        agent.install_systemd_unit("https://mc.test", "gpuops", allow_control=True)
        assert "NoNewPrivileges=no" in unit_path.read_text()

        agent.install_systemd_unit("https://mc.test", "gpuops")
        assert "NoNewPrivileges=true" in unit_path.read_text()

    def test_main_hands_the_flag_to_the_installer(self, agent, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            agent, "install_systemd_unit",
            lambda mc_url, user, allow_control=False: seen.update(allow_control=allow_control),
        )
        monkeypatch.setattr(agent, "install_sudoers", lambda user: None)
        agent.main(["--mc-url", "http://x", "--install", "--allow-control"])
        assert seen["allow_control"] is True
        agent.main(["--mc-url", "http://x", "--install"])
        assert seen["allow_control"] is False
