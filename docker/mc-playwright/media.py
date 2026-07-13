"""Pure helpers for the /record and /compose endpoints — request models +
ffmpeg command builders. Deliberately NO playwright/ffmpeg imports so this
module is unit-testable in the backend venv (pydantic only):

    cd backend && python -m pytest ../docker/mc-playwright/tests/test_media.py -v

service.py imports VIEWPORTS + the models + the builders from here.
"""
from __future__ import annotations

import html as _html
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# Single source for viewport presets (service.py imports this).
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile":  {"width":  390, "height": 844},  # iPhone 13-ish
    "tablet":  {"width":  768, "height": 1024},
}

FFMPEG_BIN = "ffmpeg"
# DejaVu ships in the playwright jammy image; the Dockerfile additionally
# installs fonts-dejavu-core so this path is guaranteed.
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Grid cell size: 2x2 -> 1920x1080, 2x1 -> 1920x540, 3x1 -> 2880x540.
CELL_WIDTH = 960
CELL_HEIGHT = 540


# ── Request/Response models ──────────────────────────────────────────────────


class RecordRequest(BaseModel):
    """POST /record — load a page (URL or local HTML file), record duration_s
    seconds of video, save recording.mp4 (H.264) + screenshot.png to output_dir."""
    html_path: Optional[str] = None
    url: Optional[str] = None
    duration_s: int = Field(default=10, ge=1, le=60)
    viewport: str = "desktop"
    output_dir: str

    @model_validator(mode="after")
    def _check(self) -> "RecordRequest":
        if bool(self.html_path) == bool(self.url):
            raise ValueError("Entweder 'html_path' ODER 'url' angeben (genau eines).")
        if self.viewport not in VIEWPORTS:
            raise ValueError(f"viewport muss einer von {list(VIEWPORTS)} sein")
        return self

    @property
    def target_url(self) -> str:
        if self.url:
            return self.url
        return Path(self.html_path).as_uri()  # type: ignore[arg-type]


class RecordResponse(BaseModel):
    video_path: str
    screenshot_path: str
    duration_s: int
    bytes: int


class BrandingModelSpec(BaseModel):
    """One model's chip in the branded frame (label + tag, e.g. 'LOCAL · SPARK')."""
    label: str
    tag: str


class BrandingOutroRow(BaseModel):
    """One row of the outro results table. `cost` is optional (default em
    dash) so older callers without cost attribution stay compatible."""
    name: str
    time: str
    size: str
    cost: str = "\u2014"


class BrandingSpec(BaseModel):
    """POST /compose branding payload — fills the argyelan frame/outro templates
    and composites the recording(s) into the frame's video slot(s) instead of
    the plain labeled grid. 1 model -> single-slot frame_single.html, 2 models
    -> side-by-side frame.html (2026-07-13, single-video branding).
    See templates/bench/{frame,frame_single,outro}.html."""
    title: str
    run_label: str
    prompt_line: str
    models: List[BrandingModelSpec]
    outro_rows: List[BrandingOutroRow]

    @model_validator(mode="after")
    def _check(self) -> "BrandingSpec":
        if len(self.models) not in (1, 2):
            raise ValueError(
                "branding.models muss 1 (solo) oder 2 (side-by-side) Eintraege haben"
            )
        return self


class ComposeRequest(BaseModel):
    """POST /compose — N mp4 recordings -> one labeled grid video (H.264),
    or (with `branding` set) one or two recordings composited into the
    branded argyelan frame + a 2s outro card appended."""
    inputs: List[str] = Field(min_length=1, max_length=4)
    labels: List[str]
    layout: Literal["grid"] = "grid"
    speed_labels: Optional[List[str]] = None  # e.g. ["42 s", "87 tok/s"] — appended to labels
    output_path: str
    branding: Optional[BrandingSpec] = None

    @model_validator(mode="after")
    def _check(self) -> "ComposeRequest":
        if len(self.labels) != len(self.inputs):
            raise ValueError("labels muss gleich viele Eintraege haben wie inputs")
        if self.speed_labels is not None and len(self.speed_labels) != len(self.inputs):
            raise ValueError("speed_labels muss gleich viele Eintraege haben wie inputs")
        if self.branding is not None and len(self.inputs) != len(self.branding.models):
            raise ValueError(
                "branding.models muss gleich viele Eintraege haben wie inputs (1 oder 2)"
            )
        return self


class ComposeResponse(BaseModel):
    output_path: str
    bytes: int
    inputs: int


# ── ffmpeg command builders (pure) ───────────────────────────────────────────


