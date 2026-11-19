"""templates/bench/fetch_fonts.py — the brand fonts must end up INSIDE the
generated embedded-fonts.css, never as a CDN reference.

Live finding (2026-10-22): the renderer's embedded-fonts.css held 18
`url('//cdn.fontshare.com/…')` references — Fontshare quotes its URLs and
lists woff2 + woff + ttf per face; the old regex only matched unquoted
`url(….woff2)`, so Clash Display / General Sans were never inlined and every
video fell back to DejaVu Sans. The container has no network at render time,
so a remote reference is a silently missing font.

No network here: every test injects a fake fetcher.
"""
from __future__ import annotations

import base64
import importlib.util
import re
import urllib.error
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "templates" / "bench" / "fetch_fonts.py"
_spec = importlib.util.spec_from_file_location("fetch_fonts", _SCRIPT)
fetch_fonts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_fonts)

# Shape of the real Fontshare CSS (api.fontshare.com/v2/css, 2026-10-22):
# single-quoted protocol-relative URLs, three formats per face.
FONTSHARE_CSS = """/* Clash Display */
@font-face {
  font-family: 'Clash Display';
  src: url('//cdn.fontshare.com/wf/AAA/BBB/CLASH600.woff2') format('woff2'),
       url('//cdn.fontshare.com/wf/AAA/BBB/CLASH600.woff') format('woff'),
       url('//cdn.fontshare.com/wf/AAA/BBB/CLASH600.ttf') format('truetype');
  font-weight: 600;
  font-display: swap;
  font-style: normal;
}
/* General Sans */
@font-face {
  font-family: 'General Sans';
  src: url("//cdn.fontshare.com/wf/CCC/DDD/GENERAL400.woff2") format("woff2"),
       url("//cdn.fontshare.com/wf/CCC/DDD/GENERAL400.woff") format("woff"),
       url("//cdn.fontshare.com/wf/CCC/DDD/GENERAL400.ttf") format("truetype");
  font-weight: 400;
  font-display: swap;
  font-style: normal;
}
"""

# Shape of the Google Fonts css2 response: unquoted absolute https URL.
GOOGLE_CSS = """/* latin */
@font-face {
  font-family: 'JetBrains Mono';
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url(https://fonts.gstatic.com/s/jetbrainsmono/v24/JBM400.woff2) format('woff2');
  unicode-range: U+0000-00FF;
}
"""

FONT_BYTES = b"wOF2-fake-font-bytes"


def _fake_fetch(calls: list[str] | None = None):
    def fetch(url: str) -> bytes:
        if calls is not None:
            calls.append(url)
        if url.startswith("https://api.fontshare.com/"):
            return FONTSHARE_CSS.encode()
        if url.startswith("https://fonts.googleapis.com/"):
            return GOOGLE_CSS.encode()
        if url.endswith(".woff2"):
            return FONT_BYTES
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


def _remote_url_count(css: str) -> int:
    return len(re.findall(r"url\(\s*['\"]?(?:https?:)?//", css))


# ── embed_fonts_css ──────────────────────────────────────────────────────────


def test_quoted_fontshare_urls_are_inlined_and_no_cdn_reference_remains():
    out = fetch_fonts.embed_fonts_css(FONTSHARE_CSS + GOOGLE_CSS, _fake_fetch())
    # The acceptance grep (concept W0.4): `grep -c cdn.fontshare` must be 0.
    assert "cdn.fontshare" not in out
    assert _remote_url_count(out) == 0
    b64 = base64.b64encode(FONT_BYTES).decode()
    # one inlined woff2 per face — Clash Display, General Sans, JetBrains Mono
    assert out.count(f"url(data:font/woff2;base64,{b64})") == 3


def test_woff_and_ttf_fallbacks_are_dropped_not_fetched():
    calls: list[str] = []
    out = fetch_fonts.embed_fonts_css(FONTSHARE_CSS, _fake_fetch(calls))
    assert not [c for c in calls if c.endswith((".woff", ".ttf"))]
    assert "format('woff')" not in out and 'format("woff")' not in out
    assert "truetype" not in out
    # the declaration stays well-formed: one source, terminated by ";"
    for decl in fetch_fonts._SRC_DECL_RE.findall(out):
        decl = decl[1]
        assert decl.strip().startswith("url(data:font/woff2;base64,")
        assert decl.count("url(") == 1


def test_protocol_relative_url_is_fetched_over_https():
    calls: list[str] = []
    fetch_fonts.embed_fonts_css(FONTSHARE_CSS, _fake_fetch(calls))
    assert "https://cdn.fontshare.com/wf/AAA/BBB/CLASH600.woff2" in calls


