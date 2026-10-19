"""``install.sh`` sets up the cli-bridge — the host helper that is also the
update door (ADR-094 decision 6, update plan PR 3).

* Only after a yes: the bridge is a service in the user's account.
* Linux: the backend container reaches the host through Docker, not
  loopback. The bridge listens on the gateway of MC's own Docker network
  (``mission-control_default``) — reachable from the containers, not from
  the LAN — and backend + worker are pointed at exactly that address.
* Linux: without *linger* the user's services stop at logout and do not
  start after a reboot, so an interrupted update could never finish. The
  installer offers ``sudo loginctl enable-linger`` and runs it **only after
  an explicit yes typed at the terminal**; otherwise it prints the command.
* macOS: Docker Desktop delivers ``host.docker.internal`` to loopback — the
  bridge stays on 127.0.0.1, nothing else changes.

The tests source install.sh with ``MC_INSTALL_LIB_ONLY=1`` and call
``mc_bridge_setup`` in a scratch install with stubs for ``uname``,
``docker``, ``sudo`` and the service script; answers come from a file
(``MC_TTY``). Nothing is installed, no sudo runs.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUBS = {
    "uname": '#!/bin/bash\necho "${STUB_OS:-Linux}"\n',
    "docker": r"""#!/bin/bash
echo "docker $*" >> "$STATE/docker"
case "$1 $2" in
  "network inspect")
    [ "$3" = "mission-control_default" ] || exit 1
    [ -n "${STUB_GW:-}" ] || { echo "Error: No such network: $3" >&2; exit 1; }
    printf '%s \n' "$STUB_GW" ;;
esac
exit 0
""",
    "sudo": r"""#!/bin/bash
echo "sudo $*" >> "$STATE/sudo"
if [ "$1 $2" = "loginctl enable-linger" ] && [ "${STUB_SUDO_RC:-0}" = 0 ]; then
  mkdir -p "$MC_LINGER_DIR" && touch "$MC_LINGER_DIR/$3"
