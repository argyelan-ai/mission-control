"""cdp-browser (the shared agent browser behind playwright-mcp) must keep
WebGL alive and must not pile up zombies.

Live finding 02.10.2026: an agent built a WebGL scene and could never see it —
the browser, up for 14 days, had logged 63 GPU-process crashes; after 3 of them
Chromium stops restarting the GPU process and ``getContext('webgl')`` returns
null until restart. The same container held 71 716 zombie ``timeout``
processes from its healthcheck (PID 1 = Chromium never reaps), eating the
Docker VM's shared pid space. The runtime proof is
``docker/cdp-browser/test_webgl.sh`` (CI); these checks pin the config.
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cdp_browser() -> dict:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["cdp-browser"]


def test_cdp_browser_runs_under_init():
    assert _cdp_browser().get("init") is True, (
        "cdp-browser: init: true fehlt -- Chromium als PID 1 raeumt die "
        "Healthcheck-Waisen nie ab (Zombies fressen den PID-Raum der Docker-VM)"
    )


def test_cdp_browser_survives_gpu_process_crashes():
    flags = _cdp_browser()["command"]
    assert "--disable-gpu-process-crash-limit" in flags, (
        "ohne --disable-gpu-process-crash-limit ist WebGL nach 3 GPU-Abstuerzen "
        "bis zum Browser-Neustart weg"
    )


def test_cdp_browser_keeps_swiftshader_webgl_fallback():
    assert "--enable-unsafe-swiftshader" in _cdp_browser()["command"]


def test_runtime_webgl_test_reads_the_shipped_compose():
    """test_webgl.sh must test docker-compose.yml, not its own flag copy."""
    script = (REPO_ROOT / "docker/cdp-browser/test_webgl.sh").read_text(encoding="utf-8")
    assert '["services"]["cdp-browser"]' in script
    assert "--disable-gpu-process-crash-limit" not in script.split("set -euo pipefail", 1)[1]


# ── images (ADR-088 step "images"): Chromium 154 + thin playwright-mcp ─────

CDP_DOCKERFILE = REPO_ROOT / "docker/cdp-browser/Dockerfile"
PW_DOCKERFILE = REPO_ROOT / "docker/playwright-mcp/Dockerfile"


def _service(name: str) -> dict:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"][name]


def test_cdp_browser_base_is_debian_pinned_by_digest():
    """zenika/alpine-chrome is frozen at Chromium 124 (even its `latest`), and
    Alpine's chromium has no SwiftShader (WebGL = 0). Debian's has both; the
    base is pinned by digest so a rebuild never changes it silently."""
    text = CDP_DOCKERFILE.read_text(encoding="utf-8")
    assert "zenika/alpine-chrome" not in text.split("\nFROM", 1)[-1]
    assert "FROM debian:trixie-slim@sha256:" in text


def test_websocat_download_is_checksum_verified():
    text = CDP_DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG WEBSOCAT_VERSION=1.14.1" in text and "websocat/releases/download/v${WEBSOCAT_VERSION}/" in text
    assert "sha256sum -c" in text
    for digest in (
        "711a69576a2ff473fb01a90ffafb571c2ed019e55479d7ae71b12c2eadeb7011",  # aarch64
        "66f8dd3a0394761556339117f8bb5123bddefd44e087af2a72ec22b0bd08d514",  # x86_64
    ):
        assert digest in text


def test_gateway_dependency_matches_the_ci_pin():
    """The gateway CI job installs websockets==15.0.1 'same version pinned in
    docker/cdp-browser/Dockerfile' — keep them equal."""
    assert '"websockets==15.0.1"' in CDP_DOCKERFILE.read_text(encoding="utf-8")
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '"websockets==15.0.1"' in ci


def test_cdp_browser_window_is_large_enough():
    """Chromium's default headless window (780x493 on 124) made the live
    panel a small picture with a border."""
    assert "--window-size=1280,900" in _cdp_browser()["command"]


def test_cdp_browser_memory_is_capped():
    """One Chromium serves every session; a runaway tab must not eat the
    shared Docker VM."""
    assert _cdp_browser().get("mem_limit") == "2g"


def test_playwright_mcp_is_built_from_a_pinned_version():
    svc = _service("playwright-mcp")
    assert svc.get("build") == "./docker/playwright-mcp"
    assert "image" not in svc or not str(svc["image"]).endswith(":latest")
    text = PW_DOCKERFILE.read_text(encoding="utf-8")
    assert "@playwright/mcp@0.0.83" in text
    assert "FROM node:22-bookworm-slim@sha256:" in text


def test_playwright_mcp_writes_screenshots_into_the_output_mount():
    """Named screenshots (`<task_id>/x.png`) resolve against the working
    directory: only WORKDIR /output puts them into the mounted folder
    (lab: 0.0.70 -> ENOENT under /home/node, 0.0.83 without it -> 'outside
    allowed roots')."""
    assert "WORKDIR /output" in PW_DOCKERFILE.read_text(encoding="utf-8")
    assert "--output-dir" in _service("playwright-mcp")["command"]


def test_playwright_mcp_viewport_matches_the_panel():
    args = _service("playwright-mcp")["command"]
    assert "--viewport-size" in args
    assert args[args.index("--viewport-size") + 1] == "1280x800"
