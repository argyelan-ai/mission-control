"""The clone's .git/ is writable by the head (commits land there). The wrapper
runs git in that clone OUTSIDE the sandbox (fetch, worktree add, result
check), so nothing the head planted in .git/ may make those calls run code:
the wrapper rewrites the clone's config from scratch before every step and
passes hardening overrides on every call (review finding on PR #662).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.heads_host_helpers import (
    MC_HEAD,
    fake_harness,
    make_origin,
    mark_scratch,
    run_head,
    seed_clone,
    wait_phase,
    write_spec,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="host script is POSIX-only")

PUSH = (
    'echo change > change.txt && git add change.txt'
    ' && git -c user.name=t -c user.email=t@example.invalid commit -q -m "fix: change"'
    ' && git push -q -u origin "$(git branch --show-current)"'
)


def _load():
    loader = importlib.machinery.SourceFileLoader("mc_head_hardening", str(MC_HEAD))
    spec = importlib.util.spec_from_loader("mc_head_hardening", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def scratch(tmp_path: Path) -> dict:
    tmp_path = tmp_path.resolve()
    mc_home = tmp_path / "mc"
    so = mc_home / "heads" / "scratch-origin"
    so.mkdir(parents=True)
    origin, _ = make_origin(so, "probe")
    full_name = "scratch/probe"
    clone = seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)
    return {"mc_home": mc_home, "origin": origin, "full_name": full_name, "clone": clone, "tmp": tmp_path}


def _evil_hooks(tmp: Path, marker: Path) -> Path:
    """A hooks dir whose every hook writes the marker."""
    hdir = tmp / "evil-hooks"
    hdir.mkdir(exist_ok=True)
    for name in ("post-checkout", "reference-transaction", "post-merge", "pre-auto-gc", "post-index-change"):
        h = hdir / name
        h.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        h.chmod(0o755)
    return hdir


def _evil_script(tmp: Path, marker: Path, name: str) -> Path:
    p = tmp / name
    p.write_text(f"#!/bin/sh\necho {name} >> {marker}\ncat\n")
    p.chmod(0o755)
    return p


def _plant(clone: Path, tmp: Path, marker: Path) -> None:
    """What a head can write into the clone's .git/ during a run."""
    git = ["git", "-C", str(clone), "config"]
    subprocess.run([*git, "core.hooksPath", str(_evil_hooks(tmp, marker))], check=True)
    subprocess.run([*git, "core.fsmonitor", str(_evil_script(tmp, marker, "fsmonitor"))], check=True)
    smudge = _evil_script(tmp, marker, "smudge")
    subprocess.run([*git, "filter.evil.smudge", str(smudge)], check=True)
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "attributes").write_text("* filter=evil\n")


def _extra(harness: Path) -> dict:
    return {"MC_HEAD_BIN_OMP": str(harness), "MC_HEAD_BIN_CLAUDE": str(harness)}


