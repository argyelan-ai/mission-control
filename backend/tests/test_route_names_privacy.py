"""No API route carries an agent's personal name as a path segment.

The guided playbook setup once served its routes under a person's name, and
that name shipped in the public OpenAPI spec (fixed in #690). This guard
checks the whole router table against the names the repo already knows —
the privacy scan's fleet list and the built-in agent templates — so the test
itself never has to spell a name out.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _fleet_names() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        "privacy_scan", _REPO_ROOT / "scripts" / "privacy-scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {n.lower() for n in module.FLEET_NAMES}


def _template_names() -> set[str]:
    from app.routers.agents import AGENT_CONFIGS

    names: set[str] = set()
    for key, config in AGENT_CONFIGS.items():
        names.add(key.lower())
        names.update(n.lower() for n in config.get("match_names", []))
    return names


def _route_words() -> dict[str, set[str]]:
    """{word: {route path, ...}} for every static path segment, split on - _ ."""
    from app.main import app

    words: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        for seg in path.strip("/").split("/"):
            if not seg or seg.startswith("{"):
                continue
            for word in re.split(r"[-_.]", seg.lower()):
                if word:
                    words.setdefault(word, set()).add(path)
    return words


def test_name_sources_are_not_empty():
    # A guard with an empty name list passes forever — make sure it has teeth.
    assert _fleet_names()
    assert _template_names()


def test_no_route_segment_is_an_agent_name():
    names = _fleet_names() | _template_names()
    words = _route_words()
    hits = {name: sorted(words[name]) for name in names if name in words}
    assert not hits, f"route segments carry agent names: {hits}"
