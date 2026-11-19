"""W0.9 — the /record browser runs model-written code, so it gets no network.

Pure helpers only (record_network.py has no playwright import): which requests the
recorded page may make, how blocked attempts are counted and reported, the
browser-context proxy that backs the block up, and the library-banner scan.
The browser wiring itself is proven in a throwaway container (see PR text).

    cd backend && python -m pytest ../docker/mc-playwright/tests/test_record_network.py -v
"""
from pathlib import Path

import pytest

from media import RecordResponse
from record_network import (
    RECORD_DEAD_PROXY,
    RecordNetworkGuard,
    detect_inlined_libraries,
    load_record_network_guard_script,
    record_proxy_settings,
)

PAGE = "file:///shared-deliverables/bench-x/Model-A/index.html"


# ── what the recorded page may load ──────────────────────────────────────


def test_the_page_itself_is_allowed():
    guard = RecordNetworkGuard(PAGE)
    assert guard.allow(PAGE)
    assert guard.allow(PAGE + "#scene")  # fragment = same document
    assert guard.attempts == 0


@pytest.mark.parametrize("url", [
    "data:image/png;base64,iVBORw0KGgo=",
    "blob:file:///6f1c2e1a-0000-4000-8000-000000000000",
    "about:blank",
])
def test_inline_schemes_are_allowed(url):
    guard = RecordNetworkGuard(PAGE)
    assert guard.allow(url)
    assert guard.attempts == 0


@pytest.mark.parametrize("url", [
    # A model server on the LAN (TEST-NET address stands in for the real one).
    "http://192.0.2.10:8000/v1/models",
    # A CDN import.
    "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js",
    # Other containers on the compose network, the sidecar itself, loopback.
    "http://backend:8000/api/v1/agents",
    "http://localhost:8790/health",
    "http://127.0.0.1:9/",
    # WebSockets.
    "ws://192.0.2.10:8000/stream",
    "wss://example.com/socket",
    # Other local files than the page itself.
    "file:///etc/passwd",
    "file:///shared-deliverables/bench-x/Model-B/index.html",
])
def test_everything_else_is_blocked_and_counted(url):
    guard = RecordNetworkGuard(PAGE)
    assert guard.allow(url) is False
    assert guard.attempts == 1


def test_report_counts_every_attempt_but_lists_only_a_bounded_sample():
    guard = RecordNetworkGuard(PAGE)
    for i in range(40):
        guard.allow(f"https://cdn.example.com/lib-{i}.js?token=abc")
    guard.note_blocked("rtc", count=2)
    report = guard.report()
    assert report["network_blocked"] is True
    assert report["network_attempts"] == 42
    assert len(report["blocked_requests"]) == 20
    # Query strings are dropped (they are noise and could carry a value).
    assert report["blocked_requests"][0] == "https://cdn.example.com/lib-0.js"


def test_an_http_page_url_allows_only_its_own_document():
    page = "http://203.0.113.5:8080/demo/index.html"
    guard = RecordNetworkGuard(page)
    assert guard.allow(page)
    assert not guard.allow("http://203.0.113.5:8080/demo/app.js")
    assert guard.attempts == 1


# ── proxy: second line of defence for anything routing cannot see ────────


def test_proxy_for_a_file_page_sends_all_traffic_to_a_dead_port():
    proxy = record_proxy_settings(PAGE)
    assert proxy["server"] == RECORD_DEAD_PROXY
    # Chromium bypasses the proxy for loopback unless told not to.
    assert "<-loopback>" in proxy["bypass"]


def test_proxy_for_an_http_page_bypasses_only_its_own_host():
    proxy = record_proxy_settings("http://203.0.113.5:8080/demo/index.html")
    assert proxy["server"] == RECORD_DEAD_PROXY
    assert proxy["bypass"].split(",") == ["<-loopback>", "203.0.113.5"]


def test_guard_script_stubs_webrtc_and_counts():
    js = load_record_network_guard_script()
    assert "RTCPeerConnection" in js
    assert "__mcNetBlocked" in js


# ── library banners: who pasted a library instead of writing code ────────


def test_detects_inlined_three_js_with_its_revision_minified():
    html = (
        "<script>/**\n * @license\n * Copyright 2010-2023 Three.js Authors\n */\n"
        '!function(t){const e="160";'
        'window.__THREE__?console.warn("x"):window.__THREE__=e}()</script>'
    )
    assert detect_inlined_libraries(html) == ["three.js r160"]


