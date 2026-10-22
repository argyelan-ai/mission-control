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


def test_backend_and_agent_images_share_one_node_major():
    """The backend's `npm install` fills ~/.mc/mcp-servers; the agent
    containers run those servers with their own node. Native addons only load
    under the major they were built for."""
    backend = re.search(
        r"^ARG NODE_MAJOR=(\d+)",
        (REPO_ROOT / "backend/Dockerfile").read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)
    for rel in ("docker/mc-agent-base/Dockerfile", "docker/mc-claude-agent/Dockerfile"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        agent = re.search(r"^FROM\s+node:(\d+)\.", text, re.MULTILINE).group(1)
        assert agent == backend, f"{rel}: node {agent}.x vs backend node {backend}.x"


def test_backend_lock_pins_certifi_at_or_above_the_floor():
    lock = (REPO_ROOT / "backend/requirements.lock").read_text(encoding="utf-8")
    m = re.search(r"^certifi==(\d+)\.(\d+)\.(\d+)$", lock, re.MULTILINE)
    assert m, "backend/requirements.lock: no certifi pin"
    assert tuple(int(g) for g in m.groups()) >= CERTIFI_FLOOR
    pyproject = (REPO_ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    floor = re.search(r'"certifi>=(\d+)\.(\d+)\.(\d+)"', pyproject)
    assert floor, "backend/pyproject.toml: no certifi floor (a re-lock could go back)"
    assert tuple(int(g) for g in floor.groups()) >= CERTIFI_FLOOR
