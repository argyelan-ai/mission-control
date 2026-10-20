"""Contract: backend and bridge name MC's copy folder the same.

The omp bridge writes MC's display copy of a session into
``<session folder>/.mc/`` (``docker/omp-bridge/acp_chat_events.py``,
``MC_SINK_DIRNAME``) so omp never mistakes it for its own memory file. The
backend finds that copy through its own constant (``_MC_SINK_DIRNAME`` in
``app/services/omp_chat.py``). The two share no code — the bridge runs in a
container image and on the host's Python — so if one name changes alone,
the chat view goes empty without an error anywhere.

Read as text on purpose: importing the bridge module here would test the
import path, not the shipped file.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_FILE = REPO_ROOT / "backend" / "app" / "services" / "omp_chat.py"
BRIDGE_FILE = REPO_ROOT / "docker" / "omp-bridge" / "acp_chat_events.py"


def _constant(path: Path, name: str) -> str:
    """The string literal assigned to ``name`` at module level."""
    pattern = re.compile(
        rf"^{re.escape(name)}\s*(?::\s*\w+\s*)?=\s*(['\"])(?P<value>[^'\"]+)\1\s*(?:#.*)?$",
        re.M,
    )
    matches = pattern.findall(path.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"{path.name}: expected exactly one `{name} = '...'`, found {len(matches)}"
    return pattern.search(path.read_text(encoding="utf-8")).group("value")


def test_backend_and_bridge_use_the_same_copy_folder():
    bridge = _constant(BRIDGE_FILE, "MC_SINK_DIRNAME")
    backend = _constant(BACKEND_FILE, "_MC_SINK_DIRNAME")
    assert bridge == backend == ".mc", (bridge, backend)


def test_the_reader_finds_the_constant_it_claims(tmp_path):
    """Guard for the guard: the regex reads plain and annotated assignments
    and refuses a second definition."""
    sample = tmp_path / "sample.py"
    sample.write_text('X = ".mc"  # why\n', encoding="utf-8")
    assert _constant(sample, "X") == ".mc"
    sample.write_text("X: str = '.mc'\n", encoding="utf-8")
    assert _constant(sample, "X") == ".mc"
    sample.write_text('X = ".mc"\nX = "mc"\n', encoding="utf-8")
    try:
        _constant(sample, "X")
    except AssertionError:
        pass
    else:  # pragma: no cover - the guard itself failed
        raise AssertionError("a second definition must not pass")
