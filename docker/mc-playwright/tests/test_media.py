"""Pure-function tests for docker/mc-playwright/media.py.

Request-model validation + ffmpeg command builders only — the live /record
and /compose E2E happens at the supervised live gate, not here (no browser,
no ffmpeg binary needed).
"""
from pathlib import Path

import pytest
from pydantic import ValidationError

from media import (
    CELL_HEIGHT,
    CELL_WIDTH,
    DEJAVU_BOLD,
    DETERMINISTIC_SHIM_PATH,
    SLOT_A_XY,
    SLOT_B_XY,
    SLOT_BG_COLOR,
    SLOT_HEIGHT,
    SLOT_SINGLE_HEIGHT,
    SLOT_SINGLE_WIDTH,
    SLOT_SINGLE_XY,
    SLOT_WIDTH,
    VIEWPORTS,
    BrandingModelSpec,
    BrandingOutroRow,
    BrandingSpec,
    ComposeRequest,
    RecordRequest,
    TranscodeRequest,
    build_branded_compose_cmd,
    build_compose_cmd,
    build_pipe_encode_cmd,
    build_transcode_poster_cmd,
    build_transcode_video_cmd,
    clamp_poster_at_s,
    escape_drawtext,
    fill_bench_template,
    load_deterministic_shim,
    render_outro_rows_html,
    resolve_contained_path,
)


# ── RecordRequest validation ──────────────────────────────────────────────────


def test_record_request_url_ok():
    req = RecordRequest(url="http://caddy/demo", output_dir="/shared-deliverables/bench-1/m1")
    assert req.duration_s == 10
    assert req.viewport == "desktop"
    assert req.target_url == "http://caddy/demo"


def test_record_request_html_path_becomes_file_uri():
    req = RecordRequest(
        html_path="/shared-deliverables/bench-1/m1/index.html",
        output_dir="/shared-deliverables/bench-1/m1",
    )
    assert req.target_url == "file:///shared-deliverables/bench-1/m1/index.html"


def test_record_request_requires_exactly_one_source():
    with pytest.raises(ValidationError):
        RecordRequest(output_dir="/shared-deliverables/x")
    with pytest.raises(ValidationError):
        RecordRequest(
            url="http://a", html_path="/shared-deliverables/x/index.html",
            output_dir="/shared-deliverables/x",
        )


def test_record_request_rejects_unknown_viewport():
    with pytest.raises(ValidationError):
        RecordRequest(url="http://a", viewport="cinema", output_dir="/shared-deliverables/x")


def test_record_request_duration_bounds():
    with pytest.raises(ValidationError):
        RecordRequest(url="http://a", duration_s=0, output_dir="/shared-deliverables/x")
    with pytest.raises(ValidationError):
        RecordRequest(url="http://a", duration_s=61, output_dir="/shared-deliverables/x")


def test_viewports_have_presets():
    assert set(VIEWPORTS) == {"desktop", "mobile", "tablet"}


# ── ComposeRequest validation ─────────────────────────────────────────────────


def test_compose_request_ok():
    req = ComposeRequest(
        inputs=["/shared-deliverables/a.mp4", "/shared-deliverables/b.mp4"],
        labels=["Claude", "DeepSeek"],
        output_path="/shared-deliverables/grid.mp4",
    )
    assert req.layout == "grid"
    assert req.speed_labels is None


def test_compose_request_label_count_must_match():
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=["/a.mp4", "/b.mp4"], labels=["only-one"], output_path="/g.mp4"
        )


def test_compose_request_speed_label_count_must_match():
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=["/a.mp4", "/b.mp4"],
            labels=["A", "B"],
            speed_labels=["42 s"],
            output_path="/g.mp4",
        )


def test_compose_request_max_four_inputs():
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=[f"/{i}.mp4" for i in range(5)],
            labels=[str(i) for i in range(5)],
            output_path="/g.mp4",
        )


