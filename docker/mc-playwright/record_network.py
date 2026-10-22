"""Network block for /record (W0.9) — pure helpers, no playwright import.

/record opens a model-written index.html in Chromium. That is untrusted code,
and the sidecar sits on the compose network, so without a block the page
could reach other containers and machines on the LAN (an unauthenticated
model server, for example) and could load libraries from a CDN that its
competitors were told not to use. The recorded page therefore gets no
network at all, except the page document itself and inline schemes:

  1. ``RecordNetworkGuard`` decides per request (browser-context routing for
     HTTP(S), file and WebSocket requests) and counts what it blocked.
  2. ``record_proxy_settings`` points the browser context at a dead local
     port as a second line: anything routing does not see still goes nowhere.
  3. ``record_network_guard.js`` (init script) stubs WebRTC, which neither
     routing nor an HTTP proxy covers, and counts attempts.

``detect_inlined_libraries`` scans the HTML for vendored library banners so a
page that pasted three.js is labelled as such (P2 V2), not compared as if it
were hand-written.

Unit tests: cd backend && python -m pytest ../docker/mc-playwright/tests/test_record_network.py
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

# Schemes that never leave the browser process.
RECORD_ALLOWED_SCHEMES = frozenset({"data", "blob", "about"})

# Discard port on loopback: nothing listens there, so a request that slips
# past routing fails at once instead of reaching the network.
RECORD_DEAD_PROXY = "http://127.0.0.1:9"

# How many blocked URLs the response lists (the count is always exact).
MAX_BLOCKED_URLS_REPORTED = 20
_BLOCKED_URL_MAX_LEN = 160

RECORD_NETWORK_GUARD_JS_PATH = Path(__file__).resolve().parent / "record_network_guard.js"


def load_record_network_guard_script() -> str:
    return RECORD_NETWORK_GUARD_JS_PATH.read_text(encoding="utf-8")


def _without_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _describe(url: str) -> str:
    """scheme://host/path without query or fragment, length-capped."""
    p = urlparse(url)
    if p.netloc:
        text = f"{p.scheme}://{p.netloc}{p.path}"
    else:
        text = f"{p.scheme}:{p.path}" if p.scheme else p.path
    return text[:_BLOCKED_URL_MAX_LEN]


class RecordNetworkGuard:
    """Per-recording allow/deny decision plus the attempt counter."""

    def __init__(self, page_url: str) -> None:
        self._page = _without_fragment(page_url)
        self.attempts = 0
        self._blocked: list[str] = []

    def allow(self, url: str) -> bool:
        scheme = urlparse(url).scheme.lower()
        if scheme in RECORD_ALLOWED_SCHEMES:
            return True
        if _without_fragment(url) == self._page:
            return True
        self.note_blocked(url)
        return False

    def note_blocked(self, url: str, count: int = 1) -> None:
        if count <= 0:
            return
        self.attempts += count
        if len(self._blocked) < MAX_BLOCKED_URLS_REPORTED:
            self._blocked.append(_describe(url))

    def report(self) -> dict:
        return {
            "network_blocked": True,
            "network_attempts": self.attempts,
            "blocked_requests": list(self._blocked),
        }


def record_proxy_settings(page_url: str) -> dict:
    """Playwright `proxy=` for the recording context. ``<-loopback>`` removes
    Chromium's implicit proxy bypass for localhost/127.0.0.1; an http(s) page
    bypasses the proxy only for its own host so the document itself loads
    (routing still blocks everything on that host except the document)."""
    bypass = ["<-loopback>"]
    p = urlparse(page_url)
    if p.scheme in ("http", "https") and p.hostname:
        bypass.append(p.hostname)
    return {"server": RECORD_DEAD_PROXY, "bypass": ",".join(bypass)}


# ── Library banner scan ────────────────────────────────────────────────────

# (display name, presence pattern, version pattern or None). Banners are what
# minifiers keep (`/*! … */`, `@license`); a <script src=cdn> tag is not
# inlined code and does not match (it shows up as a blocked request instead).
_LIBRARIES: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
    ("Chart.js", re.compile(r"Chart\.js v\d"), re.compile(r"Chart\.js v(\d+(?:\.\d+)*)")),
    ("D3", re.compile(r"d3js\.org v\d"), re.compile(r"d3js\.org v(\d+(?:\.\d+)*)")),
    ("GSAP", re.compile(r"\bGSAP \d"), re.compile(r"\bGSAP (\d+(?:\.\d+)*)")),
    ("jQuery", re.compile(r"jQuery (?:JavaScript Library )?v\d"),
     re.compile(r"jQuery (?:JavaScript Library )?v(\d+(?:\.\d+)*)")),
    ("p5.js", re.compile(r"p5\.js v\d"), re.compile(r"p5\.js v(\d+(?:\.\d+)*)")),
    ("PixiJS", re.compile(r"pixi\.js - v\d", re.I), re.compile(r"pixi\.js - v(\d+(?:\.\d+)*)", re.I)),
    ("anime.js", re.compile(r"anime\.js v\d", re.I), re.compile(r"anime\.js v(\d+(?:\.\d+)*)", re.I)),
    ("matter-js", re.compile(r"matter-js \d"), re.compile(r"matter-js (\d+(?:\.\d+)*)")),
    ("Babylon.js", re.compile(r"Babylon\.js v?\d|babylonjs\.com"), None),
    ("Tone.js", re.compile(r"Tone\.js v?\d"), re.compile(r"Tone\.js v?(\d+(?:\.\d+)*)")),
    ("howler.js", re.compile(r"howler\.js v\d"), re.compile(r"howler\.js v(\d+(?:\.\d+)*)")),
    ("Lodash", re.compile(r"@license\s+Lodash|lodash\.com/license"), None),
    ("React", re.compile(r"@license React|react\.production\.min\.js"), None),
    ("Vue", re.compile(r"Vue\.js v\d"), re.compile(r"Vue\.js v(\d+(?:\.\d+)*)")),
]

_THREE_PRESENT = re.compile(r"Three\.js Authors|window\.__THREE__\s*=")
_THREE_REVISION_CONST = re.compile(r"\bREVISION\s*=\s*['\"](\d+)['\"]")
_THREE_GLOBAL_VAR = re.compile(r"window\.__THREE__\s*=\s*([A-Za-z_$][\w$]*)")


def _three_revision(html: str) -> str | None:
    m = _THREE_REVISION_CONST.search(html)
    if m:
        return m.group(1)
    # Minified builds: `const e="160"; … window.__THREE__=e`.
    m = _THREE_GLOBAL_VAR.search(html)
    if m:
        var = re.escape(m.group(1))
        values = re.findall(rf"(?<![\w$]){var}\s*=\s*['\"](\d{{2,3}})['\"]", html[: m.start()])
        if values:
            return values[-1]
    return None


def detect_inlined_libraries(html: str) -> list[str]:
    """Names (with version when the banner carries one) of libraries pasted
    into the page, sorted case-insensitively. Empty for hand-written code."""
    found: list[str] = []
    if _THREE_PRESENT.search(html):
        rev = _three_revision(html)
        found.append(f"three.js r{rev}" if rev else "three.js")
    for name, present, version in _LIBRARIES:
        if not present.search(html):
            continue
        m = version.search(html) if version else None
        found.append(f"{name} {m.group(1)}" if m else name)
    return sorted(found, key=str.lower)
