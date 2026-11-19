"""omp 18.10.1 over ACP: images from tool results (chat package P2).

Fixture rpc/acp-tool-image-18.10.1.ndjson is the verbatim wire stream of a
REAL `omp acp` 18.10.1 run (throwaway container without network,
13.10.2026) driven by the bridge's own ACPClient. The model endpoint was a
local OpenAI-compatible stub that answered

  1. a `read` call on a 160x100 PNG in the workspace -> omp converts it to
     WebP and settles the call with ONE `tool_call_update` (status
     `completed`) whose ``content`` holds a text item AND an
     ``{"type":"content","content":{"type":"image","data":<base64>,
     "mimeType":"image/webp"}}`` item; ``rawOutput.content`` carries the
     same image as a bare ``{"type":"image",...}`` block,
  2. text.

omp's browser `screenshot` settles the same way (tab-worker.ts pushes
``{type:"image", data, mimeType}`` into the result content), so one path
serves both.

The contract under test: the image bytes NEVER enter the transcript JSONL —
they land as a file in ``<session dir>/media/<sha256>.<ext>`` and the
toolResult line carries only ``details.images = [{sha256, mimeType,
bytes}]``. No file written -> no reference (fail closed: a dangling
reference would render a broken image).
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat_events  # noqa: E402
from test_acp_adapter import InProcessFake  # noqa: E402

FIXTURE = ROOT / "rpc" / "acp-tool-image-18.10.1.ndjson"

# Smallest valid PNG (1x1) — magic bytes are what the bridge checks.
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _wire() -> list[dict]:
    return [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]


def _run_turn():
    fake = InProcessFake(FIXTURE, [], fake_delay=0.0)
    client = fake.client
    mapper = acp_chat_events.ACPEventMapper()
    mapped: list[dict] = []
    client.on_event(lambda params: mapped.extend(mapper.map_update(params, stream=True)))
    client.on_permission(lambda _req: "allow_once")
    try:
        client.initialize(timeout=10)
        sid = client.new_session(cwd=str(HERE), timeout=10)
        client.set_config_option(sid, "model", "mc-openai/mock-model", timeout=10)
        client.prompt(sid, "look at shot.png", timeout=20)
    finally:
        fake.close()
    return mapper, mapped


def _tool_results(entries: list[dict]) -> list[dict]:
    return [
        e for e in entries
        if e.get("type") == "message" and (e.get("message") or {}).get("role") == "toolResult"
    ]


def _fixture_image_bytes() -> bytes:
    for msg in _wire():
        update = (msg.get("params") or {}).get("update") or {}
        for item in update.get("content") or []:
            inner = item.get("content") if isinstance(item, dict) else None
            if isinstance(inner, dict) and inner.get("type") == "image":
                return base64.b64decode(inner["data"])
    raise AssertionError("fixture has no image")


def _seed(mapper, tool_id: str = "call_img") -> None:
    mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "tool_call", "toolCallId": tool_id, "title": "browser",
        "kind": "other", "status": "pending", "rawInput": {"action": "screenshot"},
    }}, stream=True)


def _settle(mapper, content=None, raw=None, tool_id: str = "call_img", status="completed"):
    update = {"sessionUpdate": "tool_call_update", "toolCallId": tool_id, "status": status}
    if content is not None:
        update["content"] = content
    if raw is not None:
        update["rawOutput"] = raw
    return mapper.map_update({"sessionId": "s", "update": update}, stream=True)


def _img_item(data: bytes, mime: str = "image/png") -> dict:
    return {"type": "content", "content": {"type": "image", "mimeType": mime,
                                           "data": base64.b64encode(data).decode()}}


class Writer:
    def __init__(self, fail: bool = False):
        self.files: dict[str, bytes] = {}
        self.fail = fail

    def __call__(self, name: str, data: bytes) -> bool:
        if self.fail:
            return False
        self.files[name] = data
        return True


# ── the fixture itself ──────────────────────────────────────────────────────


def test_fixture_is_a_real_omp_18_10_1_read_of_an_image():
    wire = _wire()
    assert wire[0]["result"]["agentInfo"]["version"] == "18.10.1"
    raw = _fixture_image_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


# ── mapping ─────────────────────────────────────────────────────────────────


def test_settled_image_becomes_a_reference_and_a_media_payload():
    _mapper, mapped = _run_turn()
    results = _tool_results(mapped)
    assert len(results) == 1
    raw = _fixture_image_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    images = results[0]["message"]["details"]["images"]
    # content AND rawOutput.content both carry the picture — ONE reference.
    assert images == [{"sha256": sha, "mimeType": "image/webp", "bytes": len(raw)}]
    assert results[0]["_media"] == [{"name": f"{sha}.webp", "data": raw}]
    # The text part survives as the card's result text.
    assert "Read image file" in results[0]["message"]["content"][0]["text"]


def test_dump_writes_the_file_and_keeps_base64_out_of_the_line():
    _mapper, mapped = _run_turn()
    writer = Writer()
    lines = acp_chat_events.ACPEventMapper.dump(mapped, media_writer=writer)
    raw = _fixture_image_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    assert writer.files == {f"{sha}.webp": raw}
    joined = "\n".join(lines)
    assert base64.b64encode(raw).decode()[:40] not in joined
    assert "_media" not in joined
    tool_line = next(json.loads(l) for l in lines if '"toolResult"' in l)
    assert tool_line["message"]["details"]["images"][0]["sha256"] == sha
    # The whole line stays small — the point of the exercise.
    assert len(max(lines, key=len)) < 4000


def test_dump_without_a_writer_drops_the_reference():
    _mapper, mapped = _run_turn()
    lines = acp_chat_events.ACPEventMapper.dump(mapped)
    tool_line = next(json.loads(l) for l in lines if '"toolResult"' in l)
    assert "images" not in tool_line["message"]["details"]


def test_a_failed_write_drops_the_reference_not_the_line():
    _mapper, mapped = _run_turn()
    lines = acp_chat_events.ACPEventMapper.dump(mapped, media_writer=Writer(fail=True))
    tool_line = next(json.loads(l) for l in lines if '"toolResult"' in l)
    assert "images" not in tool_line["message"]["details"]
    assert tool_line["message"]["details"]["status"] == "completed"


def test_a_raising_writer_never_kills_the_dump():
    _mapper, mapped = _run_turn()

    def boom(_name, _data):
        raise OSError("disk gone")

    lines = acp_chat_events.ACPEventMapper.dump(mapped, media_writer=boom)
    assert any('"toolResult"' in l for l in lines)


def test_blob_references_and_foreign_types_are_dropped():
    mapper = acp_chat_events.ACPEventMapper()
    _seed(mapper)
    out = _settle(mapper, content=[
        {"type": "content", "content": {"type": "image", "mimeType": "image/png",
                                        "data": "blob:sha256:" + "a" * 64}},
        _img_item(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        _img_item(b"MZ\x90\x00 an exe, labelled png", "image/png"),  # no image magic
        {"type": "content", "content": {"type": "image", "mimeType": "image/png",
                                        "data": "%%% not base64 %%%"}},
    ])
    details = _tool_results(out)[0]["message"]["details"]
    assert "images" not in details
    assert "_media" not in _tool_results(out)[0]


def test_declared_mime_follows_the_bytes_not_the_label():
    """A PNG labelled image/jpeg is still a PNG — the file extension and the
    reference say what the bytes ARE."""
    mapper = acp_chat_events.ACPEventMapper()
    _seed(mapper)
    out = _settle(mapper, content=[_img_item(PNG_1x1, "image/jpeg")])
    entry = _tool_results(out)[0]
    assert entry["message"]["details"]["images"][0]["mimeType"] == "image/png"
    assert entry["_media"][0]["name"].endswith(".png")


def test_raw_output_images_count_when_content_has_none():
    mapper = acp_chat_events.ACPEventMapper()
    _seed(mapper)
    out = _settle(mapper, raw={"content": [
        {"type": "text", "text": "Screenshot captured"},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG_1x1).decode()},
    ]})
    entry = _tool_results(out)[0]
    assert len(entry["message"]["details"]["images"]) == 1
    assert entry["message"]["content"][0]["text"] == "Screenshot captured"


def test_size_and_count_caps(monkeypatch):
    monkeypatch.setattr(acp_chat_events, "_MAX_IMAGE_BYTES", 100)
    mapper = acp_chat_events.ACPEventMapper()
    _seed(mapper)
    big = PNG_1x1 + b"\0" * 200
    out = _settle(mapper, content=[_img_item(big)])
    assert "images" not in _tool_results(out)[0]["message"]["details"]

    monkeypatch.setattr(acp_chat_events, "_MAX_IMAGE_BYTES", 10_000)
    monkeypatch.setattr(acp_chat_events, "_MAX_IMAGES", 2)
    _seed(mapper, "call_many")
    many = [_img_item(PNG_1x1 + bytes([i])) for i in range(5)]
    out = _settle(mapper, content=many, tool_id="call_many")
    assert len(_tool_results(out)[0]["message"]["details"]["images"]) == 2


def test_in_progress_updates_carry_no_images():
    mapper = acp_chat_events.ACPEventMapper()
    _seed(mapper)
    out = _settle(mapper, content=[_img_item(PNG_1x1)], status="in_progress")
    assert _tool_results(out) == []
    assert all("_media" not in e for e in out)


# ── the sink ────────────────────────────────────────────────────────────────


def test_sink_writes_media_next_to_the_transcript(tmp_path):
    sink = acp_chat_events.ChatEventSink(tmp_path, "sid-1")
    name = hashlib.sha256(PNG_1x1).hexdigest() + ".png"
    assert sink.write_media(name, PNG_1x1) is True
    # Next to the transcript copy, which lives in <folder>/.mc/ (S1-R3).
    media = tmp_path / acp_chat_events.MC_SINK_DIRNAME / "media"
    assert sink.path is not None and sink.path.parent / "media" == media
    target = media / name
    assert target.read_bytes() == PNG_1x1
    # Idempotent: the same picture twice is one file, no error.
    assert sink.write_media(name, PNG_1x1) is True
    assert [p.name for p in media.iterdir()] == [name]


def test_sink_refuses_names_that_could_leave_the_media_folder(tmp_path):
    sink = acp_chat_events.ChatEventSink(tmp_path, "sid-1")
    for bad in ("../x.png", "a/b.png", "x.svg", "short.png", ("A" * 64) + ".png"):
        assert sink.write_media(bad, PNG_1x1) is False
    media = tmp_path / acp_chat_events.MC_SINK_DIRNAME / "media"
    assert not media.exists() or list(media.iterdir()) == []
    assert not (tmp_path / "media").exists()


def test_sink_without_a_file_writes_nothing(tmp_path):
    sink = acp_chat_events.ChatEventSink(None, "sid-1")
    assert sink.write_media("a" * 64 + ".png", PNG_1x1) is False


def test_sink_prunes_old_media(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setattr(acp_chat_events.ChatEventSink, "_MEDIA_KEEP", 3)
    sink = acp_chat_events.ChatEventSink(tmp_path, "sid-1")
    media = tmp_path / acp_chat_events.MC_SINK_DIRNAME / "media"
    names = []
    for i in range(5):
        data = PNG_1x1 + bytes([i])
        name = hashlib.sha256(data).hexdigest() + ".png"
        names.append(name)
        assert sink.write_media(name, data)
        os.utime(media / name, (time.time() - 100 + i, time.time() - 100 + i))
    sink.write_media(hashlib.sha256(b"x" + PNG_1x1).hexdigest() + ".png", PNG_1x1)
    left = sorted(p.name for p in media.iterdir())
    assert len(left) == 3
    assert names[0] not in left and names[1] not in left


# ── wired end to end: the chat daemon ──────────────────────────────────────


def test_chat_daemon_writes_the_picture_and_a_small_line(tmp_path):
    from test_acp_chat import FakeClient, SESSION_RESULT, make_session, read_lines

    updates = [m["params"] for m in _wire()
               if m.get("method") == "session/update"
               and (m["params"].get("update") or {}).get("sessionUpdate", "").startswith("tool_call")]

    class ImageClient(FakeClient):
        def prompt(self, session_id, text, timeout=600.0):
            for params in updates:
                self.fire(dict(params, sessionId=session_id))
            return super().prompt(session_id, text, timeout)

    client = ImageClient(session_result=SESSION_RESULT, turns=[{"chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("look")
    assert sess.wait_idle(timeout=5)
    sess.close()

    raw = _fixture_image_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    transcript = Path(sess.transcript_path)
    assert transcript.parent.name == acp_chat_events.MC_SINK_DIRNAME
    assert (transcript.parent / "media" / f"{sha}.webp").read_bytes() == raw
    lines = read_lines(transcript)
    tool = [l for l in lines if (l.get("message") or {}).get("role") == "toolResult"]
    assert tool[0]["message"]["details"]["images"][0]["sha256"] == sha
    assert base64.b64encode(raw).decode()[:40] not in transcript.read_text()


def test_task_run_factory_writes_the_picture(tmp_path, monkeypatch):
    """The task path (bridge._make_acp_run_factory -> run_acp_once) writes
    the same file next to ITS transcript."""
    import bridge

    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.delenv("OMP_PROFILE", raising=False)
    monkeypatch.delenv("OMP_HOME", raising=False)

    def fake_client():
        fake = InProcessFake(FIXTURE, [], fake_delay=0.0)
        return fake.client

    run = bridge._make_acp_run_factory(
        model="mc-openai/mock-model", max_time=30, permission_policy="yolo",
        task_id="t-1", cwd=str(tmp_path / "work"),
    )
    (tmp_path / "work").mkdir()
    import inspect

    if "client_factory" not in inspect.signature(bridge.run_acp_once).parameters:
        raise AssertionError("run_acp_once lost its client_factory seam")
    real = bridge.run_acp_once

    def patched(prompt, **kw):
        kw["client_factory"] = fake_client
        return real(prompt, **kw)

    monkeypatch.setattr(bridge, "run_acp_once", patched)
    run("look at shot.png")

    raw = _fixture_image_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    found = list((tmp_path / "agent" / "sessions").rglob(f"media/{sha}.webp"))
    assert found and found[0].read_bytes() == raw
    # Under the task's transcript copy in <folder>/.mc/ (S1-R3).
    assert [f.parent.parent.name for f in found] == [acp_chat_events.MC_SINK_DIRNAME]


# ── omp's js browser tool (live 13.10.2026) ────────────────────────────────


def test_js_eval_screenshot_settles_with_one_picture():
    """The `eval` tool (js cell with ``tab.screenshot()``) as omp 18.10.1
    sends it over ACP: ``content`` carries the RESOLVED base64 picture
    (acp-agent.ts resolveImageData), ``rawOutput`` the session's own
    ``blob:sha256:`` reference plus the cell details (shape of the live
    native transcript, 13.10.2026). One picture, from ``content``; the blob
    reference is never mistaken for bytes."""
    raw = (ROOT.parent.parent / "backend" / "tests" / "fixtures" / "omp_js_screenshot"
           / "fb94467986aa47ebe8ab906f027f7e8b5055e6e1ca1c8c7c6f984b1a0d04d5bd.webp").read_bytes()
    text = "Screenshot captured\nFormat: image/webp (23.79 KB)\nDimensions: 800x600"
    mapper = acp_chat_events.ACPEventMapper()
    mapper.map_update({"sessionId": "s", "update": {
        "sessionUpdate": "tool_call", "toolCallId": "call_js", "kind": "execute",
        "title": "[js] Capturing screenshot of example.org", "status": "pending",
        "rawInput": {"code": "const tab = await browser.tab(\"example-org\");\nawait tab.screenshot({});",
                     "language": "js", "title": "Capturing screenshot of example.org"},
    }}, stream=True)
    out = _settle(mapper, tool_id="call_js", content=[
        {"type": "content", "content": {"type": "text", "text": text}},
        _img_item(raw, "image/webp"),
    ], raw={
        "content": [{"type": "text", "text": text},
                    {"type": "image", "data": "blob:sha256:" + hashlib.sha256(raw).hexdigest(),
                     "mimeType": "image/webp"}],
        "details": {"language": "js", "cells": [{"index": 0, "status": "complete"}]},
    })
    entry = _tool_results(out)[0]
    assert entry["message"]["details"]["images"] == [
        {"sha256": hashlib.sha256(raw).hexdigest(), "mimeType": "image/webp", "bytes": len(raw)}
    ]
    assert entry["message"]["content"][0]["text"].startswith("Screenshot captured")
