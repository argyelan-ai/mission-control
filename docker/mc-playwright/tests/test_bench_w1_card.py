"""Bench W1 renderer: one card template for the 16:9 receipt card and the
1080×1350 PNG, the frame with chips above the panes, the X-ready encode,
the type floor (57 px @1920 / 32 px @1080), seeded Math.random, the page
check, the checksum on every artefact. Pure — no browser, no ffmpeg run."""
from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

import media

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "bench"
CARD = (TEMPLATES / "card.html").read_text(encoding="utf-8")
FRAME = (TEMPLATES / "frame_w1.html").read_text(encoding="utf-8")
SHIM = (Path(__file__).resolve().parents[1] / "deterministic_shim.js").read_text(encoding="utf-8")

DS = "DeepSeek-V4.1-Flash-1017"  # model-catalog: allow (test data)


def _card(layout="solo", n=1, run_class="A"):
    ct = {"model": DS, "chips": "local · DGX Spark ×2 · raw API · default effort",
          "numbers": [{"value": "1:06", "label": "compute time", "sub": "median of 3"},
                      {"value": "6.6k", "label": "output tokens", "sub": "median of 3"},
                      {"value": "101", "label": "tok/s", "sub": "decode"}]}
    return media.CardSpec.model_validate({
        "layout": layout, "run_class": run_class, "top": "Pagoda Garden · same-prompt showdown · 2026-10-22",
        "contestants": [ct] * n, "foot": "median of 3 runs · same prompt", "handle": "@example_handle",
        "checksum": "f" * 64,
    })


def test_features_announce_w1():
    assert {"branding_effort", "bench_card_v1", "seeded_random", "page_check"} <= set(media.RENDERER_FEATURES)


# ── type floor ───────────────────────────────────────────────────────────


def test_card_wide_never_below_57px():
    sizes = media.css_font_sizes(CARD, ".wide")
    assert sizes, "no wide font sizes found"
    small = [(sel, px) for sel, px in sizes if px < media.W1_FLOOR_WIDE_PX]
    assert not small, small


def test_card_tall_never_below_32px():
    sizes = media.css_font_sizes(CARD, ".tall")
    assert sizes
    small = [(sel, px) for sel, px in sizes if px < media.W1_FLOOR_TALL_PX]
    assert not small, small


def test_frame_never_below_57px():
    sizes = media.css_font_sizes(FRAME, ".solo") + media.css_font_sizes(FRAME, ".duo") + \
        media.css_font_sizes(FRAME, "body")
    assert sizes
    assert all(px >= media.W1_FLOOR_WIDE_PX for _sel, px in sizes), sizes


def test_fit_script_stops_at_the_floor():
    for html, floor in ((media.render_card_html(CARD, _card(), "wide"), 57),
                        (media.render_card_html(CARD, _card(), "tall"), 32),
                        (media.render_frame_w1_html(FRAME, media.FrameSpec.model_validate(
                            {"top": "t", "panes": [{"model": DS, "chips": "c"}]})), 57)):
        assert f"var floor = {floor};" in html
        assert "document.fonts.ready" in html and "dataset.fit = 'done'" in html


# ── one template, two shapes ─────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["wide", "tall"])
@pytest.mark.parametrize("layout,n", [("solo", 1), ("duo", 2)])
def test_card_fills_completely_and_escapes(shape, layout, n):
    spec = _card(layout, n)
    spec.contestants[0].model = "<script>x</script>"
    html = media.render_card_html(CARD, spec, shape)
    assert "{{" not in html and "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert f'class="{shape} {layout}' in html
    assert "@example_handle" in html and "1:06" in html


def test_duo_wide_uses_short_column_heads():
    html = media.render_card_html(CARD, _card("duo", 2), "wide")
    assert "<span>time</span><span>out tok</span><span>tok/s</span>" in html


def test_not_measured_has_its_own_style():
    spec = _card()
    spec.contestants[0].numbers[1] = media.CardNumber(value="not measured", label="output tokens", measured=False)
    html = media.render_card_html(CARD, spec, "tall")
    assert 'class="v num nm">not measured' in html


def test_card_spec_limits():
    with pytest.raises(Exception):
        media.CardSpec.model_validate({**_card().model_dump(), "contestants": [_card().contestants[0].model_dump()] * 3})


# ── frame: chips above the panes, one geometry source ────────────────────


