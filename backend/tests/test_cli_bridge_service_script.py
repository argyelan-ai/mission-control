"""scripts/cli-bridge-service.sh against stubbed launchctl/lsof/curl/uname.

The install must retire the legacy launchd label first (else the old process
keeps :18792 open on all interfaces without a token and the new service
crash-loops), be idempotent, refuse to start while a foreign process holds the
port, and check the address it was told to bind.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cli-bridge-service.sh"
LEGACY = "com.example.old-bridge"  # any hand-made label that runs cli-bridge.py

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# State lives in $STATE: loaded-<label> marks a loaded launchd job,
# foreign-listener simulates a process we do not own on the port.
STUBS = {
    "uname": "#!/bin/bash\necho Darwin\n",
    "launchctl": r"""#!/bin/bash
echo "launchctl $*" >> "$STATE/calls"
label="${2##*/}"
case "$1" in
  print) [[ -f "$STATE/loaded-$label" ]] ;;
  bootout) rm -f "$STATE/loaded-$label" ;;
  bootstrap) touch "$STATE/loaded-com.mc.cli-bridge" ;;
  *) exit 0 ;;
esac
""",
    "lsof": r"""#!/bin/bash
echo "lsof $*" >> "$STATE/calls"
echo "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME"
[[ -f "$STATE/foreign-listener" ]] && echo "nc 4242 u 3u IPv4 0 0t0 TCP *:18792 (LISTEN)"
[[ -f "$STATE/loaded-com.example.old-bridge" ]] && echo "Python 747 u 3u IPv4 0 0t0 TCP *:18792 (LISTEN)"
[[ -f "$STATE/loaded-com.mc.cli-bridge" ]] && echo "Python 900 u 3u IPv4 0 0t0 TCP ${STUB_BIND:-127.0.0.1}:18792 (LISTEN)"
exit 0
""",
    "curl": "#!/bin/bash\necho \"curl $*\" >> \"$STATE/calls\"\nexit 0\n",
    "sleep": "#!/bin/bash\nexit 0\n",
}


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    e = {
        "HOME": str(home),
        "STATE": str(state),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CLI_BRIDGE_PYTHON": sys.executable,
        "CLI_BRIDGE_PORT_WAIT": "1",
    }
    for k in ("CLI_BRIDGE_BIND", "CLI_BRIDGE_TOKEN", "CLI_BRIDGE_TOKEN_FILE", "CLI_BRIDGE_PORT"):
        e.pop(k, None)
    return e, home, state


def _run(e, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True,
                          text=True, timeout=60)


def _calls(state: Path) -> list[str]:
    f = state / "calls"
    return f.read_text().splitlines() if f.exists() else []


def test_install_retires_the_legacy_label_before_starting(env):
    e, home, state = env
    legacy_plist = home / "Library" / "LaunchAgents" / f"{LEGACY}.plist"
    legacy_plist.write_text(
        "<plist><string>/usr/bin/python3</string>"
        "<string>/somewhere/mission-control/scripts/cli-bridge.py</string></plist>"
    )
    other = home / "Library" / "LaunchAgents" / "com.example.unrelated.plist"
    other.write_text("<plist><string>/usr/local/bin/something-else</string></plist>")
    (state / f"loaded-{LEGACY}").touch()

    r = _run(e)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _calls(state)
    bootout_legacy = next(i for i, c in enumerate(calls) if c.endswith(f"bootout gui/{os.getuid()}/{LEGACY}"))
    bootstrap_new = next(i for i, c in enumerate(calls) if c.startswith("launchctl bootstrap"))
    assert bootout_legacy < bootstrap_new
    assert not legacy_plist.exists()
    backups = list((home / ".mc" / "backups" / "launchd").glob(f"{LEGACY}.plist.bak-*"))
    assert len(backups) == 1
    assert f"Stopped legacy launchd service {LEGACY}" in r.stdout
    assert "Listening on: 127.0.0.1:18792" in r.stdout
    assert other.exists()  # only plists that run cli-bridge.py are touched
    assert not any("com.example.unrelated" in c for c in calls)
    assert (home / ".mc" / "secrets" / "cli-bridge.token").exists()


def test_extra_legacy_label_from_env(env):
    e, _, state = env
    (state / "loaded-com.example.other").touch()
    r = _run({**e, "CLI_BRIDGE_LEGACY_LABELS": "com.example.other"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Stopped legacy launchd service com.example.other" in r.stdout


def test_install_is_idempotent(env):
    e, home, state = env
    assert _run(e).returncode == 0
    r = _run(e)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "legacy" not in r.stdout.lower()
    assert not (home / ".mc" / "backups").exists()


def test_install_refuses_while_a_foreign_process_holds_the_port(env):
    e, _, state = env
    (state / "foreign-listener").touch()
    r = _run(e)
    assert r.returncode == 1
    assert "still in use" in r.stderr and "nc 4242" in r.stderr
    assert not any(c.startswith("launchctl bootstrap") for c in _calls(state))


def test_status_probes_the_configured_bind(env):
    e, _, state = env
    e = {**e, "CLI_BRIDGE_BIND": "10.0.0.5", "STUB_BIND": "10.0.0.5"}
    r = _run(e, "--status")
    assert "10.0.0.5:18792" in r.stdout
    assert any("http://10.0.0.5:18792/ping" in c for c in _calls(state))


def test_status_reads_the_bind_from_the_installed_unit(env):
    """`make bridge-status` has no env: it must check the address the
    service was installed with, not hard-coded loopback."""
    if not Path("/usr/libexec/PlistBuddy").exists():
        pytest.skip("PlistBuddy is macOS-only")
    e, _, state = env
    assert _run({**e, "CLI_BRIDGE_BIND": "10.0.0.5", "STUB_BIND": "10.0.0.5"}).returncode == 0
    (state / "calls").unlink()
    _run(e, "--status")
    assert any("http://10.0.0.5:18792/ping" in c for c in _calls(state))


def test_plist_carries_bind_and_token_file(env):
    e, home, _ = env
    assert _run(e).returncode == 0
    plist = (home / "Library" / "LaunchAgents" / "com.mc.cli-bridge.plist").read_text()
    assert "<key>CLI_BRIDGE_BIND</key>\n        <string>127.0.0.1</string>" in plist
    assert "<key>CLI_BRIDGE_TOKEN_FILE</key>" in plist


# ── update door (ADR-094): the engine must outlive a bridge restart ─────────


def test_plist_lets_a_detached_update_outlive_the_bridge(env):
    """launchd kills the job's whole process group when the bridge stops,
    unless AbandonProcessGroup is set — the belt next to the double fork."""
    e, home, _ = env
    assert _run(e).returncode == 0
    plist = (home / "Library" / "LaunchAgents" / "com.mc.cli-bridge.plist").read_text()
    assert "<key>AbandonProcessGroup</key>\n    <true/>" in plist
    assert (home / ".mc" / "secrets" / "mc-update.token").exists()


LINUX_STUBS = {
    "uname": "#!/bin/bash\necho Linux\n",
    # Logs every call; for `stop` also the KillMode systemd would apply — the
    # one in the unit file as of the last daemon-reload (default control-group,
    # which kills the tmux server the bridge started for host agents).
    "systemctl": r"""#!/bin/bash
