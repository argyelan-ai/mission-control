#!/usr/bin/env python3
"""The omp agent image ships Node.js, so an agent can check its own JS.

Live 21.10.2026: a bench task asked an omp agent for one ``index.html`` with
a canvas. To check its file the agent ran ``which node npx chromium …``,
found nothing, failed to build a JS parser with pip (the image has no
compiler) and fell back to a pure-Python parser. With Node in the image a
JS file is one command: ``node --check app.js``. Node does NOT read an
``.html`` file (``node --check index.html`` → ERR_UNKNOWN_FILE_EXTENSION):
an inline ``<script>`` is checked in the browser (the omp card's
self-check) or extracted to a ``.js`` file first.

Two layers:

* **Dockerfile checks** (always run, CI too): Node comes from the official
  nodejs.org release tarball, pinned by version AND sha256, verified with
  ``sha256sum -c`` before it is unpacked; no step of that install can fail
  quietly; the build itself runs ``node``, ``npm`` and ``npx`` (a broken
  unpack fails the build, not the agent's first task); it is installed while
  the build is still root, and before the omp layer, so an omp bump does not
  download Node again; npm neither nags about its own update nor waits at
  an install prompt; Node's LICENSE ships with it.
* **Image checks** (run when ``MC_OMP_IMAGE`` names a built image, e.g.
  ``MC_OMP_IMAGE=mc-omp-agent:latest``): a throwaway container without
  network runs ``node -e``, ``node --check`` (a valid file passes WITHOUT
  running, a broken one fails), ``npx`` as the image's own user — also
  under a PTY, the way omp's bash tool runs it in the native TUI. CI does
  not build this image — omp is pinned arm64-only (release.yml) — so these
  skip there.

Why real Node and not the Bun inside omp (``BUN_BE_BUN=1 omp`` is Bun
1.4.3): Bun's ``--check`` RUNS the file instead of only parsing it, and the
variable leaks into child processes — an ``omp`` started from such a script
answers as Bun, not as omp (both live 21.10.2026). The image checks below
pin both differences.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DOCKERFILE = HERE.parent / "Dockerfile"

NODE_URL = ("https://nodejs.org/dist/v${NODE_VERSION}/"
            "node-v${NODE_VERSION}-linux-arm64.tar.gz")
NODE_DIR = "node-v${NODE_VERSION}-linux-arm64"


def instructions() -> list[str]:
    """The Dockerfile as Docker reads it: one string per instruction.

    Continuation lines (``\\`` at the end) are joined, and comment lines
    inside a continuation are dropped — Docker does the same.
    """
    out: list[str] = []
    current: list[str] = []
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cont = line.endswith("\\")
        current.append(line[:-1].strip() if cont else line)
        if not cont:
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return out


def env_of(ins: list[str]) -> dict[str, str]:
    """Every ``ENV`` as Docker sets it: ``ENV A=1 B="x y"`` and the old
    one-pair form ``ENV A 1``. A later ENV wins, as in the image."""
    env: dict[str, str] = {}
    for one in ins:
        m = re.match(r"ENV\s+(.*)", one)
        if not m:
            continue
        body = m.group(1)
        words = shlex.split(body)
        if words and "=" not in words[0]:          # ENV KEY value with spaces
            env[words[0]] = body.split(None, 1)[1].strip() if len(words) > 1 else ""
            continue
        for word in words:
            key, sep, value = word.partition("=")
            assert sep, f"cannot read ENV pair {word!r} in {one!r}"
            env[key] = value
    return env


def arg(name: str) -> str:
    for ins in instructions():
        m = re.fullmatch(rf"ARG\s+{name}=(\S+)", ins)
        if m:
            return m.group(1)
    raise AssertionError(f"no `ARG {name}=<default>` in the Dockerfile")


def position_of(pattern: str) -> int:
    for i, ins in enumerate(instructions()):
        if re.fullmatch(pattern, ins):
            return i
    raise AssertionError(f"no instruction matches {pattern!r}")


def node_install() -> tuple[int, str]:
    """(position, text) of the RUN that downloads Node."""
    for i, ins in enumerate(instructions()):
        if ins.startswith("RUN ") and NODE_URL in ins:
            return i, ins
    raise AssertionError(
        f"no RUN downloads the official Node tarball {NODE_URL} — the image "
        "has no node, and an agent cannot check its own JS")


def steps(run: str) -> list[str]:
    """The ``&&`` chain of a RUN, one command per entry."""
    return [s.strip() for s in run[len("RUN "):].split("&&")]


# --------------------------------------------------------------------------
# Dockerfile checks — always run
# --------------------------------------------------------------------------

def test_node_is_pinned_to_an_lts_release_and_its_checksum():
    version = arg("NODE_VERSION")
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    assert m, f"NODE_VERSION must be an exact release (x.y.z), got {version!r}"
    # Node's LTS lines are the even majors (22, 24, 26 …).
    assert int(m.group(1)) % 2 == 0, f"Node {version} is not an LTS line"
    assert int(m.group(1)) >= 22, f"Node {version} is past end of life"
    assert re.fullmatch(r"[0-9a-f]{64}", arg("NODE_SHA256")), (
        "NODE_SHA256 must be the sha256 of the tarball (64 hex characters)")


def test_the_tarball_is_verified_before_it_is_unpacked():
    _, run = node_install()
    chain = steps(run)
    download = re.search(rf"curl -fsSL \"{re.escape(NODE_URL)}\" -o (\S+)", chain[0])
    assert download, f"the first step is not the Node download: {chain[0]!r}"
    tarball = download.group(1)
    check = [i for i, s in enumerate(chain) if re.fullmatch(
        rf"echo \"\$\{{NODE_SHA256\}}  {re.escape(tarball)}\" \| sha256sum -c -", s)]
    unpack = [i for i, s in enumerate(chain)
              if re.match(rf"tar\s+-\w*x\w*\s+{re.escape(tarball)}\b", s)]
    assert check, f"{tarball} is not checked against NODE_SHA256 on its own step"
    assert unpack, "the Node tarball is never unpacked"
    assert check[0] < min(unpack), (
        "the tarball is unpacked before its checksum is checked")


def test_no_step_of_the_node_install_can_fail_quietly():
    """``sha256sum -c - || true`` or ``(npx --version || true)`` would keep
    the build green with a wrong tarball or a broken npx. Every step is a
    plain ``&&`` link; the only pipe feeds the checksum to ``sha256sum``,
    whose status is the pipe's status."""
    _, run = node_install()
    assert "||" not in run, f"a step of the Node install swallows a failure: {run!r}"
    assert ";" not in run, f"`;` ignores the status of the step before it: {run!r}"
    piped = [s for s in steps(run) if "|" in s]
    assert all(s.endswith("| sha256sum -c -") for s in piped), (
        f"a pipe hides the status of a Node install step: {piped!r}")


