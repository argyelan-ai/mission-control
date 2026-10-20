#!/usr/bin/env python3
"""The chat modules also run on the host's Python 3.9.

The Hermes chat daemon runs on the host and imports ``acp_chat.py`` and
``acp_chat_events.py`` straight from a checkout with the system Python
(3.9.6, live 20.10.2026); the operator runs ``sink_migration.py`` there too,
in the deploy window. The container has 3.11 and CI 3.12 — so syntax or
library calls newer than 3.9 would pass every other test and leave Hermes'
chat dead after the next pull of the checkout.

Checked here without a 3.9 interpreter: the files parse with the 3.9
grammar, postpone annotations (``X | None`` in a signature is fine then),
and use none of the newer library names that grammar cannot see.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Files the host Python loads. S1-R6 adds model_errors.py here.
HOST_MODULES = ("acp_chat.py", "acp_chat_events.py", "sink_migration.py")

#: Library attributes newer than 3.9: (attribute, where it is new).
NEWER_ATTRIBUTES = {
    "UTC": "datetime.UTC (3.11)",
    "walk": "Path.walk (3.12)",
    "TaskGroup": "asyncio.TaskGroup (3.11)",
    "from_iso_week_date": "3.11",
}
#: Names imported from typing that 3.9 does not have.
NEWER_TYPING = {"TypeAlias", "ParamSpec", "Concatenate", "TypeGuard", "Self",
                "LiteralString", "Never", "assert_never", "reveal_type", "Required",
                "NotRequired", "Unpack", "TypeVarTuple", "override"}


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _is_os_walk(node: ast.Attribute) -> bool:
    """``os.walk`` is old and fine; only ``Path.walk`` is 3.12."""
    return node.attr == "walk" and isinstance(node.value, ast.Name) and node.value.id == "os"


@pytest.mark.parametrize("name", HOST_MODULES)
def test_parses_with_the_python_39_grammar(name):
    # feature_version rejects match statements, parenthesised context
    # managers, except* and the other grammar added after 3.9.
    ast.parse(_source(name), filename=name, feature_version=(3, 9))


@pytest.mark.parametrize("name", HOST_MODULES)
def test_postpones_annotations(name):
    tree = ast.parse(_source(name))
    futures = [node for node in tree.body
               if isinstance(node, ast.ImportFrom) and node.module == "__future__"]
    assert any(alias.name == "annotations" for node in futures for alias in node.names), (
        f"{name}: `from __future__ import annotations` missing — `X | None` in a "
        "signature raises TypeError on 3.9")


@pytest.mark.parametrize("name", HOST_MODULES)
def test_uses_no_library_names_newer_than_39(name):
    tree = ast.parse(_source(name))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in NEWER_ATTRIBUTES and not _is_os_walk(node):
            found.append(f"line {node.lineno}: .{node.attr} — {NEWER_ATTRIBUTES[node.attr]}")
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            found += [f"line {node.lineno}: typing.{a.name}" for a in node.names
                      if a.name in NEWER_TYPING]
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            found += [f"line {node.lineno}: datetime.{a.name}" for a in node.names
                      if a.name == "UTC"]
        if isinstance(node, ast.Call) and any(
                kw.arg == "strict" for kw in node.keywords) and (
                isinstance(node.func, ast.Name) and node.func.id == "zip"):
            found.append(f"line {node.lineno}: zip(strict=) — 3.10")
    assert not found, f"{name}: " + "; ".join(found)


def test_the_check_catches_what_it_claims():
    """Guard for the guard: each rule fires on a sample."""
    with pytest.raises(SyntaxError):
        ast.parse("match x:\n    case 1:\n        pass\n", feature_version=(3, 9))
    sample = ast.parse("import datetime, os\nnow = datetime.UTC\nfor _ in p.walk(): pass\n"
                       "for _ in os.walk('.'): pass\n")
    flagged = [n for n in ast.walk(sample) if isinstance(n, ast.Attribute)
               and n.attr in NEWER_ATTRIBUTES and not _is_os_walk(n)]
    assert sorted(n.attr for n in flagged) == ["UTC", "walk"], "os.walk must not count"
