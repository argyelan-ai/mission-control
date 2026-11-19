"""Python twin of the rule omp uses to find its own session files.

Not a test module: a helper the sink tests use to ask "would omp see this
file as one of its sessions?". If it would, omp can load MC's display copy
instead of its own memory file — the bug S1-R3 removes by writing MC's copy
into ``<folder>/.mc/``.

Read in the omp 18.10.1 release binary (the bundled JavaScript; the darwin
and the linux build carry the same code, 13 load-bearing passages compared
word for word on 20.10.2026). Byte offsets are from the darwin-arm64 build:

* Header rule (``xc`` at byte 155 632 037, ``kc`` at 155 632 809, ``Ec``):
  line 1 — or line 2 when line 1 is ``{"type": "title"}`` — must have
  ``type == "session"`` and an ``id`` that is a string. Anything else is not
  a session. MC's header ``{"type": "session", "id": ..., "bridge": "acp"}``
  passes, so omp treats MC's copy as a session of its own.
* ``SessionManager.list(cwd)`` -> ``ZB`` -> ``In`` (155 583 490,
  155 710 476): ``listFilesSync(dir, "*.jsonl")`` = ``Bun.Glob("*.jsonl")``
  over the session folder — files directly in it, never in a subfolder.
* ``listAll()`` -> ``tG`` (155 637 507, 155 710 642):
  ``new Bun.Glob("*/*.jsonl").scan(root)`` — one level below the sessions
  root, so ``<root>/<folder>/.mc/x.jsonl`` (two levels) is never listed.
* Both globs run without ``dot: true``: names that start with a dot are not
  matched (Bun's default). That is why the folder is ``.mc`` and not ``mc``
  — it would still hold if omp ever searched recursively.
* Load / resume / fork pick the file by the HEADER ``id``, newest
  modification time first (``#W`` -> ``#_`` / ``#H``). With two files of one
  id, the younger one wins — which is how MC's copy got loaded.

Kept deliberately small: only what decides "listed or not".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

#: The omp release this twin was read from.
OMP_VERSION = "18.10.1"

#: The longest header line read. omp reads the whole first lines; a header
#: is a few hundred bytes, the cap only guards against a broken file.
_HEADER_BYTES = 65536


def session_header(path: Path) -> Optional[dict]:
    """The session header omp would accept, or None (``xc``/``kc``/``Ec``)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [fh.readline(_HEADER_BYTES), fh.readline(_HEADER_BYTES)]
    except OSError:
        return None
    parsed = []
    for raw in lines:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        parsed.append(value if isinstance(value, dict) else None)
    first, second = parsed
    header = second if first is not None and first.get("type") == "title" else first
    if header is None or header.get("type") != "session":
        return None
    if not isinstance(header.get("id"), str):
        return None
    return header


def _glob_jsonl(folder: Path) -> list[Path]:
    """``Bun.Glob("*.jsonl")`` over one folder: files only, no dot names."""
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    return [
        p for p in entries
        if p.name.endswith(".jsonl") and not p.name.startswith(".") and p.is_file()
    ]


def list_cwd(folder: Path) -> list[Path]:
    """``SessionManager.list(cwd)``: the sessions directly in ``folder``."""
    return [p for p in _glob_jsonl(Path(folder)) if session_header(p) is not None]


def list_all(root: Path) -> list[Path]:
    """``listAll()``: ``*/*.jsonl`` under the sessions root."""
    out: list[Path] = []
    try:
        folders = sorted(Path(root).iterdir())
    except OSError:
        return out
    for folder in folders:
        if folder.name.startswith(".") or not folder.is_dir():
            continue
        out.extend(list_cwd(folder))
    return out


def omp_would_list(root: Path) -> list[Path]:
    """Every file omp would treat as a session under ``root``: ``listAll()``
    plus ``list(cwd)`` for each folder (the same set — kept as two calls so
    a future change to one rule shows up here)."""
    seen: dict[str, Path] = {}
    for path in list_all(root):
        seen[str(path)] = path
    try:
        folders = sorted(Path(root).iterdir())
    except OSError:
        folders = []
    for folder in folders:
        if folder.is_dir() and not folder.name.startswith("."):
            for path in list_cwd(folder):
                seen[str(path)] = path
    return sorted(seen.values())


def is_mc_copy(path: Path) -> bool:
    """MC's display copy: a session header that names the bridge."""
    header = session_header(path)
    return header is not None and header.get("bridge") == "acp"
