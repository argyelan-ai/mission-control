"""Pytest wrapper that runs the shell smoke-test for context-detect.sh.

CTX-01 Nachzug (2026-08-09): assertions live in test_context_detect.sh — this
wrapper wires it into the normal pytest suite so CI catches regressions of
the harness-aware context%-scraper poll.sh's heartbeat() relies on.
"""
import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required"
)
def test_context_detect_smoke():
    here = os.path.dirname(__file__)
    script = os.path.join(here, "test_context_detect.sh")
    os.chmod(script, 0o755)
    result = subprocess.run(
        [script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"context-detect smoke-test failed:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "PASS" in result.stdout