def test_planted_clone_config_does_not_run_at_the_next_start(scratch):
    """A previous head planted hooks, an fsmonitor and a filter driver in
    the clone config: the next start (fetch + worktree add, unsandboxed)
    must not run any of them."""
    mc_home, clone, tmp = scratch["mc_home"], scratch["clone"], scratch["tmp"]
    marker = tmp / "PWNED"
    _plant(clone, tmp, marker)
    # control: the planted config really fires on a plain git call
    ctl = tmp / "ctl-wt"
    subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", str(ctl), "origin/main"],
                   capture_output=True, check=True)
    assert marker.exists(), "control failed: planted config does not fire"
    subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force", str(ctl)], capture_output=True)
    marker.unlink()

    harness = fake_harness(tmp, "true")
    run_id = write_spec(mc_home, repo_full_name=scratch["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] is None, status
    assert not marker.exists(), marker.read_text()
    cfg = (clone / ".git" / "config").read_text()
    assert "evil" not in cfg and "fsmonitor" not in cfg
    assert str(scratch["origin"]) in cfg  # the origin survives the rewrite


def test_config_planted_during_the_run_does_not_run_in_the_result_check(scratch):
    """The head pushes, then plants hooks: the wrapper's result check after
    the run (unsandboxed) must not run them — and still sees the push."""
    mc_home, clone, tmp = scratch["mc_home"], scratch["clone"], scratch["tmp"]
    marker = tmp / "PWNED"
    hooks = _evil_hooks(tmp, marker)
    fsm = _evil_script(tmp, marker, "fsmonitor")
    body = (
        f"{PUSH}\n"
        f"git config core.hooksPath {hooks}\n"
        f"git config core.fsmonitor {fsm}\n"
    )
    harness = fake_harness(tmp, body)
    run_id = write_spec(mc_home, repo_full_name=scratch["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["exit_code"] == 0, status
    assert status["scratch_branch_pushed"] is True
    assert not marker.exists(), marker.read_text()


def test_commondir_redirect_is_removed(scratch):
    """.git/commondir would point git at a config the head wrote elsewhere."""
    mc_home, clone, tmp = scratch["mc_home"], scratch["clone"], scratch["tmp"]
    marker = tmp / "PWNED"
    evil = tmp / "evil-common"
    subprocess.run(["cp", "-R", str(clone / ".git"), str(evil)], check=True)
    subprocess.run(["git", "--git-dir", str(evil), "config", "core.hooksPath", str(_evil_hooks(tmp, marker))],
                   check=True)
    (clone / ".git" / "commondir").write_text(str(evil) + "\n")
    harness = fake_harness(tmp, "true")
    run_id = write_spec(mc_home, repo_full_name=scratch["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    wait_phase(mc_home, run_id, "exited")
    assert not (clone / ".git" / "commondir").exists()
    assert not marker.exists()


@pytest.mark.parametrize("url", [
    "ext::sh -c touch% {marker}",
    "https://github.com/someone/else.git",
    "ssh://attacker.invalid/x.git",
])
def test_untrusted_origin_url_is_refused(scratch, url):
    mc_home, clone, tmp = scratch["mc_home"], scratch["clone"], scratch["tmp"]
    marker = tmp / "PWNED"
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", url.format(marker=marker)], check=True)
    harness = fake_harness(tmp, "true")
    run_id = write_spec(mc_home, repo_full_name=scratch["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] == "prepare_failed", status
    assert "origin URL not allowed" in (status.get("detail") or ""), status
    assert not marker.exists()


@pytest.mark.parametrize("full_name,url,ok", [
    ("owner/real", "https://github.com/owner/real.git", True),
    ("owner/real", "https://github.com/Owner/Real", True),
    ("owner/real", "git@github.com:owner/real.git", True),
    ("owner/real", "ssh://git@github.com/owner/real.git", True),
    ("owner/real", "https://github.com/owner/other.git", False),
    ("owner/real", "/abs/local/path.git", False),          # real repo: GitHub only
    ("owner/real", "https://github.com/owner/real.git/../x", False),
    ("scratch/probe", "/abs/local/path.git", True),        # scratch: local paths too
    ("scratch/probe", "file:///abs/local/path.git", True),
    ("scratch/probe", "ext::sh -c id", False),
    ("scratch/probe", "relative/path.git", False),
    ("scratch/probe", "/abs/with\nnewline", False),
])
def test_origin_url_allowlist(scratch, monkeypatch, full_name, url, ok):
    monkeypatch.setenv("MC_HOME", str(scratch["mc_home"]))
    mod = _load()
    assert mod.origin_url_allowed(full_name, url) is ok


def test_include_in_clone_config_is_not_followed(scratch, monkeypatch):
    """remote.origin.url only via an [include] must not count (--no-includes)."""
    monkeypatch.setenv("MC_HOME", str(scratch["mc_home"]))
    mod = _load()
    cfg = scratch["clone"] / ".git" / "config"
    inc = scratch["tmp"] / "inc.cfg"
    inc.write_text(f'[remote "origin"]\n\turl = {scratch["origin"]}\n')
    subprocess.run(["git", "-C", str(scratch["clone"]), "remote", "remove", "origin"], check=True)
    with cfg.open("a") as fh:
        fh.write(f"[include]\n\tpath = {inc}\n")
    assert mod.scratch_origin({"repo_full_name": scratch["full_name"]}) is None


def test_every_wrapper_git_call_carries_the_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MC_HOME", str(tmp_path / "mc"))
    mod = _load()
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod._git(["status"])
    argv, env = seen["argv"], seen["env"]
    assert argv[0] == "git" and argv[-1] == "status"
    joined = " ".join(argv)
    assert "core.fsmonitor=false" in joined
    assert f"core.hooksPath={mod.hooks_dir()}" in joined
    assert "protocol.ext.allow=never" in joined
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
