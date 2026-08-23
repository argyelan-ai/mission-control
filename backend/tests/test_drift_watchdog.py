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
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "auto-rebuild-agents-on-drift.sh"


def _write_docker_stub(bin_dir: Path, *, md5sum_fails: bool) -> None:
    """A fake `docker` covering exactly the calls the script makes.

    md5sum_fails=True reproduces "the container has no such file".

    Two different `ps` calls have to be served, because the watchdog no longer
    carries a list of agents: it DISCOVERS them from the running containers
    (a hardcoded fleet only ever described its author's machine).
      * `ps --format '{{.Names}}\t{{.Image}}' --filter name=^/mc-agent-`
        → one "name<TAB>image" line per agent container; the image decides
          which scripts that container carries.
      * `ps -q --filter name=^/<container>$` → still running? any output = yes.
    """
    behaviour = "exit 1" if md5sum_fails else "echo 'deadbeef  /file'"
    trace = bin_dir.parent / "docker-calls.log"
    stub = f"""#!/usr/bin/env bash
echo "$*" >> "{trace}"
case "$1" in
  info) exit 0 ;;
  ps)
    case "$*" in
      *--format*)
        printf 'mc-agent-alpha\tmc-agent-base:latest\n'
        printf 'mc-agent-beta\tghcr.io/example/mc-claude-agent:latest\n'
        ;;
      *) echo "containerid123" ;;
    esac
    exit 0
    ;;
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


def _fake_repo(tmp_path: Path) -> Path:
    """Build the checkout layout the script works on, and hand it over via
    MC_REPO_PATH.

    The script used to compute REPO="${HOME}/Workspace/Projects/mission-control"
    and bail out with `cd "$REPO" || exit 0` when it did not exist. On its
    author's Mac that path is real, so the tests passed; anywhere else the
    script exited immediately having done nothing — and three of these tests
    still went green, because a script that does nothing also returns 0. The
    path is configurable now (MC_REPO_PATH, else the script's own location),
    which is what this fixture uses; the guard below stays either way.
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
    return repo


def _run(tmp_path: Path, *, md5sum_fails: bool) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_docker_stub(bin_dir, md5sum_fails=md5sum_fails)
    repo = _fake_repo(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(repo.parents[2])
    env["MC_REPO_PATH"] = str(repo)
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


# ── Keine Flotte im Skript: die Agenten kommen zur Laufzeit ─────────────────
#
# Vorher trug `ALL_AGENTS` acht Containernamen und Zeile 22 den Pfad
# `${HOME}/Workspace/Projects/mission-control`. Fuer jeden anderen Nutzer
# machte das Skript still gar nichts — eine Wache, die niemand laufen sieht
# und deren Ausfall niemandem auffaellt.


def test_no_agent_names_are_baked_into_the_script():
    """Keine Namensliste — die naechste Flotte sieht anders aus als diese."""
    text = SCRIPT.read_text(encoding="utf-8")
    names = re.findall(r"mc-agent-(?!base\b)[a-z][a-z0-9_-]*", text)
    assert not names, f"fest verdrahtete Agentennamen: {sorted(set(names))}"


def test_repo_path_is_not_hardcoded_to_one_machine():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Workspace/Projects/mission-control" not in text, (
        "der Pfad einer bestimmten Maschine steht im Skript"
    )


def _fake_docker(tmp_path: Path, ps_output: str) -> dict[str, str]:
    """Ein `docker` im PATH, das nur `ps` und `info` beantwortet."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        f'  ps) cat <<\'EOF\'\n{ps_output}\nEOF\n    ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bindir / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


def test_agents_are_discovered_from_running_containers(tmp_path):
    """`--list` zeigt, was der Waechter beobachten wuerde — ermittelt aus den
    laufenden Containern und deren Image, nicht aus einer Liste."""
    env = _fake_docker(
        tmp_path,
        "mc-agent-alpha\tghcr.io/example/mc-claude-agent:latest\n"
        "mc-agent-beta\tmc-agent-base:latest\n"
        "mc-agent-gamma\tmc-omp-agent:latest",
    )
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--list"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]

    assert "mc-agent-alpha:claude" in lines, proc.stdout
    assert "mc-agent-beta:base" in lines, proc.stdout
    # omp/kimi bringen eigene Startskripte mit — es gibt nichts zu vergleichen,
    # also darf der Waechter sie nicht anfassen.
    assert not any(l.startswith("mc-agent-gamma") for l in lines), proc.stdout


def test_no_running_agents_is_a_clean_no_op(tmp_path):
    env = _fake_docker(tmp_path, "")
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--list"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout


def test_repo_path_follows_the_script_location(tmp_path):
    """Ohne MC_REPO_PATH leitet sich das Repo aus dem Skript-Ort ab — der
    Waechter findet also SEIN Checkout, egal wie das Verzeichnis heisst."""
    env = _fake_docker(tmp_path, "")
    env.pop("MC_REPO_PATH", None)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--print-repo"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(tmp_path),  # bewusst woanders gestartet
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == REPO_ROOT


def test_repo_path_is_overridable(tmp_path):
    env = _fake_docker(tmp_path, "")
    env["MC_REPO_PATH"] = str(tmp_path)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--print-repo"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(tmp_path)


@pytest.mark.parametrize("doc", ["docs/agent-configuration-standard.md"])
def test_role_doc_does_not_list_someones_fleet(doc):
    """Dieselbe Liste stand auch in der Doku — Rollen sind das Produkt, die
    Flotte ist Privatsache des Betreibers."""
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    names = re.findall(r"mc-agent-(?!base\b|\{slug\}|<slug>)[a-z][a-z0-9_-]*", text)
    assert not names, f"Agentennamen in {doc}: {sorted(set(names))}"
