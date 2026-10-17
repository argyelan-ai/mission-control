"""release.yml: verify first, tag second — and only strict release tags.

A broken image must never land under `latest`. The workflow therefore pushes
every image only as ``sha-<commit>``, verifies it, and a separate ``promote``
job (needs: all images) re-tags the verified manifests. Which tags a ref is
promoted to is decided by ``scripts/release-image-tags.sh`` — tested here
directly, plus text-level checks that the workflow is wired that way (no
YAML parser on purpose: the CI lane runs this with bare pytest).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release-image-tags.sh"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _tags(ref: str) -> list[str]:
    r = subprocess.run(["bash", str(SCRIPT), ref], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


@pytest.mark.parametrize(
    "ref,want",
    [
        ("refs/tags/v0.3.0", ["0.3.0", "0.3", "latest"]),
        ("refs/tags/v10.20.30", ["10.20.30", "10.20", "latest"]),
        ("refs/tags/v1.0.0-rc1", ["1.0.0-rc1"]),            # pre-release: never latest / X.Y
        ("refs/tags/v1.0.0-beta.2", ["1.0.0-beta.2"]),
        ("refs/tags/v0.5", []),                            # the old loose tag
        ("refs/tags/0.3.0", []),
        ("refs/tags/v0.3.0x", []),
        ("refs/heads/main", []),                           # manual run from a branch
        ("", []),
    ],
)
def test_release_image_tags(ref, want):
    assert _tags(ref) == want


def _job(text: str, name: str) -> str:
    """The text of one top-level job (2-space indented key under jobs:)."""
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][\w-]*:\n|\Z)", text, re.M | re.S)
    assert m, f"job {name} missing"
    return m.group(1)


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_trigger_is_strict_release_tags_only():
    text = _code(RELEASE_YML.read_text())
    on = text.split("\njobs:", 1)[0]
    tags = re.findall(r'^\s+- "([^"]+)"$', on, re.M)
    assert tags == ["v[0-9]+.[0-9]+.[0-9]+", "v[0-9]+.[0-9]+.[0-9]+-*"]
    assert '"v*"' not in on


def test_build_pushes_only_the_commit_tag():
    images = _code(_job(RELEASE_YML.read_text(), "images"))
    meta_tags = images.split("tags: |", 1)[1].split("\n\n", 1)[0]
    assert meta_tags.split() == ["type=sha,format=long,prefix=sha-"]
    assert not re.search(r"(?<!ubuntu-)latest", images)
    assert "semver" not in images
    assert "imagetools create" not in images
    # the verify step runs in this job, after the push
    assert images.index("build-push-action") < images.index("app/worker.py")


def test_promote_runs_only_after_all_images_and_only_for_tags():
    promote = _code(_job(RELEASE_YML.read_text(), "promote"))
    assert re.search(r"^\s+needs: images$", promote, re.M)
    assert "startsWith(github.ref, 'refs/tags/v')" in promote
    assert "scripts/release-image-tags.sh" in promote
    assert "docker buildx imagetools create" in promote
    assert ':sha-$GITHUB_SHA"' in promote
    assert "build-push-action" not in promote      # re-tag, never rebuild
    for image in ("mc-backend", "mc-frontend", "mc-claude-agent", "mc-agent-base"):
        assert image in promote
