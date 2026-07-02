"""Pytest wrapper that runs the shell smoke-test for ui-detect.sh.

Bug 14 (2026-05-13): assertions live in test_ui_detect.sh — this wrapper
wires it into the normal pytest suite so CI catches regressions of the
runtime-UI detection that paste_and_submit relies on.
"""
import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required"
)
def test_ui_detect_smoke():
    here = os.path.dirname(__file__)
    script = os.path.join(here, "test_ui_detect.sh")
    os.chmod(script, 0o755)
    result = subprocess.run(
        [script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"ui-detect smoke-test failed:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "PASS" in result.stdout
