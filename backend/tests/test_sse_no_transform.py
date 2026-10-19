"""SSE responses must tell every proxy in front of them not to transform them.

Live finding 19.10.2026 (PR #875): Next's built-in compression gzipped and
buffered ``text/event-stream`` behind the ``/api`` rewrite on :3000 — the chat
StatusLine sat on "Status unklar" because ping + state never left the buffer.
``compress: false`` in next.config.ts fixes that one proxy; ``Cache-Control:
no-transform`` from the backend is the belt to that braces: the
``compression`` middleware Next uses (and any standard proxy/CDN) skips a
response that carries it, so a later config change cannot silently bring the
stall back.
"""
from __future__ import annotations

import pathlib
import re

from app.services.sse import SSE_HEADERS, make_sse_response, sse_response


def _cache_control(resp) -> str:
    return resp.headers.get("cache-control", "")


def test_make_sse_response_carries_no_transform():
    resp = make_sse_response(["mc:test"])
    assert "no-transform" in _cache_control(resp)
    # sse-starlette's own default (no-store) must survive next to it.
    assert "no-store" in _cache_control(resp)
    assert resp.headers.get("x-accel-buffering") == "no"


def test_sse_response_wraps_any_generator_with_the_same_headers():
    async def gen():
        yield {"event": "ping", "data": "{}"}

    resp = sse_response(gen())
    assert "no-transform" in _cache_control(resp)
    assert SSE_HEADERS["Cache-Control"] == _cache_control(resp)


def test_no_router_builds_a_bare_event_source_response():
    """Every SSE endpoint goes through app.services.sse — a bare
    ``EventSourceResponse(...)`` elsewhere would ship without no-transform."""
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name == "sse.py" and path.parent.name == "services":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bEventSourceResponse\(", text):
            offenders.append(str(path.relative_to(app_dir)))
    assert offenders == [], f"bare EventSourceResponse in: {offenders}"
