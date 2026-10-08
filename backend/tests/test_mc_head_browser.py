"""mc-head wires a head to its own browser session (ADR-088 harness wiring).

head.env may carry the session's two addresses. mc-head never trusts them
blindly (the backend container can write the run folder): only loopback
addresses of the expected shape are used.

- Claude Code (`-p --bare`): an `--mcp-config` file with the playwright-mcp
  router address of the session, `--strict-mcp-config`, and the browser
  tools allowed in the head's permission list.
- omp: Puppeteer drops a path prefix, so mc-head starts the CDP relay with
  `--prefix /s/<token>` on a free port for the run and points omp's profile
  (`browser.cdpUrl`) at it; the relay ends with the run.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest

from tests.heads_host_helpers import fake_harness, make_origin, mark_scratch, run_head, seed_clone, wait_phase, write_spec
from tests.test_mc_head_parity import _load_mc_head

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="host script is POSIX-only")

TOKEN = "head-session-token-" + "x" * 25
MCP_URL = f"http://127.0.0.1:8931/s/{TOKEN}/mcp"


@pytest.fixture
def mc_head():
    return _load_mc_head()


def test_only_loopback_session_addresses_are_used(mc_head):
    ok = {"MC_BROWSER_CDP_URL": f"http://127.0.0.1:9300/s/{TOKEN}/", "MC_BROWSER_MCP_URL": MCP_URL}
    assert mc_head.browser_addresses(ok) == ok
    for bad in (
        {"MC_BROWSER_MCP_URL": f"http://evil.example:8931/s/{TOKEN}/mcp"},
        {"MC_BROWSER_MCP_URL": f"http://127.0.0.1:8931/a/alpha/mcp"},
        {"MC_BROWSER_MCP_URL": "http://127.0.0.1:8931/s/short/mcp"},
        {"MC_BROWSER_CDP_URL": f"http://127.0.0.1:9300/s/{TOKEN}/../../x/"},
        {"MC_BROWSER_CDP_URL": f"https://127.0.0.1:9300/s/{TOKEN}/"},
    ):
        assert mc_head.browser_addresses(bad) == {}


def test_claude_gets_the_session_router_as_its_only_mcp_server(mc_head, tmp_path):
    run = tmp_path / "run"
    (run / "wt").mkdir(parents=True)
    (run / "job.md").write_text("# Job\n")
    spec = {"harness": "claude", "model": "glm", "run_id": "r", "time_limit_s": 600}
    argv = mc_head.harness_argv(spec, run, run / "home", {"MC_BROWSER_MCP_URL": MCP_URL})
    i = argv.index("--mcp-config")
    assert argv[i + 2] == "--strict-mcp-config"            # the variadic flag is closed right away
    config = json.loads(Path(argv[i + 1]).read_text())
    assert config == {"mcpServers": {"browser": {"type": "http", "url": MCP_URL}}}
    assert oct(Path(argv[i + 1]).stat().st_mode & 0o777) == "0o600"
    allow = json.loads((run / "head-settings.json").read_text())["permissions"]["allow"]
    assert "mcp__browser" in allow                            # -p refuses tools that are not allowed


def test_claude_without_a_session_gets_no_mcp_config(mc_head, tmp_path):
    run = tmp_path / "run"
    (run / "wt").mkdir(parents=True)
    (run / "job.md").write_text("# Job\n")
    spec = {"harness": "claude", "model": "glm", "run_id": "r", "time_limit_s": 600}
    argv = mc_head.harness_argv(spec, run, run / "home", {})
    assert "--mcp-config" not in argv
    allow = json.loads((run / "head-settings.json").read_text())["permissions"]["allow"]
    assert "mcp__browser" not in allow


class _Upstream:
    """Fake gateway: records the first request line of every connection."""

    def __init__(self):
        self.lines: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = conn.recv(4096)
            if data:  # mc-head's readiness probe opens a connection without a request
                self.lines.append(data.split(b"\r\n", 1)[0].decode())
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            conn.close()


def test_omp_head_reaches_its_session_through_the_relay(tmp_path):
    """End to end through `mc-head start`: the fake omp reads its profile's
    browser.cdpUrl and sends a request there while it runs; the fake
    gateway sees it under /s/<token>. Sabotage: no relay / no prefix -> the
    request is missing or arrives without the session path."""
    up = _Upstream()
    mc_home = tmp_path / "mc"
    mc_home.mkdir()
    origin, full_name = make_origin(tmp_path)
    seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)
    probe = (
        "import re, urllib.request, pathlib, os\n"
        "cfg = pathlib.Path(os.environ['HOME'], '.omp/profiles/mc-head/agent/config.yml').read_text()\n"
        "url = re.search(r'cdpUrl: (\\S+)', cfg).group(1)\n"
        "print('cdp', url)\n"
        "print(urllib.request.urlopen(url + '/json/version', timeout=5).read().decode())\n"
    )
    (tmp_path / "probe.py").write_text(probe)
    harness = fake_harness(tmp_path, f"{sys.executable} {tmp_path / 'probe.py'} > ../probe.txt 2>&1")
    run_id = write_spec(mc_home)
    run = mc_home / "heads" / run_id
    (run / "head.env").write_text(f"MC_BROWSER_CDP_URL='http://127.0.0.1:{up.port}/s/{TOKEN}/'\n")
    res = run_head(mc_home, "start", run_id, env_extra={"MC_HEAD_BIN_OMP": str(harness), "MC_HEAD_BIN_CLAUDE": str(harness)})
    assert res.returncode == 0, res.stdout + res.stderr
    wait_phase(mc_home, run_id, "exited")
    seen = (run / "probe.txt").read_text()
    assert "cdp http://127.0.0.1:" in seen and TOKEN not in seen   # omp only sees the local relay
    assert up.lines and up.lines[0] == f"GET /s/{TOKEN}/json/version HTTP/1.1"
    port = int(seen.split("cdp http://127.0.0.1:", 1)[1].split()[0])
    with pytest.raises(OSError):                                   # the relay ended with the run
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    assert TOKEN not in (run / "head.log").read_text()


def test_the_relay_gets_its_token_by_environment_not_on_the_command_line(mc_head, tmp_path, monkeypatch):
    """Another host process can read a command line (ps); the token goes to
    the relay through its environment."""
    up = _Upstream()
    run_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(mc_head, "wrapper_dir", lambda _rid: tmp_path)
    env = {"MC_BROWSER_CDP_URL": f"http://127.0.0.1:{up.port}/s/{TOKEN}/"}
    proc = mc_head.start_browser_relay(run_id, tmp_path / "home", env)
    try:
        assert proc is not None
        assert TOKEN not in " ".join(map(str, proc.args))
        port = int((tmp_path / "home/.omp/profiles/mc-head/agent/config.yml").read_text().rsplit(":", 1)[1])
        import urllib.request

        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5).read()
        assert up.lines[-1] == f"GET /s/{TOKEN}/json/version HTTP/1.1"
    finally:
        mc_head.stop_browser_relay(proc)
