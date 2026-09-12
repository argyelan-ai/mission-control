"""Pytest wrapper that runs the shell smoke-test for the interrupted-nudge fix.

The actual assertions live in test_paste_classify.sh — we just wire it into
the normal test run so CI/manual pytest catches regressions alongside
everything else. Covers:

- classify_paste_outcome three-way verification (submitted / sitting-in-input-
  box / never-arrived) — the old binary verify_paste_landed mislabeled "text
  visible in the input box, Enter lost" as "fingerprint not visible".
- pane_in_interrupted_dialog — the post-interrupt dialog must block
  wait_for_clean_prompt so the nudge paste waits for prompt readiness.
"""
import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required"
)
def test_paste_classify_smoke():
    here = os.path.dirname(__file__)
    script = os.path.join(here, "test_paste_classify.sh")
    os.chmod(script, 0o755)
    result = subprocess.run(
        [script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"paste-classify smoke-test failed:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "PASS" in result.stdout
