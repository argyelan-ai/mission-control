"""Bench compilation (phase 1, 30.10.2026) — pure helpers for POST /compilation.

2–4 FINISHED bench runs in one 4:5 video (1080×1350) — no new runs, only
their recordings and receipts:

    hook → one segment per run → the numbers (ranking) → the details

The artwork is never covered: every word, bar and box lives in the band
ABOVE the art (hardware · model · settings) or BELOW it (stats, bars,
footnote). The recording is scaled INTO COMP_ART, full width, so a 16:9
recording fills it edge to edge (no dead side bars). All words come from
the backend (English, public, privacy-checked there); the template holds no
copy. The racing bars are the only motion besides the art: ffmpeg drawbox
fills grown frame by frame through sendcmd, on tracks the template draws at
the SAME rectangles (comp_bar_rects is the one source).

Colours: warm off-black + one amber accent — never cyan (operator rule),
which is also why the template does not load shared.css.

Like media.py: NO playwright/ffmpeg imports, unit-testable with pydantic only.
"""
from __future__ import annotations

import html as _html
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

FFMPEG_BIN = "ffmpeg"

COMP_SIZE = (1080, 1350)
COMP_FLOOR_PX = 32          # the 1080-wide phone floor (same as media.W1_FLOOR_TALL_PX)
COMP_FPS = 30
COMP_VIDEO_BITRATE = "8M"
#: Header band 0..COMP_HEADER_H, the art below it, the lower band after.
COMP_HEADER_H = 208
COMP_ART = (0, 216, 1080, 608)
COMP_LOWER_TOP = COMP_ART[1] + COMP_ART[3] + 18
#: Racing bars: label column | track | value, one row per run.
COMP_BAR_TOP = 1064
COMP_BAR_ROW = 44
COMP_BAR_X = 472
COMP_BAR_W = 388
COMP_BAR_H = 16
COMP_BAR_GROW_S = 1.1
COMP_BAR_STAGGER_S = 0.12
COMP_BAR_DELAY_S = 0.25
#: Where the footnote sits when a part has no bars.
COMP_NOTE_TOP = 1236
#: Closing card: a row of the runs' screenshots right under the header,
#: then the pick, then the details (text flows down, pictures never move).
COMP_THUMB_Y = COMP_HEADER_H + 24
COMP_THUMB_GAP = 16
COMP_CONTENT_X = 56
COMP_CONTENT_W = COMP_SIZE[0] - 2 * COMP_CONTENT_X
#: ffmpeg colours (0xRRGGBB) — the template's CSS tokens carry the same.
COMP_BG = "0x0E0D0B"
COMP_ACCENT = "0xE9A23B"
COMP_BAR_MUTED = "0x6B645A"


class CompStat(BaseModel):
    value: str
    label: str
    unit: Optional[str] = None
    unit_before: bool = False
    #: False = arithmetic or estimate: drawn in the text colour, never the
    #: accent (the accent is for measured numbers).
    measured: bool = True


class CompBar(BaseModel):
    label: str
    value: str
    fraction: float = Field(ge=0, le=1)
    highlight: bool = False


class CompHeader(BaseModel):
    hardware: str
    model: str
    meta: str = ""


class CompArt(BaseModel):
    path: str
    kind: Literal["video", "image"] = "video"
    start_s: float = Field(default=0.0, ge=0, le=600)


class CompTableRow(BaseModel):
    cells: List[str]
    highlight: bool = False


class CompTable(BaseModel):
    columns: List[str] = Field(min_length=2, max_length=6)
    rows: List[CompTableRow] = Field(min_length=1, max_length=4)
    #: The column holding the run's name (left-aligned text, all others
    #: are right-aligned numbers).
    name_col: int = Field(default=0, ge=0, le=5)

    @model_validator(mode="after")
    def _check(self) -> "CompTable":
        for row in self.rows:
            if len(row.cells) != len(self.columns):
                raise ValueError("every table row needs one cell per column")
        return self


