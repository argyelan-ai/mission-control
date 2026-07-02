"""Pytest wrapper that runs the shell smoke-test for paste-verify.sh.

The actual assertions live in test_paste_verify.sh — we just wire
it into the normal test run so CI/manual pytest catches Bug 10
regressions alongside everything else.
"""
import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required"
)
def test_paste_verify_smoke():
    here = os.path.dirname(__file__)
    script = os.path.join(here, "test_paste_verify.sh")
    os.chmod(script, 0o755)
    result = subprocess.run(
        [script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"paste-verify smoke-test failed:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "PASS" in result.stdout