def escape_drawtext(text: str) -> str:
    """Makes a label safe inside drawtext text='...' in an ffmpeg filtergraph:
    - backslash and percent are drawtext expansion chars -> escape
    - a literal single quote would terminate the filtergraph quoting; the
      escape dance ('\\'') is fragile, so use the typographic quote instead
    """
    return (
        text.replace("\\", "\\\\")
        .replace("'", "’")
        .replace("%", "\\%")
    )


# Head-trim for /record (2026-07-12): Playwright starts recording at context
# creation, so the first frames show Chromium's default white page before the
# HTML's first paint — a white flash at the start of every bench video. Fix:
# record RECORD_SETTLE_S extra seconds and cut them off the head in the
# transcode step — the delivered mp4 keeps the requested duration_s and
# starts on real content.
RECORD_SETTLE_S = 1.0


def build_transcode_cmd(src: str, dst: str, trim_start_s: float = 0.0) -> List[str]:
    """webm (Playwright record_video) -> H.264 mp4, X-compatible
    (yuv420p + faststart), constant 30 fps, no audio track.

    trim_start_s > 0 cuts that many seconds off the HEAD of the recording
    (white-flash fix, see RECORD_SETTLE_S). The `-ss` sits AFTER `-i`
    deliberately: output-side seeking decodes from the start and trims
    frame-accurately, while input-side `-ss` snaps to keyframes — with
    Playwright's sparse-keyframe webm that cuts visibly wrong. Output
    duration = source duration - trim_start_s.
    """
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", src,
    ]
    if trim_start_s > 0:
        cmd += ["-ss", str(trim_start_s)]
    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        dst,
    ]
    return cmd


def build_compose_cmd(
    inputs: List[str],
    labels: List[str],
    output_path: str,
    speed_labels: Optional[List[str]] = None,
    fontfile: str = DEJAVU_BOLD,
) -> List[str]:
    """N mp4s -> one labeled grid (H.264 mp4).

    Layouts by count: 1 = single cell, 2 = 2x1 hstack, 3 = 3x1 hstack,
    4 = 2x2 xstack. Every cell is scaled+padded to 960x540 (aspect kept,
    black bars), label drawn top-left on a semi-transparent box.
    shortest=1: the grid ends with the shortest input (entries may differ
    by a few frames after transcode).
    """
    n = len(inputs)
    chains: List[str] = []
    for i, label in enumerate(labels):
        text = label
        if speed_labels and speed_labels[i]:
            text = f"{label} · {speed_labels[i]}"
        chains.append(
            f"[{i}:v]"
            f"scale={CELL_WIDTH}:{CELL_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={CELL_WIDTH}:{CELL_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"drawtext=fontfile={fontfile}:text='{escape_drawtext(text)}':"
            f"x=16:y=16:fontsize=32:fontcolor=white:"
            f"box=1:boxcolor=black@0.55:boxborderw=8"
            f"[v{i}]"
        )

    if n == 1:
        out_label = "v0"
    elif n == 2:
        chains.append("[v0][v1]hstack=inputs=2:shortest=1[v]")
        out_label = "v"
    elif n == 3:
        chains.append("[v0][v1][v2]hstack=inputs=3:shortest=1[v]")
        out_label = "v"
    else:
        chains.append(
            "[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:shortest=1[v]"
        )
        out_label = "v"

    cmd: List[str] = [FFMPEG_BIN, "-y"]
    for path in inputs:
        cmd += ["-i", path]
    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", f"[{out_label}]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        output_path,
    ]
    return cmd


# ── Branded compose (2026-07-12, Benchmark Studio video branding;
#    2026-07-13, single-video branding) ──────────────────────────────────────
#
# Slot geometry is canonical from templates/bench/frame.html (side-by-side)
# and templates/bench/frame_single.html (solo): the side-by-side frame has
# two 872x560 slots at (64,290) and (984,290); the single frame has one
# 1792x560 slot at (64,290) — full inner width, same top/bottom margins, on
# the shared 1920x1080 frame. The frame + outro PNG screenshots are rendered
# by the caller (service.py, via Playwright) from the filled HTML templates
# — this module stays pure (no playwright import).

SLOT_WIDTH = 872
SLOT_HEIGHT = 560
SLOT_A_XY = (64, 290)
SLOT_B_XY = (984, 290)
SLOT_SINGLE_WIDTH = 1792
SLOT_SINGLE_HEIGHT = 560
SLOT_SINGLE_XY = (64, 290)
SLOT_BG_COLOR = "0x090B10"  # matches .slot background in shared.css
OUTRO_DURATION_S = 2.0
FRAME_SIZE = "1920x1080"


