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
