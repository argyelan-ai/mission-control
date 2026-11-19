"""lib:three-local — the recorder answers three.js CDN requests with ONE pinned
local copy; everything else stays blocked (record_three.py + record_network.py).

Pure helpers only. The browser wiring is proven in a throwaway container
(rotating cube via a CDN import map, see the PR text).

    cd backend && python -m pytest ../docker/mc-playwright/tests/test_record_three.py -v
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

from media import RENDERER_FEATURES, RecordResponse
from record_network import RecordNetworkGuard, detect_inlined_libraries, route_decision
from record_three import (
    CANONICAL_MODULE_URL,
    THREE_REVISION,
    VENDOR_DIR,
    VENDORED_ADDONS,
    LocalThree,
    classify,
    inject_import_map,
    needs_import_map,
    revision_of,
)

PAGE = "file:///shared-deliverables/bench-x/Model-A/index.html"


# ── the vendored copy ────────────────────────────────────────────────────


def test_one_pinned_version_with_licence_and_checksums():
    manifest = json.loads((VENDOR_DIR / "MANIFEST.json").read_text())
    assert THREE_REVISION == manifest["revision"] and THREE_REVISION.isdigit()
    assert "MIT License" in (VENDOR_DIR / "LICENSE").read_text()
    for rel, sha in manifest["files"].items():
        assert hashlib.sha256((VENDOR_DIR / rel).read_bytes()).hexdigest() == sha, rel
    # the module and the global build are both there, the global one sets window.THREE
    assert (VENDOR_DIR / "three.module.min.js").stat().st_size > 100_000
    assert "window.THREE" in (VENDOR_DIR / "three.global.min.js").read_text()[-400:]


def test_orbit_controls_and_bloom_are_vendored():
    for addon in ("controls/OrbitControls.js", "postprocessing/EffectComposer.js",
                  "postprocessing/UnrealBloomPass.js", "postprocessing/OutputPass.js"):
        assert addon in VENDORED_ADDONS


def test_every_relative_import_of_an_addon_is_vendored_too():
    """An addon that imports a sibling we do not ship would be blocked mid-way."""
    for addon in VENDORED_ADDONS:
        text = (VENDOR_DIR / "addons" / addon).read_text()
        for rel in re.findall(r"""from\s*['"](\.{1,2}/[^'"]+)['"]""", text):
            target = (Path(addon).parent / rel).as_posix()
            target = str(Path(target)).replace("\\", "/")
            parts: list[str] = []
            for seg in target.split("/"):
                if seg == "..":
                    parts.pop()
                elif seg != ".":
                    parts.append(seg)
            assert "/".join(parts) in VENDORED_ADDONS, (addon, rel)


def test_the_renderer_announces_the_feature():
    assert "three_local" in RENDERER_FEATURES


# ── which requests are answered ──────────────────────────────────────────


@pytest.mark.parametrize("url,kind,rev", [
    ("https://unpkg.com/three@0.160.0/build/three.module.js", "module", "160"),
    ("https://unpkg.com/three@0.160.0/build/three.module.min.js", "module", "160"),
    ("https://unpkg.com/three@0.149.0/build/three.min.js", "global", "149"),
    ("https://unpkg.com/three/build/three.module.js", "module", None),
    ("https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js", "module", "170"),
    ("https://cdn.jsdelivr.net/npm/three@0.163/+esm", "module", "163"),
    ("https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js", "global", "128"),
    ("https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js", "global", "128"),
    ("https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.module.min.js", "module", "160"),
    ("https://esm.sh/three@0.160.0", "module", "160"),
    ("https://esm.sh/three", "module", None),
    ("https://esm.sh/v135/three@0.160.0/es2022/three.mjs", "module", "160"),
    ("https://cdn.skypack.dev/three@0.150.1", "module", "150"),
    ("https://ga.jspm.io/npm:three@0.160.0/build/three.module.js", "module", "160"),
    ("https://threejs.org/build/three.module.js", "module", None),
    (CANONICAL_MODULE_URL, "module", THREE_REVISION),
])
def test_three_itself_on_known_cdns(url, kind, rev):
    hit = classify(url)
    assert hit is not None and hit.kind == kind and hit.requested == rev


@pytest.mark.parametrize("url,addon", [
    ("https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js", "controls/OrbitControls.js"),
    ("https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/postprocessing/UnrealBloomPass.js",
     "postprocessing/UnrealBloomPass.js"),
    ("https://unpkg.com/three@0.160.0/addons/controls/OrbitControls.js", "controls/OrbitControls.js"),
    ("https://esm.sh/three@0.160.0/examples/jsm/controls/OrbitControls.js", "controls/OrbitControls.js"),
])
def test_vendored_addons(url, addon):
    hit = classify(url)
    assert hit is not None and hit.kind == "addon" and hit.addon == addon


@pytest.mark.parametrize("url", [
    # an addon MC does not ship
    "https://unpkg.com/three@0.160.0/examples/jsm/loaders/GLTFLoader.js",
    # other libraries on the same CDNs
    "https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js",
    "https://unpkg.com/three-stdlib@2.29.0/index.js",
    "https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.9.0/p5.min.js",
    "https://esm.sh/threejs-toys",
    # three's WebGPU build (not served)
    "https://unpkg.com/three@0.170.0/build/three.webgpu.js",
    # an unknown host serving a "three.module.js"
    "https://evil.example/three@0.160.0/build/three.module.js",
    "http://192.0.2.10:8000/three.module.js",
    # models, fonts, textures from three's own site
    "https://threejs.org/examples/models/gltf/DamagedHelmet.glb",
    "file:///shared-deliverables/three.module.js",
])
def test_everything_else_is_not_three(url):
    assert classify(url) is None


def test_a_classic_examples_js_addon_is_already_in_the_global_build():
    hit = classify("https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js")
    assert hit is not None and hit.kind == "classic_addon" and hit.addon == "OrbitControls"
    assert classify("https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/GLTFLoader.js") is None


@pytest.mark.parametrize("text,rev", [
    ("0.160.0", "160"), ("0.160", "160"), ("^0.160.0", "160"), ("r128", "128"),
    ("128", "128"), ("latest", None), (None, None), ("1.2.3", None),
])
def test_revision_of(text, rev):
    assert revision_of(text) == rev


# ── answering ────────────────────────────────────────────────────────────


def test_served_bytes_are_the_vendored_files():
    three = LocalThree()
    body, ctype = three.serve("https://unpkg.com/three@0.160.0/build/three.module.js")
    assert body == (VENDOR_DIR / "three.module.min.js").read_bytes()
    assert ctype.startswith("text/javascript")
    body, _ = three.serve("https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js")
    assert body == (VENDOR_DIR / "three.global.min.js").read_bytes()
    body, _ = three.serve("https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js")
    assert body == (VENDOR_DIR / "addons/controls/OrbitControls.js").read_bytes()


def test_an_addon_from_a_rewriting_cdn_imports_that_cdns_three():
    """esm.sh rewrites `from 'three'` to its own URL — so do we, or the page
    (which has no import map) could not resolve the bare specifier."""
    body, _ = LocalThree().serve("https://esm.sh/three@0.160.0/examples/jsm/controls/OrbitControls.js")
    text = body.decode()
    assert "from 'https://esm.sh/three@0.160.0'" in text
    assert "from 'three'" not in text


def test_report_names_the_lane_and_the_version_the_page_asked_for():
    three = LocalThree()
    assert three.report() == {"lib_track": "none", "libraries_provided": []}
    three.serve("https://unpkg.com/three@0.160.0/build/three.module.js")
    three.serve("https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js")
    report = three.report()
    assert report["lib_track"] == "three-local"
    assert report["libraries_provided"] == [f"three.js r{THREE_REVISION}"]
    assert report["three_requested"] == ["r160"]
    assert report["three_version_mismatch"] is (THREE_REVISION != "160")
    assert report["three_import_map_injected"] is False


def test_same_version_is_no_mismatch():
    three = LocalThree()
    three.serve(f"https://unpkg.com/three@0.{THREE_REVISION}.0/build/three.module.js")
    assert three.report()["three_version_mismatch"] is False


# ── route decision: three served, the rest still blocked ─────────────────


def test_route_serves_three_without_counting_it_as_blocked():
    guard, three = RecordNetworkGuard(PAGE), LocalThree()
    action = route_decision("https://unpkg.com/three@0.160.0/build/three.module.js", "script",
                            guard=guard, three=three)
    assert action[0] == "fulfill"
    assert guard.report()["network_attempts"] == 0
    assert guard.report()["network_blocked_scripts"] == 0


def test_route_still_blocks_other_libraries_and_counts_them():
    guard, three = RecordNetworkGuard(PAGE), LocalThree()
    assert route_decision("https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js", "script",
                          guard=guard, three=three) == ("abort",)
    assert route_decision("https://unpkg.com/three@0.160.0/examples/jsm/loaders/GLTFLoader.js", "script",
                          guard=guard, three=three) == ("abort",)
    assert route_decision("http://192.0.2.10:8000/v1/models", "fetch", guard=guard, three=three) == ("abort",)
    report = guard.report()
    assert report["network_attempts"] == 3 and report["network_blocked_scripts"] == 2
    assert three.report()["lib_track"] == "none"


def test_route_passes_the_page_and_injects_the_import_map_only_into_the_page():
    guard, three = RecordNetworkGuard(PAGE), LocalThree()
    assert route_decision(PAGE, "document", guard=guard, three=three) == ("continue",)
    action = route_decision(PAGE, "document", guard=guard, three=three, page_body="<html>x</html>")
    assert action == ("fulfill", b"<html>x</html>", "text/html; charset=utf-8")
    other = "file:///shared-deliverables/bench-x/Model-B/index.html"
    assert route_decision(other, "document", guard=guard, three=three, page_body="<html>x</html>") == ("abort",)


# ── bare `import … from 'three'` ─────────────────────────────────────────


BARE = """<!doctype html><html><head><title>t</title></head><body>
<script type="module">import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';</script></body></html>"""
MAPPED = """<html><head><script type="importmap">{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js"}}</script>
</head><body><script type="module">import * as THREE from 'three';</script></body></html>"""


