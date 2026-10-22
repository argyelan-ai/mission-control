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

import ast
import json
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
    "noble": "20260816ubuntu0.24.04.1",
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
    "docker/mc-playwright/Dockerfile": "noble",
}
_DISTRO_OF_FROM = (
    (r"python:3\.\d+-slim\b", "trixie"),
    (r"-trixie\b|debian:trixie", "trixie"),
    (r"-bookworm\b|debian:bookworm", "bookworm"),
    (r"-jammy\b", "jammy"),
    (r"-noble\b", "noble"),
)


@pytest.mark.parametrize("rel,distro", sorted(APT_IMAGES.items()))
def test_apt_image_enforces_the_ca_certificates_floor_at_build(rel, distro):
    """Review of PR #938: an unchanged `apt-get install ca-certificates` line
    is served from the layer cache, so the image kept the stale bundle until
    someone remembered `--pull` / `--no-cache`. Each apt image now declares the
    fixed version and fails its build below it."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    # The image's own base = the first FROM that is not the `AS node` copy
    # stage (mc-playwright copies its Node binary out of a node image).
    first_from = next(
        m.group(1)
        for m in re.finditer(r"^FROM\s+(\S+)(.*)$", text, re.MULTILINE)
        if m.group(2).strip() != "AS node"
    )
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


# Claude Code verifies TLS with its own root store (bundled + system), not
# Node's: on node 22.23.4 + the fixed Debian bundle, 2.1.207 still accepted
# the emSign test chain (`API Error: 405` from the test host instead of a
# certificate error). Measured on 2026-10-22 against the incident page's test
# hosts: 2.1.302 accepts both chains; 2.1.303 (the version the incident page
# lists) rejects both on the fixed bundle but still accepts the AAA chain
# when the image's system bundle is stale; 2.1.304+ rejects both even then.
CLAUDE_CLI_FLOOR = (2, 1, 304)


def test_claude_cli_pin_is_at_or_above_the_emudhra_floor():
    import json

    manifest = json.loads((REPO_ROOT / "docker/cli-versions.json").read_text(encoding="utf-8"))
    pin = _ver(manifest["claude"]["version"])
    assert pin >= CLAUDE_CLI_FLOOR, f"docker/cli-versions.json claude {pin} is below {CLAUDE_CLI_FLOOR}"
    # The Dockerfile default is the fallback for a manual `docker build`
    # without build-args — it must not quietly bring the vulnerable CLI back.
    text = (REPO_ROOT / "docker/mc-claude-agent/Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ARG CLAUDE_VERSION=(\d+\.\d+\.\d+)$", text, re.MULTILINE)
    assert m, "docker/mc-claude-agent/Dockerfile: no exact ARG CLAUDE_VERSION default"
    assert _ver(m.group(1)) >= CLAUDE_CLI_FLOOR


def test_kimi_cli_verifies_tls_against_the_system_bundle():
    """The kimi binary embeds its own Node (v24.15.0 up to at least 2.3.0),
    whose built-in root store still trusts the compromised eMudhra chain.
    `--use-openssl-ca` switches it to the image's system bundle, which the
    ca-certificates floor keeps clean. Remove together with this test once a
    kimi release embeds Node >= 24.22.1."""
    text = (REPO_ROOT / "docker/mc-kimi-agent/Dockerfile").read_text(encoding="utf-8")
    settings = re.findall(r"^ENV NODE_OPTIONS=(.+)$", text, re.MULTILINE)
    assert settings, "docker/mc-kimi-agent/Dockerfile: no ENV NODE_OPTIONS"
    # The last ENV wins at runtime — a later line must not drop the flag.
    assert "--use-openssl-ca" in settings[-1].split()
    # The system bundle it switches to must be the floored one.
    assert text.index("ARG CA_CERTIFICATES_MIN") < text.index("ENV NODE_OPTIONS")


# kimi versions whose embedded Node was checked against the eMudhra test
# chains (all embed v24.15.0 → still need the NODE_OPTIONS workaround above).
KIMI_EMBEDDED_NODE_CHECKED = {"0.29.1"}


def test_kimi_version_bump_rechecks_the_embedded_node():
    """Reminder hook: a kimi bump must re-check the embedded Node. If the new
    binary embeds Node >= 24.22.1, drop `ENV NODE_OPTIONS` and the test above;
    otherwise add the version to KIMI_EMBEDDED_NODE_CHECKED."""
    manifest = json.loads((REPO_ROOT / "docker/cli-versions.json").read_text(encoding="utf-8"))
    dockerfile = (REPO_ROOT / "docker/mc-kimi-agent/Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ARG KIMI_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert m, "docker/mc-kimi-agent/Dockerfile: no ARG KIMI_VERSION default"
    for where, version in (("cli-versions.json", manifest["kimi"]["version"]), ("Dockerfile", m.group(1))):
        assert version in KIMI_EMBEDDED_NODE_CHECKED, (
            f"kimi {version} ({where}) not checked: does its embedded Node still "
            "accept the eMudhra chain? See the docstring."
        )


# ── mc-playwright: Playwright's own Node and Chromium ────────────────────────
MC_PLAYWRIGHT = "docker/mc-playwright/Dockerfile"


def test_mc_playwright_driver_runs_on_a_patched_node():
    """Playwright's Python driver bundles its own Node (1.48: v20.18.0, end of
    life; 1.64.0: v24.21.0 — both below the floor). The image swaps in a
    patched Node from a pinned node image and fails its build below NODE_MIN."""
    text = (REPO_ROOT / MC_PLAYWRIGHT).read_text(encoding="utf-8")
    stage = re.search(
        r"^FROM node:(\d+\.\d+\.\d+)-bookworm-slim@sha256:[0-9a-f]{64} AS node$",
        text, re.MULTILINE,
    )
    assert stage, f"{MC_PLAYWRIGHT}: no pinned `FROM node:<ver>-bookworm-slim@sha256:… AS node` stage"
    _assert_node_floor(_ver(stage.group(1)), f"{MC_PLAYWRIGHT} node stage")
    minimum = re.search(r"^ARG NODE_MIN=(\d+\.\d+\.\d+)$", text, re.MULTILINE)
    assert minimum, f"{MC_PLAYWRIGHT}: no ARG NODE_MIN"
    _assert_node_floor(_ver(minimum.group(1)), f"{MC_PLAYWRIGHT} NODE_MIN")
    assert _ver(stage.group(1)) >= _ver(minimum.group(1))
    assert "COPY --from=node /usr/local/bin/node /usr/local/bin/node" in text
    assert "ENV PLAYWRIGHT_NODEJS_PATH=/usr/local/bin/node" in text
    # The bundled binary is replaced, and the floor is checked on the driver
    # path itself (what Playwright actually starts), at build time.
    assert 'ln -sf /usr/local/bin/node "$d/node"' in text
    assert '"$d/node" -e' in text and '"${NODE_MIN}"' in text


def test_mc_playwright_package_matches_its_base_image():
    """The pip `playwright` must match the base image's browser builds — a
    mismatch makes Playwright look for a browser revision the image lacks."""
    text = (REPO_ROOT / MC_PLAYWRIGHT).read_text(encoding="utf-8")
    base = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)[-1]
    m = re.fullmatch(
        r"mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-noble@sha256:[0-9a-f]{64}", base
    )
    assert m, f"{MC_PLAYWRIGHT}: base {base} is not a pinned playwright/python noble tag+digest"
    pin = re.search(r"^\s+playwright==(\d+\.\d+\.\d+) \\$", text, re.MULTILINE)
    assert pin and pin.group(1) == m.group(1), (pin and pin.group(1), m.group(1))
    # 1.48 = Chromium 130 + a Node 20 driver; 1.64 = Chrome for Testing 156.
    assert _ver(m.group(1)) >= (1, 64, 0)


def test_mc_playwright_chromium_gets_the_emudhra_distrust_policy():
    text = (REPO_ROOT / MC_PLAYWRIGHT).read_text(encoding="utf-8")
    # Chrome for Testing and Chromium builds read different directories.
    for d in ("/etc/opt/chrome_for_testing/policies/managed", "/etc/chromium/policies/managed"):
        assert f"COPY policies/emudhra-distrust.json {d}/emudhra-distrust.json" in text, d
    # The headless shell ignores managed policies — it is removed, so a
    # launch without channel="chromium" fails instead of trusting the chain.
    assert "RUN rm -rf /ms-playwright/chromium_headless_shell-*" in text
    # …and the build proves both by effect (chrome://policy shows the policy
    # with status OK; a channel-less launch finds no browser), after the
    # policy and the service code are in place.
    check = text.index("RUN python3 /app/check_browser.py")
    assert check > text.index("COPY media.py /app/media.py")
    assert check > text.index("RUN rm -rf /ms-playwright/chromium_headless_shell-*")
    assert check > text.index("/etc/opt/chrome_for_testing/policies/managed/emudhra-distrust.json")
    assert "COPY check_browser.py /app/check_browser.py" in text
    # Same certificate list as cdp-browser (pinned by SHA-256 in
    # test_compose_cdp_browser.py) — one list, two build contexts.
    ours = (REPO_ROOT / "docker/mc-playwright/policies/emudhra-distrust.json").read_bytes()
    theirs = (REPO_ROOT / "docker/cdp-browser/policies/emudhra-distrust.json").read_bytes()
    assert ours == theirs


def test_mc_playwright_launches_only_the_policy_honouring_browser():
    """chrome-headless-shell — Playwright's default for headless=True — reads
    no managed policy and loaded the emSign test site (measured 2026-10-22,
    Chrome for Testing 156.0.8078.4); the full browser in new headless mode
    (`channel="chromium"`) refuses it. Every launch in the service goes
    through one helper with those kwargs. (Checked as text: service.py
    imports playwright, which the backend venv does not have — parsed, not
    imported.)"""
    media = ast.parse((REPO_ROOT / "docker/mc-playwright/media.py").read_text(encoding="utf-8"))
    kwargs = next(
        n.value for n in media.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CHROMIUM_LAUNCH_KWARGS" for t in n.targets)
    )
    assert ast.literal_eval(kwargs) == {"headless": True, "channel": "chromium"}

    service = ast.parse((REPO_ROOT / "docker/mc-playwright/service.py").read_text(encoding="utf-8"))
    # Every `<x>.chromium.launch(…)` / `.launch_persistent_context(…)` call,
    # however it is wrapped over lines, with the function it sits in.
    launches = []
    helper_calls = 0
    for fn in ast.walk(service):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute)
                    and f.attr in ("launch", "launch_persistent_context")
                    and isinstance(f.value, ast.Attribute) and f.value.attr == "chromium"):
                launches.append((fn.name, ast.unparse(node)))
            if isinstance(f, ast.Name) and f.id == "_launch_chromium":
                helper_calls += 1
    assert launches == [("_launch_chromium", "p.chromium.launch(**CHROMIUM_LAUNCH_KWARGS)")], launches
    assert helper_calls >= 6
