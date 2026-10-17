"""install.sh pins code and images to the same release.

The one-liner used to clone ``main`` while pulling ``:latest`` images, which
only release tags publish — today's compose file on top of the last
release's code (a worker service whose module the old image lacks, crashing
forever). install.sh now resolves the latest release tag, clones exactly
that tag and pins ``MC_IMAGE_TAG`` to it.

These tests source install.sh with ``MC_INSTALL_LIB_ONLY=1`` and drive the
helpers in a real bash. The network is replaced by ``file://`` URLs and a
local git repository, so they run offline and deterministic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(snippet: str, *, cwd: Path | None = None, env: dict[str, str] | None = None):
    full_env = {**os.environ, "MC_INSTALL_LIB_ONLY": "1", **(env or {})}
    script = f'set -euo pipefail; . "{INSTALL_SH}"; {snippet}'
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=full_env, capture_output=True, text=True, timeout=60
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def tagged_repo(tmp_path: Path) -> Path:
    """A repository with release tags, a pre-release and a loose tag."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    versions = repo / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    for i, tag in enumerate(["v0.1.0", "v0.2.0", "v0.9.0", "v0.10.0"], start=1):
        (versions / f"{i:04d}_m.py").write_text(f'revision: str = "{i:04d}"\n')
        (repo / "VERSION").write_text(tag)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        _git(repo, "tag", tag)
    (repo / "VERSION").write_text("unreleased")
    _git(repo, "commit", "-qam", "main moves on")
    _git(repo, "tag", "v0.11.0-rc1")   # pre-release: never picked
    _git(repo, "tag", "v1.5")          # loose tag: never picked
    return repo


# ── pure helpers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "tag,ok",
    [("v0.2.0", True), ("v10.20.30", True), ("0.2.0", False), ("v0.5", False),
     ("v1.0.0-rc1", False), ("", False), ("v1.0.0\nv2.0.0", False)],
)
def test_release_tag_shape(tag, ok):
    assert (_run(f"mc_is_release_tag {json.dumps(tag)}").returncode == 0) is ok


def test_tag_from_pretty_and_minified_json():
    body = {"url": "x", "tag_name": "v0.2.0", "name": "v0.2.0 — something", "assets": []}
    pretty = json.dumps(body, indent=2)
    minified = json.dumps(body, separators=(",", ":"))
    for text in (pretty, minified):
        r = _run(f"printf '%s' {json.dumps(text)} | mc_tag_from_release_json")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "v0.2.0"


@pytest.mark.parametrize(
    "a,b,gt",
    [("v0.10.0", "v0.9.0", True), ("v0.9.0", "v0.10.0", False), ("v1.0.0", "v0.99.99", True),
     ("v0.2.0", "v0.2.0", False), ("v0.2.1", "v0.2.0", True), ("v0.2.08", "v0.2.9", False)],
)
def test_version_compare_is_numeric(a, b, gt):
    assert (_run(f"mc_version_gt {a} {b}").returncode == 0) is gt


def test_highest_release_tag_ignores_prereleases_and_loose_tags():
    tags = "v0.1.0\\nv0.10.0\\nv0.9.0\\nv0.11.0-rc1\\nv1.5\\nnot-a-tag\\n"
    r = _run(f"printf '{tags}' | mc_highest_release_tag")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "v0.10.0"
    assert _run("printf 'v1.5\\nfoo\\n' | mc_highest_release_tag").returncode != 0


def test_image_tag_drops_the_v_like_release_yml():
    # release.yml: docker/metadata-action type=semver,pattern={{version}} → "0.2.0"
    assert _run("mc_image_tag v0.2.0").stdout.strip() == "0.2.0"
    release_yml = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "type=semver,pattern={{version}}" in release_yml


@pytest.mark.parametrize(
    "given,want", [("stable", "stable"), ("release", "stable"), ("edge", "edge"), ("main", "edge")]
)
def test_channel_aliases(given, want):
    assert _run(f"mc_normalize_channel {given}").stdout.strip() == want


def test_unknown_channel_is_rejected():
    assert _run("mc_normalize_channel nightly").returncode != 0


# ── latest release: API first, git tags as fallback ─────────────────────────

