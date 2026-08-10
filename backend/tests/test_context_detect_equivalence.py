"""Equivalence guard: docker/mc-agent-base/lib/context-detect.sh (Bash) and
scripts/context_detect.py (Python) implement the SAME regex patterns for two
different runtimes (poll.sh vs. the hermes/grok/omp Python bridges). Without
a test proving they agree, the two will silently drift apart the next time
either gets edited without the other.

Runs every fixture in context_scrape_fixtures.py through BOTH
implementations and asserts identical results. Also guards that
scripts/context_detect.py and docker/omp-bridge/context_detect.py (a
hand-maintained duplicate, needed because the omp-bridge Docker build only
COPYs individual files, not the whole scripts/ directory — see
docker/omp-bridge/Dockerfile) stay byte-identical, the same way
test_adapter_tck.py guards the docker/*/lib/*.sh duplicates.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .context_scrape_fixtures import FIXTURES

REPO_ROOT = Path(__file__).resolve().parents[2]
BASH_LIB = REPO_ROOT / "docker" / "mc-agent-base" / "lib" / "context-detect.sh"
PY_MODULE = REPO_ROOT / "scripts" / "context_detect.py"
PY_MODULE_OMP_COPY = REPO_ROOT / "docker" / "omp-bridge" / "context_detect.py"

BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(
    not BASH_LIB.exists() or shutil.which("bash") is None,
    reason="bash + canonical context-detect.sh required",
)


def _bash_scrape(text: str, harness: str | None) -> str:
    """Invokes scrape_context_pct() from the real bash lib via a one-shot
    subprocess. Returns the raw string ("" for no value) so it compares
    directly against the Python side's None (mapped below)."""
    script = (
        f'source "{BASH_LIB}"\n'
        f'PANE_UI_OVERRIDE={harness or ""} scrape_context_pct "$1"\n'
    )
    res = subprocess.run(
        [BASH, "-c", script, "bash", text],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, f"bash scrape failed: {res.stderr}"
    return res.stdout.strip()


@pytest.fixture(scope="module")
def context_detect():
    import importlib.util

    spec = importlib.util.spec_from_file_location("context_detect_equiv", PY_MODULE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("desc,harness,text,expected", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_bash_and_python_agree(context_detect, desc, harness, text, expected):
    bash_result = _bash_scrape(text, harness)
    py_result = context_detect.scrape_context_pct(text, harness=harness)

    # Normalize: bash "" == python None, otherwise both are the same integer
    # as a string.
    bash_normalized = None if bash_result == "" else int(bash_result)
    assert bash_normalized == py_result, (
        f"{desc}: bash={bash_normalized!r} python={py_result!r} — implementations diverged"
    )
    # And both must match the fixture's own expectation (belt-and-braces —
    # test_context_detect.sh / test_context_detect_python.py already assert
    # this individually, but a fixture typo here would otherwise pass this
    # test vacuously by having both sides "agree" on the wrong answer).
    assert py_result == expected, f"{desc}: expected {expected!r}, python got {py_result!r}"


def test_omp_bridge_copy_is_byte_identical_to_canonical():
    """docker/omp-bridge/context_detect.py is a hand-maintained duplicate
    (the Docker build COPYs individual files, not a directory) — a drift here
    is exactly how a fix lands for hermes/grok but silently not for omp."""
    canonical = PY_MODULE.read_text()
    omp_copy = PY_MODULE_OMP_COPY.read_text()
    assert canonical == omp_copy, (
        "scripts/context_detect.py and docker/omp-bridge/context_detect.py "
        "have drifted — re-sync the omp-bridge copy from the canonical file"
    )
