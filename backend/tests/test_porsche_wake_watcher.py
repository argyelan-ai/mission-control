"""The host-side wake watcher (scripts/porsche-wake-watcher.py) — round-7 L5.

Two promises pinned here:

* **Heartbeat:** every run touches ``<wake dir>/watcher.alive`` — also when no
  request waits — so the backend can tell a running helper from a missing one
  (``runtime_manager.WAKE_HELPER_HEARTBEAT_NAME``). The file is updated in
  place: a new directory entry would fire launchd's ``WatchPaths`` again.
* **No late wakes:** a request older than ``MAX_REQUEST_AGE_S`` is retired
  without waking. Live, a request from 15 Aug still lay in the folder; a
  freshly installed helper must not wake the box for it.

And the launchd plist points at the stable host checkout, never a worktree.

No real wake: the wake script is a tmp stub that only records its arguments.
RFC 5737 IPs + documentation MACs only — public repo.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services import runtime_manager

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHER_PATH = REPO_ROOT / "scripts" / "porsche-wake-watcher.py"
PLIST_PATH = REPO_ROOT / "scripts" / "launchd" / "com.mc.porsche-wake-watcher.plist"
_MAC = "00:00:5E:00:53:01"


def _load_watcher():
    spec = importlib.util.spec_from_file_location("porsche_wake_watcher", WATCHER_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    mod = _load_watcher()
    wake_dir = tmp_path / "wake-requests"
    calls = tmp_path / "calls.jsonl"
    stub = tmp_path / "wake_stub.py"
    stub.write_text(
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "WAKE_DIR", wake_dir)
    monkeypatch.setattr(mod, "HEARTBEAT", wake_dir / mod.HEARTBEAT_NAME)
    monkeypatch.setattr(mod, "WAKE_SCRIPT", stub)
    mod._calls = calls  # type: ignore[attr-defined]
    return mod


def _calls(mod) -> list[list[str]]:
    path: Path = mod._calls
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _request(wake_dir: Path, slug: str = "box", age_s: float = 0) -> Path:
    wake_dir.mkdir(parents=True, exist_ok=True)
    req = wake_dir / f"{slug}.request.json"
    req.write_text(json.dumps({"slug": slug, "mac": _MAC, "ip": "192.0.2.20",
                               "broadcast": "192.0.2.255"}), encoding="utf-8")
    if age_s:
        at = time.time() - age_s
        os.utime(req, (at, at))
    return req


def test_the_backend_and_the_watcher_agree_on_the_heartbeat_name(watcher):
    assert watcher.HEARTBEAT_NAME == runtime_manager.WAKE_HELPER_HEARTBEAT_NAME
    # The watcher beats every 30 s; the backend allows three misses.
    assert runtime_manager.WAKE_HELPER_HEARTBEAT_S >= 3 * 30


def test_a_run_without_requests_still_beats(watcher):
    """The quiet 30 s run is the one that proves the helper is alive."""
    assert watcher.main() == 0
    beat = watcher.WAKE_DIR / watcher.HEARTBEAT_NAME
    assert beat.exists()
    assert time.time() - beat.stat().st_mtime < 5
    assert _calls(watcher) == []


def test_the_heartbeat_is_updated_in_place(watcher):
    """No new directory entry per run — WatchPaths would fire the watcher again."""
    watcher.main()
    beat = watcher.WAKE_DIR / watcher.HEARTBEAT_NAME
    inode = beat.stat().st_ino
    old = time.time() - 300
    os.utime(beat, (old, old))
    watcher.main()
    assert beat.stat().st_ino == inode
    assert time.time() - beat.stat().st_mtime < 5
    assert sorted(p.name for p in watcher.WAKE_DIR.iterdir()) == [watcher.HEARTBEAT_NAME]


def test_a_fresh_request_is_woken_and_retired(watcher):
    req = _request(watcher.WAKE_DIR, age_s=10)
    watcher.main()
    assert len(_calls(watcher)) == 1
    assert "--mac" in _calls(watcher)[0] and _MAC in _calls(watcher)[0]
    assert not req.exists()
    status = json.loads((watcher.WAKE_DIR / "box.status.json").read_text())
    assert status["ok"] is True


def test_an_old_request_is_retired_without_waking(watcher):
    """The 15-Aug leftover: installing the helper must not wake the box at once."""
    req = _request(watcher.WAKE_DIR, slug="leftover", age_s=watcher.MAX_REQUEST_AGE_S + 60)
    watcher.main()
    assert _calls(watcher) == []
    assert not req.exists()
    assert (watcher.WAKE_DIR / "leftover.done.json").exists()
    status = json.loads((watcher.WAKE_DIR / "leftover.status.json").read_text())
    assert status["ok"] is False
    assert "expired" in status["detail"]


def test_an_old_request_does_not_block_a_fresh_one(watcher):
    _request(watcher.WAKE_DIR, slug="leftover", age_s=watcher.MAX_REQUEST_AGE_S + 60)
    _request(watcher.WAKE_DIR, slug="box", age_s=5)
    watcher.main()
    calls = _calls(watcher)
    assert len(calls) == 1
    assert "192.0.2.20" in calls[0]


def test_the_max_age_is_beyond_the_backend_stale_rule(watcher):
    """A request the backend still counts as on its way must not be thrown away."""
    assert watcher.MAX_REQUEST_AGE_S > runtime_manager.WAKE_HELPER_STALE_S


def test_a_missing_folder_is_created_for_the_heartbeat(watcher):
    assert not watcher.WAKE_DIR.exists()
    assert watcher.main() == 0
    assert (watcher.WAKE_DIR / watcher.HEARTBEAT_NAME).exists()


def test_a_heartbeat_that_cannot_be_written_never_crashes_the_run(watcher, monkeypatch):
    req = _request(watcher.WAKE_DIR, age_s=5)

    def boom(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(watcher, "_touch", boom)
    assert watcher.main() == 0
    assert not req.exists()
    assert len(_calls(watcher)) == 1


# ── launchd plist: a stable path, never a worktree ──────────────────────────


def test_plist_runs_the_watcher_from_the_stable_host_checkout():
    """A git worktree or the operator's own checkout can vanish or switch branch.

    ``~/.mc/checkouts/mission-control`` is pinned to main and never used for
    development (docker/shared/ensure-checkout.sh).
    """
    plist = plistlib.loads(PLIST_PATH.read_bytes())
    script = plist["ProgramArguments"][-1]
    assert script.endswith("/.mc/checkouts/mission-control/scripts/porsche-wake-watcher.py"), script
    assert "worktrees" not in script and "Workspace/Projects" not in script
    assert plist["WatchPaths"] == [script.split("/.mc/")[0] + "/.mc/wake-requests"]
    assert plist["StartInterval"] == 30


def test_plist_comment_tells_where_the_script_must_live():
    text = PLIST_PATH.read_text(encoding="utf-8")
    assert "ensure-checkout.sh" in text
    assert "worktree" in text


def test_watcher_is_stdlib_only():
    """It runs under the Mac's /usr/bin/python3 — no third-party imports."""
    src = WATCHER_PATH.read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith(("import ", "from ")):
            mod = line.split()[1].split(".")[0]
            assert mod in sys.stdlib_module_names, line


