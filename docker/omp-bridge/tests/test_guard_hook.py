"""Runs tests/test_guard_hook.mjs (the omp guard hook) under pytest, so the
omp-bridge CI lane covers it. Skips only when node is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_guard_hook_mjs():
    r = subprocess.run(
        ["node", str(HERE / "test_guard_hook.mjs")],
        capture_output=True, text=True, timeout=120,
    )
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL PASS" in r.stdout


def test_acp_client_loads_the_guard_hook_when_present(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(HERE.parent))
    import acp_client

    hook = tmp_path / "guard-hook.mjs"
    hook.write_text("export default () => {};\n")
    monkeypatch.setenv("OMP_GUARD_HOOK_FILE", str(hook))
    monkeypatch.delenv("OMP_GUARD_HOOK", raising=False)
    assert acp_client.default_command() == ["omp", "acp", "--hook", str(hook)]

    monkeypatch.setenv("OMP_GUARD_HOOK", "0")  # kill switch
    assert acp_client.default_command() == ["omp", "acp"]

    monkeypatch.delenv("OMP_GUARD_HOOK")
    monkeypatch.setenv("OMP_GUARD_HOOK_FILE", str(tmp_path / "missing.mjs"))
    assert acp_client.default_command() == ["omp", "acp"]

    c = acp_client.ACPClient(cwd=str(tmp_path))
    assert c._command == ["omp", "acp"]


def test_launch_omp_passes_the_guard_hook():
    text = (HERE.parent / "launch-omp.sh").read_text()
    assert "OMP_GUARD_HOOK_FILE" in text
    assert '--hook "$GUARD_HOOK"' in text or "${GUARD_ARGS[@]" in text


def test_image_sync_carries_everything_the_hooks_import(tmp_path):
    """The image gets tools/guards through sync_guards_into. The hooks import
    hookparse.py; without it they fail open and the fleet has no guards at all."""
    import os
    root = HERE.parent.parent.parent
    script = (root / "scripts" / "build-agent-images.sh").read_text()
    start = script.index("sync_guards_into() {")
    end = script.index("\n}\n", start) + 3
    fn = script[start:end]
    subprocess.run(["bash", "-c", fn + '\nsync_guards_into "$1"', "x", str(tmp_path)],
                   check=True, env={**os.environ, "ROOT": str(root)})
    synced = tmp_path / "guards"
    assert (synced / "bash-hook.sh").is_file()
    assert (synced / "hookparse.py").is_file()
    hook_json = '{"tool_name": "Bash", "cwd": "/", "tool_input": {"command": "set -- $ITEM"}}'
    r = subprocess.run(["bash", str(synced / "bash-hook.sh")], input=hook_json, text=True,
                       capture_output=True, env={**os.environ, "GUARD_HOOK_MODE": "block",
                                                 "HOME": str(tmp_path), "GUARD_HOOK_LOG": str(tmp_path / "l")})
    assert r.returncode == 2, r.stderr
    hook_json = ('{"tool_name": "Bash", "cwd": "/", "tool_input": {"command": '
                 '"git -C /nonexistent-wt push origin feat/x"}}')
    r = subprocess.run(["bash", str(synced / "post-push-hook.sh")], input=hook_json, text=True,
                       capture_output=True, env={**os.environ, "GUARD_HOOK_MODE": "block",
                                                 "HOME": str(tmp_path), "GUARD_HOOK_LOG": str(tmp_path / "l")})
    assert r.returncode == 2 and "not provable" in r.stderr, r.stderr