class CompPart(BaseModel):
    kind: Literal["hook", "entry", "ranking", "closing"]
    duration_s: float = Field(ge=0.5, le=15)
    header: CompHeader
    art: Optional[CompArt] = None
    caption: str = ""
    badge: Optional[str] = None
    hero: Optional[CompStat] = None
    hero_sub: Optional[str] = None
    stats: List[CompStat] = Field(default_factory=list, max_length=3)
    bars: List[CompBar] = Field(default_factory=list, max_length=4)
    table: Optional[CompTable] = None
    pick: Optional[str] = None
    pick_reason: Optional[str] = None
    details: List[str] = Field(default_factory=list, max_length=6)
    #: Closing only: the runs' screenshots in a row (whole, never cropped).
    thumbs: List[str] = Field(default_factory=list, max_length=4)
    footnote: str = ""

    @model_validator(mode="after")
    def _check(self) -> "CompPart":
        if self.thumbs and self.kind != "closing":
            raise ValueError("only the closing part has thumbnails")
        if self.kind == "closing":
            if self.art is not None:
                raise ValueError("the closing part carries the details, no art")
        elif self.art is None:
            raise ValueError(f"a {self.kind} part needs its art (recording or screenshot)")
        if self.kind == "ranking" and self.table is None:
            raise ValueError("the ranking part needs a table")
        return self


class CompilationSpec(BaseModel):
    shape: Literal["tall"] = "tall"
    parts: List[CompPart] = Field(min_length=4, max_length=8)
    footer: str = ""
    handle: str = ""
    checksum: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "CompilationSpec":
        n = sum(1 for p in self.parts if p.kind == "entry")
        if not 2 <= n <= 4:
            raise ValueError("a compilation shows 2 to 4 runs")
        return self


class CompilationRequest(BaseModel):
    """POST /compilation — the finished 4:5 video."""
    spec: CompilationSpec
    output_path: str


class CompilationResponse(BaseModel):
    output_path: str
    bytes: int
    duration_s: float
    parts: int


def compilation_inputs(spec: CompilationSpec) -> List[str]:
    """Every art file the encode reads, in part order (the service checks
    each one is below /shared-deliverables)."""
    out: List[str] = []
    for p in spec.parts:
        if p.art is not None:
            out.append(p.art.path)
        out.extend(p.thumbs)
    return out


def compilation_duration_s(spec: CompilationSpec) -> float:
    return float(sum(p.duration_s for p in spec.parts))


def comp_bar_rects(n: int) -> List[tuple[int, int, int, int]]:
    """Track rectangles (x, y, w, h) of n racing bars — the template draws
    the tracks here, ffmpeg grows the fills here."""
    pad = (COMP_BAR_ROW - COMP_BAR_H) // 2
    return [(COMP_BAR_X, COMP_BAR_TOP + i * COMP_BAR_ROW + pad, COMP_BAR_W, COMP_BAR_H) for i in range(n)]


def comp_thumb_rects(n: int) -> List[tuple[int, int, int, int]]:
    """n 16:9 thumbnails in one row at the top of the closing card."""
    if n <= 0:
        return []
    w = int((COMP_CONTENT_W - COMP_THUMB_GAP * (n - 1)) / n) // 2 * 2
    h = int(round(w * 9 / 16 / 2)) * 2
    return [(COMP_CONTENT_X + i * (w + COMP_THUMB_GAP), COMP_THUMB_Y, w, h) for i in range(n)]


def comp_bar_width(fraction: float, track_w: int) -> int:
    """Final fill width, even, never below 2 (drawbox w=0 = full width)."""
    w = int(round(max(0.0, min(1.0, fraction)) * track_w / 2)) * 2
    return max(2, w)


def _ease_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def comp_bar_commands(part: CompPart, part_idx: int, fps: int = COMP_FPS) -> str:
    """sendcmd script that grows every bar fill from 2 px to its value,
    one command per frame, staggered, ease-out. Empty without bars."""
    if not part.bars:
        return ""
    rects = comp_bar_rects(len(part.bars))
    steps = max(1, int(round(COMP_BAR_GROW_S * fps)))
    times: dict[float, List[str]] = {}
    for i, (bar, (_x, _y, tw, _h)) in enumerate(zip(part.bars, rects)):
        final = comp_bar_width(bar.fraction, tw)
        start = COMP_BAR_DELAY_S + i * COMP_BAR_STAGGER_S
        last = 2
        for k in range(steps + 1):
            t = round(start + k / fps, 3)
            w = final if k == steps else max(2, int(round(final * _ease_out(k / steps) / 2)) * 2)
            w = max(w, last)
            last = w
            times.setdefault(t, []).append(f"drawbox@p{part_idx}b{i} w {w}")
    return "".join(f"{t:.3f} " + ", ".join(times[t]) + ";\n" for t in sorted(times))


def _e(text: Optional[str]) -> str:
    return _html.escape(text or "")


def _stat_html(st: CompStat, cls: str) -> str:
    unit = f"<u>{_e(st.unit)}</u>" if st.unit else ""
    value = f"{unit}{_e(st.value)}" if st.unit_before else f"{_e(st.value)}{unit}"
    tone = "" if st.measured else " est"
    return (f'<div class="{cls}"><span class="v{tone}">{value}</span>'
            f'<span class="l">{_e(st.label)}</span></div>')