def test_the_build_itself_runs_node_npm_and_npx():
    """npm and npx are links into lib/node_modules — an unpack that drops
    lib/ leaves a node that works and an npx that does not. The build must
    fail on that, not the agent's first task."""
    _, run = node_install()
    chain = steps(run)
    for cmd in ("node --version", "npm --version", "npx --version"):
        assert cmd in chain, f"the Node install step does not run `{cmd}` on its own"


def test_npm_does_not_tell_the_agent_to_update_npm():
    """npm's update notice ("run npm install -g npm@…") lands in every npx
    output; an agent takes it as work, and the global install fails as the
    agent user. Live 21.10.2026 with node 24.22.1: npm 11 → 12 notice."""
    assert env_of(instructions()).get("NPM_CONFIG_UPDATE_NOTIFIER") == "false"


def test_npx_never_stops_at_an_install_prompt():
    """omp's bash tool runs a command under a PTY when the model asks for
    one in the native TUI. There ``npx <tool>`` asks "Ok to proceed? (y)"
    and nobody answers — the call hangs until its timeout (live 21.10.2026:
    killed after 27 s; with NPM_CONFIG_YES=true it ran in 1 s). Without a
    PTY npm already says yes on its own, so this only removes the hang."""
    assert env_of(instructions()).get("NPM_CONFIG_YES") == "true"


def test_node_is_installed_while_the_build_is_root():
    position, _ = node_install()
    users = [i for i, ins in enumerate(instructions())
             if re.fullmatch(r"USER\s+agent", ins)]
    assert users, "the Dockerfile has no `USER agent`"
    assert position < users[0], (
        "Node is installed after `USER agent` — /usr/local is root's")


def test_an_omp_bump_does_not_download_node_again():
    """A changed layer rebuilds every layer after it. The in-app CLI update
    (ADR-058) bumps omp and rebuilds; with Node after omp every omp bump
    downloaded Node again — and failed (rolled back) whenever nodejs.org
    was unreachable."""
    position, _ = node_install()
    assert position < position_of(r"ARG\s+OMP_VERSION=\S+"), (
        "the Node layer sits after the omp layer — an omp bump downloads Node again")


def test_node_license_ships_in_the_image():
    """Only bin/ and lib/ are unpacked; Node's LICENSE sits at the tarball
    root and goes to /usr/local/share/doc/nodejs/."""
    _, run = node_install()
    assert any(re.fullmatch(
        rf"tar\s+-\w*x\w*\s+\S+\s+-C\s+/usr/local/share/doc/nodejs\s+.*"
        rf"\"{re.escape(NODE_DIR)}/LICENSE\"", s) for s in steps(run)), (
        "Node's LICENSE is not unpacked to /usr/local/share/doc/nodejs/")


