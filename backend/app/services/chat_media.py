"""Pictures from agent tools in the chat (chat package P2, ADR-090 §1).

A browser screenshot, an image the agent read, an MCP tool's picture: the
harness hands it back inside the tool result. The chat shows it on the tool
card as a thumbnail and opens it full screen on tap. The bytes never travel
in the transcript events — an event carries only a reference
(``{"id", "mime", "width", "height", "bytes"}``) and the frontend fetches the
picture from ``GET /agents/{id}/chat/media/{media_id}`` with the chat's own
Bearer auth.

Where the bytes come from (two harnesses, one store):

* **Claude Code** writes the picture as base64 into its JSONL
  (``tool_result.content[] = {"type":"image","source":{"type":"base64",
  "media_type":..., "data":...}}``). The parser hands the blocks through;
  :meth:`MediaContext.from_claude_blocks` decodes and stores them.
* **omp over ACP**: the bridge writes the picture as a file next to the
  transcript (``<session dir>/media/<sha256>.<ext>``) and puts only
  ``details.images = [{sha256, mimeType, bytes}]`` on the toolResult line
  (``docker/omp-bridge/acp_chat_events.py``). :meth:`MediaContext.from_refs`
  copies that file into the store — after checking that its bytes really
  hash to the name.

Why a store of its own and not the reference files (``reference_files``,
the chat attachments of #330/#331): a reference is a DB row the operator
UPLOADED for the agent to read — it shows up in the agent's reference list
and is written from an async route with a DB session. A tool picture is the
agent's OUTPUT, discovered while a transcript is parsed (sync code, no DB
session, sometimes a background thread), and there can be hundreds per
session. Same shape as the browser sessions' last image (ADR-088:
``<root>/<id>/last.jpg`` served by an authenticated route): one folder per
owner under ``~/.mc``, deleted with the owner.

Layout: ``<chat_media_root>/<agent uuid>/<sha256>.src`` (bytes as received,
validated), ``<sha256>.json`` (the reference), and — made on first request —
``<sha256>.full.<ext>`` (re-encoded: EXIF, GPS, text chunks and colour
profile dropped) and ``<sha256>.thumb.webp``. The original is never served.

Hard rules: only PNG / JPEG / WebP / GIF, recognised by their magic bytes
(an SVG can carry script); at most :data:`MAX_BYTES` and
:data:`MAX_PIXELS`; ids are 64 lowercase hex chars and every path is built
from them, never from input; the newest :data:`KEEP_PER_AGENT` pictures per
agent survive.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

logger = logging.getLogger("mc.chat_media")

MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
#: Longest edge of a thumbnail. A card shows it at most ~160 css px tall on
#: a 3x phone; 480 keeps it sharp and a few dozen KB.
THUMB_EDGE = 480
KEEP_PER_AGENT = 500
#: Pictures per tool call that get a reference (a screenshot loop must not
#: turn one card into a gallery of fifty).
MAX_PER_TOOL = 8

_ID_RE = re.compile(r"^[a-f0-9]{64}$")
VARIANTS = ("thumb", "full")


def sniff_mime(data: bytes) -> str | None:
    """The image type the BYTES say, or None for anything off the list."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def is_media_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def media_root() -> Path:
    return Path(settings.chat_media_root)


def _agent_dir(agent_key: str) -> Path | None:
    """``<root>/<agent uuid>`` — None for anything that is not a UUID."""
    try:
        key = str(uuid.UUID(str(agent_key)))
    except (ValueError, TypeError, AttributeError):
        return None
    return media_root() / key


def _write_atomic(target: Path, data: bytes) -> None:
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, target)


