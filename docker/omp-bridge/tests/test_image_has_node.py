#!/usr/bin/env python3
"""The omp agent image ships Node.js, so an agent can check its own JS.

Live 21.10.2026: a bench task asked an omp agent for one ``index.html`` with
a canvas. To check its file the agent ran ``which node npx chromium …``,
found nothing, failed to build a JS parser with pip (the image has no
compiler) and fell back to a pure-Python parser. With Node in the image the
check is ``node --check app.js``.

Two layers:

* **Dockerfile checks** (always run, CI too): Node comes from the official
  nodejs.org release tarball, pinned by version AND sha256, verified with
  ``sha256sum -c`` before it is unpacked; the build itself runs ``node`` and
  ``npx`` (a broken unpack fails the build, not the agent's first task); and
  it is installed while the build is still root, before ``USER agent``.
* **Image checks** (run when ``MC_OMP_IMAGE`` names a built image, e.g.
  ``MC_OMP_IMAGE=mc-omp-agent:latest``): a throwaway container without
  network runs ``node -e``, ``node --check`` (a valid file passes WITHOUT
  running, a broken one fails) and ``npx --version`` as the image's own
  user. CI does not build this image — omp is pinned arm64-only
  (release.yml) — so these skip there.

Why real Node and not the Bun inside omp (``BUN_BE_BUN=1 omp`` is Bun
1.4.3): Bun's ``--check`` RUNS the file instead of only parsing it, and the
variable leaks into child processes — an ``omp`` started from such a script
answers as Bun, not as omp (both live 21.10.2026). The image checks below
pin both differences.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DOCKERFILE = HERE.parent / "Dockerfile"

NODE_URL = ("https://nodejs.org/dist/v${NODE_VERSION}/"
            "node-v${NODE_VERSION}-linux-arm64.tar.gz")


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


def arg(name: str) -> str:
    for ins in instructions():
        m = re.fullmatch(rf"ARG\s+{name}=(\S+)", ins)
        if m:
            return m.group(1)
    raise AssertionError(f"no `ARG {name}=<default>` in the Dockerfile")


def node_install() -> tuple[int, str]:
    """(position, text) of the RUN that downloads Node."""
    for i, ins in enumerate(instructions()):
        if ins.startswith("RUN ") and NODE_URL in ins:
            return i, ins
    raise AssertionError(
        f"no RUN downloads the official Node tarball {NODE_URL} — the image "
        "has no node, and an agent cannot check its own JS")


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
    check = re.search(r'sha256sum -c', run)
    unpack = re.search(r'\btar\b', run)
    assert check and "${NODE_SHA256}" in run, (
        "the Node download is not checked against NODE_SHA256")
    assert unpack, "the Node tarball is never unpacked"
    assert check.start() < unpack.start(), (
        "the tarball is unpacked before its checksum is checked")


def test_the_build_itself_runs_node_and_npx():
    """npm and npx are links into lib/node_modules — an unpack that drops
    lib/ leaves a node that works and an npx that does not. The build must
    fail on that, not the agent's first task."""
    _, run = node_install()
    for cmd in ("node --version", "npx --version"):
        assert cmd in run, f"the Node install step does not run `{cmd}`"


def test_node_is_installed_while_the_build_is_root():
    position, _ = node_install()
    users = [i for i, ins in enumerate(instructions())
             if re.fullmatch(r"USER\s+agent", ins)]
    assert users, "the Dockerfile has no `USER agent`"
    assert position < users[0], (
        "Node is installed after `USER agent` — /usr/local is root's")


def test_the_parser_reads_the_real_dockerfile():
    """Guard for the guard: the parser sees instructions that exist."""
    ins = instructions()
    assert ins[0].startswith("FROM debian:")
    assert "ENTRYPOINT [\"/usr/local/bin/entrypoint.sh\"]" in ins
    assert re.fullmatch(r"[0-9a-f]{64}", arg("OMP_SHA256"))


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
    res = in_image("id -un && npm --version && npx --version")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.split()
    assert lines[0] == "agent", f"ran as {lines[0]!r}, not the agent user"
    assert len(lines) == 3 and lines[1] == lines[2], (
        f"npm and npx disagree: {res.stdout!r}")


@needs_image
def test_omp_still_answers_as_omp():
    """Node is next to omp, not instead of it (the Bun shim would turn
    `omp` into Bun for any child of a node script)."""
    res = in_image("node -e \"process.stdout.write(require('child_process')"
                   ".execFileSync('omp', ['--version']).toString())\"")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().startswith("omp/"), res.stdout
