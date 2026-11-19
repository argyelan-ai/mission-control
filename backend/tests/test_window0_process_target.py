"""Window 0's claude, not "the first claude in the container" (chats S3, review
blocker on #1044).

A further chat runs its own ``claude`` in the ``chats-<slug>`` tmux session
(``claude_chat_runner``). Two probes used to pick "a claude" with
``pgrep -x claude``:

* ``docker/mc-claude-agent/recycler.sh`` took ``pgrep -x claude | head -1``
  — the LOWEST pid. Window 0's claude is respawned by the recycler itself
  (new, higher pid), so a chat's claude soon becomes the lowest: its RSS
  then decides window 0's threshold recycle (killed every debounce period,
  even mid-task) and a busy chat blocks window 0's idle recycle.
* ``pane_state.process_alive`` (default chat's aliveness) read "alive" while
  any chat ran, even with window 0's claude dead.

Fix: the recycler resolves the process inside window 0's pane tree
(``window_cli_pid`` in docker/shared/recycler-lib.sh); ``process_alive``
ignores the process trees of the ``chats-<slug>`` session's panes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.services import pane_state
from app.services.pane_state import process_alive

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "docker" / "shared" / "recycler-lib.sh"
RECYCLER = ROOT / "docker" / "mc-claude-agent" / "recycler.sh"


# ── recycler: wiring ────────────────────────────────────────────────────────


def test_recycler_targets_window0_not_the_first_claude():
    script = RECYCLER.read_text()
    assert 'PID=$(window_cli_pid "${SESSION}:0" "$PROCESS_NAME")' in script, (
        "recycler.sh must measure the claude inside window 0's pane, the pane "
        "it respawns"
    )
    # Negative probe: no 'first claude anywhere' lookup left.
    assert "pgrep -x \"$PROCESS_NAME\"" not in script, (
        "recycler.sh still picks the first claude in the container — a "
        "chat's claude (lower pid) decides window 0's recycle"
    )


# ── recycler-lib: real tmux + real /proc (Linux) ────────────────────────────


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists() or shutil.which("tmux") is None,
    reason="needs Linux /proc and tmux (CI runner / agent image)",
)
def test_window_cli_pid_picks_window0_even_with_a_lower_pid_chat(tmp_path: Path):
    """The live shape: the chat's claude is started FIRST (lower pid), window
    0 runs claude as a grandchild under a restart loop (entrypoint.sh)."""
    cli = tmp_path / "fakecli"
    shutil.copy(shutil.which("sleep"), cli)
    sock = tmp_path / "tmux"
    sock.mkdir()
    env = {**os.environ, "TMUX_TMPDIR": str(sock), "TMPDIR": str(tmp_path)}
    env.pop("TMUX", None)

    def tmux(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True, timeout=10)

    try:
        # chat first → lower pid; tmux runs the command directly (exec form)
        assert tmux("new-session", "-d", "-s", "chats-rex", "-n", "chat-1", str(cli), "60").returncode == 0
        time.sleep(0.3)
        assert tmux(
            "new-session", "-d", "-s", "rex",
            f"d=2; while true; do sh -c '{cli} 60'; sleep 1; done",
        ).returncode == 0
        time.sleep(0.5)
        chat_pid = tmux("display-message", "-p", "-t", "chats-rex:chat-1", "#{pane_pid}").stdout.strip()

        res = subprocess.run(
            ["bash", "-c", f'. "{LIB}"\npgrep -x fakecli | head -1\necho ---\nwindow_cli_pid "rex:0" fakecli\n'],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        first, _, mine = res.stdout.partition("---")
        assert first.strip() == chat_pid, "precondition: the chat's claude is the lowest pid"
        mine = mine.strip()
        assert mine.isdigit() and mine != chat_pid, (mine, chat_pid)
        assert Path(f"/proc/{mine}/comm").read_text().strip() == "fakecli"

        # No window-0 process → empty, never the chat's.
        empty = subprocess.run(
            ["bash", "-c", f'. "{LIB}"\nwindow_cli_pid "rex:0" nosuchcli; window_cli_pid "nosession:0" fakecli\n'],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert empty.stdout.strip() == ""
    finally:
        tmux("kill-server")


# ── process_alive: chats-<slug> trees do not count ──────────────────────────


def _stub_bin(tmp_path: Path, *, panes: str | None, x: str, x_rc: int, children: dict[str, str]) -> dict:
    d = tmp_path / "bin"
    d.mkdir()
    (d / "tmux").write_text(
        "#!/bin/sh\n" + (f"printf '%s\\n' {panes}\n" if panes is not None else "exit 1\n")
    )
    kids = "".join(f"  {p}) printf '%s\\n' {c}; exit 0 ;;\n" for p, c in children.items())
    (d / "pgrep").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-x" ]; then\n'
        + (f"  printf '%s\\n' {x}\n" if x else "")
        + f"  exit {x_rc}\nfi\n"
        'case "$2" in\n' + kids + "esac\nexit 1\n"
    )
    for f in d.iterdir():
        f.chmod(0o755)
    return {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}


def _alive_rc(env: dict, slug: str = "rex", name: str = "claude") -> int:
    argv = pane_state._process_alive_argv(slug, name)
    assert argv[:5] == ["docker", "exec", "-u", "agent", f"mc-agent-{slug}"]
    return subprocess.run(argv[5:], env=env, capture_output=True, timeout=10).returncode


def test_process_alive_ignores_a_chats_claude(tmp_path: Path):
    """Window 0's claude is dead, a chat's claude (pid 10, its pane) runs:
    provably gone (rc 1), not alive."""
    env = _stub_bin(tmp_path, panes="10", x="10", x_rc=0, children={})
    assert _alive_rc(env) == 1


def test_process_alive_ignores_a_claude_below_a_chat_pane(tmp_path: Path):
    """The whole chat pane tree is excluded (pane 10 → child 11 → claude 12)."""
    env = _stub_bin(tmp_path, panes="10", x="12", x_rc=0, children={"10": "11", "11": "12"})
    assert _alive_rc(env) == 1


def test_process_alive_sees_window0_next_to_a_chat(tmp_path: Path):
    env = _stub_bin(tmp_path, panes="10", x="'10 12'", x_rc=0, children={})
    assert _alive_rc(env) == 0


def test_process_alive_unchanged_without_a_chats_session(tmp_path: Path):
    """No chats session (every agent today, every omp agent): plain pgrep."""
    env = _stub_bin(tmp_path, panes=None, x="12", x_rc=0, children={})
    assert _alive_rc(env) == 0
    (tmp_path / "b").mkdir()
    env2 = _stub_bin(tmp_path / "b", panes=None, x="", x_rc=1, children={})
    assert _alive_rc(env2) == 1


def test_process_alive_passes_pgrep_errors_through(tmp_path: Path):
    """pgrep's own failure (rc 2/3) stays 'unknown' (None), never 'gone'."""
    env = _stub_bin(tmp_path, panes="10", x="", x_rc=2, children={})
    assert _alive_rc(env) == 2


@pytest.mark.asyncio
async def test_process_alive_runs_the_scoped_probe(monkeypatch):
    monkeypatch.setattr(pane_state, "_process_alive_cache", {})
    seen: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    class _A:
        agent_runtime = "cli-bridge"
        slug = "rex"

    assert await process_alive(_A(), "claude") is False
    assert seen == [pane_state._process_alive_argv("rex", "claude")]
    assert seen[0][-2:] == ["chats-rex", "claude"]