@pytest.mark.parametrize("n", [1, 2])
def test_frame_panes_match_the_ffmpeg_geometry(n):
    frame = media.FrameSpec.model_validate({"top": "Pagoda Garden · same-prompt showdown · 2026-10-22",
                                            "panes": [{"model": DS, "chips": "local · raw API"}] * n,
                                            "handle": "@h"})
    html = media.render_frame_w1_html(FRAME, frame)
    assert "{{" not in html
    for x, y, w, h in media.W1_PANES[n]:
        assert f"left:{x}px;width:{w}px;top:{y}px" in html
        assert f"width:{w}px;height:{h}px" in html
    # chips sit ABOVE the pane, never on the picture
    assert ".chips2, .duo .chips2 { position:absolute; left:0; right:0; bottom:100%;" in FRAME
    cmd = media.build_w1_compose_cmd(["/s/a.mp4"] * n, "/s/f.png", "/s/c.png", "/s/o.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    for i, (x, y, w, h) in enumerate(media.W1_PANES[n]):
        assert f"scale={w}:{h}:force_original_aspect_ratio=decrease" in graph
        assert f"overlay={x}:{y}" in graph


def test_panes_fit_the_frame_with_16_9_pictures():
    for n, panes in media.W1_PANES.items():
        for x, y, w, h in panes:
            assert x >= 0 and y > 200 and x + w <= 1920 and y + h <= 1080
        if n == 2:
            assert all(abs(w / h - 16 / 9) < 0.01 for _x, _y, w, h in panes)


# ── encode ───────────────────────────────────────────────────────────────


def test_w1_encode_is_x_ready():
    cmd = media.build_w1_compose_cmd(["/s/a.mp4"], "/s/f.png", "/s/c.png", "/s/o.mp4", checksum="ab" * 32)
    joined = " ".join(cmd)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1920:1080" in graph and "fps=30" in graph and "format=yuv420p" in graph
    assert "-profile:v high" in joined and "-b:v 8M" in joined and "-pix_fmt yuv420p" in joined
    assert "anullsrc" in joined and "-c:a aac" in joined and "-shortest" in joined
    assert "+faststart" in joined
    assert f"-t {media.W1_RECEIPT_CARD_S}" in joined and media.W1_RECEIPT_CARD_S == 4.5
    assert "comment=bench-record:" + "ab" * 32 in joined
    assert "concat=n=2:v=1:a=0" in graph  # clean cut to the card, X loops to frame 0
    with pytest.raises(ValueError):
        media.build_w1_compose_cmd(["/a"] * 3, "/f", "/c", "/o")


def test_compose_request_checks_w1_panes():
    spec = {"card": _card().model_dump(), "frame": {"top": "t", "panes": [{"model": DS, "chips": "c"}]}}
    media.ComposeRequest.model_validate({"inputs": ["/s/a.mp4"], "labels": [DS], "output_path": "/s/o.mp4",
                                         "w1": spec})
    with pytest.raises(Exception):
        media.ComposeRequest.model_validate({"inputs": ["/s/a.mp4", "/s/b.mp4"], "labels": [DS, DS],
                                             "output_path": "/s/o.mp4", "w1": spec})


# ── checksum on the PNG, page check, seeded random ───────────────────────


def _png(w=2, h=2) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_png_carries_the_record_checksum():
    out = media.png_with_text(_png(), "bench-record", "c" * 64)
    assert out.startswith(b"\x89PNG") and b"tEXtbench-record\x00" + b"c" * 64 in out
    pos = out.index(b"tEXt")
    length = struct.unpack(">I", out[pos - 4:pos])[0]
    data = out[pos + 4:pos + 4 + length]
    crc = struct.unpack(">I", out[pos + 4 + length:pos + 8 + length])[0]
    assert crc == zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF
    assert out.index(b"IHDR") < pos < out.index(b"IDAT")
    with pytest.raises(ValueError):
        media.png_with_text(b"nope", "k", "v")


def test_blank_page_check():
    assert media.is_blank_page(None, 0, 0) is None
    assert media.is_blank_page(5_000, 0, 0) is True
    assert media.is_blank_page(5_000, 12, 0) is False  # a short text page is not blank
    assert media.is_blank_page(5_000, 0, 1) is False   # a canvas page (WebGL may read back black)
    assert media.is_blank_page(400_000, 0, 0) is False


def test_math_random_is_seeded_in_the_recorder():
    assert "Math.random = () =>" in SHIM


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_seeded_random_repeats_the_same_sequence():
    js = ("const window = {}; const document = {getAnimations: () => []};"
          "globalThis.window = window; globalThis.document = document;"
          + SHIM + ";const a=[Math.random(),Math.random(),Math.random()];"
          + SHIM + ";const b=[Math.random(),Math.random(),Math.random()];"
          "console.log(JSON.stringify(a)===JSON.stringify(b) && a.every(x=>x>=0&&x<1) && a[0]!==a[1])")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.stdout.strip() == "true", out.stderr


# ── review fixes (PR #982 design review) ─────────────────────────────────


def test_duo_frame_title_is_never_cut():
    """M2: the duo title line wraps instead of "showdow…", the handle has
    its own line under it."""
    html = media.render_frame_w1_html(FRAME, media.FrameSpec.model_validate({
        "top": "Pagoda Garden · same-prompt showdown · 2026-10-24", "handle": "@example_handle",
        "panes": [{"model": DS, "chips": "local · raw API · default effort"},
                  {"model": "deepseek-v4.1-flash", "chips": "cloud · raw API · default effort"}]}))
    css = html.split("<style>")[1].split("</style>")[0]
    duo_top = [r for r in css.split("}") if r.strip().startswith(".duo .top .t")]
    assert duo_top and "white-space:normal" in duo_top[0] and "ellipsis" not in duo_top[0]
    assert "flex-direction:column" in css.split("\n.duo .top {")[1].split("}")[0]
    assert "Pagoda Garden · same-prompt showdown · 2026-10-24" in html


def test_chips_break_only_between_parts():
    html = media.render_card_html(CARD, _card(), "tall")
    assert '<span class="cp">default effort</span>' in html
    assert '<span class="cp">DGX Spark ×2</span>' in html


def test_wide_duo_row_uses_the_short_chips_and_the_tall_card_the_full_ones():
    spec = _card("duo", 2)
    for ct in spec.contestants:
        ct.chips_short = "local · raw API · default effort"
    wide = media.render_card_html(CARD, spec, "wide")
    tall = media.render_card_html(CARD, spec, "tall")
    assert "DGX Spark" not in wide.split("<body>")[1]
    assert "DGX Spark ×2" in tall
