"""Pytest wrapper that runs the shell smoke-test for mc-pre-push.sh.

The actual assertions live in test_mc_pre_push_hook.sh — we just wire
it into the normal test run so CI/manual pytest catches hook regressions
alongside everything else.
"""
import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI required"
)
def test_mc_pre_push_hook_smoke():
    here = os.path.dirname(__file__)
    script = os.path.join(here, "test_mc_pre_push_hook.sh")
    os.chmod(script, 0o755)
    result = subprocess.run(
        [script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"hook smoke-test failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
