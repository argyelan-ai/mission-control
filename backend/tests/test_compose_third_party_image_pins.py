"""docker-compose.yml pins third-party images instead of tracking :latest.

qdrant and livekit-server were on `:latest`. A `docker compose pull` (or any
recreate that re-resolves tags) can silently swap the running binary for
whatever the registry publishes that day — against data already on disk
(mc_qdrant_data) this is exactly the risk install.sh's release pinning
(test_install_release_pinning.py) already protects the backend/worker
images from. This test extends the same guarantee to the two third-party
services compose starts directly by image name.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"

PINNED_SERVICES = {
    "qdrant": "qdrant/qdrant",
    "livekit": "livekit/livekit-server",
}


def _services() -> dict:
    data = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    return data["services"]


def test_no_latest_tag_anywhere_in_the_compose_file():
    # Belt-and-suspenders textual check: catches a reintroduced ":latest"
    # even inside a line the YAML-level check below doesn't visit.
    code = "\n".join(
        line for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert re.search(r"image:\s*\S+:latest\b", code) is None


def test_qdrant_and_livekit_are_pinned_to_a_real_version():
    services = _services()
    for name, repo in PINNED_SERVICES.items():
        image = services[name]["image"]
        assert image.startswith(f"{repo}:"), image
        tag = image.split(":", 1)[1]
        assert tag != "latest"
        assert re.match(r"^v?\d+\.\d+(\.\d+)?$", tag), f"{name} tag {tag!r} doesn't look like a release"