def test_face_without_any_woff2_source_is_an_error():
    css = """@font-face {
  font-family: 'Clash Display';
  src: url('//cdn.fontshare.com/x.ttf') format('truetype');
}"""
    with pytest.raises(fetch_fonts.FontEmbedError):
        fetch_fonts.embed_fonts_css(css, _fake_fetch())


# ── check_embedded ───────────────────────────────────────────────────────────


def test_check_embedded_rejects_a_remaining_cdn_reference():
    ok = fetch_fonts.embed_fonts_css(FONTSHARE_CSS + GOOGLE_CSS, _fake_fetch())
    fetch_fonts.check_embedded(ok)  # sanity: the good output passes
    broken = ok + "\n@font-face{font-family:'X';src:url('//cdn.fontshare.com/y.woff2');}"
    with pytest.raises(fetch_fonts.FontEmbedError, match="remote"):
        fetch_fonts.check_embedded(broken)


def test_check_embedded_requires_every_brand_family():
    only_mono = fetch_fonts.embed_fonts_css(GOOGLE_CSS, _fake_fetch())
    with pytest.raises(fetch_fonts.FontEmbedError, match="Clash Display"):
        fetch_fonts.check_embedded(only_mono)


# ── main(): the Docker build step ────────────────────────────────────────────


def test_main_writes_embedded_css_and_exits_zero(tmp_path):
    out = tmp_path / "fonts" / "embedded-fonts.css"
    rc = fetch_fonts.main(fetch=_fake_fetch(), out_path=out, env={}, sleep=lambda s: None)
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "cdn.fontshare" not in text
    assert _remote_url_count(text) == 0


def test_main_fails_the_build_when_a_remote_reference_would_remain(tmp_path, monkeypatch):
    """Belt and braces: even if the inliner missed a URL shape, main() must
    refuse to write a file with a CDN reference (exit 1 = docker build fails)."""
    monkeypatch.setattr(fetch_fonts, "embed_fonts_css", lambda css, fetch: css)
    out = tmp_path / "embedded-fonts.css"
    rc = fetch_fonts.main(fetch=_fake_fetch(), out_path=out, env={}, sleep=lambda s: None)
    assert rc == 1
    assert not out.exists()


def test_main_fails_the_build_on_network_error_by_default(tmp_path):
    def offline(url: str) -> bytes:
        raise urllib.error.URLError("no network")

    out = tmp_path / "embedded-fonts.css"
    rc = fetch_fonts.main(fetch=offline, out_path=out, env={}, sleep=lambda s: None)
    assert rc == 1
    assert not out.exists()


def test_main_retries_a_flaky_network_before_giving_up(tmp_path):
    attempts = {"n": 0}
    good = _fake_fetch()

    def flaky(url: str) -> bytes:
        if url.startswith("https://api.fontshare.com/") and attempts["n"] < 2:
            attempts["n"] += 1
            raise urllib.error.URLError("blip")
        return good(url)

    out = tmp_path / "embedded-fonts.css"
    rc = fetch_fonts.main(fetch=flaky, out_path=out, env={}, sleep=lambda s: None)
    assert rc == 0
    assert attempts["n"] == 2


def test_offline_opt_out_writes_the_fallback_without_any_remote_reference(tmp_path):
    """BENCH_FONTS_OPTIONAL=1 (docker build-arg) is the explicit offline
    escape hatch: the build passes with system fonts — but it still never
    writes a CDN reference."""
    def offline(url: str) -> bytes:
        raise urllib.error.URLError("no network")

    out = tmp_path / "embedded-fonts.css"
    rc = fetch_fonts.main(
        fetch=offline, out_path=out, env={"BENCH_FONTS_OPTIONAL": "1"}, sleep=lambda s: None
    )
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "url(" not in text


def test_offline_opt_out_is_reachable_from_compose_and_env_example():
    """Review of #944: mc-playwright is also the agents' visual verifier
    (profile `browser`). A build-arg the Dockerfile declares but compose never
    passes would leave an offline install with no way to build it at all."""
    repo = Path(__file__).resolve().parents[3]
    dockerfile = (repo / "docker" / "mc-playwright" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG BENCH_FONTS_OPTIONAL=0$", dockerfile, re.M)

    compose = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    service = compose.split("\n  mc-playwright:\n", 1)[1].split("\n  # ", 1)[0]
    service = re.split(r"\n  [a-z0-9-]+:\n", service, maxsplit=1)[0]
    assert "BENCH_FONTS_OPTIONAL: ${BENCH_FONTS_OPTIONAL:-0}" in service

    env_example = (repo / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^BENCH_FONTS_OPTIONAL=0$", env_example, re.M)
