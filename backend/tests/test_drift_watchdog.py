"""The drift watchdog must survive a container that lacks a watched file.

Live bug, found 2026-07-28: `scripts/auto-rebuild-agents-on-drift.sh` runs as a
Claude Code Stop hook. Mark kept seeing

    Stop hook error: Failed with non-blocking status code: No stderr output

The script never inspected a single container. It died on the very first one:

    container_md5() { docker exec "$1" md5sum "$2" 2>/dev/null | awk ...; }

Sparky runs the omp bridge and has no /home/agent/poll.sh, so `docker exec`
exits 1. With `set -o pipefail` the pipeline inherits that status, and

    cont_hash=$(container_md5 "$c" "$dst")

is a plain assignment — an assignment adopts the exit status of its command
substitution. `set -e` then killed the run, silently, because stderr was
already redirected to /dev/null.

The consequence was the opposite of the failure mode we assumed: the watchdog
did not die *when it found drift*, it died on the *normal* path — so it had
been decorative since the day it was written, while three containers really
had drifted.

These tests drive the script with a stub `docker` on PATH, so they need no
Docker daemon and touch no real container.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "auto-rebuild-agents-on-drift.sh"


def _write_docker_stub(bin_dir: Path, *, md5sum_fails: bool) -> None:
    """A fake `docker` covering exactly the calls pass 1 makes.

    md5sum_fails=True reproduces "the container has no such file".
    """
    behaviour = "exit 1" if md5sum_fails else "echo 'deadbeef  /file'"
    trace = bin_dir.parent / "docker-calls.log"
    stub = f"""#!/usr/bin/env bash
echo "$*" >> "{trace}"
case "$1" in
  info) exit 0 ;;
  ps)   echo "containerid123" ; exit 0 ;;
  exec)
    # $2 is the container name, rest is the command
    case "$*" in
      *md5sum*) {behaviour} ;;
      *)        exit 1 ;;
    esac
    ;;
  inspect) exit 0 ;;
  compose) exit 0 ;;
  *) exit 0 ;;
esac
"""
    p = bin_dir / "docker"
    p.write_text(stub, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fake_home(tmp_path: Path) -> Path:
    """Build the repo layout the script derives from $HOME.

    The script computes REPO="${HOME}/Workspace/Projects/mission-control" and
    bails out with `cd "$REPO" || exit 0` when it does not exist. On a
    developer Mac that path is real, so the tests passed; in CI it is not, so
    the script exited immediately having done nothing — and three of these
    tests still went green, because a script that does nothing also returns 0.

    Pointing HOME at a purpose-built tree makes every assertion here about the
    script's actual behaviour rather than about the machine it runs on.
    """
    repo = tmp_path / "home" / "Workspace" / "Projects" / "mission-control"
    for rel in (
        "docker/shared/poll.sh",
        "docker/mc-agent-base/recycler.sh",
        "docker/mc-agent-base/entrypoint.sh",
        "docker/mc-agent-base/start-claude.sh",
        "docker/mc-agent-base/lib/turn-state.sh",
        "docker/mc-claude-agent/recycler.sh",
        "docker/mc-claude-agent/entrypoint.sh",
        "scripts/build-agent-images.sh",
        "docker-compose.yml",
        "docker/docker-compose.agents.yml",
        ".env",
        "docker/.env.agents",
    ):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# fixture\n", encoding="utf-8")
    (repo / "scripts" / "build-agent-images.sh").chmod(0o755)
    return tmp_path / "home"


def _run(tmp_path: Path, *, md5sum_fails: bool) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_docker_stub(bin_dir, md5sum_fails=md5sum_fails)
    home = _fake_home(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(home)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    # Guard the guard: the stub records every call, so an empty trace proves
    # the script bailed before reaching the code under test and every
    # assertion about it would be vacuous. This is exactly how the CI failure
    # hid — three tests green against a script that never ran.
    trace = tmp_path / "docker-calls.log"
    assert trace.exists() and "md5sum" in trace.read_text(), (
        "the watchdog never got as far as hashing a container file — the test "
        "would be asserting nothing. Check the $HOME fixture layout."
    )
    return proc


@pytest.mark.skipif(not SCRIPT.exists(), reason="watchdog script not present")
def test_survives_container_without_the_watched_file(tmp_path):
    """THE regression: a missing file inside the container is a normal answer.

    Before the fix this exited non-zero with completely empty stderr — the
    exact signature the operator reported.
    """
    proc = _run(tmp_path, md5sum_fails=True)
    assert proc.returncode == 0, (
        f"watchdog died on a container missing the watched file "
        f"(rc={proc.returncode}, stderr={proc.stderr!r}). This is the silent "
        f"Stop-hook death: an assignment adopts its command substitution's "
        f"exit status, and set -e turns that fatal."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="watchdog script not present")
def test_silent_death_leaves_no_stderr_trace(tmp_path):
    """Pin the diagnosis, not just the symptom.

    The failure was hard to find precisely because it printed nothing. If a
    future edit reintroduces a fatal path, this asserts we at least learn about
    it through the exit code rather than through a mute hook.
    """
    proc = _run(tmp_path, md5sum_fails=True)
    assert not (proc.returncode != 0 and proc.stderr.strip() == ""), (
        "watchdog failed without saying why — the operator sees only "
        "'non-blocking status code: No stderr output'"
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="watchdog script not present")
def test_full_rebuild_path_reaches_its_summary(tmp_path):
    """Walk the whole script, not just detection.

    The stub returns a readable but different container hash, so every stage
    runs: detect → busy-filter → build → recreate → token verification →
    summary. This caught a SECOND instance of the same bug class, in the
    token-verification loop:

        tok=$(docker inspect ... | grep "^CLAUDE_CODE_OAUTH_TOKEN=" | cut ...)

    A container without that variable makes grep exit 1, and the assignment
    adopts it. The script died after rebuilding but before reporting — so even
    a working run looked like a hook error to the operator.
    """
    proc = _run(tmp_path, md5sum_fails=False)
    assert proc.returncode == 0, (
        f"watchdog failed while walking the rebuild path (rc={proc.returncode}, "
        f"stderr={proc.stderr!r})"
    )
    assert "systemMessage" in proc.stdout, (
        "watchdog finished without emitting its summary — the Stop hook would "
        f"stay mute. stdout={proc.stdout!r}"
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="watchdog script not present")
def test_hash_helpers_never_return_nonzero():
    """Guards the fix at its source.

    Both helpers must answer "I could not hash that" with an empty string and
    status 0. Any future rewrite that drops the `|| true` — or adds a new
    helper following the old pattern — fails here rather than three months
    later in production.
    """
    probe = """
set -euo pipefail
source_funcs() { sed -n '/^local_md5()/,/^}/p;/^container_md5()/,/^}/p' "$1"; }
eval "$(source_funcs "$1")"
out=$(local_md5 /definitely/not/a/real/file)
[ -z "$out" ] || { echo "local_md5 returned '$out'"; exit 3; }
echo OK
"""
    proc = subprocess.run(
        ["bash", "-c", probe, "_", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        f"hash helper turned an unreadable file into a fatal error "
        f"(rc={proc.returncode}, out={proc.stdout!r}, err={proc.stderr!r})"
    )
