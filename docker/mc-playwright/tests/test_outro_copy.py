"""Outro + frame copy of the branded bench video (concept W0.5, 2026-10-22).

Every sentence on the card has to be true for the run it closes:
- no "weekly" (there is no cadence) and no "local llm benchmarks" (cloud runs
  exist),
- a solo run never asks "which one wins" (there is only one),
- no `RUN 0xx` counter (it shifted after deletes and never matched the
  published episode number) — the run date instead, plus "episode N" only
  when the run is published,
- the outro is held 4.5 s (2 s was too short to read on a phone),
- SIZE is not on the card (an inlined three.js copy is not a quality signal).
"""
from __future__ import annotations

from pathlib import Path

from media import (
    OUTRO_DURATION_S,
    BrandingModelSpec,
    BrandingOutroRow,
    BrandingSpec,
    build_branded_compose_cmd,
    build_frame_tokens,
    render_outro_html,
    render_outro_rows_html,
)

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "bench"


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _spec(n_models: int, date_label: str = "2026-10-21") -> BrandingSpec:
    models = [
        BrandingModelSpec(label="DeepSeek-V4.1-Flash-1017", tag="LOCAL · DIRECT API"),
        BrandingModelSpec(label="grok-4.5", tag="GROK"),
    ][:n_models]
    rows = [
        BrandingOutroRow(name="DeepSeek-V4.1-Flash-1017", time="1.1 min", size="17 KB",
                         cost="local", tokens="117 → 6.6k"),
        BrandingOutroRow(name="grok-4.5", time="9.4 min", size="709 KB",
                         cost="—", tokens="—"),
    ][:n_models]
    return BrandingSpec(title="Pagoda Garden", date_label=date_label,
                        prompt_line="Build a pagoda garden.", models=models, outro_rows=rows)


def _visible_text(html: str) -> str:
    """What ends up on the card: template text without <style> and HTML
    comments (both explain the old copy)."""
    import re

    html = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


# ── outro copy ───────────────────────────────────────────────────────────────


def test_outro_never_promises_a_weekly_cadence_or_local_only():
    for n in (1, 2):
        html = _visible_text(render_outro_html(_tpl("outro.html"), _spec(n))).lower()
        assert "weekly" not in html
        assert "local llm benchmarks" not in html


def test_solo_outro_does_not_ask_which_one_wins():
    html = _visible_text(render_outro_html(_tpl("outro.html"), _spec(1))).upper()
    assert "WHICH ONE WINS" not in html
    assert "YOUR EYES" in html


def test_duo_outro_keeps_the_comparison_question():
    html = _visible_text(render_outro_html(_tpl("outro.html"), _spec(2))).upper()
    assert "WHICH ONE WINS" in html


def test_outro_shows_the_run_date_instead_of_a_run_counter():
    html = _visible_text(render_outro_html(_tpl("outro.html"), _spec(2, "2026-10-21")))
    assert "2026-10-21" in html
    assert "run 0" not in html.lower()
    assert "{{" not in html


def test_outro_shows_the_episode_label_when_given():
    html = render_outro_html(_tpl("outro.html"), _spec(1, "episode 4 · 2026-10-21"))
    assert "episode 4 · 2026-10-21" in html


def test_outro_card_has_no_size_column():
    html = _visible_text(render_outro_html(_tpl("outro.html"), _spec(2)))
    assert ">Size<" not in html
    assert "709 KB" not in html and "17 KB" not in html
    # header Model/Time/Cost/Tokens, one value cell per remaining column
    assert "<span>Model</span><span>Time</span><span>Cost</span><span>Tokens</span>" in html


def test_outro_rows_render_time_cost_tokens_only():
    rows = [BrandingOutroRow(name="A", time="1 min", size="1 KB", cost="local", tokens="1 → 2")]
    html = render_outro_rows_html(rows)
    assert html.count('class="val"') == 3
    assert "1 KB" not in html


def test_outro_row_size_is_optional_for_callers():
    """The backend still sends `size` (the W1 receipt will show it as "HTML
    size"); a caller that stops sending it must not 422."""
    row = BrandingOutroRow(name="A", time="1 min")
    assert row.size == "—"


# ── frame copy ───────────────────────────────────────────────────────────────


def test_frames_carry_the_date_label_not_a_run_counter():
    for name in ("frame.html", "frame_single.html", "outro.html"):
        text = _tpl(name)
        assert "{{DATE_LABEL}}" in text, name
        assert "run {{" not in text, name
        assert "{{RUN_LABEL}}" not in text, name


def test_frame_tokens_fill_every_placeholder():
    for n, name in ((1, "frame_single.html"), (2, "frame.html")):
        from media import fill_bench_template

        html = fill_bench_template(_tpl(name), build_frame_tokens(_spec(n)))
        assert "{{" not in html, name
        assert "2026-10-21" in html, name


def test_branding_spec_ignores_a_legacy_run_label():
    """An old backend still sending run_label must not 422 the compose."""
    spec = BrandingSpec(title="t", run_label="035", prompt_line="p",
                        models=[BrandingModelSpec(label="A", tag="X")],
                        outro_rows=[BrandingOutroRow(name="A", time="1 min")])
    assert spec.date_label == ""
    html = _visible_text(render_outro_html(_tpl("outro.html"), spec))
    assert "035" not in html
    assert "{{" not in html


# ── outro hold time ──────────────────────────────────────────────────────────


def test_outro_is_held_four_and_a_half_seconds():
    assert OUTRO_DURATION_S == 4.5
    cmd = build_branded_compose_cmd(["/d/a.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4")
    first = cmd.index("-loop")
    second = cmd.index("-loop", first + 1)
    assert cmd[second:second + 4] == ["-loop", "1", "-t", "4.5"]