# ── escape_drawtext ───────────────────────────────────────────────────────────


def test_escape_drawtext_neutralizes_quotes_backslashes_percent():
    assert escape_drawtext("it's 100% \\great") == "it’s 100\\% \\\\great"


def test_escape_drawtext_plain_label_unchanged():
    assert escape_drawtext("DeepSeek-V4 · 87 tok/s") == "DeepSeek-V4 · 87 tok/s"


# ── build_pipe_encode_cmd (2026-07-15, deterministic frame-pipe capture) ──────


def test_build_pipe_encode_cmd_default_2k():
    cmd = build_pipe_encode_cmd("/shared-deliverables/out.mp4")
    assert cmd == [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-nostats",
        "-f", "image2pipe",
        "-framerate", "30",
        "-i", "-",
        "-vf", "scale=2560:1440",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        "/shared-deliverables/out.mp4",
    ]


def test_build_pipe_encode_cmd_silences_stderr_stats():
    """Review finding (2026-07-15): unsilenced ffmpeg stats fill the stderr
    pipe buffer on long recordings and deadlock the caller's stdin writes."""
    cmd = build_pipe_encode_cmd("/sd/out.mp4")
    assert "-nostats" in cmd
    assert cmd[cmd.index("-loglevel") + 1] == "error"


def test_build_pipe_encode_cmd_reads_stdin_not_a_file():
    cmd = build_pipe_encode_cmd("/sd/out.mp4")
    i_idx = cmd.index("-i")
    assert cmd[i_idx + 1] == "-"  # stdin, not a webm path — no intermediate file


def test_build_pipe_encode_cmd_custom_dimensions_and_fps():
    cmd = build_pipe_encode_cmd("/sd/out.mp4", width=1920, height=1080, fps=24)
    assert "-vf" in cmd
    assert cmd[cmd.index("-vf") + 1] == "scale=1920:1080"
    assert cmd[cmd.index("-framerate") + 1] == "24"
    assert cmd[cmd.index("-r") + 1] == "24"


def test_build_pipe_encode_cmd_no_ss_trim_needed():
    """Deterministic capture starts only after page.goto's load event — no
    pre-paint white-flash frame exists to trim, unlike the old real-time
    record_video path."""
    cmd = build_pipe_encode_cmd("/sd/out.mp4")
    assert "-ss" not in cmd


# ── deterministic_shim.js ──────────────────────────────────────────────────


def test_deterministic_shim_file_exists():
    assert DETERMINISTIC_SHIM_PATH.is_file()


def test_deterministic_shim_defines_mc_tick():
    shim = load_deterministic_shim()
    assert "__mcTick" in shim
    assert "requestAnimationFrame" in shim
    assert "getAnimations" in shim


def test_deterministic_shim_clamps_zero_delay_timers():
    """Review finding (2026-07-15): a self-rearming setTimeout(fn, 0) must
    not stay perpetually "due" at the same virtual time — that would hang
    __mcTick's timer-draining loop forever (vt never advances) instead of
    firing at most ~stepMs times per tick. The old code only clamped
    setInterval (Math.max(1,d)), not setTimeout (Math.max(0,d))."""
    shim = load_deterministic_shim()
    assert "Math.max(0,d)" not in shim


def test_deterministic_shim_has_timer_iteration_cap():
    """Belt-and-braces on top of the delay clamp: __mcTick must hard-cap how
    many timers it drains per tick so no pathological chain can turn one
    frame's tick into an unbounded synchronous loop."""
    shim = load_deterministic_shim()
    assert "MC_TICK_MAX_TIMER_ITERATIONS" in shim


# ── build_compose_cmd cell size (2026-07-15, 2K bump) ─────────────────────


def test_cell_size_is_2k():
    assert (CELL_WIDTH, CELL_HEIGHT) == (1280, 720)


# ── build_compose_cmd ─────────────────────────────────────────────────────────