def _read_meta(adir: Path, media_id: str) -> dict[str, Any] | None:
    try:
        meta = json.loads((adir / f"{media_id}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("id") != media_id or meta.get("mime") not in MIME_EXT:
        return None
    return meta


def ingest_bytes(agent_key: str, raw: bytes) -> dict[str, Any] | None:
    """Store one picture for ``agent_key``; its reference, or None when it
    is not an acceptable picture or the disk refuses. Idempotent: the same
    bytes are one entry. Never raises."""
    try:
        return _ingest(agent_key, raw)
    except Exception:  # noqa: BLE001 — a picture never breaks a chat read
        logger.debug("chat_media: ingest failed", exc_info=True)
        return None


def _ingest(agent_key: str, raw: bytes) -> dict[str, Any] | None:
    adir = _agent_dir(agent_key)
    if adir is None or not raw or len(raw) > MAX_BYTES:
        return None
    mime = sniff_mime(raw)
    if mime is None:
        return None
    media_id = hashlib.sha256(raw).hexdigest()
    known = _read_meta(adir, media_id)
    if known is not None and (adir / f"{media_id}.src").is_file():
        return known

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as im:
            width, height = im.size
            if im.format not in ("PNG", "JPEG", "WEBP", "GIF"):
                return None
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        return None

    adir.mkdir(parents=True, exist_ok=True)
    meta = {"id": media_id, "mime": mime, "width": width, "height": height, "bytes": len(raw)}
    _write_atomic(adir / f"{media_id}.src", raw)
    _write_atomic(adir / f"{media_id}.json", json.dumps(meta).encode())
    _prune(adir)
    return meta


def _prune(adir: Path) -> None:
    """Keep the newest :data:`KEEP_PER_AGENT` pictures of one agent."""
    try:
        metas = sorted(
            (p for p in adir.glob("*.json") if _ID_RE.match(p.stem)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in metas[KEEP_PER_AGENT:]:
        for path in adir.glob(f"{stale.stem}.*"):
            try:
                path.unlink()
            except OSError:
                pass


def open_variant(agent_key: str, media_id: str, variant: str) -> tuple[Path, str] | None:
    """The file to send for ``variant`` (``thumb`` or ``full``) and its
    mime type; None when the id, the agent or the picture is unknown.
    The cleaned files are made on first request and kept. Sync (PIL work):
    the route calls this in a thread."""
    if variant not in VARIANTS or not is_media_id(media_id):
        return None
    adir = _agent_dir(agent_key)
    if adir is None:
        return None
    meta = _read_meta(adir, media_id)
    src = adir / f"{media_id}.src"
    if meta is None or not src.is_file() or src.is_symlink():
        return None
    try:
        if variant == "thumb":
            target = adir / f"{media_id}.thumb.webp"
            if not target.is_file():
                _write_atomic(target, _render_thumb(src))
            return target, "image/webp"
        ext = MIME_EXT[meta["mime"]]
        target = adir / f"{media_id}.full.{ext}"
        if not target.is_file():
            _write_atomic(target, _render_clean(src, meta["mime"]))
        return target, meta["mime"]
    except Exception:  # noqa: BLE001 — a broken picture is "unavailable"
        logger.debug("chat_media: render %s/%s failed", media_id, variant, exc_info=True)
        return None


def _open_checked(src: Path):
    from PIL import Image

    im = Image.open(src)
    if im.size[0] * im.size[1] > MAX_PIXELS:
        im.close()
        raise ValueError("too many pixels")
    return im


def _render_clean(src: Path, mime: str) -> bytes:
    """Re-encode without any metadata: EXIF (camera, GPS, owner), PNG text
    chunks, XMP and the colour profile stay behind. JPEG orientation is
    applied first so dropping EXIF does not turn a photo on its side."""
    from PIL import ImageOps

    out = io.BytesIO()
    with _open_checked(src) as im:
        if mime == "image/gif":
            frames = getattr(im, "n_frames", 1)
            im.info.pop("comment", None)
            if frames > 1:
                im.save(out, format="GIF", save_all=True, loop=im.info.get("loop", 0),
                        duration=im.info.get("duration", 100), disposal=2)
            else:
                im.save(out, format="GIF")
            return out.getvalue()
        clean = ImageOps.exif_transpose(im)
        if mime == "image/jpeg":
            clean = clean.convert("RGB")
            clean.save(out, format="JPEG", quality=92, optimize=True)
        elif mime == "image/webp":
            clean.save(out, format="WEBP", quality=90, method=4)
        else:
            if clean.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                clean = clean.convert("RGBA")
            clean.save(out, format="PNG", optimize=False)
    return out.getvalue()


def _render_thumb(src: Path) -> bytes:
    from PIL import ImageOps

    out = io.BytesIO()
    with _open_checked(src) as im:
        im.seek(0)
        frame = ImageOps.exif_transpose(im)
        frame = frame.convert("RGBA" if frame.mode in ("RGBA", "LA", "P") else "RGB")
        frame.thumbnail((THUMB_EDGE, THUMB_EDGE))
        frame.save(out, format="WEBP", quality=80, method=4)
    return out.getvalue()


def delete_for_agent(agent_key: str) -> bool:
    """Remove every picture of one agent (agent delete). Never raises."""
    adir = _agent_dir(agent_key)
    if adir is None or not adir.is_dir() or adir.is_symlink():
        return False
    try:
        root = media_root().resolve()
        if not adir.resolve().is_relative_to(root):
            return False
        shutil.rmtree(adir)
        return True
    except OSError as exc:
        logger.warning("chat_media: could not delete pictures of %s: %s", agent_key, exc)
        return False


@dataclass(frozen=True)
class MediaContext:
    """Where one chat read stores its pictures: the agent they belong to and
    the transcript folder an omp bridge wrote its ``media/`` files into."""

    agent_key: str
    session_dir: Path | None = None

    def from_claude_blocks(self, blocks: Iterable[Any]) -> list[dict[str, Any]]:
        """Claude Code ``{"type":"image","source":{"type":"base64",...}}``
        blocks -> references. Anything else is skipped."""
        refs: list[dict[str, Any]] = []
        for block in blocks:
            if len(refs) >= MAX_PER_TOOL:
                break
            source = block.get("source") if isinstance(block, dict) else None
            if not isinstance(source, dict) or source.get("type") != "base64":
                continue
            data = source.get("data")
            if not isinstance(data, str) or len(data) > (MAX_BYTES * 4) // 3 + 8:
                continue
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                continue
            ref = ingest_bytes(self.agent_key, raw)
            if ref is not None and all(r["id"] != ref["id"] for r in refs):
                refs.append(ref)
        return refs

    def from_refs(self, items: Iterable[Any]) -> list[dict[str, Any]]:
        """omp bridge ``details.images`` -> references, reading each file
        from ``<session dir>/media/`` (no symlinks, size-capped, and the
        bytes must hash to the name)."""
        refs: list[dict[str, Any]] = []
        if self.session_dir is None:
            return refs
        mdir = self.session_dir / "media"
        for item in items:
            if len(refs) >= MAX_PER_TOOL:
                break
            if not isinstance(item, dict):
                continue
            sha, mime = item.get("sha256"), item.get("mimeType")
            if not is_media_id(sha) or mime not in MIME_EXT:
                continue
            adir = _agent_dir(self.agent_key)
            known = _read_meta(adir, sha) if adir is not None else None
            if known is not None and (adir / f"{sha}.src").is_file():
                ref = known
            else:
                ref = self._copy_bridge_file(mdir / f"{sha}.{MIME_EXT[mime]}", sha)
            if ref is not None and all(r["id"] != ref["id"] for r in refs):
                refs.append(ref)
        return refs

    def _copy_bridge_file(self, path: Path, sha: str) -> dict[str, Any] | None:
        try:
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                return None
            if path.stat().st_size > MAX_BYTES:
                return None
            raw = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(raw).hexdigest() != sha:
            return None
        return ingest_bytes(self.agent_key, raw)

    def collect(self, tool_result: dict[str, Any]) -> list[dict[str, Any]]:
        """All pictures of one parsed ``_tool_result`` event."""
        refs = self.from_claude_blocks(tool_result.get("images") or [])
        for ref in self.from_refs(tool_result.get("image_refs") or []):
            if all(r["id"] != ref["id"] for r in refs):
                refs.append(ref)
        return refs[:MAX_PER_TOOL]


def context_for(agent: Any, session_path: Path | None) -> MediaContext | None:
    """The media context of one agent's chat read, or None (no agent row ->
    no pictures; fail closed)."""
    agent_id = getattr(agent, "id", None)
    if agent_id is None:
        return None
    return MediaContext(
        agent_key=str(agent_id),
        session_dir=session_path.parent if session_path is not None else None,
    )