def fill_bench_template(template_text: str, tokens: dict) -> str:
    """Replaces every `{{TOKEN}}` placeholder with the html-escaped value.

    Pure str.replace — no Jinja, matches the rest of this module's
    "boring and testable" style. Unknown tokens present in `tokens` but not
    in the template are simply unused (no error); tokens referenced in the
    template but missing from `tokens` are left as literal `{{TOKEN}}` text
    (fail loud in the rendered screenshot, not silently).
    """
    out = template_text
    for key, value in tokens.items():
        out = out.replace("{{" + key + "}}", _html.escape(str(value)))
    return out


def render_outro_rows_html(rows: List["BrandingOutroRow"]) -> str:
    """Builds the outro table row markup for the `{{ROWS}}` placeholder in
    templates/bench/outro.html — one `.row` div per BrandingOutroRow, values
    html-escaped. No winner-highlight styling (Mark's decision 2026-07-12:
    the human judges, the card stays neutral)."""
    parts = []
    for row in rows:
        name = _html.escape(row.name)
        time_val = _html.escape(row.time)
        size_val = _html.escape(row.size)
        cost_val = _html.escape(row.cost)
        parts.append(
            f'<div class="row"><span class="name">{name}</span>'
            f'<span class="val">{time_val}</span>'
            f'<span class="val">{size_val}</span>'
            f'<span class="val">{cost_val}</span></div>'
        )
    return "".join(parts)


def build_branded_compose_cmd(
    inputs: List[str],
    frame_png: str,
    outro_png: str,
    output_path: str,
    *,
    outro_duration_s: float = OUTRO_DURATION_S,
    fps: int = 30,
) -> List[str]:
    """Branded compose: overlays the recording(s) into the frame's video
    slot(s), then appends a static outro card for `outro_duration_s` seconds.
    Single ffmpeg invocation (no intermediate files):
      - the frame PNG is looped indefinitely as the background; the overlay
        chain uses shortest=1 (same contract as build_compose_cmd's
        hstack/xstack) so the branded main segment ends exactly when the
        (shortest, same-duration) recording(s) end — no need to know
        duration_s up front,
      - the outro PNG is looped for a fixed outro_duration_s,
      - the two segments are joined with the concat filter.

    Accepts exactly 1 (solo, frame_single.html geometry) or 2 inputs
    (side-by-side, frame.html geometry) — enforced by
    ComposeRequest/BrandingSpec at the request-validation layer already;
    this builder trusts its caller for the input/frame pairing. 2-input
    output is byte-identical to the pre-single-video-branding builder
    (2026-07-13 regression contract).
    """
    if len(inputs) not in (1, 2):
        raise ValueError("build_branded_compose_cmd erwartet 1 oder 2 inputs")

    if len(inputs) == 2:
        (ax, ay) = SLOT_A_XY
        (bx, by) = SLOT_B_XY
        filter_complex = ";".join([
            f"[1:v]scale={SLOT_WIDTH}:{SLOT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={SLOT_WIDTH}:{SLOT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={SLOT_BG_COLOR}[va]",
            f"[2:v]scale={SLOT_WIDTH}:{SLOT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={SLOT_WIDTH}:{SLOT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={SLOT_BG_COLOR}[vb]",
            f"[0:v][va]overlay={ax}:{ay}:shortest=1[bg1]",
            f"[bg1][vb]overlay={bx}:{by}:shortest=1[main]",
            f"[main]fps={fps},format=yuv420p[mainv]",
            f"[3:v]fps={fps},format=yuv420p[outrov]",
            "[mainv][outrov]concat=n=2:v=1:a=0[outv]",
        ])
        cmd = [
            FFMPEG_BIN, "-y",
            "-loop", "1", "-i", frame_png,
            "-i", inputs[0],
            "-i", inputs[1],
            "-loop", "1", "-t", str(outro_duration_s), "-i", outro_png,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
        ]
    else:
        (sx, sy) = SLOT_SINGLE_XY
        filter_complex = ";".join([
            f"[1:v]scale={SLOT_SINGLE_WIDTH}:{SLOT_SINGLE_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={SLOT_SINGLE_WIDTH}:{SLOT_SINGLE_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={SLOT_BG_COLOR}[vs]",
            f"[0:v][vs]overlay={sx}:{sy}:shortest=1[main]",
            f"[main]fps={fps},format=yuv420p[mainv]",
            f"[2:v]fps={fps},format=yuv420p[outrov]",
            "[mainv][outrov]concat=n=2:v=1:a=0[outv]",
        ])
        cmd = [
            FFMPEG_BIN, "-y",
            "-loop", "1", "-i", frame_png,
            "-i", inputs[0],
            "-loop", "1", "-t", str(outro_duration_s), "-i", outro_png,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
        ]

    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        output_path,
    ]
    return cmd
