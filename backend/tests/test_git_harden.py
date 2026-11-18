"""git in agent-writable trees must not run programs named by the tree's
own config (fsmonitor, hooks, filters, diff/merge drivers, transports,
credential helpers).

Every test plants such a program in a real tmp repo — a script that only
touches a marker file — and drives the real backend code path with the
real git binary. The marker must never appear. Each case first proves the
plant is live (unhardened git DOES create the marker), so a test can't
pass because the setup was wrong.
"""
from __future__ import annotations

import asyncio
import http.server
import os
import subprocess
import threading
from pathlib import Path

import pytest

from app.services import chat_actions, git_harden, workspace_diff as wd
from app.services.git_harden import hardened_env, run_git
from app.services.git_service import GitService

_OLD = (946684800, 946684800)  # 2000-01-01: forces git to re-hash the file


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _plain_git(*args: str, cwd: Path, timeout: float = 10) -> None:
    """Unhardened git, the way the backend called it before."""
    try:
        subprocess.run(
            ["git", "--no-pager", "-c", "safe.directory=*", *args],
            cwd=str(cwd), capture_output=True, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        pass


class Plant:
    def __init__(self, root: Path):
        self.marker = root / "EXECUTED"
        self.script = root / "payload.sh"
        # Passes stdin through (works as filter / textconv input), fails as
        # anything else — what matters is only that it ran.
        self.script.write_text(f'#!/bin/sh\ntouch "{self.marker}"\ncat "$@" 2>/dev/null\n')
        self.script.chmod(0o755)
        self.hooks = root / "hooks"
        self.hooks.mkdir()
        for name in ("post-checkout", "pre-commit", "pre-push", "reference-transaction"):
            hook = self.hooks / name
            hook.write_text(f'#!/bin/sh\ntouch "{self.marker}"\n')
            hook.chmod(0o755)

    def fired(self) -> bool:
        return self.marker.exists()

    def reset(self) -> None:
        self.marker.unlink(missing_ok=True)


@pytest.fixture
def plant(tmp_path) -> Plant:
    return Plant(tmp_path)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", "-b", "main", cwd=r)
    _git("config", "user.email", "t@mc.local", cwd=r)
    _git("config", "user.name", "T", cwd=r)
    (r / ".gitattributes").write_text("*.txt filter=agentf diff=agentd merge=agentm\n")
    (r / "a.txt").write_text("hi\n")
    _git("add", "-A", cwd=r)
    _git("commit", "-qm", "init", cwd=r)
    return r


def _dirty(repo: Path) -> None:
    (repo / "a.txt").write_text("ho\n")  # same size: git must hash it
    os.utime(repo / "a.txt", _OLD)


def _plant_config(repo: Path, plant: Plant, key: str) -> None:
    value = str(plant.hooks) if key == "core.hooksPath" else str(plant.script)
    _git("config", key, value, cwd=repo)


def _assert_live_then_blocked(plant, unhardened, hardened) -> None:
    unhardened()
    assert plant.fired(), "setup broken: the planted program never ran unhardened"
    plant.reset()
    hardened()
    assert not plant.fired(), "hardened git ran a program from the repo's config"


# ── Changes panel (workspace_diff) ──────────────────────────────────────────


@pytest.mark.parametrize("key", [
    "core.fsmonitor",
    "filter.agentf.clean",
    "filter.agentf.process",
    "diff.agentd.textconv",
    "diff.agentd.command",
    "diff.external",
])
def test_worktree_diff_runs_nothing_from_repo_config(repo, plant, key):
    _plant_config(repo, plant, key)

    def unhardened():
        _dirty(repo)
        _plain_git("status", "--porcelain", cwd=repo, timeout=5)
        _plain_git("diff", "HEAD", cwd=repo, timeout=5)

    def hardened():
        _dirty(repo)
        result = wd.workspace_diff(repo, scope="worktree")
        assert [f["filename"] for f in result["files"]] == ["a.txt"]

    _assert_live_then_blocked(plant, unhardened, hardened)


# git show never uses an external diff on its own; textconv it does.
@pytest.mark.parametrize("key", ["diff.agentd.textconv"])
def test_last_commit_diff_runs_nothing_from_repo_config(repo, plant, key):
    (repo / "a.txt").write_text("hi\nthere\n")
    _git("commit", "-qam", "second", cwd=repo)
    _plant_config(repo, plant, key)

    def hardened():
        result = wd.workspace_diff(repo, scope="last-commit")
        assert result["files"][0]["additions"] == 1

    _assert_live_then_blocked(
        plant, lambda: _plain_git("show", "HEAD", cwd=repo, timeout=5), hardened,
    )


def test_included_config_file_is_neutralised_too(repo, plant, tmp_path):
    inc = tmp_path / "included.cfg"
    inc.write_text(f"[core]\n\tfsmonitor = {plant.script}\n[filter \"agentf\"]\n\tclean = {plant.script}\n")
    _git("config", "include.path", str(inc), cwd=repo)

    def unhardened():
        _dirty(repo)
        _plain_git("status", "--porcelain", cwd=repo, timeout=5)

    def hardened():
        _dirty(repo)
        wd.workspace_diff(repo, scope="worktree")

    _assert_live_then_blocked(plant, unhardened, hardened)


def test_submodule_config_is_not_reached(repo, plant, tmp_path):
    """git diff/status in the parent runs status inside each submodule,
    under the submodule's OWN config — whose filter names the parent
    never lists."""
    src = tmp_path / "subsrc"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    _git("config", "user.email", "t@mc.local", cwd=src)
    _git("config", "user.name", "T", cwd=src)
    (src / ".gitattributes").write_text("*.txt filter=subf\n")
    (src / "x.txt").write_text("1\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "s", cwd=src)
    _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sub", cwd=repo)
    _git("commit", "-qm", "sub", cwd=repo)
    sub = repo / "sub"
    _git("config", "filter.subf.clean", str(plant.script), cwd=sub)

    def dirty_sub():
        (sub / "x.txt").write_text("2\n")
        os.utime(sub / "x.txt", _OLD)

    def unhardened():
        dirty_sub()
        _plain_git("diff", "HEAD", cwd=repo, timeout=5)

    def hardened():
        dirty_sub()
        wd.workspace_diff(repo, scope="worktree")

    _assert_live_then_blocked(plant, unhardened, hardened)


# ── Chat file revert (chat_actions) ─────────────────────────────────────────


@pytest.mark.parametrize("key", ["filter.agentf.smudge", "core.hooksPath"])
def test_chat_revert_checkout_runs_nothing_from_repo_config(repo, plant, key):
    _plant_config(repo, plant, key)

    def unhardened():
        _dirty(repo)
        _plain_git("checkout", "HEAD", "--", "a.txt", cwd=repo)

    def hardened():
        _dirty(repo)
        res = chat_actions._git("checkout", "HEAD", "--", "a.txt", cwd=repo)
        assert res.returncode == 0
        assert (repo / "a.txt").read_text() == "hi\n"

    _assert_live_then_blocked(plant, unhardened, hardened)


def test_chat_commit_runs_no_repo_hooks(repo, plant):
    _plant_config(repo, plant, "core.hooksPath")

    def unhardened():
        (repo / "a.txt").write_text("one\n")
        _plain_git("commit", "-qam", "x", cwd=repo)

    def hardened():
        (repo / "a.txt").write_text("two\n")
        assert chat_actions._git("commit", "-qam", "y", cwd=repo).returncode == 0

    _assert_live_then_blocked(plant, unhardened, hardened)


def test_merge_driver_is_neutralised(repo, plant):
    _git("checkout", "-qb", "other", cwd=repo)
    (repo / "a.txt").write_text("other\n")
    _git("commit", "-qam", "other", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "a.txt").write_text("main\n")
    _git("commit", "-qam", "main", cwd=repo)
    _plant_config(repo, plant, "merge.agentm.driver")

    def unhardened():
        _plain_git("merge", "--no-commit", "other", cwd=repo)
        _git("merge", "--abort", cwd=repo)

    def hardened():
        run_git("merge", "--no-commit", "other", cwd=repo, capture_output=True)

    _assert_live_then_blocked(plant, unhardened, hardened)


# ── Task push / PR (git_service._run_cmd) ───────────────────────────────────


def _service() -> GitService:
    svc = GitService()

    async def _no_auth() -> None:
        return None

    svc._ensure_git_auth = _no_auth  # type: ignore[method-assign]
    return svc


def test_git_service_status_runs_no_fsmonitor(repo, plant):
    _plant_config(repo, plant, "core.fsmonitor")

    def hardened():
        asyncio.run(_service()._run_cmd("git", "status", "--porcelain", cwd=str(repo)))

    _assert_live_then_blocked(
        plant, lambda: _plain_git("status", cwd=repo, timeout=5), hardened,
    )


def test_git_service_push_ignores_repo_receive_pack_and_hooks(repo, plant, tmp_path):
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    _plant_config(repo, plant, "remote.origin.receivepack")
    _plant_config(repo, plant, "core.hooksPath")

    def hardened():
        asyncio.run(_service()._run_cmd("git", "push", "origin", "main", cwd=str(repo)))
        assert (bare / "refs" / "heads" / "main").exists()

    _assert_live_then_blocked(
        plant, lambda: _plain_git("push", "origin", "HEAD:refs/heads/probe", cwd=repo, timeout=5),
        hardened,
    )


@pytest.mark.parametrize("key", ["remote.origin.uploadpack", "core.sshCommand"])
def test_fetch_ignores_repo_transport_programs(repo, plant, tmp_path, key):
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    url = "ssh://agent.invalid/x" if key == "core.sshCommand" else str(bare)
    _git("remote", "add", "origin", url, cwd=repo)
    _plant_config(repo, plant, key)

    def hardened():
        try:
            asyncio.run(_service()._run_cmd("git", "fetch", "origin", cwd=str(repo)))
        except RuntimeError:
            pass  # ssh to .invalid fails — what matters is what ran

    _assert_live_then_blocked(
        plant, lambda: _plain_git("fetch", "origin", cwd=repo, timeout=5), hardened,
    )


def test_ext_transport_stays_blocked(repo, plant):
    _git("config", "protocol.ext.allow", "always", cwd=repo)
    _git("remote", "add", "origin", f"ext::{plant.script}", cwd=repo)

    def hardened():
        run_git("fetch", "origin", cwd=repo, capture_output=True, timeout=10)

    _assert_live_then_blocked(
        plant, lambda: _plain_git("fetch", "origin", cwd=repo, timeout=5), hardened,
    )


@pytest.fixture
def auth_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.mark.parametrize("scoped", [False, True])
def test_repo_credential_helper_never_runs(repo, plant, auth_server, scoped):
    """A repo-local helper would be handed the backend's GitHub token."""
    key = f"credential.{auth_server}.helper" if scoped else "credential.helper"
    _git("config", key, f"!{plant.script}", cwd=repo)

    def hardened():
        run_git("ls-remote", f"{auth_server}/x", cwd=repo, capture_output=True, timeout=10)

    _assert_live_then_blocked(
        plant, lambda: _plain_git("ls-remote", f"{auth_server}/x", cwd=repo), hardened,
    )


def test_trusted_credential_helpers_survive(repo, tmp_path):
    """The reset keeps the backend's own (global) helper — git_service
    pushes with it."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    _git("config", "credential.helper", "!evil", cwd=repo)
    env = hardened_env(repo, (), {**os.environ, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"})
    res = subprocess.run(
        ["git", "config", "--get-all", "credential.helper"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    # git keeps every value; the empty one is the reset point, only what
    # follows it is used.
    values = res.stdout.splitlines()
    assert values[values.index(""):] == ["", "store"]


def test_gh_child_git_gets_the_overrides(repo, plant):
    """gh runs git itself; the overrides travel in the environment."""
    _plant_config(repo, plant, "core.fsmonitor")
    env = hardened_env(repo)
    subprocess.run(["git", "-c", "safe.directory=*", "status"], cwd=repo, env=env, capture_output=True)
    assert not plant.fired()


# ── Vault ───────────────────────────────────────────────────────────────────


def test_vault_add_runs_no_repo_filter(repo, plant):
    from app.services.vault_git import VaultGit

    _plant_config(repo, plant, "filter.agentf.clean")

    def unhardened():
        (repo / "b.txt").write_text("x\n")
        _plain_git("add", "b.txt", cwd=repo)

    def hardened():
        (repo / "c.txt").write_text("y\n")
        VaultGit(repo).stage(repo / "c.txt")

    _assert_live_then_blocked(plant, unhardened, hardened)


# ── argv shape ──────────────────────────────────────────────────────────────


def test_flags_go_after_the_subcommand():
    assert git_harden.hardened_argv(["-C", "/x", "-c", "a=b", "diff", "HEAD"]) == [
        "git", "--no-pager", "-C", "/x", "-c", "a=b", "diff",
        "--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty", "HEAD",
    ]
    assert git_harden.hardened_argv(["push", "origin", "main"]) == [
        "git", "--no-pager", "push", "--receive-pack=git-receive-pack", "origin", "main",
    ]
    assert git_harden.hardened_argv(["rev-parse", "HEAD"]) == ["git", "--no-pager", "rev-parse", "HEAD"]