def test_detects_three_js_module_revision_constant():
    html = "<script type=module>const REVISION = '171';/* Three.js Authors */</script>"
    assert detect_inlined_libraries(html) == ["three.js r171"]


def test_detects_several_libraries_with_versions():
    html = (
        "/*! GSAP 3.12.5 https://gsap.com */"
        "/*! p5.js v1.9.0 January 1, 2024 */"
        "/*! jQuery v3.7.1 | (c) OpenJS Foundation */"
        "/*! Chart.js v4.4.1 */"
        "// https://d3js.org v7.8.5 Copyright 2010-2023 Mike Bostock"
    )
    assert detect_inlined_libraries(html) == [
        "Chart.js 4.4.1", "D3 7.8.5", "GSAP 3.12.5", "jQuery 3.7.1", "p5.js 1.9.0",
    ]


def test_hand_written_page_has_no_libraries():
    html = "<canvas id=c></canvas><script>const three = 3; // threejs-like but mine</script>"
    assert detect_inlined_libraries(html) == []


def test_a_cdn_script_tag_is_not_an_inlined_library():
    """A <script src=cdn> is a blocked request, not code in the file."""
    html = '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>'
    assert detect_inlined_libraries(html) == []


# ── response contract ─────────────────────────────────────────────────────


def test_record_response_defaults_stay_backward_compatible():
    r = RecordResponse(video_path="/v.mp4", screenshot_path="", duration_s=10, bytes=1)
    assert r.network_blocked is False
    assert r.network_attempts == 0
    assert r.blocked_requests == []
    assert r.libraries_inlined == []
    assert r.html_bytes is None


def test_guard_script_file_ships_next_to_media():
    assert (Path(__file__).resolve().parents[1] / "record_network_guard.js").is_file()


def test_dockerfile_ships_the_network_guard_files():
    """service.py imports record_network and loads the JS at runtime — a
    missing COPY would crash every /record in the built image."""
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY record_network.py /app/record_network.py" in dockerfile
    assert "COPY record_network_guard.js /app/record_network_guard.js" in dockerfile


def test_record_endpoint_uses_the_guard():
    """Structural guard (service.py imports playwright, so it is parsed, not
    imported): /record routes the context through the guard, blocks
    WebSockets and service workers, sets the dead proxy and the init script."""
    import ast

    src = (Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    record_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "record"
    )
    body = ast.unparse(record_fn)
    for needle in (
        "RecordNetworkGuard(",
        "record_proxy_settings(",
        "service_workers='block'",
        ".route('**/*'",
        ".route_web_socket(",
        "load_record_network_guard_script()",
        "detect_inlined_libraries(",
        "network_attempts",
    ):
        assert needle in body, needle


# ── what was blocked: a script means the page could not run (25.10.2026) ──


def test_a_blocked_script_is_counted_apart_from_other_requests():
    """A CDN module (three.js via an import map) = the scene never draws.
    Fonts, images and stylesheets are counted as attempts only."""
    guard = RecordNetworkGuard(PAGE)
    assert guard.allow("https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js", "script") is False
    assert guard.allow("https://fonts.googleapis.com/css2?family=Inter", "stylesheet") is False
    assert guard.allow("https://fonts.gstatic.com/s/inter.woff2", "font") is False
    assert guard.allow("https://example.com/sky.png", "image") is False
    assert guard.allow("data:text/javascript,1", "script") is True
    report = guard.report()
    assert report["network_attempts"] == 4
    assert report["network_blocked_scripts"] == 1


def test_a_request_without_a_type_is_not_a_script():
    guard = RecordNetworkGuard(PAGE)
    guard.allow("https://cdn.example.com/x.js")
    guard.note_blocked("webrtc:", count=2)
    assert guard.report()["network_blocked_scripts"] == 0


def test_record_response_carries_the_blocked_script_count():
    r = RecordResponse(video_path="/v.mp4", screenshot_path="", duration_s=10, bytes=1)
    assert r.network_blocked_scripts is None  # not reported = unknown, never "0"


def test_record_route_passes_the_resource_type():
    import ast

    src = (Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    record_fn = next(n for n in ast.walk(ast.parse(src))
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == "record")
    body = ast.unparse(record_fn)
    assert "route.request.resource_type" in body
    assert "network_blocked_scripts" in body