def test_build_compose_cmd_two_inputs_hstack():
    cmd = build_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4"], ["Claude", "DeepSeek"], "/d/grid.mp4"
    )
    # inputs in order
    assert cmd[:2] == ["ffmpeg", "-y"]
    assert cmd[cmd.index("-i") + 1] == "/d/a.mp4"
    assert cmd.count("-i") == 2
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]" in fc and "[1:v]" in fc
    assert "text='Claude'" in fc
    assert "text='DeepSeek'" in fc
    assert f"fontfile={DEJAVU_BOLD}" in fc
    assert "hstack=inputs=2:shortest=1[v]" in fc
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert cmd[-1] == "/d/grid.mp4"
    # H.264 output for X
    assert "libx264" in cmd and "yuv420p" in cmd


def test_build_compose_cmd_three_inputs_hstack3():
    cmd = build_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4", "/d/c.mp4"], ["A", "B", "C"], "/d/grid.mp4"
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "hstack=inputs=3:shortest=1[v]" in fc


def test_build_compose_cmd_four_inputs_xstack_2x2():
    cmd = build_compose_cmd(
        [f"/d/{i}.mp4" for i in range(4)], ["A", "B", "C", "D"], "/d/grid.mp4"
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:shortest=1[v]" in fc


def test_build_compose_cmd_single_input_maps_v0():
    cmd = build_compose_cmd(["/d/a.mp4"], ["Solo"], "/d/out.mp4")
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "hstack" not in fc and "xstack" not in fc
    assert cmd[cmd.index("-map") + 1] == "[v0]"


def test_build_compose_cmd_speed_labels_appended():
    cmd = build_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4"],
        ["Claude", "DeepSeek"],
        "/d/grid.mp4",
        speed_labels=["12 s", "87 tok/s"],
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "text='Claude · 12 s'" in fc
    assert "text='DeepSeek · 87 tok/s'" in fc


def test_build_compose_cmd_labels_are_escaped():
    cmd = build_compose_cmd(["/d/a.mp4"], ["Mark's 100% run"], "/d/out.mp4")
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "text='Mark’s 100\\% run'" in fc


# ── ComposeRequest branding validation ────────────────────────────────────


def _branding(n_models: int = 2) -> BrandingSpec:
    models = [
        BrandingModelSpec(label="Qwen 3.6 35B A3B", tag="LOCAL · SPARK"),
        BrandingModelSpec(label="Grok 4.5", tag="GROK"),
    ][:n_models]
    return BrandingSpec(
        title="SVG timeline animation",
        run_label="019",
        prompt_line="single HTML file · timeline animation",
        models=models,
        outro_rows=[
            BrandingOutroRow(name="Qwen 3.6 35B", time="12.8 min", size="48 KB"),
            BrandingOutroRow(name="Grok 4.5", time="9.4 min", size="61 KB"),
        ],
    )


def test_branding_spec_allows_one_model_solo():
    spec = _branding(n_models=1)
    assert len(spec.models) == 1


def test_branding_spec_allows_two_models_side_by_side():
    spec = _branding(n_models=2)
    assert len(spec.models) == 2


def test_branding_spec_rejects_zero_models():
    with pytest.raises(ValidationError):
        BrandingSpec(
            title="t", run_label="001", prompt_line="p", models=[], outro_rows=[]
        )


def test_branding_spec_rejects_more_than_two_models():
    with pytest.raises(ValidationError):
        BrandingSpec(
            title="t",
            run_label="001",
            prompt_line="p",
            models=[
                BrandingModelSpec(label="A", tag="A"),
                BrandingModelSpec(label="B", tag="B"),
                BrandingModelSpec(label="C", tag="C"),
            ],
            outro_rows=[],
        )


def test_compose_request_branding_ok_with_two_inputs():
    req = ComposeRequest(
        inputs=["/d/a.mp4", "/d/b.mp4"],
        labels=["Qwen", "Grok"],
        output_path="/d/branded.mp4",
        branding=_branding(n_models=2),
    )
    assert req.branding is not None


def test_compose_request_branding_ok_with_one_input():
    req = ComposeRequest(
        inputs=["/d/a.mp4"],
        labels=["Qwen"],
        output_path="/d/branded.mp4",
        branding=_branding(n_models=1),
    )
    assert req.branding is not None
    assert len(req.branding.models) == 1


def test_compose_request_branding_rejects_input_model_count_mismatch():
    # 2 inputs but branding.models has only 1 (or vice versa) -> reject.
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=["/d/a.mp4", "/d/b.mp4"],
            labels=["Qwen", "Grok"],
            output_path="/d/branded.mp4",
            branding=_branding(n_models=1),
        )
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=["/d/a.mp4"],
            labels=["Qwen"],
            output_path="/d/branded.mp4",
            branding=_branding(n_models=2),
        )
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=["/d/a.mp4", "/d/b.mp4", "/d/c.mp4"],
            labels=["Qwen", "Grok", "Claude"],
            output_path="/d/branded.mp4",
            branding=_branding(n_models=2),
        )


