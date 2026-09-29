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


def _walk(routes, seen: set[int], prefix: str = ""):
    """Yield every route path, descending into nested routers.

    FastAPI 0.141+ wraps each included router in an ``_IncludedRouter``
    (``original_router`` + ``include_context.prefix``), so a flat walk over
    ``app.routes`` sees only a handful of entries — a guard built on that
    passes on an empty table. Best effort: the OpenAPI schema below is the
    version-stable baseline, this walk adds websockets and hidden routes.
    """
    for route in routes:
        if id(route) in seen:
            continue
        seen.add(id(route))
        path = getattr(route, "path", None)
        if isinstance(path, str):
            yield prefix + path
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            yield from _walk(inner.routes, seen, prefix + (getattr(ctx, "prefix", "") or ""))


def _route_paths() -> set[str]:
    """Documented paths from the OpenAPI schema (stable across versions) plus
    whatever the route walk finds (websockets and ``include_in_schema=False``
    routes are not in the schema)."""
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert len(paths) > 200, f"route table looks empty: {len(paths)} paths"
    paths |= set(_walk(app.routes, set()))
    return paths


def _route_words() -> dict[str, set[str]]:
    """{word: {route path, ...}} for every static path segment, split on - _ ."""
    words: dict[str, set[str]] = {}
    for path in _route_paths():
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


def test_route_table_is_not_empty():
    # Same for the other side: the words must come from the real route table,
    # including the websocket routes the schema does not list.
    words = _route_words()
    assert "tasks" in words and "boards" in words
    assert any("/terminal/ws" in p for ps in words.values() for p in ps)


def test_no_route_segment_is_an_agent_name():
    names = _fleet_names() | _template_names()
    words = _route_words()
    hits = {name: sorted(words[name]) for name in names if name in words}
    assert not hits, f"route segments carry agent names: {hits}"