def _band(cls: str, top: int, inner: str, height: Optional[int] = None) -> str:
    h = f"height:{height}px;" if height is not None else ""
    return f'<div class="{cls}" style="top:{top}px;{h}">{inner}</div>'


def render_compilation_body(part: CompPart, spec: CompilationSpec) -> str:
    """Markup for templates/bench/compilation.html ``{{BODY}}`` — every block
    placed by number: the header band above the art, all else below it."""
    hd = part.header
    out: List[str] = [_band(
        "band-top", 0,
        f'<div class="hw" data-fit="hw">{_e(hd.hardware)}</div>'
        f'<div class="mdl" data-fit="model">{_e(hd.model)}</div>'
        f'<div class="meta">{_e(hd.meta)}</div><i class="rule"></i>',
        COMP_HEADER_H,
    )]
    has_art = part.art is not None
    if has_art:
        x, y, w, h = COMP_ART
        out.append(f'<div class="art" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"></div>')
    lower = COMP_LOWER_TOP if has_art else COMP_HEADER_H + 40
    badge = f'<span class="badge">{_e(part.badge)}</span>' if part.badge else ""
    if part.kind != "closing" and (part.caption or badge):
        out.append(_band("band-cap", lower, f'<span class="cap">{_e(part.caption)}</span>{badge}'))
    if part.hero is not None:
        sub = f'<div class="sub">{_e(part.hero_sub)}</div>' if part.hero_sub else ""
        out.append(_band("band-hero", lower + 52, _stat_html(part.hero, "hero") + sub))
    if part.stats:
        cells = "".join(_stat_html(st, "st") for st in part.stats)
        out.append(_band("band-stats", lower + 60, f'<div class="stats n{len(part.stats)}">{cells}</div>'))
    if part.bars:
        rows = []
        for (bx, by, bw, bh), bar in zip(comp_bar_rects(len(part.bars)), part.bars):
            row_top = by - (COMP_BAR_ROW - COMP_BAR_H) // 2
            hi = " hi" if bar.highlight else ""
            rows.append(
                f'<div class="brow{hi}" style="top:{row_top}px">'
                f'<span class="bl">{_e(bar.label)}</span><span class="bv">{_e(bar.value)}</span></div>'
                f'<div class="track" style="left:{bx}px;top:{by}px;width:{bw}px;height:{bh}px"></div>'
            )
        out.append(_band("band-bars", COMP_BAR_TOP, "", len(part.bars) * COMP_BAR_ROW))
        out.extend(rows)
    if part.table is not None:
        nc = part.table.name_col

        def cell(tag: str, j: int, text: str) -> str:
            cls = "nm" if j == nc else ("rk" if j < nc else "n")
            return f'<{tag} class="{cls}">{_e(text)}</{tag}>'

        head = "".join(cell("th", j, col) for j, col in enumerate(part.table.columns))
        body = "".join(
            f'<tr class="{"hi" if r.highlight else ""}">' + "".join(cell("td", j, c) for j, c in enumerate(r.cells))
            + "</tr>"
            for r in part.table.rows
        )
        out.append(_band("band-table", lower + 58,
                         f'<table class="tbl cols{len(part.table.columns)}"><tr>{head}</tr>{body}</table>'))
    if part.kind == "closing":
        thumbs = comp_thumb_rects(len(part.thumbs))
        for x, y, w, h in thumbs:
            out.append(f'<div class="thumb" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"></div>')
        body_top = (thumbs[0][1] + thumbs[0][3] + 36) if thumbs else lower
        inner = ""
        if part.pick:
            reason = f'<div class="why">{_e(part.pick_reason)}</div>' if part.pick_reason else ""
            inner += f'<div class="pick"><div class="pk">{_e(part.pick)}</div>{reason}</div>'
        if part.caption:
            inner += f'<div class="cap dl">{_e(part.caption)}</div>'
        if part.details:
            inner += '<ul class="det">' + "".join(f"<li>{_e(d)}</li>" for d in part.details) + "</ul>"
        out.append(_band("band-body", body_top, inner))
    if part.footnote:
        note_top = (COMP_BAR_TOP + len(part.bars) * COMP_BAR_ROW + 12) if part.bars else COMP_NOTE_TOP
        out.append(_band("band-note", note_top, f"<span>{_e(part.footnote)}</span>"))
    foot = f'<span class="fl">{_e(spec.footer)}</span>' if spec.footer else "<span></span>"
    brand = f'<span class="brand"><i></i>{_e(spec.handle)}</span>' if spec.handle else ""
    out.append(_band("band-foot", COMP_SIZE[1] - 58, foot + brand))
    return "".join(out)