# ── fill_bench_template ────────────────────────────────────────────────────


def test_fill_bench_template_replaces_all_tokens():
    template = "<h1>{{TITLE}}</h1><span>{{RUN_LABEL}}</span>"
    out = fill_bench_template(template, {"TITLE": "My Run", "RUN_LABEL": "007"})
    assert out == "<h1>My Run</h1><span>007</span>"


def test_fill_bench_template_html_escapes_values():
    template = "<b>{{PROMPT_LINE}}</b>"
    out = fill_bench_template(template, {"PROMPT_LINE": "A & B <script>x</script>"})
    assert "<script>" not in out
    assert out == "<b>A &amp; B &lt;script&gt;x&lt;/script&gt;</b>"


def test_fill_bench_template_leaves_unknown_placeholders_untouched():
    template = "{{KNOWN}} {{UNKNOWN}}"
    out = fill_bench_template(template, {"KNOWN": "x"})
    assert out == "x {{UNKNOWN}}"


def test_fill_bench_template_all_frame_tokens_replaced():
    tokens = {
        "TITLE": "SVG timeline animation",
        "RUN_LABEL": "019",
        "MODEL_A": "Qwen 3.6 35B A3B",
        "TAG_A": "LOCAL · SPARK",
        "MODEL_B": "Grok 4.5",
        "TAG_B": "GROK",
        "PROMPT_LINE": "single HTML file · timeline animation",
        "MODE_LINE": "side by side",
    }
    template = " ".join(f"{{{{{k}}}}}" for k in tokens)
    out = fill_bench_template(template, tokens)
    assert "{{" not in out
    for v in tokens.values():
        assert v in out


def test_fill_bench_template_all_frame_single_tokens_replaced():
    tokens = {
        "TITLE": "SVG timeline animation",
        "RUN_LABEL": "019",
        "MODEL": "Qwen 3.6 35B A3B",
        "TAG": "LOCAL · SPARK",
        "PROMPT_LINE": "single HTML file · timeline animation",
        "MODE_LINE": "solo run",
    }
    template = " ".join(f"{{{{{k}}}}}" for k in tokens)
    out = fill_bench_template(template, tokens)
    assert "{{" not in out
    for v in tokens.values():
        assert v in out


