"""three.js served locally for /record (lane ``lib:three-local``) — pure, no playwright.

The recorder blocks every network request (record_network.py). Models write
3D pages the way three.js documents them: an import map pointing ``three``
at a CDN, or a classic ``<script src=".../three.min.js">``. Blocked, the
scene stays empty and the bench gate turns red (R6 ``needs_network``).

MC therefore answers exactly those requests itself, from ONE pinned copy in
``vendor/three/`` (see its build.sh + MANIFEST.json, MIT licence alongside):

- ``three`` as an ES module (import map, ``import … from 'https://…'``,
  esm.sh / skypack / jspm style URLs),
- the classic global ``THREE`` (``build/three.min.js``, cdnjs ``rNNN``),
- a short list of addons (``three/addons/…`` = ``examples/jsm/…``): orbit
  camera, bloom post-processing, noise, sky, rounded box, room environment.
  An addon that is not vendored stays blocked — and stays R6 red.

Only known CDN hosts and three's own paths are answered; everything else is
still blocked and counted. A page that imports the bare specifier ``three``
without any import map (works only with a bundler) gets an import map
injected into its document. Every contestant gets the same file; the
version the page ASKED for is reported next to the one MC served, so a
mismatch (page wants r128, MC serves r170) is printed, not hidden.

Unit tests: docker/mc-playwright/tests/test_record_three.py
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "three"

#: Lane names (receipt ``lib_track``). One test never compares them silently.
LANE_THREE_LOCAL = "three-local"
LANE_NONE = "none"

#: Host the injected import map points at. ``.invalid`` never resolves, so
#: even a request that slipped past routing could not leave the machine.
CANONICAL_HOST = "three.mc.invalid"

_CDN_HOSTS = frozenset({
    "unpkg.com", "cdn.jsdelivr.net", "fastly.jsdelivr.net", "cdnjs.cloudflare.com",
    "esm.sh", "cdn.skypack.dev", "ga.jspm.io", "threejs.org", CANONICAL_HOST,
})
# Hosts that rewrite a module's imports to absolute URLs themselves. Our
# vendored addons import the bare ``three`` — served from these hosts, that
# import is rewritten to the same host's three, as the real CDN would.
_REWRITING_HOSTS = frozenset({"esm.sh", "cdn.skypack.dev", "ga.jspm.io"})

_MODULE_FILES = frozenset({
    "build/three.module.js", "build/three.module.min.js",
    "build/three.core.js", "build/three.core.min.js",
    "three.module.js", "three.module.min.js",  # cdnjs layout
})
_GLOBAL_FILES = frozenset({"build/three.min.js", "build/three.js", "three.min.js", "three.js"})
_ESM_BUNDLE = re.compile(r"^(?:es20\d\d|esnext|denonext)/three(?:\.\w+)*\.m?js$")


@dataclass(frozen=True)
class ThreeHit:
    """One request the recorder answers with the local copy."""

    kind: str  # "module" | "global" | "addon" | "classic_addon"
    requested: str | None  # revision the URL asked for ("160"), None = unversioned
    addon: str | None = None  # "controls/OrbitControls.js"
    rewrite_three_to: str | None = None  # absolute URL for a bare `three` in an addon


def _load_manifest() -> dict:
    try:
        return json.loads((VENDOR_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_MANIFEST = _load_manifest()
#: The one revision MC serves ("170").
THREE_REVISION: str = str(_MANIFEST.get("revision") or "")
THREE_VERSION: str = str(_MANIFEST.get("three_version") or "")
VENDORED_ADDONS: frozenset[str] = frozenset(
    p[len("addons/"):] for p in (_MANIFEST.get("files") or {}) if p.startswith("addons/")
)
#: Names the classic build carries as ``THREE.<Name>`` (OrbitControls, …).
GLOBAL_ADDON_NAMES: frozenset[str] = frozenset(Path(a).stem for a in VENDORED_ADDONS)

CANONICAL_MODULE_URL = f"https://{CANONICAL_HOST}/three@{THREE_VERSION}/build/three.module.js"
CANONICAL_ADDONS_URL = f"https://{CANONICAL_HOST}/three@{THREE_VERSION}/examples/jsm/"


def provided_label() -> str:
    return f"three.js r{THREE_REVISION}"


def revision_of(version: str | None) -> str | None:
    """'0.160.0' / '0.160' / '^0.160.0' / 'r128' / '128' -> '160' / '128'.
    Tags ('latest') and missing versions -> None."""
    if not version:
        return None
    v = version.strip().lstrip("^~=v")
    m = re.fullmatch(r"r?(\d{2,3})", v)
    if m:
        return m.group(1)
    m = re.fullmatch(r"0\.(\d{2,3})(?:\.\d+)?(?:[-+.].*)?", v)
    return m.group(1) if m else None


def _split_package(path: str, prefix: str) -> tuple[str | None, str] | None:
    """'/npm/three@0.160.0/build/x.js' with prefix '/npm/' -> ('0.160.0', 'build/x.js')."""
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    m = re.match(r"^three(?:@([^/]+))?(?:/(.*))?$", rest)
    if not m:
        return None
    return m.group(1), m.group(2) or ""


def _hit_for(host: str, version: str | None, inner: str, *, bare_is: str,
             three_url: str) -> ThreeHit | None:
    rev = revision_of(version)
    inner = inner.strip("/")
    if inner in ("", "+esm") or inner.startswith("+esm") or _ESM_BUNDLE.match(inner):
        if inner == "":
            return ThreeHit(bare_is, rev)
        return ThreeHit("module", rev)
    if inner in _MODULE_FILES:
        return ThreeHit("module", rev)
    if inner in _GLOBAL_FILES:
        return ThreeHit("global", rev)
    for prefix in ("examples/jsm/", "addons/"):
        if inner.startswith(prefix):
            addon = inner[len(prefix):]
            if addon.endswith("/+esm"):
                addon = addon[: -len("/+esm")]
            if not addon.endswith(".js"):
                addon += ".js"
            if addon in VENDORED_ADDONS:
                rewrite = three_url if host in _REWRITING_HOSTS else None
                return ThreeHit("addon", rev, addon=addon, rewrite_three_to=rewrite)
            return None
    if inner.startswith("examples/js/"):
        name = Path(inner).stem
        if name in GLOBAL_ADDON_NAMES:
            return ThreeHit("classic_addon", rev, addon=name)
    return None


def classify(url: str) -> ThreeHit | None:
    """The local three.js file a request stands for, or None (stays blocked)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower()
    if host not in _CDN_HOSTS:
        return None
    path = p.path
    origin = f"{p.scheme}://{p.netloc}"
    query = p.query
    if host in ("unpkg.com",):
        parts = _split_package(path, "/")
        if parts:
            v, inner = parts
            bare = "module" if "module" in query else "global"
            return _hit_for(host, v, inner, bare_is=bare, three_url="")
        return None
    if host in ("cdn.jsdelivr.net", "fastly.jsdelivr.net"):
        parts = _split_package(path, "/npm/")
        if parts:
            v, inner = parts
            return _hit_for(host, v, inner, bare_is="global", three_url="")
        m = re.match(r"^/gh/mrdoob/three\.js@([^/]+)/(.*)$", path)
        if m:
            return _hit_for(host, m.group(1), m.group(2), bare_is="global", three_url="")
        return None
    if host == "cdnjs.cloudflare.com":
        m = re.match(r"^/ajax/libs/three\.js/([^/]+)/(.+)$", path)
        if m:
            return _hit_for(host, m.group(1), m.group(2), bare_is="global", three_url="")
        return None
    if host in ("esm.sh", "cdn.skypack.dev"):
        # esm.sh also serves '/v135/three@0.160.0/es2022/three.mjs'.
        stripped = re.sub(r"^/(?:v\d+|stable)/", "/", path)
        parts = _split_package(stripped, "/")
        if parts:
            v, inner = parts
            three_url = f"{origin}/three@{v}" if v else f"{origin}/three"
            return _hit_for(host, v, inner, bare_is="module", three_url=three_url)
        return None
    if host == "ga.jspm.io":
        parts = _split_package(path, "/npm:")
        if parts:
            v, inner = parts
            three_url = f"{origin}/npm:three@{v}/build/three.module.js"
            return _hit_for(host, v, inner, bare_is="module", three_url=three_url)
        return None
    if host == "threejs.org":
        return _hit_for(host, None, path, bare_is="global", three_url="")
    if host == CANONICAL_HOST:
        parts = _split_package(path, "/")
        if parts:
            v, inner = parts
            return _hit_for(host, v, inner, bare_is="module", three_url="")
    return None


