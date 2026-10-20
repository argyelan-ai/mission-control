#!/usr/bin/env python3
"""S1-R3 — MC's display copy lives in ``<folder>/.mc/``, where omp never looks.

MC writes a copy of every ACP session (``ChatEventSink``) for the chat view.
Until S1-R3 it sat NEXT TO omp's own memory file, with the same session id
in the header. omp 18.10.1 treats both as sessions (``tests/omp_listing_rule``)
and loads the YOUNGER one — often MC's copy. Live 20.10.2026: loading the
current chat failed with "ACP session load was cancelled" and the chat
silently started over after a restart of the daemon.

Contract (tests 1-3, 12 of the plan):
1. a new ``ChatEventSink`` writes ``<folder>/.mc/<ts>_<id>.jsonl``; its header
   is byte for byte the header of before (every header reader stays as is);
2. the preview goes to ``<folder>/.mc/previews/``, pictures to
   ``<folder>/.mc/media/``;
3. after a chat turn AND a task run, omp's listing rule finds no MC copy;
12. ``find_sink(folder, id)`` finds MC's copy in ``.mc/`` first, then in the
    old place, and never omp's own file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import bridge  # noqa: E402
import omp_listing_rule  # noqa: E402
from test_acp_chat import SESSION_RESULT, FakeClient  # noqa: E402

FIXED_NOW = "2026-01-02T03:04:05.678Z"


def _omp_file(folder: Path, sid: str, name: str = "2026-01-02T03-04-05-678Z") -> Path:
    """A file the way omp writes it: omp's name, no ``bridge``, a ``cwd``."""
    path = folder / f"{name}_{sid}.jsonl"
    path.write_text(
        json.dumps({"type": "session", "version": 3, "id": sid,
                    "timestamp": FIXED_NOW, "cwd": "/workspace"}) + "\n"
        + json.dumps({"type": "message", "id": "0a1b2c3d",
                      "message": {"role": "user", "content": "hello"}}) + "\n",
        encoding="utf-8",
    )
    return path


def _legacy_copy(folder: Path, sid: str, name: str = "2026-01-02T03-04-05") -> Path:
    """MC's copy where the bridge wrote it before S1-R3: next to omp's file."""
    path = folder / f"{name}_{sid}.jsonl"
    path.write_text(
        json.dumps({"type": "session", "version": 3, "id": sid,
                    "timestamp": FIXED_NOW, "cwd": "", "bridge": "acp"}) + "\n",
        encoding="utf-8",
    )
    return path


# ── 1. a new copy lands in .mc/, header unchanged ──────────────────────────


def test_1_sink_writes_into_the_mc_folder_with_the_same_header(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_chat_events, "_now_iso", lambda: FIXED_NOW)
    monkeypatch.delenv("OMP_ACP_CWD", raising=False)
    folder = tmp_path / "--workspace--"
    folder.mkdir()

    chat = acp_chat_events.ChatEventSink(folder, "sess-1")
    task = acp_chat_events.ChatEventSink(
        folder, "sess-2", task_id="0f0e0d0c-0000-4000-8000-000000000001")

    assert acp_chat_events.MC_SINK_DIRNAME == ".mc"
    assert chat.path is not None and chat.path.parent == folder / ".mc"
    assert task.path is not None and task.path.parent == folder / ".mc"
    assert chat.path.name.endswith("_sess-1.jsonl")
    assert list(folder.glob("*.jsonl")) == [], "nothing may land next to omp's file"
    # Byte for byte the header the bridge wrote before S1-R3: the backend's
    # header readers (bridge tag, taskId, cwd) stay untouched.
    assert chat.path.read_text(encoding="utf-8") == (
        '{"type": "session", "version": 3, "id": "sess-1", '
        '"timestamp": "2026-01-02T03:04:05.678Z", "cwd": "", "bridge": "acp"}\n'
    )
    assert task.path.read_text(encoding="utf-8") == (
        '{"type": "session", "version": 3, "id": "sess-2", '
        '"timestamp": "2026-01-02T03:04:05.678Z", "cwd": "", "bridge": "acp", '
        '"taskId": "0f0e0d0c-0000-4000-8000-000000000001"}\n'
    )


