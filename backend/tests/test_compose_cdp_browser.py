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
import re
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
    # Version tag + digest: the digest is what Docker pulls, the tag says
    # which Node it is (the security floor is checked in
    # test_image_security_floor.py).
    assert re.search(r"^FROM node:\d+\.\d+\.\d+-bookworm-slim@sha256:[0-9a-f]{64}$", text, re.M)


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


def test_websocat_architecture_comes_from_the_image_not_a_default():
    """A classic `docker build` leaves TARGETARCH empty; the old amd64
    fallback would have shipped an x86_64 websocat in an arm64 image."""
    text = CDP_DOCKERFILE.read_text(encoding="utf-8")
    assert "dpkg --print-architecture" in text
    assert "TARGETARCH:-amd64" not in text


# ── router (ADR-088 router step): one playwright-mcp child per session ─────

def test_playwright_mcp_entrypoint_is_the_router():
    text = PW_DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["node", "/opt/router/router.mjs"]' in text
    assert "COPY router.mjs /opt/router/router.mjs" in text


def test_playwright_mcp_router_runs_under_init():
    # The router is PID 1 with many children: without init, exited children
    # stay zombies and SIGTERM is not forwarded.
    assert _service("playwright-mcp").get("init") is True


def test_router_sends_sessions_through_the_gateway_and_keeps_the_legacy_path():
    args = _service("playwright-mcp")["command"]
    own, child = args[: args.index("--")], args[args.index("--") + 1:]
    assert own[own.index("--gateway") + 1] == "http://cdp-browser:9300"
    # /mcp stays exactly where every existing MCP config points today.
    assert own[own.index("--legacy-cdp-endpoint") + 1] == "http://172.30.99.10:9223"
    # The router sets these per child; a fixed value would send every
    # session to the same address again.
    for flag in ("--cdp-endpoint", "--port", "--host"):
        assert flag not in child
    assert "--isolated" in child and "--output-dir" in child


def test_playwright_mcp_health_does_not_start_a_child():
    test = " ".join(_service("playwright-mcp")["healthcheck"]["test"])
    assert "/healthz" in test and "/mcp" not in test


def test_playwright_mcp_stays_off_the_host_network():
    """The host port for heads comes with the harness wiring, not here."""
    assert "ports" not in _service("playwright-mcp")


# SHA-256 of the 12 compromised eMudhra TLS intermediates + Sectigo
# cross-signs (Oct 2026; https://emudhra.we-have-a-problem.com/
# impacted-intermediates.json, usage "tls"). Chromium checks its built-in root
# store, not the Debian bundle, and the CRLSet block never reaches this
# headless browser — the managed policy is what makes it refuse them.
EMUDHRA_TLS_INTERMEDIATES = {
    "42C4ACCD12C4F59597925E1A08A7A90102608543809308D50EC427E118BA1493",
    "45A883F349AEDFD5F3A649B5498FFB121712BA8B448F3F2A91DBC5A766FCDDBE",
    "98B896625C0C230ADEEC3EB2CF6AAD5001ECF9156A68644E82374CE5833BC25B",
    "5F1DED4DD590AF74631290CF073B122A2AAC6A1FA479778A0BD43579BD832699",
    "E7568FE6E12331F4FD7DE61DA4C7559D0F8EB192A218D4741A3BA75184333838",
    "579480AEA166290BD3C643589E795FC0D0E54E86505D180F1DFF99EFD3199512",
    "0164D7CEAE5BB909C272AF151FF44B1177AE9B47D9424B94F1D9558C30934D19",
    "62B7F9868263F72F6911FC78CEAF72C1979158B7368BF648683BB2AA39B02E72",
    "CB6E15165AC802E9B3F993F16C25D41F4E81C21EBDE71AE1B012AC00BF9C7C3F",
    "7485B51C0ED192878FA1D2F948FB529AE1842CC5086C4B67F7BE5B2039DE9489",
    "432EC25F0D28DE9006D97BF752F17BBB2CD41F669933FCB072D9A1148D0261F3",
    "FD9D5C2DBBD4995AB69B8D50C439D9527B00529199C2FD01B2174EB58F07021F",
}


def test_cdp_browser_distrusts_the_compromised_emudhra_intermediates():
    import base64
    import hashlib
    import json

    from cryptography import x509

    policy_file = REPO_ROOT / "docker/cdp-browser/policies/emudhra-distrust.json"
    certs = json.loads(policy_file.read_text(encoding="utf-8"))["CADistrustedCertificates"]
    ders = [base64.b64decode(c) for c in certs]
    assert {hashlib.sha256(d).hexdigest().upper() for d in ders} == EMUDHRA_TLS_INTERMEDIATES
    # Only eMudhra intermediates — a wrong entry would break unrelated sites.
    for der in ders:
        subject = x509.load_der_x509_certificate(der).subject.rfc4514_string()
        assert "O=eMudhra" in subject, subject
    # Chromium reads managed policies from exactly this directory.
    assert (
        "COPY policies/emudhra-distrust.json "
        "/etc/chromium/policies/managed/emudhra-distrust.json"
    ) in CDP_DOCKERFILE.read_text(encoding="utf-8")