_BARE_THREE_IMPORT = re.compile(r"""(\bfrom\s*|\bimport\s*\(?\s*)(['"])three(['"])""")


def _addon_body(hit: ThreeHit) -> bytes:
    text = (VENDOR_DIR / "addons" / str(hit.addon)).read_text(encoding="utf-8")
    if hit.rewrite_three_to:
        text = _BARE_THREE_IMPORT.sub(lambda m: f"{m.group(1)}{m.group(2)}{hit.rewrite_three_to}{m.group(3)}", text)
    return text.encode("utf-8")


def body_for(hit: ThreeHit) -> bytes:
    if hit.kind == "module":
        return (VENDOR_DIR / "three.module.min.js").read_bytes()
    if hit.kind == "global":
        return (VENDOR_DIR / "three.global.min.js").read_bytes()
    if hit.kind == "addon":
        return _addon_body(hit)
    # A classic examples/js/… addon: the global build already carries it.
    return (f"/* MC bench recorder: THREE.{hit.addon} is part of the local {provided_label()} "
            f"global build. */\n").encode("utf-8")


# ── import map for a bare `import … from 'three'` ────────────────────────

_IMPORT_MAP_TAG = re.compile(r"<script\b[^>]*\btype\s*=\s*['\"]?importmap['\"]?[^>]*>(.*?)</script>", re.I | re.S)
_MODULE_SCRIPT = re.compile(r"<script\b[^>]*\btype\s*=\s*['\"]?module['\"]?[^>]*>(.*?)</script>", re.I | re.S)
_BARE_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)(['"])three(?:/[^'"]*)?\1""")


def needs_import_map(html: str) -> bool:
    """A module script imports the bare ``three`` and no import map maps it."""
    if not any(_BARE_SPECIFIER.search(m.group(1)) for m in _MODULE_SCRIPT.finditer(html)):
        return False
    for m in _IMPORT_MAP_TAG.finditer(html):
        try:
            imports = (json.loads(m.group(1)) or {}).get("imports") or {}
        except (ValueError, AttributeError):
            continue
        if "three" in imports:
            return False
    return True


def import_map_tag() -> str:
    data = {"imports": {"three": CANONICAL_MODULE_URL, "three/addons/": CANONICAL_ADDONS_URL,
                        "three/examples/jsm/": CANONICAL_ADDONS_URL}}
    return f'<script type="importmap">{json.dumps(data)}</script>'


def inject_import_map(html: str) -> str:
    """The page with MC's import map placed before its first script (an
    import map must come before the module that uses it)."""
    tag = import_map_tag()
    m = re.search(r"<head\b[^>]*>", html, re.I)
    if m:
        return html[: m.end()] + tag + html[m.end():]
    m = re.search(r"<script\b", html, re.I)
    if m:
        return html[: m.start()] + tag + html[m.start():]
    return tag + html


# ── per recording ────────────────────────────────────────────────────────


class LocalThree:
    """What one recording asked for and got. ``serve`` answers a request
    (bytes, content type) or returns None (the network block decides)."""

    def __init__(self) -> None:
        self.served = 0
        self.import_map_injected = False
        self._requested: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(THREE_REVISION) and (VENDOR_DIR / "three.module.min.js").is_file()

    def serve(self, url: str) -> tuple[bytes, str] | None:
        if not self.available:
            return None
        hit = classify(url)
        if hit is None:
            return None
        try:
            body = body_for(hit)
        except OSError:
            return None
        self.served += 1
        # MC's own import map points at the canonical host: that version is
        # what MC chose, not what the page asked for.
        if hit.requested and urlparse(url).hostname != CANONICAL_HOST:
            self._requested.add(hit.requested)
        return body, "text/javascript; charset=utf-8"

    def report(self) -> dict:
        if not self.served:
            return {"lib_track": LANE_NONE, "libraries_provided": []}
        requested = sorted(self._requested, key=int)
        return {
            "lib_track": LANE_THREE_LOCAL,
            "libraries_provided": [provided_label()],
            "three_requested": [f"r{r}" for r in requested],
            "three_version_mismatch": any(r != THREE_REVISION for r in requested),
            "three_import_map_injected": self.import_map_injected,
        }