def test_the_parser_reads_the_real_dockerfile():
    """Guard for the guard: the parser sees instructions that exist, and
    ENV reads both forms Docker accepts."""
    ins = instructions()
    assert ins[0].startswith("FROM debian:")
    assert "ENTRYPOINT [\"/usr/local/bin/entrypoint.sh\"]" in ins
    assert re.fullmatch(r"[0-9a-f]{64}", arg("OMP_SHA256"))
    assert env_of(ins).get("OMP_PROFILE") == "mc-agent"
    assert env_of(['ENV A=1 B="x y"', "ENV C hello world", "ENV A=2"]) == {
        "A": "2", "B": "x y", "C": "hello world"}


# --------------------------------------------------------------------------
# Image checks — only with MC_OMP_IMAGE set (a built image), never in CI
# --------------------------------------------------------------------------

IMAGE = os.environ.get("MC_OMP_IMAGE", "")

needs_image = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None,
    reason="set MC_OMP_IMAGE=<built mc-omp-agent image> to run (needs docker)")


def in_image(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a throwaway container: no network, image's own user."""
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "--entrypoint", "bash", IMAGE, "-c", script],
        capture_output=True, text=True, timeout=120)


@needs_image
def test_node_npm_and_npx_are_on_the_path():
    res = in_image("command -v node npm npx")
    assert res.returncode == 0, (
        f"missing on PATH in {IMAGE}: {res.stdout!r} {res.stderr!r}")


@needs_image
def test_node_is_real_node_at_the_pinned_version():
    res = in_image("node --version && node -p process.release.name "
                   "&& node -e 'console.log(1 + 1)'")
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == [f"v{arg('NODE_VERSION')}", "node", "2"], (
        f"not Node {arg('NODE_VERSION')}: {res.stdout!r}")


@needs_image
def test_node_check_parses_without_running():
    res = in_image(
        "cd /tmp && printf 'console.log(\"RAN\")\\n' > ok.js "
        "&& printf 'const a = ;\\n' > bad.js "
        "&& node --check ok.js && echo OK_PASSED "
        "&& { node --check bad.js; echo \"BAD_RC=$?\"; }")
    assert "OK_PASSED" in res.stdout, (res.stdout, res.stderr)
    assert "RAN" not in res.stdout, "`node --check` ran the file"
    assert "BAD_RC=1" in res.stdout and "SyntaxError" in res.stderr, (
        "a broken file passed `node --check`", res.stdout, res.stderr)


@needs_image
def test_npx_and_npm_answer_as_the_agent_user():
    res = in_image("id -un && npm --version && npx --version "
                   "&& npm config get update-notifier")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.split()
    assert lines[0] == "agent", f"ran as {lines[0]!r}, not the agent user"
    assert len(lines) == 4 and lines[1] == lines[2], (
        f"npm and npx disagree: {res.stdout!r}")
    assert lines[3] == "false", "npm still shows its update notice"


@needs_image
def test_npx_under_a_pty_runs_without_waiting_for_a_yes():
    """A package npx has not seen yet, offline (a local tarball), run under
    a PTY by ``script`` — the way omp's bash tool runs it in the TUI."""
    res = in_image(
        "set -e; mkdir -p /tmp/p && cd /tmp/p "
        "&& printf '{\"name\":\"pty-probe\",\"version\":\"1.0.0\","
        "\"bin\":{\"pty-probe\":\"cli.js\"}}' > package.json "
        "&& printf '#!/usr/bin/env node\\nconsole.log(\"PROBE_RAN\")\\n' > cli.js "
        "&& npm pack -q --pack-destination /tmp >/dev/null && cd /tmp "
        "&& set +e; timeout 15 script -qec "
        "'npx --package=file:/tmp/pty-probe-1.0.0.tgz pty-probe' /dev/null "
        "</dev/null; echo \"NPX_RC=$?\"")
    assert "Ok to proceed" not in res.stdout, (
        "npx waits at its install prompt under a PTY", res.stdout)
    assert "PROBE_RAN" in res.stdout and "NPX_RC=0" in res.stdout, (
        res.stdout, res.stderr)


@needs_image
def test_node_license_is_in_the_image():
    res = in_image("head -n 1 /usr/local/share/doc/nodejs/LICENSE")
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("Node.js is licensed"), res.stdout


@needs_image
def test_omp_still_answers_as_omp():
    """Node is next to omp, not instead of it (the Bun shim would turn
    `omp` into Bun for any child of a node script)."""
    res = in_image("node -e \"process.stdout.write(require('child_process')"
                   ".execFileSync('omp', ['--version']).toString())\"")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().startswith("omp/"), res.stdout
