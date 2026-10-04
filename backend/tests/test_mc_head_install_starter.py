"""``scripts/head/install-head-starter.sh`` — the ``--gc-apply`` flag (review
finding on PR #751: the flag was documented in two places
(docs/specs/head-launcher.md §13, the PR body) but the installer ignored
every argument, so the one way an operator is told to arm deletion did
nothing). Drives the real script against a temporary ``$HOME``/``$MC_HOME``;
no network, no real launchd load (that stays an explicit operator step the
script only prints)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "head" / "install-head-starter.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="installer is POSIX-only (sh, launchd)")


def _run(home: Path, mc_home: Path, *args: str) -> subprocess.CompletedProcess:
    (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["sh", str(INSTALLER), *args],
        env={"HOME": str(home), "MC_HOME": str(mc_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _plist(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / "com.mc.head-starter.plist"


def test_default_install_has_no_gc_apply_key(tmp_path: Path):
    home, mc_home = tmp_path / "home", tmp_path / "home" / ".mc"
    res = _run(home, mc_home)
    assert res.returncode == 0, res.stderr
    text = _plist(home).read_text()
    assert "<key>MC_HEAD_GC_APPLY</key>" not in text
    assert "dry-run only" in res.stdout and "ARMED" not in res.stdout
    assert subprocess.run(["plutil", "-lint", str(_plist(home))], capture_output=True).returncode == 0


def test_gc_apply_flag_arms_the_plist(tmp_path: Path):
    home, mc_home = tmp_path / "home", tmp_path / "home" / ".mc"
    res = _run(home, mc_home, "--gc-apply")
    assert res.returncode == 0, res.stderr
    text = _plist(home).read_text()
    assert "<key>MC_HEAD_GC_APPLY</key>" in text
    # The value sits on the line right after the key — a real plist reader
    # (plutil, loaded below) is the actual proof; this is just belt+braces.
    idx = text.index("<key>MC_HEAD_GC_APPLY</key>")
    assert "<string>1</string>" in text[idx: idx + 120]
    assert "ARMED" in res.stdout
    assert subprocess.run(["plutil", "-lint", str(_plist(home))], capture_output=True).returncode == 0


def test_gc_apply_env_var_is_what_mc_head_actually_reads(tmp_path: Path):
    """End-to-end tie-back: the exact key the installer writes is the exact
    key ``mc-head``'s own ``cmd_watch`` reads (``GC_APPLY_ENV``) — a
    regression guard against the two drifting apart under different names."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("mc_head_install_check", str(REPO_ROOT / "scripts" / "head" / "mc-head"))
    spec = importlib.util.spec_from_loader("mc_head_install_check", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    home, mc_home = tmp_path / "home", tmp_path / "home" / ".mc"
    _run(home, mc_home, "--gc-apply")
    text = _plist(home).read_text()
    assert f"<key>{mod.GC_APPLY_ENV}</key>" in text


def test_re_install_without_the_flag_disarms_a_previously_armed_plist(tmp_path: Path):
    home, mc_home = tmp_path / "home", tmp_path / "home" / ".mc"
    _run(home, mc_home, "--gc-apply")
    assert "<key>MC_HEAD_GC_APPLY</key>" in _plist(home).read_text()
    res = _run(home, mc_home)
    assert res.returncode == 0, res.stderr
    assert "<key>MC_HEAD_GC_APPLY</key>" not in _plist(home).read_text()


def test_unknown_flag_is_refused(tmp_path: Path):
    home, mc_home = tmp_path / "home", tmp_path / "home" / ".mc"
    res = _run(home, mc_home, "--bogus")
    assert res.returncode == 2
    assert "unknown argument" in res.stderr