unit="$HOME/.config/systemd/user/mc-cli-bridge.service"
case "$*" in
  "--user daemon-reload")
    km="$(sed -n 's/^KillMode=//p' "$unit" 2>/dev/null | head -1)"
    echo "${km:-control-group}" > "$STATE/loaded-killmode" ;;
esac
if [ "$2" = stop ]; then
  echo "systemctl $* killmode=$(cat "$STATE/loaded-killmode" 2>/dev/null || echo none)" >> "$STATE/calls"
else
  echo "systemctl $*" >> "$STATE/calls"
fi
exit 0
""",
    "lsof": "#!/bin/bash\nexit 0\n",   # nothing listens (no real lsof answers here)
}


@pytest.fixture
def linux_env(env, tmp_path):
    e, home, state = env
    bindir = Path(e["PATH"].split(":", 1)[0])
    for name, body in LINUX_STUBS.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    return e, home, state


def test_systemd_unit_keeps_children_on_restart(linux_env):
    """KillMode=process: a restart of the bridge unit stops the bridge, not a
    run it started (the run normally lives in its own systemd-run unit)."""
    e, home, _ = linux_env
    r = _run({**e, "CLI_BRIDGE_BIND": "172.18.0.1"})
    assert r.returncode == 0, r.stdout + r.stderr
    unit = (home / ".config" / "systemd" / "user" / "mc-cli-bridge.service").read_text()
    assert "KillMode=process" in unit
    assert "Environment=CLI_BRIDGE_BIND=172.18.0.1" in unit
    assert "Environment=MC_UPDATE_TOKEN_FILE=" in unit


def test_reinstall_stops_the_old_bridge_under_the_new_kill_mode(linux_env):
    """An install from before the update door has a unit without KillMode
    (default control-group): stopping it under the OLD unit kills every child
    of the bridge — the tmux server of the host agents. The new unit must be
    loaded (written + daemon-reload) before the stop."""
    e, home, state = linux_env
    unit = home / ".config" / "systemd" / "user" / "mc-cli-bridge.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/usr/bin/python3 cli-bridge.py\n")
    (state / "loaded-killmode").write_text("control-group\n")   # the old unit is loaded
    r = _run({**e, "CLI_BRIDGE_BIND": "172.18.0.1"})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _calls(state)
    stops = [c for c in calls if " stop " in f"{c} "]
    assert stops == ["systemctl --user stop mc-cli-bridge.service killmode=process"], calls
    assert calls.index("systemctl --user daemon-reload") < calls.index(stops[0])
    assert calls.index(stops[0]) < calls.index("systemctl --user enable --now mc-cli-bridge.service")