def test_1b_mc_sink_dir_creates_and_returns_the_folder(tmp_path):
    folder = tmp_path / "--w--"
    folder.mkdir()
    target = acp_chat_events.mc_sink_dir(folder)
    assert target == folder / ".mc" and target.is_dir()
    assert acp_chat_events.mc_sink_dir(folder) == target  # idempotent


def test_1c_unwritable_mc_folder_degrades_to_a_noop_sink(tmp_path):
    folder = tmp_path / "--w--"
    folder.mkdir()
    (folder / ".mc").write_text("a file where the folder should be")
    sink = acp_chat_events.ChatEventSink(folder, "s")
    preview = acp_chat_events.PreviewEventSink(folder, "s")
    assert sink.path is None and preview.path is None
    sink.write(["{}"])  # never raises
    preview.write(["{}"])


# ── 2. preview and pictures follow the copy ─────────────────────────────────


def test_2_preview_and_media_live_under_the_mc_folder(tmp_path):
    folder = tmp_path / "--workspace--"
    folder.mkdir()
    preview = acp_chat_events.PreviewEventSink(folder, "sess-1")
    assert preview.path is not None
    assert preview.path.parent == folder / ".mc" / "previews"

    sink = acp_chat_events.ChatEventSink(folder, "sess-1")
    name = "a" * 64 + ".png"
    assert sink.write_media(name, b"\x89PNG-bytes")
    assert (folder / ".mc" / "media" / name).read_bytes() == b"\x89PNG-bytes"
    assert not (folder / "media").exists()
    assert not (folder / "previews").exists()


# ── 3. omp's rule finds no MC copy after a chat turn and a task run ─────────


def test_3_omp_lists_no_mc_copy_after_a_chat_turn_and_a_task_run(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.delenv("OMP_PROFILE", raising=False)
    monkeypatch.setenv("OMP_ACP_CWD", "/workspace")
    folder = acp_chat_events.session_dir(cwd="/workspace")
    assert folder is not None
    root = folder.parent

    # omp's own files of both sessions (what `omp acp` writes itself).
    _omp_file(folder, "sid-chat")
    _omp_file(folder, "sid-task", name="2026-01-02T03-05-00-000Z")

    # A chat turn through the daemon, the way production wires it.
    client = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-chat"},
                        turns=[{"chunks": ["an", " answer"]}])
    sess = acp_chat.ChatSession(
        client_factory=lambda: client, cwd="/workspace",
        state_dir=tmp_path / "state", sessions_dir=folder,
    )
    sess.start()
    sess.prompt("a question")
    assert sess.wait_idle(timeout=5)
    sess.close()

    # A task run through the bridge's own factory (sink, preview, picture).
    def fake_run_acp_once(prompt, **kwargs):
        kwargs["on_session_id"]("sid-task")
        mapper = acp_chat_events.ACPEventMapper()
        kwargs["transcript_sink"](mapper.dump(mapper.map_user_prompt(prompt)))
        kwargs["preview_sink"](mapper.dump(mapper.map_final_assistant_message("done")))
        assert kwargs["media_sink"]("b" * 64 + ".png", b"png")
        return "outcome"

    monkeypatch.setattr(bridge, "run_acp_once", fake_run_acp_once)
    run = bridge._make_acp_run_factory(
        model="m", max_time=10, permission_policy="yolo", task_id="T1", cwd="/workspace")
    assert run("do the task") == "outcome"

    written = sorted(p for p in root.rglob("*.jsonl"))
    mc_copies = [p for p in written if omp_listing_rule.is_mc_copy(p)]
    assert len(mc_copies) == 2, [str(p.relative_to(root)) for p in written]
    assert all(p.parent == folder / ".mc" for p in mc_copies)
    listed = omp_listing_rule.omp_would_list(root)
    assert [p for p in listed if omp_listing_rule.is_mc_copy(p)] == [], (
        "omp would list MC's copy: " + ", ".join(str(p.relative_to(root)) for p in listed))
    # omp still sees its own two sessions.
    assert sorted(omp_listing_rule.session_header(p)["id"] for p in listed) == [
        "sid-chat", "sid-task"]


