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
