"""Bench compilation (phase 1, 30.10.2026): 2–4 finished bench runs in ONE
4:5 video — hook, one segment per run, the numbers/ranking, the details.

The rules these tests hold:
- the artwork is never covered: every text, bar and box sits in the bands
  ABOVE or BELOW the art rectangle; the recording is scaled INTO it,
  full width, so there are no dead side bars;
- nothing smaller than the phone floor (32 px on 1080 wide);
- no cyan anywhere in the format (operator rule);
- the racing bars end at exactly the width of their value.
Pure tests + one real ffmpeg run on synthetic inputs (skipped without ffmpeg).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import compilation as comp
import media

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "bench"
COMP = (TEMPLATES / "compilation.html").read_text(encoding="utf-8")

DS = "DeepSeek-V4.1-Flash-1017"  # model-catalog: allow (test data)
HEADER = {"hardware": "2× DGX Spark · 256 GB", "model": DS, "meta": "omp agent · effort high · Oct 30, 2026"}


def _bars(n=3, hi=0):
    vals = [79.5, 93.2, 59.7, 40.0][:n]
    top = max(vals)
    return [{"label": f"Build {i}", "value": f"{v} min", "fraction": v / top, "highlight": i == hi}
            for i, v in enumerate(vals)]


def _spec(n_entries=3, *, closing_pick=True):
    art = lambda i, s=1.0: {"path": f"/shared-deliverables/bench-x/{i}/recording.mp4", "kind": "video", "start_s": s}
    parts = [{
        "kind": "hook", "duration_s": 3.5, "header": HEADER, "art": art(0, 10.0),
        "caption": "3 agent builds · same model · different briefs",
        "hero": {"value": "0.54", "unit": "$", "unit_before": True, "label": "power for all 3 builds, at most",
                 "measured": False},
        "hero_sub": "232 min of agent work · 645k output tokens",
        "bars": _bars(n_entries, hi=-1), "footnote": "Different briefs: no head-to-head.",
    }]
    for i in range(n_entries):
        parts.append({
            "kind": "entry", "duration_s": 7.0, "header": HEADER, "art": art(i),
            "caption": f"Build {i} · agent build", "badge": None,
            "stats": [{"value": "79.5", "unit": "min", "label": "agent time"},
                      {"value": "264k", "label": "output tokens"},
                      {"value": "0.18", "unit": "$", "unit_before": True, "label": "power, at most",
                       "measured": False}],
            "bars": _bars(n_entries, hi=i), "footnote": "Single run · power is arithmetic, not measured",
        })
    parts.append({
        "kind": "ranking", "duration_s": 5.0, "header": HEADER, "art": art(0, 14.0), "caption": "The numbers",
        "table": {"columns": ["build", "time", "tokens", "power"],
                  "rows": [{"cells": [f"Build {i}", "79.5 min", "264k", "$0.18"]} for i in range(n_entries)]},
    })
    parts.append({
        "kind": "closing", "duration_s": 5.5, "header": HEADER, "caption": "How this was measured",
        "pick": "Build 0" if closing_pick else None,
        "pick_reason": "The calmest scene for the least power." if closing_pick else None,
        "details": ["Time = active agent time of the one run.", "Power = 2 × 240 W × time × $0.35/kWh."],
    })
    return comp.CompilationSpec.model_validate({
        "parts": parts, "footer": "AI-generated pages, recorded from real runs", "handle": "@example_handle",
        "checksum": "c" * 64,
    })


# ── contract ─────────────────────────────────────────────────────────────


def test_feature_is_announced():
    assert "bench_compilation_v1" in media.RENDERER_FEATURES
    # the W1 features stay
    assert {"bench_card_v1", "page_check"} <= set(media.RENDERER_FEATURES)


def test_spec_accepts_two_to_four_entries():
    for n in (2, 3, 4):
        spec = _spec(n)
        assert sum(p.kind == "entry" for p in spec.parts) == n


@pytest.mark.parametrize("n", [5])
def test_spec_refuses_more_than_four_entries(n):
    with pytest.raises(Exception):
        _spec(n)


def test_art_parts_need_art_and_closing_has_none():
    raw = _spec().model_dump()
    raw["parts"][1]["art"] = None
    with pytest.raises(Exception):
        comp.CompilationSpec.model_validate(raw)
    raw = _spec().model_dump()
    raw["parts"][-1]["art"] = {"path": "/shared-deliverables/x.mp4"}
    with pytest.raises(Exception):
        comp.CompilationSpec.model_validate(raw)


def test_ranking_needs_a_table():
    raw = _spec().model_dump()
    raw["parts"][-2]["table"] = None
    with pytest.raises(Exception):
        comp.CompilationSpec.model_validate(raw)


def test_request_paths_are_strings_the_service_contains():
    req = comp.CompilationRequest.model_validate({"spec": _spec().model_dump(),
                                                   "output_path": "/shared-deliverables/c/out.mp4"})
    assert comp.compilation_inputs(req.spec) == [
        "/shared-deliverables/bench-x/0/recording.mp4", "/shared-deliverables/bench-x/0/recording.mp4",
        "/shared-deliverables/bench-x/1/recording.mp4", "/shared-deliverables/bench-x/2/recording.mp4",
        "/shared-deliverables/bench-x/0/recording.mp4",
    ]


def test_duration_is_the_sum_of_the_parts():
    assert comp.compilation_duration_s(_spec()) == pytest.approx(3.5 + 3 * 7.0 + 5.0 + 5.5)


# ── geometry: the art is never covered ──────────────────────────────────


def test_art_rect_is_full_width_16_9_and_even():
    x, y, w, h = comp.COMP_ART
    assert (x, w) == (0, comp.COMP_SIZE[0])           # full bleed: no dead side bars
    assert abs(w / h - 16 / 9) < 0.01
    assert all(v % 2 == 0 for v in (x, y, w, h))       # yuv420p


def test_bars_sit_below_the_art_and_inside_the_frame():
    _x, ay, _w, ah = comp.COMP_ART
    for n in (2, 3, 4):
        rects = comp.comp_bar_rects(n)
        assert len(rects) == n
        for bx, by, bw, bh in rects:
            assert by >= ay + ah, "a bar would cover the artwork"
            assert by + bh <= comp.COMP_SIZE[1] and bx + bw <= comp.COMP_SIZE[0]
            assert bw % 2 == 0 and bh % 2 == 0


def _px(style: str, key: str) -> int:
    return int(re.search(rf"{key}:(\d+)px", style).group(1))


def test_rendered_parts_keep_every_text_box_out_of_the_art():
    """Every absolutely placed block of a rendered part ends above the art
    or starts below it — the template places blocks by these numbers."""
    spec = _spec()
    _x, ay, _w, ah = comp.COMP_ART
    for part in spec.parts:
        html = comp.render_compilation_html(COMP, part, spec)
        if part.art is not None:
            m = re.search(r'class="art" style="([^"]+)"', html)
            assert m, part.kind
            st = m.group(1)
            assert (_px(st, "left"), _px(st, "top"), _px(st, "width"), _px(st, "height")) == comp.COMP_ART
        for cls, st in re.findall(r'class="(band-[a-z]+)" style="([^"]+)"', html):
            top = _px(st, "top")
            if cls == "band-top":
                assert _px(st, "height") + top <= ay, cls
            elif part.art is not None:
                assert top >= ay + ah, (part.kind, cls)


def test_bar_tracks_in_html_match_the_drawbox_geometry():
    spec = _spec()
    part = spec.parts[1]
    html = comp.render_compilation_html(COMP, part, spec)
    tracks = re.findall(r'class="track" style="left:(\d+)px;top:(\d+)px;width:(\d+)px;height:(\d+)px"', html)
    assert [tuple(map(int, t)) for t in tracks] == comp.comp_bar_rects(len(part.bars))


# ── words, escaping, floors, colours ────────────────────────────────────


def test_texts_are_escaped_and_present():
    raw = _spec().model_dump()
    raw["parts"][1]["caption"] = "<script>x</script> & Co"
    spec = comp.CompilationSpec.model_validate(raw)
    html = comp.render_compilation_html(COMP, spec.parts[1], spec)
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    for text in (DS, "omp agent · effort high · Oct 30, 2026", "@example_handle",
                 "AI-generated pages, recorded from real runs", "264k", "agent time"):
        assert text in html


def test_rating_badge_only_when_given():
    spec = _spec()
    plain = comp.render_compilation_html(COMP, spec.parts[1], spec)
    assert 'class="badge"' not in plain
    raw = spec.model_dump()
    raw["parts"][1]["badge"] = "Example rating 8/10"
    spec2 = comp.CompilationSpec.model_validate(raw)
    assert "Example rating 8/10" in comp.render_compilation_html(COMP, spec2.parts[1], spec2)


def test_closing_without_pick_has_no_pick_block():
    spec = _spec(closing_pick=False)
    html = comp.render_compilation_html(COMP, spec.parts[-1], spec)
    assert 'class="pick"' not in html
    assert "How this was measured" in html


def test_template_never_below_the_phone_floor():
    sizes = media.css_font_sizes(COMP, ".c")
    assert len(sizes) >= 10, "no font sizes found under .c"
    small = [(sel, px) for sel, px in sizes if px < comp.COMP_FLOOR_PX]
    assert not small, small


CYAN = re.compile(r"00e5ff|22d3ee|--cyan|rgb\(\s*0\s*,\s*229\s*,\s*255", re.I)


def test_no_cyan_anywhere_in_the_format():
    assert not CYAN.search(COMP)
    assert 'href="shared.css"' not in COMP          # shared.css carries the old cyan tokens
    spec = _spec()
    for part in spec.parts:
        assert not CYAN.search(comp.render_compilation_html(COMP, part, spec))
    for colour in (comp.COMP_ACCENT, comp.COMP_BAR_MUTED, comp.COMP_BG):
        r, g, b = (int(colour[2:][i:i + 2], 16) for i in (0, 2, 4))
        # cyan = low red, high green AND blue
        assert not (r < 90 and g > 160 and b > 160), colour


# ── racing bars ──────────────────────────────────────────────────────────


def _parse_cmds(text: str):
    out = []
    for line in text.strip().splitlines():
        t, rest = line.rstrip(";").split(" ", 1)
        for cmd in rest.split(","):
            target, key, value = cmd.split()
            out.append((float(t), target, key, int(value)))
    return out


def test_bar_commands_grow_monotonically_to_the_exact_width():
    spec = _spec()
    part = spec.parts[1]
    cmds = _parse_cmds(comp.comp_bar_commands(part, 1))
    rects = comp.comp_bar_rects(len(part.bars))
    for i, bar in enumerate(part.bars):
        mine = [(t, v) for t, target, key, v in cmds if target == f"drawbox@p1b{i}" and key == "w"]
        assert mine, i
        widths = [v for _t, v in mine]
        assert widths == sorted(widths)
        assert widths[-1] == comp.comp_bar_width(bar.fraction, rects[i][2])
        assert all(v >= 2 for v in widths)   # drawbox w=0 means "full width"
        assert mine[-1][0] <= comp.COMP_BAR_GROW_S + comp.COMP_BAR_STAGGER_S * 4 + 0.05


def test_no_bars_no_commands():
    spec = _spec()
    assert comp.comp_bar_commands(spec.parts[-1], 5) == ""


def test_bar_width_never_zero():
    assert comp.comp_bar_width(0.0, 400) == 2
    assert comp.comp_bar_width(1.0, 400) == 400
    assert comp.comp_bar_width(0.5, 400) == 200


# ── the encode ───────────────────────────────────────────────────────────


def _cmd(tmp="/tmp/r"):
    spec = _spec()
    bgs = [f"{tmp}/bg{i}.png" for i in range(len(spec.parts))]
    cmds = [f"{tmp}/p{i}.cmd" if p.bars else None for i, p in enumerate(spec.parts)]
    return spec, comp.build_compilation_cmd(spec, bgs, cmds, "/shared-deliverables/c/out.mp4")


def test_cmd_scales_the_art_into_the_rect_full_width_and_overlays_it_there():
    spec, cmd = _cmd()
    graph = cmd[cmd.index("-filter_complex") + 1]
    x, y, w, h = comp.COMP_ART
    assert f"scale={w}:{h}:force_original_aspect_ratio=decrease" in graph
    assert f"overlay={x}:{y}" in graph
    assert "crop" not in graph                     # letterbox, never crop the art
    assert "drawtext" not in graph                 # every word comes from the template
    assert f"concat=n={len(spec.parts)}:v=1:a=0" in graph


def test_cmd_loops_short_recordings_and_seeks_per_part():
    spec, cmd = _cmd()
    assert cmd.count("-stream_loop") == len(comp.compilation_inputs(spec))
    assert "10.0" in cmd and "14.0" in cmd                      # hook + ranking start later in the clip


def test_cmd_is_x_ready():
    _spec_, cmd = _cmd()
    s = " ".join(cmd)
    for token in ("-c:v libx264", "-pix_fmt yuv420p", "-movflags +faststart", "-r 30", "anullsrc"):
        assert token in s
    assert cmd[-1] == "/shared-deliverables/c/out.mp4"
    assert "comment=bench-compilation:" + "c" * 64 in s


def test_cmd_sends_bar_commands_only_where_there_are_bars():
    spec, cmd = _cmd()
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.count("sendcmd=") == sum(1 for p in spec.parts if p.bars)
    assert "drawbox@p1b0" in graph


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg not installed")
def test_cmd_really_encodes_a_4_5_video_of_the_right_length(tmp_path):
    """Proof by effect: synthetic recordings + plain backgrounds through the
    real command give a 1080×1350 H.264 video as long as the parts."""
    rec = tmp_path / "rec.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4",
                    "-pix_fmt", "yuv420p", str(rec)], check=True)
    raw = _spec(2).model_dump()
    for p in raw["parts"]:
        p["duration_s"] = 1.0
        if p.get("art"):
            p["art"]["path"] = str(rec)
    spec = comp.CompilationSpec.model_validate(raw)
    bgs, cmds = [], []
    for i, part in enumerate(spec.parts):
        bg = tmp_path / f"bg{i}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x0E0D0B:s=1080x1350",
                        "-frames:v", "1", str(bg)], check=True)
        bgs.append(str(bg))
        text = comp.comp_bar_commands(part, i)
        if text:
            cf = tmp_path / f"p{i}.cmd"
            cf.write_text(text)
            cmds.append(str(cf))
        else:
            cmds.append(None)
    out = tmp_path / "out.mp4"
    subprocess.run(comp.build_compilation_cmd(spec, bgs, cmds, str(out)), check=True, capture_output=True)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,codec_name:format=duration", "-of", "json", str(out)],
        check=True, capture_output=True, text=True).stdout)
    st = probe["streams"][0]
    assert (st["width"], st["height"], st["codec_name"]) == (1080, 1350, "h264")
    assert float(probe["format"]["duration"]) == pytest.approx(len(spec.parts) * 1.0, abs=0.25)


# ── closing thumbnails + ranking name column ────────────────────────────


def _with_thumbs(n=3):
    raw = _spec(n).model_dump()
    raw["parts"][-1]["thumbs"] = [f"/shared-deliverables/bench-x/{i}/screenshot.png" for i in range(n)]
    return comp.CompilationSpec.model_validate(raw)


def test_thumbs_only_on_the_closing_card():
    raw = _spec().model_dump()
    raw["parts"][1]["thumbs"] = ["/shared-deliverables/x.png"]
    with pytest.raises(Exception):
        comp.CompilationSpec.model_validate(raw)


def test_thumbs_are_inputs_and_sit_between_header_and_details():
    spec = _with_thumbs(3)
    assert comp.compilation_inputs(spec)[-3:] == [f"/shared-deliverables/bench-x/{i}/screenshot.png"
                                                  for i in range(3)]
    for n in (2, 3, 4):
        rects = comp.comp_thumb_rects(n)
        assert all(abs(w / h - 16 / 9) < 0.02 and w % 2 == 0 and h % 2 == 0 for _x, _y, w, h in rects)
        assert rects[0][1] >= comp.COMP_HEADER_H and rects[-1][0] + rects[-1][2] <= comp.COMP_SIZE[0] - 56
    html = comp.render_compilation_html(COMP, spec.parts[-1], spec)
    body_top = int(re.search(r'class="band-body" style="top:(\d+)px', html).group(1))
    _x, ty, _w, th = comp.comp_thumb_rects(3)[0]
    assert body_top >= ty + th, "the details would run over the thumbnails"


def test_cmd_overlays_the_thumbs_letterboxed():
    spec = _with_thumbs(3)
    bgs = [f"/tmp/bg{i}.png" for i in range(len(spec.parts))]
    cmds = [f"/tmp/p{i}.cmd" if p.bars else None for i, p in enumerate(spec.parts)]
    cmd = comp.build_compilation_cmd(spec, bgs, cmds, "/shared-deliverables/c/out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    for x, y, w, h in comp.comp_thumb_rects(3):
        assert f"scale={w}:{h}:force_original_aspect_ratio=decrease" in graph
        assert f"overlay={x}:{y}" in graph


def test_table_name_column_is_marked():
    raw = _spec().model_dump()
    raw["parts"][-2]["table"] = {"columns": ["#", "build", "rating", "time"], "name_col": 1,
                                 "rows": [{"cells": ["1", "Build 0", "8/10", "79.5 min"], "highlight": True}]}
    spec = comp.CompilationSpec.model_validate(raw)
    html = comp.render_compilation_html(COMP, spec.parts[-2], spec)
    assert '<td class="rk">1</td><td class="nm">Build 0</td><td class="n">8/10</td>' in html
    assert '<tr class="hi">' in html


def test_unsafe_command_file_path_is_refused():
    spec = _spec()
    bgs = [f"/tmp/bg{i}.png" for i in range(len(spec.parts))]
    cmds = [("/tmp/x:y,z.cmd" if p.bars else None) for p in spec.parts]
    with pytest.raises(ValueError, match="not safe"):
        comp.build_compilation_cmd(spec, bgs, cmds, "/shared-deliverables/c/out.mp4")


def test_template_reports_overflow_instead_of_shipping_a_cut_text():
    """The renderer answers 422 with data-overflow — the template must
    measure every single-line text, the header band and the text blocks."""
    assert "dataset.overflow" in COMP
    for sel in (".cap", ".meta", ".sub", ".bl", ".band-note", ".fl", ".st .l", ".hero .l", ".badge"):
        assert sel in COMP.split("dataset.overflow")[0].split("Overflow check")[1], sel
    for block in (".band-body", ".band-table", ".band-stats", ".band-hero"):
        assert f"'{block}'" in COMP, block
    assert "header taller than its band" in COMP


def _rgb_at(video: Path, t: float, xy: tuple[int, int]) -> tuple[int, int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                          "-vf", f"format=rgb24,crop=2:2:{xy[0]}:{xy[1]}", "-f", "rawvideo", "-pix_fmt", "rgb24",
                          "-"],
                         check=True, capture_output=True).stdout
    return raw[0], raw[1], raw[2]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_encode_puts_the_art_in_its_rect_and_grows_the_bar_to_its_value(tmp_path):
    """Proof by effect on the pixels: the recording lands inside COMP_ART
    (and nowhere above it), the highlighted bar ends amber at exactly its
    width, and the closing thumbnails are drawn."""
    rec = tmp_path / "rec.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x30C030:s=640x360:r=30:d=3",
                    "-pix_fmt", "yuv420p", str(rec)], check=True)
    shot = tmp_path / "shot.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xC03030:s=640x360",
                    "-frames:v", "1", str(shot)], check=True)
    raw = _spec(2).model_dump()
    for p in raw["parts"]:
        p["duration_s"] = 2.0
        if p.get("art"):
            p["art"]["path"] = str(rec)
    raw["parts"][-1]["thumbs"] = [str(shot), str(shot)]
    spec = comp.CompilationSpec.model_validate(raw)
    bg = tmp_path / "bg.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x0E0D0B:s=1080x1350",
                    "-frames:v", "1", str(bg)], check=True)
    cmds = []
    for i, part in enumerate(spec.parts):
        text = comp.comp_bar_commands(part, i)
        if text:
            (tmp_path / f"p{i}.cmd").write_text(text)
        cmds.append(str(tmp_path / f"p{i}.cmd") if text else None)
    out = tmp_path / "out.mp4"
    subprocess.run(comp.build_compilation_cmd(spec, [str(bg)] * len(spec.parts), cmds, str(out)),
                   check=True, capture_output=True)
    ax, ay, aw, ah = comp.COMP_ART
    t_entry_end = 2.0 + 1.9                       # part 1 = first entry, bars done after ~1.4 s
    g = _rgb_at(out, t_entry_end, (ax + aw // 2, ay + ah // 2))
    assert g[1] > 150 and g[0] < 100, g           # the green recording fills the art rect
    above = _rgb_at(out, t_entry_end, (ax + aw // 2, ay - 6))
    assert max(above) < 40, above                 # header band stays background
    bx, by, bw, bh = comp.comp_bar_rects(2)[0]
    final = comp.comp_bar_width(spec.parts[1].bars[0].fraction, bw)
    amber = _rgb_at(out, t_entry_end, (bx + final - 6, by + bh // 2))
    assert amber[0] > 180 and 120 < amber[1] < 200 and amber[2] < 110, amber
    past = _rgb_at(out, t_entry_end, (bx + final + 6, by + bh // 2))
    assert max(past) < 40, past                   # the fill stops at its value
    tx, ty, tw, th = comp.comp_thumb_rects(2)[0]
    red = _rgb_at(out, len(spec.parts) * 2.0 - 0.5, (tx + tw // 2, ty + th // 2))
    assert red[0] > 150 and red[1] < 100, red



# ── solo: one run ────────────────────────────────────────────────────────


def _solo_spec(**over):
    art = lambda s=1.0: {"path": "/shared-deliverables/bench-x/0/recording.mp4", "kind": "video", "start_s": s}
    parts = [
        {"kind": "hook", "duration_s": 3.5, "header": HEADER, "art": art(10.0), "caption": "Hourglass · agent build",
         "hero": {"value": "0.22", "unit": "$", "unit_before": True, "label": "power for the whole build, at most",
                  "measured": False}, "hero_sub": "79.5 min of agent work · 264k output tokens",
         "footnote": "One run · power is arithmetic, not measured"},
        {"kind": "entry", "duration_s": 13.0, "header": HEADER, "art": art(), "caption": "Hourglass",
         "stats": [{"value": "79.5", "unit": "min", "label": "agent time"}, {"value": "264k", "label": "output tokens"},
                   {"value": "0.22", "unit": "$", "unit_before": True, "label": "power, at most", "measured": False}],
         "bars": [{"label": "of its 120-min time limit", "value": "66%", "fraction": 0.66, "highlight": True}],
         "facts": [{"label": "effort", "value": "high"}, {"label": "runs", "value": "1"},
                   {"label": "harness", "value": "omp agent"}],
         "footnote": "One run · power is arithmetic, not measured"},
        {"kind": "closing", "duration_s": 6.0, "header": HEADER, "caption": "How this was measured",
         "details": ["Time = active agent time of the run."],
         "thumbs": ["/shared-deliverables/bench-x/0/screenshot.png"]},
    ]
    raw = {"parts": parts, "footer": "AI-generated pages from real runs", "handle": "@h"}
    raw.update(over)
    return comp.CompilationSpec.model_validate(raw)


def test_solo_spec_is_valid_with_three_parts():
    spec = _solo_spec()
    assert [p.kind for p in spec.parts] == ["hook", "entry", "closing"]
    assert comp.compilation_duration_s(spec) == pytest.approx(22.5)


def test_a_single_run_has_no_ranking():
    raw = _solo_spec().model_dump()
    raw["parts"].insert(2, {"kind": "ranking", "duration_s": 5.0, "header": HEADER,
                            "art": {"path": "/shared-deliverables/x.mp4"},
                            "table": {"columns": ["build", "time"], "rows": [{"cells": ["H", "79.5 min"]}]}})
    with pytest.raises(Exception, match="no ranking"):
        comp.CompilationSpec.model_validate(raw)


def test_solo_facts_sit_below_the_bar_and_above_the_footnote_never_on_the_art():
    spec = _solo_spec()
    part = spec.parts[1]
    html = comp.render_compilation_html(COMP, part, spec)
    facts_top = int(re.search(r'class="band-facts" style="top:(\d+)px', html).group(1))
    note_top = int(re.search(r'class="band-note" style="top:(\d+)px', html).group(1))
    _bx, by, _bw, bh = comp.part_bar_rects(part)[0]
    tracks = re.findall(r'class="track" style="left:(\d+)px;top:(\d+)px;width:(\d+)px;height:(\d+)px"', html)
    assert [tuple(map(int, t)) for t in tracks] == comp.part_bar_rects(part)   # template == ffmpeg geometry
    ax, ay, aw, ah = comp.COMP_ART
    assert facts_top > by + bh and facts_top >= ay + ah
    assert note_top >= facts_top + comp.COMP_FACTS_H
    assert note_top + 40 <= comp.COMP_SIZE[1] - 58           # footnote line ends above the footer
    assert '<span class="fk">effort</span><span class="fv">high</span>' in html


def test_one_thumbnail_is_capped_not_a_second_artwork():
    [(x, y, w, h)] = comp.comp_thumb_rects(1)
    assert w == comp.COMP_THUMB_MAX_W and abs(w / h - 16 / 9) < 0.02


def test_overflow_check_covers_the_facts():
    assert "'.band-facts'" in COMP and ".fk, .fv" in COMP


def test_solo_cmd_draws_the_bar_where_the_template_put_its_track():
    spec = _solo_spec()
    bgs = [f"/tmp/bg{i}.png" for i in range(len(spec.parts))]
    cmds = [f"/tmp/p{i}.cmd" if p.bars else None for i, p in enumerate(spec.parts)]
    graph = comp.build_compilation_cmd(spec, bgs, cmds, "/shared-deliverables/c/solo.mp4")
    graph = graph[graph.index("-filter_complex") + 1]
    bx, by, _bw, bh = comp.part_bar_rects(spec.parts[1])[0]
    assert f"drawbox@p1b0=x={bx}:y={by}:" in graph
    assert "concat=n=3:v=1:a=0" in graph