def test_3b_the_twin_flags_the_old_layout(tmp_path):
    """The twin itself: an MC copy next to omp's file IS listed (the bug)."""
    folder = tmp_path / "--workspace--"
    folder.mkdir()
    _omp_file(folder, "sid-1")
    legacy = _legacy_copy(folder, "sid-1")
    (folder / ".mc").mkdir()
    hidden = _legacy_copy(folder / ".mc", "sid-2")
    (folder / ".lock-like.jsonl").write_text(legacy.read_text())
    listed = omp_listing_rule.omp_would_list(tmp_path)
    assert legacy in listed
    assert hidden not in listed
    assert folder / ".lock-like.jsonl" not in listed
    # A title line first: the header is line 2.
    titled = folder / "2026-01-02T00-00-00-000Z_sid-3.jsonl"
    titled.write_text('{"type": "title", "title": "x"}\n'
                      '{"type": "session", "id": "sid-3"}\n', encoding="utf-8")
    assert omp_listing_rule.session_header(titled)["id"] == "sid-3"
    # No string id: not a session.
    (folder / "bad.jsonl").write_text('{"type": "session", "id": 7}\n')
    assert omp_listing_rule.session_header(folder / "bad.jsonl") is None


# ── 12. find_sink: .mc/ first, then the old place, never omp's file ────────


def test_12_find_sink_prefers_the_mc_folder_then_the_old_place(tmp_path):
    folder = tmp_path / "--workspace--"
    folder.mkdir()
    omp_own = _omp_file(folder, "sid-1")
    assert acp_chat_events.find_sink(folder, "sid-1") is None, "never omp's file"

    legacy = _legacy_copy(folder, "sid-1")
    assert acp_chat_events.find_sink(folder, "sid-1") == legacy

    (folder / ".mc").mkdir()
    moved = _legacy_copy(folder / ".mc", "sid-1", name="2026-01-01T00-00-00")
    assert acp_chat_events.find_sink(folder, "sid-1") == moved, ".mc/ comes first"
    assert acp_chat_events.find_sink(folder, "sid-unknown") is None
    assert acp_chat_events.find_sink(tmp_path / "missing", "sid-1") is None
    assert omp_own.exists()


def test_12b_find_sink_skips_omp_files_inside_the_mc_folder(tmp_path):
    """Only a header with ``bridge: "acp"`` counts — wherever the file is."""
    folder = tmp_path / "--workspace--"
    (folder / ".mc").mkdir(parents=True)
    _omp_file(folder / ".mc", "sid-1")
    assert acp_chat_events.find_sink(folder, "sid-1") is None


def test_12c_find_sink_takes_the_newest_of_two_copies(tmp_path):
    folder = tmp_path / "--workspace--"
    (folder / ".mc").mkdir(parents=True)
    old = _legacy_copy(folder / ".mc", "sid-1", name="2026-01-01T00-00-00")
    new = _legacy_copy(folder / ".mc", "sid-1", name="2026-01-03T00-00-00")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert acp_chat_events.find_sink(folder, "sid-1") == new


def test_12d_find_sink_treats_the_id_literally(tmp_path):
    """The id is matched as text, never as a glob pattern."""
    folder = tmp_path / "--workspace--"
    (folder / ".mc").mkdir(parents=True)
    _legacy_copy(folder / ".mc", "sid-1")
    assert acp_chat_events.find_sink(folder, "*") is None
    assert acp_chat_events.find_sink(folder, "sid-?") is None


# ── the daemon reads omp's file, never its own copy ─────────────────────────


def test_omp_session_file_is_omps_file_even_in_an_old_tree(tmp_path):
    """``_omp_session_file`` names tools from omp's own file. In a tree that
    was not moved yet, MC's old copy (same id, younger) sits next to it — it
    must never be taken for omp's file."""
    folder = tmp_path / "sessions"
    folder.mkdir()
    omp_own = _omp_file(folder, "sid-1")
    legacy = _legacy_copy(folder, "sid-1")
    os.utime(omp_own, (1_000_000, 1_000_000))
    os.utime(legacy, (2_000_000, 2_000_000))  # the copy is younger
    client = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-1"})
    sess = acp_chat.ChatSession(
        client_factory=lambda: client, cwd="/workspace",
        state_dir=tmp_path / "state", sessions_dir=folder,
    )
    sess._session_id = "sid-1"
    assert sess._omp_session_file() == omp_own
    legacy.unlink()
    assert sess._omp_session_file() == omp_own
    omp_own.unlink()
    assert sess._omp_session_file() is None
