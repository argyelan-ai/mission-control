"""Pure-function tests for docker/mc-playwright/media.py.

Request-model validation + ffmpeg command builders only — the live /record
and /compose E2E happens at the supervised live gate, not here (no browser,
no ffmpeg binary needed).
"""
import pytest
from pydantic import ValidationError

from media import (
    DEJAVU_BOLD,
    VIEWPORTS,
    ComposeRequest,
    RecordRequest,
    build_compose_cmd,
    build_transcode_cmd,
    escape_drawtext,
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
    assert escape_drawtext("it's 100% \\great") == "it's 100\\% \\\\great"


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
    assert "text='Mark's 100\\% run'" in fc
