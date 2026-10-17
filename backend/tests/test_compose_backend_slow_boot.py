"""The backend must get time to boot on slow hardware before it counts as unhealthy.

Fresh install of v0.3.1 on a Raspberry Pi (SD card): migrations plus the
first vector-store collections took longer than the ~60 s the backend
healthcheck allowed (no ``start_period``; interval 15 s x 3 retries).
``docker compose up`` then aborted with "dependency failed to start:
container ... backend ... is unhealthy" although the backend log already
showed "Application startup complete" a little later — frontend, caddy and
worker were never started and the installer stopped half way.
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _seconds(value: str) -> int:
    total = 0
    for amount, unit in re.findall(r"(\d+)([hms])", value):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def test_backend_healthcheck_allows_a_slow_first_boot():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    hc = compose["services"]["backend"]["healthcheck"]
    assert _seconds(str(hc.get("start_period", "0s"))) >= 300, (
        "backend healthcheck needs start_period >= 300s: a first boot on a "
        "Raspberry Pi runs past 60 s and compose aborts the whole stack"
    )


def test_installer_explains_a_slow_start_instead_of_a_bare_compose_error():
    script = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    body = script[script.index("boot_stack() {"):]
    body = body[: body.index("\n}\n")]
    assert "docker compose up -d ||" in body, "boot_stack must handle a failed first 'up'"
    assert "re-run" in body.lower(), "the failure text must say a re-run is safe"
