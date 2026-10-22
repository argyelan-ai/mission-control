"""Every image MC builds stays at or above the current security floor.

Floor = the Node.js 2026-10-21 security release (22.23.4 / 24.22.1 / 26.12.1:
TLS hostname-verification bypasses, Brotli RCE, and a CA store that no longer
trusts the compromised eMudhra intermediates) plus certifi 2026.10.19 for the
Python CA bundle. The backend image had drifted onto Node 20 (end of life) and
the Claude agent images onto node:22.11.0 — both silently, because nothing
looked. This test looks: it parses the Node pin of every Dockerfile and the
certifi pin of the backend lock.

Raise the floor here when the next security release lands; never lower it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Lowest fixed patch per Node major. A major that is not listed is either end
# of life (20 and older) or unknown — both fail.
NODE_FLOOR = {22: (22, 23, 4), 24: (24, 22, 1), 26: (26, 12, 1)}
CERTIFI_FLOOR = (2026, 10, 19)

# Dockerfiles that ship a Node runtime, and how each one pins it.
FROM_NODE_IMAGES = [
    "docker/mc-agent-base/Dockerfile",
    "docker/mc-claude-agent/Dockerfile",
    "docker/playwright-mcp/Dockerfile",
]


def _ver(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def _assert_node_floor(version: tuple[int, ...], where: str) -> None:
    floor = NODE_FLOOR.get(version[0])
    assert floor is not None, f"{where}: Node {version[0]}.x is end of life or unknown"
    assert version >= floor, (
        f"{where}: Node {'.'.join(map(str, version))} is below the security "
        f"floor {'.'.join(map(str, floor))}"
    )


@pytest.mark.parametrize("rel", FROM_NODE_IMAGES)
def test_node_base_image_is_pinned_at_or_above_the_floor(rel):
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    froms = re.findall(r"^FROM\s+node:(\S+)", text, re.MULTILINE)
    assert froms, f"{rel}: no `FROM node:` line"
    for ref in froms:
        # Exact version tag required — a floating `node:22-…` says nothing
        # about which patch the digest (or a rebuild) actually brings.
        m = re.match(r"(\d+\.\d+\.\d+)-", ref)
        assert m, f"{rel}: `FROM node:{ref}` is not pinned to an exact version"
        _assert_node_floor(_ver(m.group(1)), rel)


def test_omp_bridge_node_tarball_is_at_or_above_the_floor():
    text = (REPO_ROOT / "docker/omp-bridge/Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ARG NODE_VERSION=(\d+\.\d+\.\d+)", text, re.MULTILINE)
    assert m, "docker/omp-bridge/Dockerfile: no ARG NODE_VERSION"
    _assert_node_floor(_ver(m.group(1)), "docker/omp-bridge/Dockerfile")


def test_backend_nodesource_line_and_build_guard():
    text = (REPO_ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    major = re.search(r"^ARG NODE_MAJOR=(\d+)", text, re.MULTILINE)
    minimum = re.search(r"^ARG NODE_MIN=(\d+\.\d+\.\d+)", text, re.MULTILINE)
    assert major and minimum, "backend/Dockerfile: NODE_MAJOR / NODE_MIN args missing"
    assert "deb.nodesource.com/node_${NODE_MAJOR}.x" in text
    assert "node_20.x" not in text
    assert int(major.group(1)) == _ver(minimum.group(1))[0]
    _assert_node_floor(_ver(minimum.group(1)), "backend/Dockerfile NODE_MIN")
    # The floor must be enforced at build time, not just declared.
    assert '"${NODE_MIN}"' in text and "process.exit(ok?0:1)" in text


def _agent_node_majors() -> dict[str, int]:
    """Node major of every agent image that can run a /mc-servers MCP server.
    mc-kimi-agent has no system node (its CLI binary embeds its own runtime),
    so node MCP servers cannot run there at all — nothing to compare."""
    majors: dict[str, int] = {}
    for rel in ("docker/mc-agent-base/Dockerfile", "docker/mc-claude-agent/Dockerfile"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        majors[rel] = int(re.search(r"^FROM\s+node:(\d+)\.", text, re.MULTILINE).group(1))
    omp = (REPO_ROOT / "docker/omp-bridge/Dockerfile").read_text(encoding="utf-8")
    majors["docker/omp-bridge/Dockerfile"] = int(
        re.search(r"^ARG NODE_VERSION=(\d+)\.", omp, re.MULTILINE).group(1)
    )
    return majors


def test_backend_node_is_not_newer_than_any_agent_node():
    """The backend's `npm install` fills ~/.mc/mcp-servers (mounted into every
    agent at /mc-servers) and npm resolves packages for the node it runs
    under; the agents then execute them with THEIR node. An installer newer
    than a runtime could pick a release that needs the newer node and then
    break on the older agent. Older-or-equal is safe. (Native addons do not
    carry over between these images anyway — musl vs glibc — so this is not
    a "same major" rule.)"""
    backend = int(
        re.search(
            r"^ARG NODE_MAJOR=(\d+)",
            (REPO_ROOT / "backend/Dockerfile").read_text(encoding="utf-8"),
            re.MULTILINE,
        ).group(1)
    )
    majors = _agent_node_majors()
    assert set(majors) == {
        "docker/mc-agent-base/Dockerfile",
        "docker/mc-claude-agent/Dockerfile",
        "docker/omp-bridge/Dockerfile",
    }
    for rel, agent in majors.items():
        assert backend <= agent, f"backend node {backend}.x is newer than {rel} node {agent}.x"


def test_ci_busybox_lanes_run_on_the_fleet_base_image():
    """The CI BusyBox/musl lanes stand in for the openclaude agent runtime.
    They run on the exact FROM image of mc-agent-base, so a base bump cannot
    leave them on an older Alpine/BusyBox (review of PR #938: they had stayed
    on alpine:3.20 while the fleet moved to Alpine 3.24)."""
    base = re.search(
        r"^FROM\s+(\S+)",
        (REPO_ROOT / "docker/mc-agent-base/Dockerfile").read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for step in (
        "Disk preflight lib tests (BusyBox/musl)",
        "Bash lib smoke test on the BusyBox/musl runtime",
    ):
        i = ci.index(f"- name: {step}")
        body = ci[i : ci.index("- name:", i + 1)]
        images = re.findall(r"^\s+(\S+:\S+) sh -c '", body, re.MULTILINE)
        assert images == [base], f"CI step {step!r} runs on {images}, fleet base is {base}"


# ca-certificates package with the eMudhra fix, per distro. Debian version
# order matters: on trixie the UNFIXED package is plain `20250419`, which
# sorts ABOVE bookworm's fixed `20250419~deb12u2` — one floor for all would
# let the stale trixie bundle through.
CA_CERTIFICATES_FLOOR = {
    "bookworm": "20250419~deb12u2",
    "trixie": "20250419+deb13u1",
    "jammy": "20260816ubuntu0.22.04.1",
}
# Every apt-based image MC builds, and the distro its FROM resolves to.
# (python:3.12-slim floats with Debian stable = trixie today.)
APT_IMAGES = {
    "backend/Dockerfile": "trixie",
    "voice_worker/Dockerfile": "trixie",
    "docker/cdp-browser/Dockerfile": "trixie",
    "docker/mc-claude-agent/Dockerfile": "bookworm",
    "docker/mc-kimi-agent/Dockerfile": "bookworm",
    "docker/omp-bridge/Dockerfile": "bookworm",
    "docker/mc-playwright/Dockerfile": "jammy",
}
_DISTRO_OF_FROM = (
    (r"python:3\.\d+-slim\b", "trixie"),
    (r"-trixie\b|debian:trixie", "trixie"),
    (r"-bookworm\b|debian:bookworm", "bookworm"),
    (r"-jammy\b", "jammy"),
)


@pytest.mark.parametrize("rel,distro", sorted(APT_IMAGES.items()))
def test_apt_image_enforces_the_ca_certificates_floor_at_build(rel, distro):
    """Review of PR #938: an unchanged `apt-get install ca-certificates` line
    is served from the layer cache, so the image kept the stale bundle until
    someone remembered `--pull` / `--no-cache`. Each apt image now declares the
    fixed version and fails its build below it."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    first_from = re.search(r"^FROM\s+(\S+)", text, re.MULTILINE).group(1)
    detected = next((d for pat, d in _DISTRO_OF_FROM if re.search(pat, first_from)), None)
    assert detected == distro, f"{rel}: FROM {first_from} is {detected}, table says {distro}"
    m = re.search(r"^ARG CA_CERTIFICATES_MIN=(\S+)$", text, re.MULTILINE)
    assert m, f"{rel}: no ARG CA_CERTIFICATES_MIN"
    assert m.group(1) == CA_CERTIFICATES_FLOOR[distro], (
        f"{rel}: CA_CERTIFICATES_MIN={m.group(1)}, {distro} floor is "
        f"{CA_CERTIFICATES_FLOOR[distro]}"
    )
    # Enforced in the build, after the package is installed — not just declared.
    assert 'dpkg --compare-versions "$v" ge "${CA_CERTIFICATES_MIN}"' in text
    assert text.index("ARG CA_CERTIFICATES_MIN") < text.index("dpkg --compare-versions")
    assert text.index("ca-certificates") < text.index("dpkg --compare-versions")


def test_backend_lock_pins_certifi_at_or_above_the_floor():
    lock = (REPO_ROOT / "backend/requirements.lock").read_text(encoding="utf-8")
    m = re.search(r"^certifi==(\d+)\.(\d+)\.(\d+)$", lock, re.MULTILINE)
    assert m, "backend/requirements.lock: no certifi pin"
    assert tuple(int(g) for g in m.groups()) >= CERTIFI_FLOOR
    pyproject = (REPO_ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    floor = re.search(r'"certifi>=(\d+)\.(\d+)\.(\d+)"', pyproject)
    assert floor, "backend/pyproject.toml: no certifi floor (a re-lock could go back)"
    assert tuple(int(g) for g in floor.groups()) >= CERTIFI_FLOOR
