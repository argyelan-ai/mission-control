"""Pure helpers for the /record and /compose endpoints — request models +
ffmpeg command builders. Deliberately NO playwright/ffmpeg imports so this
module is unit-testable in the backend venv (pydantic only):

    cd backend && python -m pytest ../docker/mc-playwright/tests/test_media.py -v

service.py imports VIEWPORTS + the models + the builders from here.
"""
from __future__ import annotations

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


class ComposeRequest(BaseModel):
    """POST /compose — N mp4 recordings -> one labeled grid video (H.264)."""
    inputs: List[str] = Field(min_length=1, max_length=4)
    labels: List[str]
    layout: Literal["grid"] = "grid"
    speed_labels: Optional[List[str]] = None  # e.g. ["42 s", "87 tok/s"] — appended to labels
    output_path: str

    @model_validator(mode="after")
    def _check(self) -> "ComposeRequest":
        if len(self.labels) != len(self.inputs):
            raise ValueError("labels muss gleich viele Eintraege haben wie inputs")
        if self.speed_labels is not None and len(self.speed_labels) != len(self.inputs):
            raise ValueError("speed_labels muss gleich viele Eintraege haben wie inputs")
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


def build_transcode_cmd(src: str, dst: str) -> List[str]:
    """webm (Playwright record_video) -> H.264 mp4, X-compatible
    (yuv420p + faststart), constant 30 fps, no audio track."""
    return [
        FFMPEG_BIN, "-y",
        "-i", src,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        "-an",
        dst,
    ]


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