# ── the Mac's Python: launchd runs /usr/bin/python3 (3.9.6) ─────────────────
#
# Round-7 L5 re-check: ``-> float | None`` loaded fine under the container's
# 3.12 but crashed under 3.9 while the module loaded — no heartbeat, every
# wake a 503 for good. The tests above load the module under the container's
# Python, so they could not see it.
SYSTEM_PYTHON = Path("/usr/bin/python3")
MAC_PYTHON = (3, 9)


def _annotations(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if a is not None and a.annotation is not None:
                    yield a.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def test_watcher_source_is_python_39():
    """Parses with 3.9 grammar, and no ``X | Y`` type unions (3.10+ at run time)."""
    src = WATCHER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=MAC_PYTHON)
    unions = [
        ast.get_source_segment(src, ann)
        for ann in _annotations(tree)
        for sub in ast.walk(ann)
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)
    ]
    assert unions == [], f"use Optional[...] / Union[...] — 3.9 crashes on: {unions}"


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="no /usr/bin/python3 here")
def test_watcher_runs_under_the_system_python(tmp_path):
    """The real thing, where it exists: the plist's interpreter, a real run."""
    wake_dir = tmp_path / "wake-requests"
    calls = tmp_path / "calls.jsonl"
    stub = tmp_path / "wake_stub.py"
    stub.write_text(
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    old = _request(wake_dir, slug="leftover", age_s=15 * 24 * 3600)
    fresh = _request(wake_dir, slug="box", age_s=5)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MC_WAKE_REQUEST_DIR": str(wake_dir),
        "MC_WAKE_SCRIPT": str(stub),
    }
    proc = subprocess.run(
        [str(SYSTEM_PYTHON), str(WATCHER_PATH)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert time.time() - (wake_dir / "watcher.alive").stat().st_mtime < 30
    assert not old.exists() and not fresh.exists()
    assert "expired" in json.loads((wake_dir / "leftover.status.json").read_text())["detail"]
    woken = [json.loads(line) for line in calls.read_text().splitlines() if line]
    assert len(woken) == 1 and "192.0.2.20" in woken[0]