def test_latest_release_from_api(tmp_path: Path, tagged_repo: Path):
    api = tmp_path / "latest.json"
    api.write_text(json.dumps({"tag_name": "v0.2.0"}, indent=2))
    # API wins even though the repo has a higher tag: the API knows which tag
    # is a *published release*, a bare tag may not have images yet.
    r = _run("mc_latest_release_tag", env={"MC_RELEASE_API": api.as_uri(), "MC_REPO_URL": str(tagged_repo)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "v0.2.0"


def test_rate_limited_api_falls_back_to_git_tags(tmp_path: Path, tagged_repo: Path):
    # A rate-limited GitHub answers 403 with a JSON message and no tag_name;
    # a body without a usable tag must fall through to the git fallback.
    api = tmp_path / "ratelimit.json"
    api.write_text(json.dumps({"message": "API rate limit exceeded for 203.0.113.7."}))
    r = _run("mc_latest_release_tag", env={"MC_RELEASE_API": api.as_uri(), "MC_REPO_URL": str(tagged_repo)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "v0.10.0"
    assert "falling back" in r.stderr


def test_unreachable_api_falls_back_to_git_tags(tmp_path: Path, tagged_repo: Path):
    missing = (tmp_path / "nope.json").as_uri()
    r = _run("mc_latest_release_tag", env={"MC_RELEASE_API": missing, "MC_REPO_URL": str(tagged_repo)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "v0.10.0"


def test_fully_offline_fails_instead_of_guessing(tmp_path: Path):
    r = _run(
        "mc_latest_release_tag",
        env={"MC_RELEASE_API": (tmp_path / "nope.json").as_uri(), "MC_REPO_URL": str(tmp_path / "no-repo")},
    )
    assert r.returncode != 0
    assert r.stdout.strip() == ""


# ── the clone the installer makes, and what it pins ─────────────────────────

def test_tag_clone_is_detected_and_pins_images(tmp_path: Path, tagged_repo: Path):
    dest = tmp_path / "install"
    subprocess.run(
        ["git", "-c", "advice.detachedHead=false", "clone", "-q", "--depth", "1",
         "--branch", "v0.2.0", str(tagged_repo), str(dest)],
        check=True,
    )
    assert (dest / "VERSION").read_text() == "v0.2.0"   # code of the release, not main
    (dest / ".env").write_text("# MC_IMAGE_TAG=latest\nMC_IMAGE_TAG=latest\nOTHER=1\n")

    r = _run('t="$(mc_current_release_tag)"; mc_write_pins stable "$t"; echo "$t"', cwd=dest)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "v0.2.0"
    env = (dest / ".env").read_text().splitlines()
    assert "MC_CHANNEL=stable" in env
    assert "MC_IMAGE_TAG=0.2.0" in env and "MC_IMAGE_TAG=latest" not in env
    assert "MC_AGENT_IMAGE_TAG=0.2.0" in env
    assert "OTHER=1" in env and "# MC_IMAGE_TAG=latest" in env

    r = _run("mc_write_pins edge", cwd=dest)
    assert r.returncode == 0, r.stderr
    env = (dest / ".env").read_text().splitlines()
    assert "MC_CHANNEL=edge" in env and "MC_IMAGE_TAG=edge" in env
    assert not any(line.startswith("MC_AGENT_IMAGE_TAG=") for line in env)


def test_branch_checkout_is_not_a_release(tagged_repo: Path):
    assert _run("mc_current_release_tag", cwd=tagged_repo).returncode != 0


def test_schema_guard_knows_which_release_contains_a_revision(tagged_repo: Path):
    # 0002 arrived with v0.2.0; 0004 only with v0.10.0.
    assert _run("mc_ref_has_revision refs/tags/v0.2.0 0002", cwd=tagged_repo).returncode == 0
    assert _run("mc_ref_has_revision refs/tags/v0.2.0 0004", cwd=tagged_repo).returncode != 0
    assert _run("mc_ref_has_revision refs/tags/v0.10.0 0004", cwd=tagged_repo).returncode == 0
    # Never interpolate junk into the grep pattern.
    assert _run("mc_ref_has_revision refs/tags/v0.10.0 '.*'", cwd=tagged_repo).returncode != 0


def test_the_real_migration_chain_matches_the_guard_pattern():
    """The guard greps `revision = "…"` lines; if the chain's spelling ever
    drifts away from that, every legacy update would be refused."""
    import re

    head = _git(ROOT, "rev-parse", "HEAD")
    revs = []
    for f in (ROOT / "backend" / "alembic" / "versions").glob("*.py"):
        m = re.search(r"""^revision(?::\s*str)?\s*=\s*['"]([^'"]+)['"]""", f.read_text(), re.M)
        if m:
            revs.append(m.group(1))
    assert len(revs) > 100   # the chain is really there
    # Every revision committed at HEAD must be findable — sample a spread of
    # them (all would be ~200 subprocesses).
    for rev in sorted(revs)[:: max(1, len(revs) // 12)] + [sorted(revs)[-1]]:
        r = _run(f"mc_ref_has_revision {head} {rev}", cwd=ROOT)
        assert r.returncode == 0, (rev, r.stderr)


def test_lib_only_mode_does_not_run_the_installer():
    r = _run("echo loaded")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "loaded"
    assert "Mission Control installer" not in r.stdout + r.stderr
