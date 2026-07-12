#!/usr/bin/env python3
"""Build-time font fetcher for the branded bench video cards.

Downloads Clash Display (500/600/700) + General Sans (400/500/600) from
Fontshare and JetBrains Mono (400/500/700, latin subset) from Google Fonts,
inlines every .woff2 as a base64 data-URI, and writes a single
`embedded-fonts.css` next to this script.

Licensing: Fontshare's free fonts may be used in a project but its CSS/font
files must NOT be redistributed verbatim in a public repo. So this script
runs at DOCKER BUILD TIME (see Dockerfile) — the generated CSS lives only in
the built image, never in git (.gitignored).

Network-failure policy: this must never fail the image build. On any error
this prints a warning and writes an EMPTY embedded-fonts.css — the bench
cards then render with the browser's fallback fonts (still legible, just not
pixel-perfect). Never raises.
"""
from __future__ import annotations

import base64
import re
import sys
import urllib.request

HERE = __import__("pathlib").Path(__file__).resolve().parent
# frame.html / outro.html reference `fonts/embedded-fonts.css` (relative) —
# keep that directory layout.
OUT_DIR = HERE / "fonts"
OUT_PATH = OUT_DIR / "embedded-fonts.css"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

FONTSHARE_CSS_URL = (
    "https://api.fontshare.com/v2/css?f[]=clash-display@500,600,700"
    "&f[]=general-sans@400,500,600"
)
GOOGLE_JBMONO_CSS_URL = (
    "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700"
    "&display=swap&subset=latin"
)

_URL_RE = re.compile(r"url\(([^)]+\.woff2)\)")


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — build-time only
        return resp.read()


def _fetch_text(url: str) -> str:
    return _fetch(url).decode("utf-8", errors="replace")


def _inline_woff2_urls(css_text: str) -> str:
    """Replace every url(...woff2) reference with a base64 data-URI."""

    def _replace(match: re.Match) -> str:
        raw_url = match.group(1).strip("'\" ")
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        font_bytes = _fetch(raw_url)
        b64 = base64.b64encode(font_bytes).decode("ascii")
        return f"url(data:font/woff2;base64,{b64})"

    return _URL_RE.sub(_replace, css_text)


def main() -> int:
    try:
        fontshare_css = _fetch_text(FONTSHARE_CSS_URL)
        jbmono_css = _fetch_text(GOOGLE_JBMONO_CSS_URL)
        combined = fontshare_css + "\n\n" + jbmono_css
        embedded = _inline_woff2_urls(combined)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(embedded, encoding="utf-8")
        print(f"fetch_fonts: wrote {OUT_PATH} ({len(embedded)} bytes)")
        return 0
    except Exception as exc:  # noqa: BLE001 — build must never fail on network issues
        print(f"fetch_fonts: WARNING — font fetch failed ({exc}); "
              f"writing empty embedded-fonts.css (system-font fallback)", file=sys.stderr)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            "/* fetch_fonts.py failed at build time — system-font fallback */\n",
            encoding="utf-8",
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
