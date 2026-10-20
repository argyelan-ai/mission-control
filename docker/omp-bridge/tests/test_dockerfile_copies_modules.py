#!/usr/bin/env python3
"""Every bridge module the container imports is copied into the image.

The image copies its Python files ONE BY ONE (``COPY acp_chat.py
/opt/omp-bridge/acp_chat.py``, ...). A module that ``acp_chat.py``,
``bridge.py`` or ``acp_chat_events.py`` imports but the Dockerfile does not
copy is an ImportError at start: the chat daemon (window 3) or the task
driver (window 1) never comes up — Sparky would take no chat and no task.
The suite runs from the source folder, where every file is present, so no
other test would notice.

The check follows imports transitively (an import inside a function counts
too) and only looks at modules that live in this folder.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOCKERFILE = ROOT / "Dockerfile"

#: The scripts the container starts (entrypoint.sh: bridge.py --serve,
#: acp_chat.py --serve) and the module both of them write through.
ENTRY_MODULES = ("acp_chat", "bridge", "acp_chat_events")

_COPY_RE = re.compile(r"^\s*COPY\s+(?P<src>\S+)\s+/opt/omp-bridge/(?P<dst>\S*)\s*$", re.M)


def local_modules() -> set[str]:
    return {p.stem for p in ROOT.glob("*.py")}


def imports_of(module: str) -> set[str]:
    tree = ast.parse((ROOT / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & local_modules()


def needed_modules() -> set[str]:
    todo = list(ENTRY_MODULES)
    seen: set[str] = set()
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        todo.extend(imports_of(module) - seen)
    return seen


def copied_files() -> set[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    copied = set()
    for match in _COPY_RE.finditer(text):
        src, dst = match.group("src"), match.group("dst")
        if dst in (src, ""):  # same name, or copied into the folder
            copied.add(src)
    return copied


def test_every_imported_bridge_module_has_a_copy_line():
    needed = needed_modules()
    assert {"acp_chat", "bridge", "acp_chat_events", "acp_client"} <= needed
    missing = sorted(m for m in needed if f"{m}.py" not in copied_files())
    assert not missing, (
        "imported by the container's bridge code but not copied into the image "
        f"(add `COPY <name>.py /opt/omp-bridge/<name>.py`): {missing}")


def test_the_chat_daemon_needs_the_sink_migration():
    """S1-R3: the daemon moves old copies at start — without the module in
    the image it would not start at all."""
    assert "sink_migration" in imports_of("acp_chat")
    assert "sink_migration.py" in copied_files()


def test_the_copy_parser_reads_the_real_lines():
    """Guard for the guard: the parser must see the lines that exist."""
    copied = copied_files()
    assert {"bridge.py", "acp_chat.py", "acp_chat_events.py", "acp_client.py"} <= copied