def test_frame_single_html_template_has_all_expected_tokens():
    """Contract pin: frame_single.html's placeholders must match exactly what
    service.py's _render_branding_assets fills for a 1-model BrandingSpec —
    TITLE/RUN_LABEL/MODEL/TAG/PROMPT_LINE/MODE_LINE (2026-07-13)."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parent.parent / "templates" / "bench" / "frame_single.html"
    ).read_text(encoding="utf-8")
    for token in ("TITLE", "RUN_LABEL", "MODEL", "TAG", "PROMPT_LINE", "MODE_LINE"):
        assert f"{{{{{token}}}}}" in template, f"missing {{{{{token}}}}} in frame_single.html"


# ── render_outro_rows_html ──────────────────────────────────────────────────


def test_render_outro_rows_html_two_rows():
    rows = [
        BrandingOutroRow(name="Qwen 3.6 35B", time="12.8 min", size="48 KB"),
        BrandingOutroRow(name="Grok 4.5", time="9.4 min", size="61 KB"),
    ]
    html = render_outro_rows_html(rows)
    assert html.count('class="row"') == 2
    assert "Qwen 3.6 35B" in html and "12.8 min" in html and "48 KB" in html
    assert "Grok 4.5" in html and "9.4 min" in html and "61 KB" in html
    # No winner-highlight styling (Mark's decision 2026-07-12 — neutral table).
    assert "row win" not in html
    assert "●" not in html


def test_render_outro_rows_html_escapes_values():
    rows = [BrandingOutroRow(name="A & <b>B</b>", time="1 min", size="1 KB")]
    html = render_outro_rows_html(rows)
    assert "<b>B</b>" not in html
    assert "&amp;" in html and "&lt;b&gt;" in html


def test_render_outro_rows_html_defaults_cost_and_tokens_to_em_dash():
    """Older callers that don't pass cost/tokens stay compatible — both
    default to the em dash (2026-07-15)."""
    rows = [BrandingOutroRow(name="A", time="1 min", size="1 KB")]
    html = render_outro_rows_html(rows)
    assert html.count("—") == 2


def test_render_outro_rows_html_includes_tokens_column():
    rows = [
        BrandingOutroRow(name="Grok 4.5", time="9.4 min", size="61 KB",
                          cost="$0.42", tokens="12.4k → 1.8k"),
    ]
    html = render_outro_rows_html(rows)
    assert html.count('class="val"') == 4
    assert "12.4k → 1.8k" in html


# ── build_branded_compose_cmd ────────────────────────────────────────────────


def test_build_branded_compose_cmd_slot_coords_and_pad_color():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    ax, ay = SLOT_A_XY
    bx, by = SLOT_B_XY
    assert f"overlay={ax}:{ay}:shortest=1" in fc
    assert f"overlay={bx}:{by}:shortest=1" in fc
    assert f"scale={SLOT_WIDTH}:{SLOT_HEIGHT}" in fc
    assert f"color={SLOT_BG_COLOR}" in fc


def test_build_branded_compose_cmd_concat_structure():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[mainv][outrov]concat=n=2:v=1:a=0[outv]" in fc
    assert cmd[cmd.index("-map") + 1] == "[outv]"
    # frame loops indefinitely (no -t) so the branded main duration is
    # governed by shortest=1 on the video overlays, not a hardcoded length:
    # "-loop 1 -i frame.png" — the token right after "-loop 1" is "-i", not "-t".
    loop_idx = cmd.index("-loop")
    assert cmd[loop_idx:loop_idx + 3] == ["-loop", "1", "-i"]
    # outro loops for a fixed duration: the second "-loop 1" is followed by "-t 2.0".
    second_loop_idx = cmd.index("-loop", loop_idx + 1)
    assert cmd[second_loop_idx:second_loop_idx + 4] == ["-loop", "1", "-t", "2.0"]


def test_build_branded_compose_cmd_inputs_order():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    i_positions = [i for i, tok in enumerate(cmd) if tok == "-i"]
    inputs_in_order = [cmd[i + 1] for i in i_positions]
    assert inputs_in_order == ["/d/frame.png", "/d/a.mp4", "/d/b.mp4", "/d/outro.png"]


def test_build_branded_compose_cmd_h264_output():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    assert "libx264" in cmd and "yuv420p" in cmd and "+faststart" in cmd
    assert cmd[-1] == "/d/out.mp4"


def test_build_branded_compose_cmd_rejects_zero_or_more_than_two_inputs():
    with pytest.raises(ValueError):
        build_branded_compose_cmd(
            [], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
        )
    with pytest.raises(ValueError):
        build_branded_compose_cmd(
            ["/d/a.mp4", "/d/b.mp4", "/d/c.mp4"],
            "/d/frame.png", "/d/outro.png", "/d/out.mp4",
        )


# ── build_branded_compose_cmd — single-slot (solo) variant ─────────────────


def test_build_branded_compose_cmd_single_slot_coords_and_pad_color():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    sx, sy = SLOT_SINGLE_XY
    assert f"overlay={sx}:{sy}:shortest=1" in fc
    assert f"scale={SLOT_SINGLE_WIDTH}:{SLOT_SINGLE_HEIGHT}" in fc
    assert f"color={SLOT_BG_COLOR}" in fc
    # only one overlay in the solo variant (SLOT_SINGLE_XY happens to match
    # SLOT_A_XY, both (64,290) — the side-by-side frame's second slot at
    # SLOT_B_XY must not appear).
    bx, by = SLOT_B_XY
    assert f"overlay={bx}:{by}" not in fc
    assert fc.count("overlay=") == 1


def test_build_branded_compose_cmd_single_slot_concat_structure():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[mainv][outrov]concat=n=2:v=1:a=0[outv]" in fc
    assert cmd[cmd.index("-map") + 1] == "[outv]"


def test_build_branded_compose_cmd_single_slot_inputs_order():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    i_positions = [i for i, tok in enumerate(cmd) if tok == "-i"]
    inputs_in_order = [cmd[i + 1] for i in i_positions]
    assert inputs_in_order == ["/d/frame.png", "/d/a.mp4", "/d/outro.png"]


def test_build_branded_compose_cmd_single_slot_h264_output():
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    assert "libx264" in cmd and "yuv420p" in cmd and "+faststart" in cmd
    assert cmd[-1] == "/d/out.mp4"


def test_build_branded_compose_cmd_two_input_output_unchanged_by_single_support():
    """Regression pin (2026-07-13; slot geometry doubled 2026-07-15 for the
    2x/retina branding-card bump — SLOT_* constants below are imported live
    from media.py so this stays byte-pinned to whatever geometry is current):
    same filter_complex shape, same input order, same encode flags as the
    1-input path leaves untouched."""
    cmd = build_branded_compose_cmd(
        ["/d/a.mp4", "/d/b.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
    )
    ax, ay = SLOT_A_XY
    bx, by = SLOT_B_XY
    assert cmd == [
        "ffmpeg", "-y",
        "-loop", "1", "-i", "/d/frame.png",
        "-i", "/d/a.mp4",
        "-i", "/d/b.mp4",
        "-loop", "1", "-t", "2.0", "-i", "/d/outro.png",
        "-filter_complex",
        ";".join([
            f"[1:v]scale={SLOT_WIDTH}:{SLOT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={SLOT_WIDTH}:{SLOT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={SLOT_BG_COLOR}[va]",
            f"[2:v]scale={SLOT_WIDTH}:{SLOT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={SLOT_WIDTH}:{SLOT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={SLOT_BG_COLOR}[vb]",
            f"[0:v][va]overlay={ax}:{ay}:shortest=1[bg1]",
            f"[bg1][vb]overlay={bx}:{by}:shortest=1[main]",
            "[main]fps=30,format=yuv420p[mainv]",
            "[3:v]fps=30,format=yuv420p[outrov]",
            "[mainv][outrov]concat=n=2:v=1:a=0[outv]",
        ]),
        "-map", "[outv]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        "/d/out.mp4",
    ]


# ── /transcode (2026-07-16) ─────────────────────────────────────────────────


def test_transcode_request_defaults():
    req = TranscodeRequest(
        input_path="/shared-deliverables/bench-1/composed.mp4",
        output_dir="/shared-deliverables/catalog/ep-1",
    )
    assert req.max_width == 1920
    assert req.crf == 23
    assert req.poster_at_s == 1.0


def test_transcode_request_rejects_out_of_range_crf():
    with pytest.raises(ValidationError):
        TranscodeRequest(
            input_path="/sd/a.mp4", output_dir="/sd/out", crf=99,
        )


def test_build_transcode_video_cmd_defaults():
    cmd = build_transcode_video_cmd(
        "/sd/composed.mp4", "/sd/out/episode.mp4", max_width=1920, crf=23,
    )
    assert cmd == [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", "/sd/composed.mp4",
        "-vf", "scale=1920:-2",
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "slow",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",
        "/sd/out/episode.mp4",
    ]


def test_build_transcode_video_cmd_custom_width_and_crf():
    cmd = build_transcode_video_cmd(
        "/sd/in.mp4", "/sd/out.mp4", max_width=1280, crf=28,
    )
    assert cmd[cmd.index("-vf") + 1] == "scale=1280:-2"
    assert cmd[cmd.index("-crf") + 1] == "28"


def test_build_transcode_poster_cmd():
    cmd = build_transcode_poster_cmd("/sd/in.mp4", "/sd/poster.jpg", poster_at_s=2.5)
    assert cmd == [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-ss", "2.5",
        "-i", "/sd/in.mp4",
        "-frames:v", "1",
        "-q:v", "3",
        "/sd/poster.jpg",
    ]


# ── clamp_poster_at_s (2026-07-16, review finding F4) ───────────────────────


def test_clamp_poster_at_s_within_duration_unchanged():
    assert clamp_poster_at_s(1.0, 10.0) == 1.0


def test_clamp_poster_at_s_past_duration_clamps_with_margin():
    """A 0.5s clip with the default 1.0s poster_at_s must not seek past
    EOF — clamp into duration - 0.1s."""
    assert clamp_poster_at_s(1.0, 0.5) == pytest.approx(0.4)


def test_clamp_poster_at_s_very_short_clip_clamps_to_zero():
    """A clip shorter than the 0.1s margin itself must clamp to 0, never
    negative."""
    assert clamp_poster_at_s(1.0, 0.05) == 0.0


def test_clamp_poster_at_s_exact_boundary():
    assert clamp_poster_at_s(5.0, 5.1) == pytest.approx(5.0)


# ── resolve_contained_path (2026-07-16, review finding F9: /transcode path
#    containment — extracted from service.py's _require_shared_path so it's
#    testable without playwright installed) ─────────────────────────────────


def test_resolve_contained_path_accepts_path_under_root():
    result = resolve_contained_path(
        "/shared-deliverables/bench-1/composed.mp4", "/shared-deliverables",
    )
    assert result == Path("/shared-deliverables/bench-1/composed.mp4")


def test_resolve_contained_path_rejects_path_outside_root():
    assert resolve_contained_path("/etc/passwd", "/shared-deliverables") is None


def test_resolve_contained_path_rejects_traversal_outside_root():
    """`..` segments that walk back out of root must not resolve, even
    though the raw string starts with the root prefix textually — this is
    exactly the class of bug path.startswith() checks miss and .resolve()
    + is_relative_to() catches."""
    assert resolve_contained_path(
        "/shared-deliverables/../etc/passwd", "/shared-deliverables",
    ) is None


def test_resolve_contained_path_rejects_sibling_prefix_match():
    """A textual-prefix match on a sibling directory (`/shared-deliverables-evil`
    starts with `/shared-deliverables`) must NOT be treated as contained —
    only a real path-segment containment counts."""
    assert resolve_contained_path(
        "/shared-deliverables-evil/x.mp4", "/shared-deliverables",
    ) is None