fi
exit "${STUB_SUDO_RC:-0}"
""",
}

SERVICE_STUB = r"""#!/bin/bash
echo "bind=${CLI_BRIDGE_BIND:-<unset>}" >> "$STATE/service"
exit "${STUB_SERVICE_RC:-0}"
"""


@pytest.fixture
def box(tmp_path: Path):
    inst = tmp_path / "mission-control"
    (inst / "scripts").mkdir(parents=True)
    (inst / "docker-compose.yml").write_text("services: {}\n")
    (inst / ".env").write_text("DB_PASSWORD=keep\n")
    svc = inst / "scripts" / "cli-bridge-service.sh"
    svc.write_text(SERVICE_STUB)
    svc.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {
        **os.environ,
        "MC_INSTALL_LIB_ONLY": "1",
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STATE": str(state),
        "MC_LINGER_DIR": str(tmp_path / "linger"),
        "MC_TTY": str(tmp_path / "no-terminal"),
        "STUB_GW": "172.18.0.1",
    }
    for k in ("MC_BRIDGE", "MC_BRIDGE_LINGER", "CLI_BRIDGE_BIND", "FREE_CODE_BRIDGE_URL"):
        env.pop(k, None)
    return inst, state, env, tmp_path


def _answer(box, text: str) -> dict:
    _, _, env, tmp = box
    f = tmp / "tty"
    f.write_text(text)
    return {**env, "MC_TTY": str(f)}


def _setup(box, mode: str, env: dict | None = None):
    inst, _, base, _ = box
    script = f'set -euo pipefail; . "{INSTALL_SH}"; mc_bridge_setup {mode}'
    return subprocess.run(["bash", "-c", script], cwd=inst, env=env or base, capture_output=True,
                          text=True, timeout=60)


def _lines(state: Path, name: str) -> list[str]:
    f = state / name
    return f.read_text().splitlines() if f.exists() else []


def _env_value(inst: Path, key: str) -> str:
    for line in (inst / ".env").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return ""


# ── Linux ───────────────────────────────────────────────────────────────────

def test_linux_binds_to_mcs_network_gateway_and_points_the_backend_there(box):
    inst, state, _, _ = box
    r = _setup(box, "yes", _answer(box, "n\n"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "service") == ["bind=172.18.0.1"]
    assert _env_value(inst, "FREE_CODE_BRIDGE_URL") == "http://172.18.0.1:18792"
    assert _env_value(inst, "DB_PASSWORD") == "keep"
    assert "docker compose up -d backend mc-worker" in _lines(state, "docker")


def test_linger_needs_an_explicit_yes(box):
    _, state, _, tmp = box
    for answer in ("\n", "n\n", "ja bitte\n"):
        r = _setup(box, "yes", _answer(box, answer))
        assert r.returncode == 0, r.stdout + r.stderr
        assert _lines(state, "sudo") == []
        assert f"sudo loginctl enable-linger {getpass.getuser()}" in r.stdout + r.stderr
    r = _setup(box, "yes", _answer(box, "y\n"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "sudo") == [f"sudo loginctl enable-linger {getpass.getuser()}"]
    assert (tmp / "linger" / getpass.getuser()).exists()
    assert "linger is on" in r.stdout


def test_without_a_terminal_sudo_never_runs(box):
    _, state, env, _ = box
    r = _setup(box, "batch", env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "sudo") == []
    assert _lines(state, "service") == ["bind=172.18.0.1"]
    assert f"sudo loginctl enable-linger {getpass.getuser()}" in r.stdout + r.stderr


def test_batch_mode_never_asks_even_with_a_terminal(box):
    _, state, _, _ = box
    r = _setup(box, "batch", _answer(box, "y\n"))
    assert r.returncode == 0
    assert _lines(state, "sudo") == []


def test_unattended_linger_is_an_explicit_setting(box):
    _, state, env, _ = box
    r = _setup(box, "batch", {**env, "MC_BRIDGE_LINGER": "yes"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "sudo") == [f"sudo loginctl enable-linger {getpass.getuser()}"]


def test_linger_already_on_is_not_asked_again(box):
    _, state, _, tmp = box
    (tmp / "linger").mkdir()
    (tmp / "linger" / getpass.getuser()).touch()
    r = _setup(box, "yes", _answer(box, "y\n"))
    assert r.returncode == 0
    assert _lines(state, "sudo") == []
    assert "enable-linger" not in r.stdout + r.stderr


def test_failed_sudo_is_reported_not_hidden(box):
    _, _, _, _ = box
    env = {**_answer(box, "y\n"), "STUB_SUDO_RC": "1"}
    r = _setup(box, "yes", env)
    assert r.returncode == 0          # the bridge itself is set up
    assert "linger is still off" in r.stderr


def test_unchanged_address_does_not_recreate_the_backend(box):
    inst, state, env, _ = box
    with open(inst / ".env", "a") as f:
        f.write("FREE_CODE_BRIDGE_URL=http://172.18.0.1:18792\n")
    r = _setup(box, "batch", env)
    assert r.returncode == 0
    assert not any("compose up" in c for c in _lines(state, "docker"))


@pytest.mark.parametrize("gw", ["", "<no value>", "fe80::1", "172.18.0.1; rm -rf /"])
def test_without_a_usable_gateway_nothing_is_installed(box, gw):
    inst, state, env, _ = box
    r = _setup(box, "batch", {**env, "STUB_GW": gw})
    assert r.returncode != 0
    assert _lines(state, "service") == []
    assert _env_value(inst, "FREE_CODE_BRIDGE_URL") == ""
    assert "./install.sh --bridge" in r.stderr


def test_failing_service_install_is_reported(box):
    inst, _, env, _ = box
    r = _setup(box, "batch", {**env, "STUB_SERVICE_RC": "1"})
    assert r.returncode != 0
    assert _env_value(inst, "FREE_CODE_BRIDGE_URL") == ""


# ── question, macOS ─────────────────────────────────────────────────────────

def test_the_bridge_itself_needs_a_yes(box):
    inst, state, _, _ = box
    r = _setup(box, "ask", _answer(box, "\n"))
    assert r.returncode == 0
    assert _lines(state, "service") == []
    assert "./install.sh --bridge" in r.stdout
    r = _setup(box, "ask", _answer(box, "y\n"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "service") == ["bind=172.18.0.1"]


def test_macos_stays_on_loopback_and_changes_nothing_else(box):
    inst, state, env, _ = box
    r = _setup(box, "yes", {**_answer(box, "y\n"), "STUB_OS": "Darwin"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "service") == ["bind=127.0.0.1"]
    assert _env_value(inst, "FREE_CODE_BRIDGE_URL") == ""
    assert _lines(state, "docker") == []
    assert _lines(state, "sudo") == []


# ── wiring ──────────────────────────────────────────────────────────────────

def test_compose_hands_the_bridge_address_to_backend_and_worker():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for svc in ("backend", "mc-worker"):
        env = compose["services"][svc]["environment"]
        assert env["FREE_CODE_BRIDGE_URL"] == "${FREE_CODE_BRIDGE_URL:-http://host.docker.internal:18792}", svc


def test_installer_offers_the_bridge_on_install_update_and_by_flag():
    """Wiring only — what each mode does is tested on the function below."""
    text = INSTALL_SH.read_text()
    assert "--bridge)" in text
    main = text.split('[ "${MC_INSTALL_LIB_ONLY:-0}" = 1 ]', 1)[1]
    update = main.split('if [ "$UPDATE" = 1 ]', 1)[1].split("\n  exit 0\n", 1)[0]
    # --update: may ask at a terminal, --non-interactive keeps it quiet
    assert 'mc_bridge_offer "$([ "$NONINTERACTIVE_FLAG" = 1 ] && echo quiet || echo update)"' in update
    # fresh install: asked when interactive
    assert 'mc_bridge_offer "$([ "$INTERACTIVE" = 1 ] && echo ask || echo quiet)"' in main


# ── mc_bridge_offer: after an install or an update ─────────────────────────
#
# An installed bridge on Linux whose address no longer matches MC's network
# gateway (every Linux install from before this change: FREE_CODE_BRIDGE_URL
# was never set) must NOT be reinstalled behind the operator's back — a
# reinstall restarts the bridge service and recreates backend + mc-worker.
# Only an explicit yes at the terminal or MC_BRIDGE=yes moves it.

def _offer(box, mode: str, env: dict | None = None, installed: bool = True):
    inst, _, base, tmp = box
    home = tmp / "home"
    unit = home / ".config" / "systemd" / "user" / "mc-cli-bridge.service"
    if installed:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Service]\n")
    run_env = {**(env or base), "HOME": str(home)}
    script = f'set -euo pipefail; . "{INSTALL_SH}"; mc_bridge_offer {mode}'
    return subprocess.run(["bash", "-c", script], cwd=inst, env=run_env, capture_output=True,
                          text=True, timeout=60)


def _untouched(box, r):
    inst, state, _, _ = box
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "service") == []
    assert not any("compose up" in c for c in _lines(state, "docker"))
    assert _env_value(inst, "FREE_CODE_BRIDGE_URL") == ""
    assert "./install.sh --bridge" in r.stdout + r.stderr


def _moved(box, r):
    inst, state, _, _ = box
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "service") == ["bind=172.18.0.1"]
    assert _env_value(inst, "FREE_CODE_BRIDGE_URL") == "http://172.18.0.1:18792"


@pytest.mark.parametrize("mode", ["update", "quiet", "ask"])
def test_moved_gateway_without_a_terminal_only_hints(box, mode):
    """The old --update path reinstalled the bridge here, unattended."""
    r = _offer(box, mode)
    _untouched(box, r)
    assert "172.18.0.1" in r.stdout


@pytest.mark.parametrize("answer", ["\n", "n\n", "ja\n"])
def test_moved_gateway_on_update_needs_a_yes_at_the_terminal(box, answer):
    _untouched(box, _offer(box, "update", _answer(box, answer)))


def test_moved_gateway_on_update_moves_after_a_yes(box):
    r = _offer(box, "update", _answer(box, "y\nn\n"))
    _moved(box, r)
    assert "restarts the cli-bridge" in r.stdout


def test_moved_gateway_quiet_never_asks_even_with_a_terminal(box):
    _untouched(box, _offer(box, "quiet", _answer(box, "y\n")))


def test_moved_gateway_with_mc_bridge_no_is_left_alone_even_after_a_yes(box):
    _, _, env, _ = box
    for mode in ("update", "ask", "quiet"):
        r = _offer(box, mode, {**_answer(box, "y\n"), "MC_BRIDGE": "no"})
        _untouched(box, r)


def test_moved_gateway_with_mc_bridge_yes_moves_unattended(box):
    _, state, env, _ = box
    r = _offer(box, "quiet", {**env, "MC_BRIDGE": "yes"})
    _moved(box, r)
    assert _lines(state, "sudo") == []


def test_installed_bridge_at_the_right_address_is_left_alone(box):
    inst, state, env, _ = box
    with open(inst / ".env", "a") as f:
        f.write("FREE_CODE_BRIDGE_URL=http://172.18.0.1:18792\n")
    for mode in ("update", "ask", "quiet"):
        r = _offer(box, mode, {**_answer(box, "y\n"), "MC_BRIDGE": "yes"})
        assert r.returncode == 0, r.stdout + r.stderr
        assert _lines(state, "service") == []
        assert _lines(state, "docker") == [] or not any("compose up" in c for c in _lines(state, "docker"))


def test_installed_bridge_on_macos_is_left_alone(box):
    _, state, env, _ = box
    r = _offer(box, "update", {**_answer(box, "y\n"), "MC_BRIDGE": "yes", "STUB_OS": "Darwin"})
    assert r.returncode == 0
    assert _lines(state, "service") == []
    assert _lines(state, "docker") == []


def test_installed_bridge_without_mcs_network_is_left_alone(box):
    _, state, env, _ = box
    r = _offer(box, "update", {**_answer(box, "y\n"), "MC_BRIDGE": "yes", "STUB_GW": ""})
    assert r.returncode == 0
    assert _lines(state, "service") == []


def test_update_never_sets_up_a_new_bridge_unasked(box):
    """Not installed: --update hints even at a terminal; MC_BRIDGE=yes is the opt-in."""
    _, state, env, _ = box
    for mode in ("update", "quiet"):
        _untouched(box, _offer(box, mode, _answer(box, "y\n"), installed=False))
    _moved(box, _offer(box, "update", {**env, "MC_BRIDGE": "yes"}, installed=False))


def test_fresh_install_asks_and_honours_mc_bridge(box):
    _, state, env, _ = box
    _untouched(box, _offer(box, "ask", _answer(box, "\n"), installed=False))
    _untouched(box, _offer(box, "ask", {**_answer(box, "y\n"), "MC_BRIDGE": "no"}, installed=False))
    _moved(box, _offer(box, "ask", _answer(box, "y\nn\n"), installed=False))


def test_bridge_flag_sets_it_up_in_an_existing_install(box):
    """`./install.sh --bridge` end to end: prerequisites, then only the bridge."""
    inst, state, env, _ = box
    shutil.copy(INSTALL_SH, inst / "install.sh")
    (inst / "scripts" / "lib").mkdir()
    shutil.copy(ROOT / "scripts" / "lib" / "remote-access.sh", inst / "scripts" / "lib" / "remote-access.sh")
    run_env = {k: v for k, v in env.items() if k != "MC_INSTALL_LIB_ONLY"}
    r = subprocess.run(["bash", "install.sh", "--bridge", "--non-interactive"], cwd=inst, env=run_env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(state, "service") == ["bind=172.18.0.1"]
    assert _lines(state, "sudo") == []
    # nothing else of the installer ran: no clone, no setup.sh, no stack boot
    assert not any(c.startswith(("docker compose pull", "docker compose up -d\n")) or c == "docker compose up -d"
                   for c in _lines(state, "docker"))


def test_bridge_flag_outside_an_install_changes_nothing(box, tmp_path):
    _, state, env, _ = box
    empty = tmp_path / "empty"
    empty.mkdir()
    run_env = {k: v for k, v in env.items() if k != "MC_INSTALL_LIB_ONLY"}
    r = subprocess.run(["bash", str(INSTALL_SH), "--bridge", "--non-interactive"], cwd=empty, env=run_env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "installed Mission Control directory" in r.stderr
    assert _lines(state, "service") == []
