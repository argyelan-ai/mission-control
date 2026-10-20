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
    # release.yml promotes to the tags scripts/release-image-tags.sh prints;
    # its first line (X.Y.Z) must be exactly what install.sh pins.
    assert _run("mc_image_tag v0.2.0").stdout.strip() == "0.2.0"
    promoted = subprocess.run(
        ["bash", str(ROOT / "scripts" / "release-image-tags.sh"), "refs/tags/v0.2.0"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert promoted[0] == "0.2.0"


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


def test_lib_only_mode_does_not_run_the_installer():
    r = _run("echo loaded")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "loaded"
    assert "Mission Control installer" not in r.stdout + r.stderr


# ── .env is written in place (bind mounts follow the inode) ─────────────────

def test_env_writes_keep_the_inode(tmp_path: Path):
    """backend/worker bind-mount .env as a single file; a replaced file (new
    inode via mv / sed -i) leaves the running containers on the OLD content."""
    env = tmp_path / ".env"
    env.write_text("A=1\nMC_IMAGE_TAG=latest\n")
    before = env.stat().st_ino
    r = _run(
        "mc_env_set A 2; mc_env_set NEW 'x&y|z'; mc_env_unset MC_IMAGE_TAG; "
        "mc_write_pins stable v0.2.0; mc_write_pins edge",
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert env.stat().st_ino == before
    lines = env.read_text().splitlines()
    assert "A=2" in lines and "NEW=x&y|z" in lines
    assert "MC_IMAGE_TAG=edge" in lines and "A=1" not in lines
    assert not list(tmp_path.glob(".env.tmp*"))


def test_installer_has_no_inode_replacing_env_writes():
    text = INSTALL_SH.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "mv .env.tmp .env" not in code
    assert "sed_i" not in code and "sed -i" not in code


# ── pick the newest release WITH published images ───────────────────────────

def _stub_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "manifest inspect" ]; then\n'
        '  tag="${3##*:}"\n'
        '  for t in ${STUB_PUBLISHED:-}; do [ "$t" = "$tag" ] && exit 0; done\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = ps ]; then printf "%b" "${STUB_PS:-}"; exit 0; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bin_dir


def test_sort_releases_desc():
    r = _run("printf 'v0.9.0\\nv0.10.0\\nv0.5\\nv0.1.0\\nv1.0.0-rc1\\n' | mc_sort_releases_desc")
    assert r.stdout.split() == ["v0.10.0", "v0.9.0", "v0.1.0"]


@pytest.mark.parametrize(
    "published,want,warned",
    [("0.10.0", "v0.10.0", False), ("0.9.0 0.2.0", "v0.9.0", True), ("", "v0.10.0", True)],
)
def test_pick_release_skips_a_release_without_images(tmp_path, tagged_repo, published, want, warned):
    api = tmp_path / "latest.json"
    api.write_text(json.dumps({"tag_name": "v0.10.0"}))
    bin_dir = _stub_docker(tmp_path)
    r = _run(
        "mc_pick_release",
        env={"MC_RELEASE_API": api.as_uri(), "MC_REPO_URL": str(tagged_repo),
             "STUB_PUBLISHED": published, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == want
    assert (r.stderr != "") is warned


# ── never a second stack next to a running one ──────────────────────────────

def _guard(tmp_path: Path, ps: str, project_env: str = ""):
    d = tmp_path / "mission-control"
    d.mkdir(exist_ok=True)
    (d / ".env").write_text(project_env)
    bin_dir = _stub_docker(tmp_path)
    return d, _run(
        "mc_guard_single_stack && echo ok",
        cwd=d,
        env={"STUB_PS": ps, "PATH": f"{bin_dir}:{os.environ['PATH']}", "COMPOSE_PROJECT_NAME": ""},
    )


def test_guard_allows_no_stack_and_this_directorys_stack(tmp_path):
    _d, r = _guard(tmp_path, "")
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
    d = tmp_path / "mission-control"
    _d, r = _guard(tmp_path, f"mission-control|{d.resolve()}|ghcr.io/argyelan-ai/mc-backend:edge\\n")
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_guard_refuses_same_project_from_another_directory(tmp_path):
    _d, r = _guard(tmp_path, "mission-control|/srv/other/mission-control|ghcr.io/argyelan-ai/mc-backend:latest\\n")
    assert r.returncode != 0
    assert "already running from /srv/other/mission-control" in r.stderr


def test_guard_refuses_a_second_stack_with_another_name(tmp_path):
    _d, r = _guard(tmp_path, "prod|/srv/prod|ghcr.io/argyelan-ai/mc-backend:0.2.0\\n")
    assert r.returncode != 0
    assert "Another Mission Control stack ('prod'" in r.stderr


def test_guard_uses_the_project_name_from_env_and_ignores_other_apps(tmp_path):
    # .env renames this install's project, so the running "mission-control"
    # stack is someone else's and must be refused.
    _d, r = _guard(tmp_path, "mission-control|/srv/x|ghcr.io/argyelan-ai/mc-backend:latest\\n",
                   "COMPOSE_PROJECT_NAME=MC-Test\n")
    assert r.returncode != 0 and "('mission-control'" in r.stderr
    # a different app that happens to have a "backend" service is not MC
    _d, r = _guard(tmp_path, "shop|/srv/shop|example/shop-api:1\\n")
    assert r.returncode == 0, r.stderr


def test_compose_project_name_resolution(tmp_path):
    d = tmp_path / "My Mission.Control"
    d.mkdir()
    (d / ".env").write_text("")
    r = _run("mc_compose_project", cwd=d, env={"COMPOSE_PROJECT_NAME": ""})
    assert r.stdout.strip() == "mymissioncontrol"
    (d / ".env").write_text("COMPOSE_PROJECT_NAME=alpha\n")
    assert _run("mc_compose_project", cwd=d, env={"COMPOSE_PROJECT_NAME": ""}).stdout.strip() == "alpha"
    assert _run("mc_compose_project", cwd=d, env={"COMPOSE_PROJECT_NAME": "beta"}).stdout.strip() == "beta"


def test_every_boot_goes_through_the_stack_guard():
    text = INSTALL_SH.read_text()
    body = text.split("boot_stack() {", 1)[1].split("\n}", 1)[0]
    assert body.lstrip().startswith('local channel="$1"\n  mc_guard_single_stack')


# ── --update backfills new .env keys ────────────────────────────────────────

def test_update_runs_setup_sh_after_the_code_switch():
    text = INSTALL_SH.read_text()
    block = text.split('if [ "$UPDATE" = 1 ]; then', 1)[1].split("\n  exit 0\nfi", 1)[0]
    code = [l.strip() for l in block.splitlines() if not l.lstrip().startswith("#")]
    assert "./setup.sh" in code, "update path must backfill .env via setup.sh"
    assert code.index("./setup.sh") < code.index('boot_stack "$CHANNEL"')
    last_switch = max(i for i, l in enumerate(code) if "with_fleet_carried" in l)
    assert last_switch < code.index("./setup.sh")


# ── the closing lines lead to the setup wizard ──────────────────────────────

def _closing_lines() -> str:
    text = INSTALL_SH.read_text()
    return text[text.index('MC_URL=$(mc_access_url .env)'):]


def test_installer_ends_at_the_setup_wizard():
    end = _closing_lines()
    # every line that shows or opens the address points at the wizard —
    # with and without a registered admin, the browser and the SSH tunnel
    shown = [l for l in end.splitlines() if "$MC_URL" in l and ("echo" in l or "open " in l)]
    assert len(shown) == 4, shown
    for l in shown:
        assert "$MC_URL/setup" in l, l
    assert "http://localhost:8080/setup" in end
    # the wizard covers the demo board and the first agent — no side doors
    assert "demo-seed" not in end
    assert "first-agent.md" not in end
