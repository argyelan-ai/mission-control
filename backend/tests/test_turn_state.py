"""Pytest wrapper for the detect_turn_state smoke-tests (bash).

Runs backend/tests/test_turn_state.sh against BOTH lib copies
(mc-agent-base + mc-claude-agent) so a fix applied to only one of the
duplicated files fails loudly.
"""

import pathlib
import subprocess

import pytest

_HERE = pathlib.Path(__file__).parent
_ROOT = _HERE.parent.parent
_LIBS = [
    _ROOT / "docker" / "mc-agent-base" / "lib" / "turn-state.sh",
    _ROOT / "docker" / "mc-claude-agent" / "lib" / "turn-state.sh",
]


@pytest.mark.parametrize("lib", _LIBS, ids=[p.parent.parent.name for p in _LIBS])
def test_turn_state_smoke(lib):
    result = subprocess.run(
        ["bash", str(_HERE / "test_turn_state.sh"), str(lib)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "PASS" in result.stdout
