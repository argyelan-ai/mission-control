"""Pure-function tests for docker/mc-playwright/media.py.

Request-model validation + ffmpeg command builders only — the live /record
and /compose E2E happens at the supervised live gate, not here (no browser,
no ffmpeg binary needed).
"""
import pytest
from pydantic import ValidationError

from media import (
    DEJAVU_BOLD,
    SLOT_A_XY,
    SLOT_B_XY,
    SLOT_BG_COLOR,
    SLOT_HEIGHT,
    SLOT_WIDTH,
    VIEWPORTS,
    BrandingModelSpec,
    BrandingOutroRow,
    BrandingSpec,
    ComposeRequest,
    RecordRequest,
    build_branded_compose_cmd,
    build_compose_cmd,
    build_transcode_cmd,
    escape_drawtext,
    fill_bench_template,
    render_outro_rows_html,
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


# ── build_transcode_cmd ───────────────────────────────────────────────────────


def test_build_transcode_cmd():
    cmd = build_transcode_cmd("/tmp/in.webm", "/shared-deliverables/out.mp4")
    assert cmd == [
        "ffmpeg", "-y",
        "-i", "/tmp/in.webm",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        "/shared-deliverables/out.mp4",
    ]


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


def test_branding_spec_requires_exactly_two_models():
    with pytest.raises(ValidationError):
        _branding(n_models=1)


def test_compose_request_branding_ok_with_two_inputs():
    req = ComposeRequest(
        inputs=["/d/a.mp4", "/d/b.mp4"],
        labels=["Qwen", "Grok"],
        output_path="/d/branded.mp4",
        branding=_branding(),
    )
    assert req.branding is not None


def test_compose_request_branding_rejects_wrong_input_count():
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=["/d/a.mp4", "/d/b.mp4", "/d/c.mp4"],
            labels=["Qwen", "Grok", "Claude"],
            output_path="/d/branded.mp4",
            branding=_branding(),
        )
    with pytest.raises(ValidationError):
        ComposeRequest(
            inputs=["/d/a.mp4"],
            labels=["Qwen"],
            output_path="/d/branded.mp4",
            branding=_branding(),
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


def test_build_branded_compose_cmd_requires_exactly_two_inputs():
    with pytest.raises(ValueError):
        build_branded_compose_cmd(
            ["/d/a.mp4"], "/d/frame.png", "/d/outro.png", "/d/out.mp4"
        )