def render_compilation_html(template_text: str, part: CompPart, spec: CompilationSpec) -> str:
    out = template_text.replace("{{KIND}}", part.kind)
    out = out.replace("{{WIDTH}}", str(COMP_SIZE[0])).replace("{{HEIGHT}}", str(COMP_SIZE[1]))
    out = out.replace("{{FLOOR}}", str(COMP_FLOOR_PX))
    return out.replace("{{BODY}}", render_compilation_body(part, spec))


def build_compilation_cmd(
    spec: CompilationSpec,
    bg_pngs: List[str],
    cmd_files: List[Optional[str]],
    output_path: str,
    *,
    fps: int = COMP_FPS,
) -> List[str]:
    """One encode for the whole compilation: per part the background PNG,
    the art scaled INTO COMP_ART (letterboxed, never cropped; a short
    recording loops), the racing bars grown by sendcmd; then all parts
    concatenated, X-ready (H.264 High, yuv420p, 30 fps, silent AAC,
    faststart)."""
    if len(bg_pngs) != len(spec.parts) or len(cmd_files) != len(spec.parts):
        raise ValueError("one background and one command slot per part")
    w, h = COMP_SIZE
    ax, ay, aw, ah = COMP_ART
    cmd: List[str] = [FFMPEG_BIN, "-y"]
    chains: List[str] = []
    idx = 0
    for i, (part, bg, cf) in enumerate(zip(spec.parts, bg_pngs, cmd_files)):
        dur = f"{part.duration_s:.3f}"
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", dur, "-i", bg]
        chains.append(f"[{idx}:v]fps={fps},scale={w}:{h},setsar=1,format=yuv420p[b{i}]")
        idx += 1
        last = f"b{i}"
        if part.art is not None:
            if part.art.kind == "video":
                cmd += ["-stream_loop", "-1", "-ss", str(part.art.start_s), "-t", dur, "-i", part.art.path]
            else:
                cmd += ["-loop", "1", "-framerate", str(fps), "-t", dur, "-i", part.art.path]
            chains.append(
                f"[{idx}:v]fps={fps},scale={aw}:{ah}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={aw}:{ah}:(ow-iw)/2:(oh-ih)/2:color={COMP_BG},setsar=1,format=yuv420p[a{i}]"
            )
            idx += 1
            chains.append(f"[{last}][a{i}]overlay={ax}:{ay}:shortest=1[o{i}]")
            last = f"o{i}"
        for j, ((tx, ty, tw, th), path) in enumerate(zip(comp_thumb_rects(len(part.thumbs)), part.thumbs)):
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", dur, "-i", path]
            chains.append(
                f"[{idx}:v]fps={fps},scale={tw}:{th}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color={COMP_BG},setsar=1,format=yuv420p[t{i}_{j}]"
            )
            idx += 1
            chains.append(f"[{last}][t{i}_{j}]overlay={tx}:{ty}:shortest=1[u{i}_{j}]")
            last = f"u{i}_{j}"
        if part.bars:
            if not cf:
                raise ValueError(f"part {i} has bars but no command file")
            boxes = []
            for j, (bar, (bx, by, _bw, bh)) in enumerate(zip(part.bars, comp_bar_rects(len(part.bars)))):
                colour = COMP_ACCENT if bar.highlight else COMP_BAR_MUTED
                start = COMP_BAR_DELAY_S + j * COMP_BAR_STAGGER_S
                boxes.append(f"drawbox@p{i}b{j}=x={bx}:y={by}:w=2:h={bh}:color={colour}:t=fill"
                             f":enable='gte(t,{start:.3f})'")
            chains.append(f"[{last}]sendcmd=f={cf}," + ",".join(boxes) + f"[d{i}]")
            last = f"d{i}"
        chains.append(f"[{last}]trim=duration={dur},setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
    n = len(spec.parts)
    chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[outv]")
    total = f"{compilation_duration_s(spec):.3f}"
    cmd += ["-f", "lavfi", "-t", total, "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", "[outv]", "-map", f"{idx}:a",
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-b:v", COMP_VIDEO_BITRATE, "-maxrate", "10M", "-bufsize", "16M", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        "-movflags", "+faststart",
    ]
    if spec.checksum:
        cmd += ["-metadata", f"comment=bench-compilation:{spec.checksum}"]
    cmd.append(output_path)
    return cmd
