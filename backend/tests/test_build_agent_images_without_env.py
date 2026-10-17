"""`build-agent-images.sh --sync-only` must run without a `.env`.

The release workflow (`.github/workflows/release.yml`) materializes the shared
agent sources in a fresh checkout — there is no `.env` there. The image-tag
pin read the file under `set -euo pipefail`; a missing file ended the script
with exit 2 and the v0.3.0 release build failed before any image was built.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build-agent-images.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_sync_only_runs_without_an_env_file(tmp_path):
    env = dict(os.environ)
    env.pop("MC_AGENT_IMAGE_TAG", None)
    env["MC_ENV_FILE"] = str(tmp_path / "missing.env")  # like a CI checkout
    env["BUILD_MIN_FREE_GB"] = "0"
    result = subprocess.run(
        ["bash", str(SCRIPT), "--sync-only"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
