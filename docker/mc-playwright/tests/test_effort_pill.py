"""Effort as its own pill in the bench video (round-2 Q2, operator decision
23.10.2026: the video says the level in English — "Off", "Max", "Default").

- The backend sends ``effort`` per model and per outro row; the frame draws it
  as a pill next to the model name that never wraps (t2 NICE-1: "MAX" fell
  alone onto a second line of the tag).
- An empty effort leaves no empty pill behind.
- An older backend without the field still composes (default "").
- /health announces ``branding_effort`` so the backend knows it may send the
  field (an older image ignores it and gets the old text form instead).
"""
from __future__ import annotations

import re
from pathlib import Path

from media import (
    BrandingModelSpec,
    BrandingOutroRow,
    BrandingSpec,
    build_frame_tokens,
    fill_bench_template,
    render_outro_html,
    render_outro_rows_html,
)

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "bench"


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _spec(efforts=("Off", "Max")) -> BrandingSpec:
    models = [
        BrandingModelSpec(label="DeepSeek-V4.1-Flash-1017", tag="LOCAL · DIRECT API", effort=e)
        for e in efforts
    ]
    rows = [
        BrandingOutroRow(name="DeepSeek-V4.1-Flash-1017", time="1 min 35 s", cost="local",
                         tokens="111 → 3.5k", effort=e)
        for e in efforts
    ]
    return BrandingSpec(title="Floating Particle", date_label="2026-10-23",
                        prompt_line="Make particles.", models=models, outro_rows=rows)


def test_older_backend_without_effort_still_validates():
    m = BrandingModelSpec(label="x", tag="LOCAL · DIRECT API")
    r = BrandingOutroRow(name="x", time="45 s")
    assert m.effort == "" and r.effort == ""


def test_side_by_side_frame_draws_each_effort_as_its_own_pill():
    html = fill_bench_template(_tpl("frame.html"), build_frame_tokens(_spec()))
    assert "{{" not in html
    pills = re.findall(r'<span class="effort">([^<]*)</span>', html)
    assert pills == ["Off", "Max"]
    # The tag no longer carries the level.
    assert "EFFORT" not in re.sub(r"<style>.*?</style>", "", html, flags=re.S)


def test_solo_frame_draws_the_effort_pill():
    html = fill_bench_template(_tpl("frame_single.html"), build_frame_tokens(_spec(("High",))))
    assert re.findall(r'<span class="effort">([^<]*)</span>', html) == ["High"]


def test_pill_never_wraps_and_empty_pill_is_hidden():
    for name in ("frame.html", "frame_single.html", "outro.html"):
        css = re.search(r"<style>(.*?)</style>", _tpl(name), flags=re.S).group(1)
        rule = re.search(r"\.effort\s*\{([^}]*)\}", css)
        assert rule, f"{name}: no .effort rule"
        assert "white-space:nowrap" in rule.group(1).replace(" ", ""), name
        assert re.search(r"\.effort:empty\s*\{[^}]*display:\s*none", css), name


def test_tag_never_wraps_either():
    for name in ("frame.html", "frame_single.html"):
        css = re.search(r"<style>(.*?)</style>", _tpl(name), flags=re.S).group(1)
        rule = re.search(r"\.chip \.speed\s*\{([^}]*)\}", css).group(1)
        assert "white-space:nowrap" in rule.replace(" ", ""), name


def test_outro_rows_carry_the_effort_next_to_the_name():
    rows = render_outro_rows_html(_spec().outro_rows)
    assert rows.count('<span class="effort">') == 2
    assert '<span class="effort">Off</span>' in rows
    assert '<span class="effort">Max</span>' in rows


def test_outro_row_without_effort_has_no_pill():
    rows = render_outro_rows_html([BrandingOutroRow(name="grok", time="9 min 24 s")])
    assert 'class="effort"' not in rows


def test_effort_is_escaped():
    rows = render_outro_rows_html([BrandingOutroRow(name="x", time="1 s", effort="<b>")])
    assert "<b>" not in rows and "&lt;b&gt;" in rows


def test_outro_fills_completely():
    html = render_outro_html(_tpl("outro.html"), _spec())
    assert "{{" not in html


def test_health_announces_branding_effort():
    """service.py needs playwright to import (not in the CI lane) — the
    feature list lives in media.py and health() returns it."""
    from media import RENDERER_FEATURES

    assert "branding_effort" in RENDERER_FEATURES
    source = (Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    health = source[source.index("async def health"):source.index("async def health") + 300]
    assert '"features": list(RENDERER_FEATURES)' in health
