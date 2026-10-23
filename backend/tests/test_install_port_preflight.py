"""install.sh names a busy port before it starts the stack.

Found by the first-run journey (sabotage "port busy", step S0): with another
program on port 8000, the installer ran ``docker compose up`` and failed with
Docker's raw "address already in use" — on the prebuilt-image path even with
"the stack did not come up in time — on slow machines …", which points the
user the wrong way. Now ``boot_stack`` checks every host port the stack
publishes (from ``docker compose config``, so profiles and an override file
count) and stops with one line per busy port before anything is pulled or
built. A stack that already runs here (re-run, ``--update``) holds its own
ports and is not checked.

The tests source install.sh with ``MC_INSTALL_LIB_ONLY=1`` and a fake
``docker`` in PATH; the busy port is a real listening socket.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# `docker compose config` prints published ports as `published: "8000"`;
# `docker compose ps -q` prints the ids of this project's running containers.
FAKE_DOCKER = r"""#!/usr/bin/env bash
[ "$1" = compose ] || exit 0
shift
case "$1" in
  config)
    for p in $STUB_PORTS; do
      printf '    ports:\n      - mode: ingress\n        host_ip: 127.0.0.1\n        target: 80\n        published: "%s"\n        protocol: tcp\n' "$p"
    done ;;
  ps) [ -n "${STUB_OWN_RUNNING:-}" ] && echo 0123456789ab ;;
esac
exit 0
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def listener():
    """A real program listening on a port of its own."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture
def run(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)

    def _run(ports: list[int], **env: str):
        full_env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "MC_INSTALL_LIB_ONLY": "1",
            "STUB_PORTS": " ".join(str(p) for p in ports),
            **env,
        }
        script = f'set -euo pipefail; . "{INSTALL_SH}"; mc_port_preflight; echo PASSED'
        r = subprocess.run(
            ["bash", "-c", script], cwd=tmp_path, env=full_env,
            capture_output=True, text=True, timeout=60,
        )
        r.out = r.stdout + r.stderr  # type: ignore[attr-defined]
        return r

    return _run


def test_busy_port_stops_with_its_number(run, listener):
    free = _free_port()
    r = run([free, listener])
    assert r.returncode != 0
    assert "PASSED" not in r.out
    assert f"Port {listener} is already in use" in r.out
    assert f"Port {free} " not in r.out, "only the busy port is named"
    # the line says how to find the program and that a re-run is safe
    assert "re-run the installer" in r.out


def test_all_free_says_so(run):
    a, b = _free_port(), _free_port()
    r = run([a, b])
    assert r.returncode == 0, r.out
    assert "PASSED" in r.out
    assert "ports free" in r.out
    assert str(min(a, b)) in r.out


def test_own_running_stack_holds_its_ports(run, listener):
    """Re-run or --update: this stack already runs and holds the port."""
    r = run([listener], STUB_OWN_RUNNING="1")
    assert r.returncode == 0, r.out
    assert "already in use" not in r.out


def test_no_published_ports_checks_nothing(run):
    r = run([])
    assert r.returncode == 0, r.out
    assert "already in use" not in r.out


def test_boot_stack_runs_the_preflight_before_pull_or_build():
    """The check sits in boot_stack, after the single-stack guard and before
    anything is pulled or built (a busy port must not cost a 5-minute build)."""
    text = INSTALL_SH.read_text()
    body = text[text.index("boot_stack() {"):]
    body = body[: body.index("\n}\n")]
    assert body.index("mc_guard_single_stack") < body.index("mc_port_preflight")
    assert body.index("mc_port_preflight") < body.index("docker compose pull")
    assert body.index("mc_port_preflight") < body.index("build_locally")
