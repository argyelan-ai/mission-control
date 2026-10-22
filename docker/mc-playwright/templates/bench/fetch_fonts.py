#!/usr/bin/env python3
"""Build-time font fetcher for the branded bench video cards.

Downloads Clash Display (500/600/700) + General Sans (400/500/600) from
Fontshare and JetBrains Mono (400/500/700) from Google Fonts, inlines every
.woff2 as a base64 data-URI, and writes a single `embedded-fonts.css` next to
this script.

Licensing: Fontshare's free fonts may be used in a project but its CSS/font
files must NOT be redistributed verbatim in a public repo. So this script
runs at DOCKER BUILD TIME (see Dockerfile) — the generated CSS lives only in
the built image, never in git (.gitignored).

Why the output must hold NO remote URL: the renderer screenshots the cards
in a container without font access at render time — a `url(//cdn…)` left in
the CSS is a silently missing font, and every video then shows DejaVu Sans
instead of the brand fonts. That happened for months: Fontshare quotes its
URLs (`url('//cdn.fontshare.com/….woff2')`) and lists woff2 + woff + ttf per
face; the old regex only matched unquoted `url(….woff2)`, so all 18
Fontshare references stayed remote (live renderer, 2026-10-22).

Failure policy (changed 2026-10-22): a build that would produce wrong fonts
FAILS (exit 1 -> `docker build` stops) instead of shipping a fallback
silently. The network fetch is retried a few times first. For a deliberately
offline build, set `BENCH_FONTS_OPTIONAL=1` (Dockerfile build-arg): network
errors then write a font-less CSS (system-font fallback) and exit 0. A
remote reference is never written, in either mode.
"""
from __future__ import annotations

import base64
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

HERE = Path(__file__).resolve().parent
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

# Every family the templates name — a CSS without one of them is a build error.
REQUIRED_FAMILIES = ("Clash Display", "General Sans", "JetBrains Mono")

FETCH_ATTEMPTS = 3
FETCH_BACKOFF_S = 3.0

FALLBACK_CSS = "/* fetch_fonts.py: offline build (BENCH_FONTS_OPTIONAL=1) — system-font fallback */\n"

# One `src:` declaration up to its terminating ";" — a url(…) counts as one
# atomic unit, so the ";" inside "data:font/woff2;base64," never ends it.
_SRC_DECL_RE = re.compile(r"(src\s*:)((?:url\([^)]*\)|[^;])*)(;)")
# Split the source list at the commas BETWEEN sources (a source starts with
# url( or local( ) — never inside a url().
_SRC_SPLIT_RE = re.compile(r",\s*(?=(?:url|local)\s*\()")
# url(…) with no, single or double quotes.
_URL_RE = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""")
# Any url() pointing off-box: http(s):// or protocol-relative //.
_REMOTE_URL_RE = re.compile(r"""url\(\s*['"]?(?:https?:)?//""")

Fetch = Callable[[str], bytes]


class FontEmbedError(RuntimeError):
    """The generated CSS would not carry the brand fonts — fail the build."""


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — build-time only
        return resp.read()


def _absolute(url: str) -> str:
    return "https:" + url if url.startswith("//") else url


def _embed_source_list(sources: str, fetch: Fetch) -> str:
    """Keep the woff2 source of one @font-face (inlined as a data URI) and
    `local()` sources; drop the woff/ttf fallbacks — Chromium reads woff2,
    and every extra format would only be another remote reference."""
    kept: list[str] = []
    has_woff2 = False
    for item in _SRC_SPLIT_RE.split(sources.strip()):
        item = item.strip()
        if item.startswith("local"):
            kept.append(item)
            continue
        match = _URL_RE.search(item)
        if match is None:
            continue
        url = match.group(2).strip()
        if url.startswith("data:"):
            kept.append(item)
            has_woff2 = has_woff2 or url.startswith("data:font/woff2")
            continue
        if not url.split("?", 1)[0].endswith(".woff2"):
            continue  # woff / ttf / otf fallback — dropped
        b64 = base64.b64encode(fetch(_absolute(url))).decode("ascii")
        kept.append("url(data:font/woff2;base64," + b64 + ") format('woff2')")
        has_woff2 = True
    if not has_woff2:
        raise FontEmbedError(f"@font-face without a woff2 source: {sources.strip()[:120]}")
    return " " + ", ".join(kept)


def embed_fonts_css(css_text: str, fetch: Fetch) -> str:
    """Rewrite every `src:` declaration so it only holds inlined woff2 data
    URIs (plus local() sources). Raises FontEmbedError for a face without a
    woff2 source."""

    def _replace(match: re.Match) -> str:
        return match.group(1) + _embed_source_list(match.group(2), fetch) + match.group(3)

    return _SRC_DECL_RE.sub(_replace, css_text)


def check_embedded(css_text: str) -> None:
    """The gate the build relies on: no remote url() left, and every brand
    family has at least one @font-face. Raises FontEmbedError."""
    remote = _REMOTE_URL_RE.findall(css_text)
    if remote:
        raise FontEmbedError(f"{len(remote)} remote font reference(s) left in the CSS")
    for family in REQUIRED_FAMILIES:
        if not re.search(r"font-family\s*:\s*['\"]?" + re.escape(family), css_text):
            raise FontEmbedError(f"no @font-face for {family!r}")


def _fetch_with_retry(fetch: Fetch, url: str, sleep: Callable[[float], None]) -> bytes:
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return fetch(url)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            if attempt + 1 < FETCH_ATTEMPTS:
                sleep(FETCH_BACKOFF_S * (attempt + 1))
    assert last is not None
    raise last


def build_css(fetch: Fetch, sleep: Callable[[float], None]) -> str:
    def retrying(url: str) -> bytes:
        return _fetch_with_retry(fetch, url, sleep)

    fontshare_css = retrying(FONTSHARE_CSS_URL).decode("utf-8", errors="replace")
    jbmono_css = retrying(GOOGLE_JBMONO_CSS_URL).decode("utf-8", errors="replace")
    return embed_fonts_css(fontshare_css + "\n\n" + jbmono_css, retrying)


def main(
    *,
    fetch: Fetch = _fetch,
    out_path: Path = OUT_PATH,
    env: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    env = os.environ if env is None else env
    optional = env.get("BENCH_FONTS_OPTIONAL", "").strip() == "1"
    try:
        embedded = build_css(fetch, sleep)
        check_embedded(embedded)
    except FontEmbedError as exc:
        print(f"fetch_fonts: ERROR — {exc}. The bench videos would render without "
              f"the brand fonts; failing the build.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if optional:
            print(f"fetch_fonts: WARNING — font fetch failed ({exc}); "
                  f"BENCH_FONTS_OPTIONAL=1 -> system-font fallback", file=sys.stderr)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(FALLBACK_CSS, encoding="utf-8")
            return 0
        print(f"fetch_fonts: ERROR — font fetch failed after {FETCH_ATTEMPTS} attempts "
              f"({exc}). Retry the build, or build offline with "
              f"--build-arg BENCH_FONTS_OPTIONAL=1 (videos then use system fonts).",
              file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(embedded, encoding="utf-8")
    faces = embedded.count("@font-face")
    print(f"fetch_fonts: wrote {out_path} ({len(embedded)} bytes, {faces} faces, 0 remote)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
