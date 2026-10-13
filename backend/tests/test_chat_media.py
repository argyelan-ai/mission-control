"""Chat package P2 — pictures from agent tools in the chat.

Real-shaped inputs:

* Claude Code: a ``tool_result`` user line exactly as Claude Code 2.1.273
  writes it for a screenshot (``content[] = {"type":"image","source":
  {"type":"base64","media_type":"image/png","data":...}}`` plus the
  duplicate ``toolUseResult.file.base64`` the parser must ignore) — the
  picture itself is generated here, the line shape is the live one.
* omp over ACP: the toolResult line the bridge writes from the REAL omp
  18.10.1 recording (docker/omp-bridge/rpc/acp-tool-image-18.10.1.ndjson):
  ``details.images = [{sha256, mimeType, bytes}]`` + the WebP file under
  ``<session dir>/media/``.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image

from app.services import chat_media

AGENT = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
OTHER = str(uuid.UUID("99999999-2222-3333-4444-555555555555"))


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "chat-media"
    monkeypatch.setattr(chat_media.settings, "chat_media_root", root)
    return root


def _png(w: int = 64, h: int = 40, colour=(40, 40, 40), text: dict | None = None) -> bytes:
    from PIL import PngImagePlugin

    im = Image.new("RGB", (w, h), colour)
    out = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    for k, v in (text or {}).items():
        info.add_text(k, v)
    im.save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def _jpeg_with_exif() -> bytes:
    im = Image.new("RGB", (80, 60), (200, 10, 10))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"          # Make
    exif[0x013B] = "Jane Example"       # Artist
    exif[0x0112] = 1                    # Orientation
    out = io.BytesIO()
    im.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def _webp() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (160, 100), (24, 24, 24)).save(out, format="WEBP")
    return out.getvalue()


# ── the store ───────────────────────────────────────────────────────────────


def test_ingest_stores_a_png_and_returns_a_reference(media_root):
    raw = _png()
    ref = chat_media.ingest_bytes(AGENT, raw)
    sha = hashlib.sha256(raw).hexdigest()
    assert ref == {"id": sha, "mime": "image/png", "width": 64, "height": 40, "bytes": len(raw)}
    assert (media_root / AGENT / f"{sha}.src").read_bytes() == raw
    # Idempotent: the same bytes are one entry.
    assert chat_media.ingest_bytes(AGENT, raw) == ref
    assert len(list((media_root / AGENT).glob("*.src"))) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>",
        b"GIF89a but not a gif at all",
        b"\x89PNG\r\n\x1a\n truncated",
        b"",
    ],
)
def test_ingest_refuses_anything_that_is_not_a_real_raster(raw):
    assert chat_media.ingest_bytes(AGENT, raw) is None


def test_ingest_caps_bytes_and_pixels(monkeypatch):
    monkeypatch.setattr(chat_media, "MAX_BYTES", 50)
    assert chat_media.ingest_bytes(AGENT, _png()) is None
    monkeypatch.setattr(chat_media, "MAX_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(chat_media, "MAX_PIXELS", 100)
    assert chat_media.ingest_bytes(AGENT, _png(20, 20)) is None


def test_ingest_needs_a_uuid_owner(media_root):
    assert chat_media.ingest_bytes("../etc", _png()) is None
    assert not media_root.exists() or list(media_root.iterdir()) == []


def test_full_variant_drops_exif_and_text_chunks():
    jpeg = _jpeg_with_exif()
    assert b"SecretCam" in jpeg  # sabotage guard: the input really has it
    ref = chat_media.ingest_bytes(AGENT, jpeg)
    path, mime = chat_media.open_variant(AGENT, ref["id"], "full")
    data = path.read_bytes()
    assert mime == "image/jpeg"
    assert b"SecretCam" not in data and b"Jane Example" not in data and b"Exif" not in data
    with Image.open(path) as im:
        assert dict(im.getexif()) == {}

    png = _png(text={"Author": "Jane Example", "Comment": "gps 47.1,8.5"})
    assert b"Jane Example" in png
    ref = chat_media.ingest_bytes(AGENT, png)
    path, mime = chat_media.open_variant(AGENT, ref["id"], "full")
    assert mime == "image/png"
    assert b"Jane Example" not in path.read_bytes() and b"gps" not in path.read_bytes()


def test_thumb_is_a_small_webp():
    ref = chat_media.ingest_bytes(AGENT, _png(1600, 1000))
    path, mime = chat_media.open_variant(AGENT, ref["id"], "thumb")
    assert mime == "image/webp"
    with Image.open(path) as im:
        assert im.format == "WEBP"
        assert max(im.size) == chat_media.THUMB_EDGE
        assert im.size[0] / im.size[1] == pytest.approx(1.6, rel=0.02)


def test_animated_gif_stays_animated_and_its_comment_goes():
    frames = [Image.new("RGB", (20, 20), (i * 80, 0, 0)) for i in range(3)]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                   duration=80, loop=0, comment=b"made by Jane Example")
    raw = out.getvalue()
    assert b"Jane Example" in raw
    ref = chat_media.ingest_bytes(AGENT, raw)
    path, mime = chat_media.open_variant(AGENT, ref["id"], "full")
    assert mime == "image/gif"
    assert b"Jane Example" not in path.read_bytes()
    with Image.open(path) as im:
        assert im.n_frames == 3


@pytest.mark.parametrize("media_id", ["../../x", "A" * 64, "a" * 63, "", "a" * 64 + "/.."])
def test_open_variant_refuses_bad_ids(media_id):
    chat_media.ingest_bytes(AGENT, _png())
    assert chat_media.open_variant(AGENT, media_id, "full") is None


def test_open_variant_is_scoped_to_its_agent():
    ref = chat_media.ingest_bytes(AGENT, _png())
    assert chat_media.open_variant(OTHER, ref["id"], "full") is None
    assert chat_media.open_variant(AGENT, ref["id"], "original") is None


def test_prune_keeps_the_newest(monkeypatch, media_root):
    import os
    import time

    monkeypatch.setattr(chat_media, "KEEP_PER_AGENT", 2)
    ids = []
    for i in range(4):
        ref = chat_media.ingest_bytes(AGENT, _png(colour=(i, i, i)))
        ids.append(ref["id"])
        for p in (media_root / AGENT).glob(f"{ref['id']}.*"):
            os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
    chat_media.ingest_bytes(AGENT, _png(colour=(9, 9, 9)))
    left = {p.stem for p in (media_root / AGENT).glob("*.json")}
    assert len(left) == 2 and ids[0] not in left and ids[1] not in left
    assert not list((media_root / AGENT).glob(f"{ids[0]}.*"))


def test_delete_for_agent_removes_only_that_agent(media_root):
    chat_media.ingest_bytes(AGENT, _png())
    chat_media.ingest_bytes(OTHER, _png())
    assert chat_media.delete_for_agent(AGENT) is True
    assert not (media_root / AGENT).exists()
    assert (media_root / OTHER).is_dir()
    assert chat_media.delete_for_agent("not-a-uuid") is False


# ── omp bridge files ────────────────────────────────────────────────────────


def _bridge_session(tmp_path: Path, raw: bytes, ext: str = "webp") -> tuple[Path, str]:
    sdir = tmp_path / "sessions" / "--workspace--"
    (sdir / "media").mkdir(parents=True)
    sha = hashlib.sha256(raw).hexdigest()
    (sdir / "media" / f"{sha}.{ext}").write_bytes(raw)
    return sdir, sha


def test_bridge_file_is_copied_into_the_store(tmp_path, media_root):
    raw = _webp()
    sdir, sha = _bridge_session(tmp_path, raw)
    ctx = chat_media.MediaContext(AGENT, sdir)
    refs = ctx.from_refs([{"sha256": sha, "mimeType": "image/webp", "bytes": len(raw)}])
    assert [r["id"] for r in refs] == [sha]
    assert refs[0]["width"] == 160
    assert (media_root / AGENT / f"{sha}.src").read_bytes() == raw


def test_bridge_file_must_hash_to_its_name(tmp_path):
    raw = _webp()
    sdir, sha = _bridge_session(tmp_path, raw)
    (sdir / "media" / f"{sha}.webp").write_bytes(_png())  # swapped content
    ctx = chat_media.MediaContext(AGENT, sdir)
    assert ctx.from_refs([{"sha256": sha, "mimeType": "image/webp"}]) == []


def test_bridge_symlink_is_refused(tmp_path):
    secret = tmp_path / "secret.webp"
    raw = _webp()
    secret.write_bytes(raw)
    sdir = tmp_path / "sessions" / "--workspace--"
    (sdir / "media").mkdir(parents=True)
    sha = hashlib.sha256(raw).hexdigest()
    (sdir / "media" / f"{sha}.webp").symlink_to(secret)
    ctx = chat_media.MediaContext(AGENT, sdir)
    assert ctx.from_refs([{"sha256": sha, "mimeType": "image/webp"}]) == []


def test_bridge_refs_with_bad_shapes_are_skipped(tmp_path):
    ctx = chat_media.MediaContext(AGENT, tmp_path)
    assert ctx.from_refs([{"sha256": "../../x", "mimeType": "image/png"},
                          {"sha256": "a" * 64, "mimeType": "image/svg+xml"},
                          "junk"]) == []


# ── parsers + history ───────────────────────────────────────────────────────


def _claude_lines(raw: bytes) -> list[str]:
    """The tool_use + tool_result pair as Claude Code 2.1.273 writes a
    screenshot (shape copied from a live session, picture generated)."""
    b64 = base64.b64encode(raw).decode()
    tool_use = {
        "parentUuid": None, "isSidechain": False, "type": "assistant",
        "uuid": "a-1", "timestamp": "2026-10-13T08:00:00.000Z",
        "message": {"model": "claude-x", "role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_01SHOT", "name": "mcp__playwright__browser_take_screenshot",
             "input": {"type": "png"}},
        ]},
    }
    tool_result = {
        "parentUuid": "a-1", "isSidechain": False, "type": "user",
        "message": {"role": "user", "content": [{
            "tool_use_id": "toolu_01SHOT", "type": "tool_result",
            "content": [
                {"type": "text", "text": "Took the viewport screenshot"},
                {"type": "image", "source": {"type": "base64", "data": b64, "media_type": "image/png"}},
            ],
        }]},
        "uuid": "u-2", "timestamp": "2026-10-13T08:00:01.000Z",
        "toolUseResult": {"type": "image", "file": {"base64": b64, "type": "image/png",
                                                    "originalSize": len(raw)}},
        "userType": "external", "entrypoint": "cli", "version": "2.1.273",
    }
    return [json.dumps(tool_use), json.dumps(tool_result)]


def _omp_lines(sha: str, nbytes: int) -> list[str]:
    """What the bridge writes for the recorded omp 18.10.1 `read` of a PNG."""
    seed = {"type": "message", "id": "acp00001", "parentId": None,
            "timestamp": "2026-10-13T08:00:00.000Z",
            "message": {"role": "assistant", "model": None, "content": [
                {"type": "toolCall", "id": "call_read_img_1", "name": "read",
                 "arguments": {"path": "/tmp/ws/shot.png", "i": "read: /tmp/ws/shot.png"},
                 "locations": ["/tmp/ws/shot.png"]}]}}
    result = {"type": "message", "id": "acp00002", "parentId": None,
              "timestamp": "2026-10-13T08:00:01.000Z",
              "message": {"role": "toolResult", "toolCallId": "call_read_img_1", "toolName": "read",
                          "content": [{"type": "text", "text": "Read image file [image/webp]"}],
                          "details": {"title": "read: /tmp/ws/shot.png", "status": "completed",
                                      "locations": ["/tmp/ws/shot.png"],
                                      "images": [{"sha256": sha, "mimeType": "image/webp",
                                                  "bytes": nbytes}]},
                          "isError": False}}
    header = {"type": "session", "version": 3, "id": "s-1", "timestamp": "2026-10-13T08:00:00Z",
              "cwd": "/workspace", "bridge": "acp"}
    return [json.dumps(header), json.dumps(seed), json.dumps(result)]


def _history(path: Path, adapter, media):
    from app.services.transcript_chat import read_history

    return read_history(path, adapter, media=media)


def test_claude_history_shows_a_reference_never_base64(tmp_path, media_root):
    from app.services.transcript_adapters import adapter_for

    raw = _png()
    path = tmp_path / "sess.jsonl"
    path.write_text("\n".join(_claude_lines(raw)) + "\n")
    ctx = chat_media.MediaContext(AGENT, tmp_path)
    events = _history(path, adapter_for(None), ctx)["events"]
    tool = next(e for e in events if e["kind"] == "tool")
    sha = hashlib.sha256(raw).hexdigest()
    assert [i["id"] for i in tool["images"]] == [sha]
    assert tool["result"] == "Took the viewport screenshot"
    dumped = json.dumps(events)
    assert base64.b64encode(raw).decode()[:32] not in dumped
    assert '"images"' in dumped and '"source"' not in dumped


def test_claude_history_without_media_shows_no_picture_and_no_base64(tmp_path):
    from app.services.transcript_adapters import adapter_for

    raw = _png()
    path = tmp_path / "sess.jsonl"
    path.write_text("\n".join(_claude_lines(raw)) + "\n")
    events = _history(path, adapter_for(None), None)["events"]
    tool = next(e for e in events if e["kind"] == "tool")
    assert "images" not in tool
    assert base64.b64encode(raw).decode()[:32] not in json.dumps(events)


def test_omp_history_turns_bridge_references_into_images(tmp_path, media_root):
    from types import SimpleNamespace

    from app.services.transcript_adapters import adapter_for

    raw = _webp()
    sdir, sha = _bridge_session(tmp_path, raw)
    path = sdir / "2026-10-13T08-00-00_s-1.jsonl"
    path.write_text("\n".join(_omp_lines(sha, len(raw))) + "\n")
    adapter = adapter_for(SimpleNamespace(harness="omp", agent_runtime="cli-bridge", slug="x", id=AGENT))
    ctx = chat_media.MediaContext(AGENT, sdir)
    events = _history(path, adapter, ctx)["events"]
    tool = next(e for e in events if e["kind"] == "tool")
    assert tool["images"] == [{"id": sha, "mime": "image/webp", "width": 160, "height": 100,
                               "bytes": len(raw)}]
    assert tool["result"] == "Read image file [image/webp]"
    assert "image_refs" not in tool


def test_omp_history_with_a_missing_file_shows_no_picture(tmp_path):
    from types import SimpleNamespace

    from app.services.transcript_adapters import adapter_for

    sdir = tmp_path / "sessions" / "--workspace--"
    sdir.mkdir(parents=True)
    path = sdir / "2026-10-13T08-00-00_s-1.jsonl"
    path.write_text("\n".join(_omp_lines("b" * 64, 10)) + "\n")
    adapter = adapter_for(SimpleNamespace(harness="omp", agent_runtime="cli-bridge", slug="x", id=AGENT))
    events = _history(path, adapter, chat_media.MediaContext(AGENT, sdir))["events"]
    tool = next(e for e in events if e["kind"] == "tool")
    assert "images" not in tool


def test_context_for_needs_an_agent_row(tmp_path):
    from types import SimpleNamespace

    assert chat_media.context_for(None, tmp_path / "s.jsonl") is None
    ctx = chat_media.context_for(SimpleNamespace(id=uuid.UUID(AGENT)), tmp_path / "s.jsonl")
    assert ctx == chat_media.MediaContext(AGENT, tmp_path)


# ── the route ───────────────────────────────────────────────────────────────


async def test_route_serves_thumb_and_clean_full_with_safe_headers(auth_client, make_agent):
    agent = await make_agent(name="ShotAgent")
    ref = chat_media.ingest_bytes(str(agent.id), _jpeg_with_exif())

    res = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/media/{ref['id']}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/webp"  # default = thumb
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in res.headers["content-security-policy"]

    res = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/media/{ref['id']}?variant=full")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert b"SecretCam" not in res.content
    assert "inline" in res.headers["content-disposition"]
    assert str(chat_media.media_root()) not in json.dumps(dict(res.headers))


async def test_route_needs_the_chat_auth(client, make_agent):
    agent = await make_agent(name="ShotAgent2")
    ref = chat_media.ingest_bytes(str(agent.id), _png())
    res = await client.get(f"/api/v1/agents/{agent.id}/chat/media/{ref['id']}")
    assert res.status_code == 401


async def test_route_answers_404_for_another_agents_picture(auth_client, make_agent):
    owner = await make_agent(name="Owner")
    other = await make_agent(name="Stranger")
    ref = chat_media.ingest_bytes(str(owner.id), _png())
    res = await auth_client.get(f"/api/v1/agents/{other.id}/chat/media/{ref['id']}")
    assert res.status_code == 404
    assert "chat-media" not in res.text


async def test_route_rejects_bad_ids_and_variants(auth_client, make_agent):
    agent = await make_agent(name="ShotAgent3")
    ref = chat_media.ingest_bytes(str(agent.id), _png())
    for bad in ("..%2F..%2Fetc", "A" * 64, "x"):
        res = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/media/{bad}")
        assert res.status_code == 404
    res = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/media/{ref['id']}?variant=src")
    assert res.status_code == 422


# ── the live tailer ─────────────────────────────────────────────────────────


async def test_tailer_publishes_the_reference_not_the_bytes(tmp_path, monkeypatch, media_root):
    import asyncio

    import app.services.transcript_chat as transcript_chat
    from app.services.transcript_chat import ChatTailerManager

    published: list[dict] = []

    async def _fake(channel, event_type, data):
        published.append(data)

    monkeypatch.setattr(transcript_chat.sse, "broadcast", _fake)
    # The raw tailer tests run without an agent row; hand it a context.
    monkeypatch.setattr(
        transcript_chat.chat_media, "context_for",
        lambda agent, path: chat_media.MediaContext(AGENT, path.parent),
    )
    manager = ChatTailerManager()
    manager.POLL_INTERVAL = 0.02
    manager.STATE_PROBE_INTERVAL_SECONDS = 0.02

    raw = _png()
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")
    await manager.acquire("agent-1", session_file)
    try:
        with session_file.open("a") as f:
            f.write("\n".join(_claude_lines(raw)) + "\n")
        for _ in range(150):
            if any(d.get("kind") == "tool" and d.get("result") is not None for d in published):
                break
            await asyncio.sleep(0.02)
    finally:
        await manager.release("agent-1")

    merged = [d for d in published if d.get("kind") == "tool" and d.get("result") is not None]
    assert merged, published
    assert merged[-1]["images"][0]["id"] == hashlib.sha256(raw).hexdigest()
    assert base64.b64encode(raw).decode()[:32] not in json.dumps(published)