def test_mcs_own_import_map_is_not_what_the_page_asked_for():
    three = LocalThree()
    three.serve(CANONICAL_MODULE_URL)
    report = three.report()
    assert report["lib_track"] == "three-local"
    assert report["three_requested"] == [] and report["three_version_mismatch"] is False


def test_a_bare_import_without_map_gets_mcs_import_map_before_the_module():
    assert needs_import_map(BARE)
    out = inject_import_map(BARE)
    assert out.index('type="importmap"') < out.index('type="module"')
    data = json.loads(re.search(r'<script type="importmap">(.*?)</script>', out).group(1))
    assert classify(data["imports"]["three"]).kind == "module"
    assert classify(data["imports"]["three/addons/"] + "controls/OrbitControls.js").kind == "addon"


@pytest.mark.parametrize("html", [
    MAPPED,  # the page maps three itself
    "<html><body><script type=module>import * as THREE from 'https://esm.sh/three@0.160.0';</script></body></html>",
    "<html><body><script>const three = 'three';</script></body></html>",  # not a module import
    "<html><body><canvas></canvas></body></html>",
])
def test_no_import_map_when_the_page_does_not_need_one(html):
    assert needs_import_map(html) is False


# ── "pasted library" detection ignores the served copy ───────────────────


def test_a_cdn_page_is_not_labelled_as_pasted_three():
    assert detect_inlined_libraries(MAPPED) == []
    assert detect_inlined_libraries(inject_import_map(BARE)) == []


def test_record_response_carries_the_lane():
    r = RecordResponse(video_path="/v.mp4", screenshot_path="", duration_s=10, bytes=1)
    assert r.lib_track is None  # not reported = older sidecar, never "none"
    assert r.libraries_provided == []


def test_record_endpoint_uses_the_route_decision():
    import ast

    src = (Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    record_fn = next(n for n in ast.walk(ast.parse(src))
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == "record")
    body = ast.unparse(record_fn)
    for needle in ("route_decision", "LocalThree", "route.fulfill", "needs_import_map",
                   "lib_track", "libraries_provided", "three_version_mismatch"):
        assert needle in body, needle


def test_the_image_ships_the_vendored_copy():
    docker = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "COPY record_three.py /app/record_three.py" in docker
    assert "COPY vendor/ /app/vendor/" in docker
