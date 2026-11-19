"""The backend reads MC's display copy in BOTH places: the cwd folder and
``<folder>/.mc/`` (chat reliability S1, step R1).

Why: under ACP one session has two files with the same session id — omp's
own memory file and the display copy MC's bridge sink writes (header
``"bridge": "acp"``). omp 18.10.1 lists every ``<folder>/*.jsonl`` whose
first lines carry ``type: "session"`` plus an ``id`` — so it takes MC's copy
for one of its own sessions, lists every chat twice and fails to load the
current one. A later step moves MC's copies one level down into
``<folder>/.mc/``, where omp's ``list(cwd)`` (``<folder>/*.jsonl``) and
``listAll()`` (``*/*.jsonl``) never look. That move may only ship after
the reader here understands both places, so a tree in transition (old
copies in the folder, new ones in ``.mc/``) reads the same as before.

Every test here runs on two layouts where it applies:
  * ``legacy`` — copy beside omp's file (today's tree; must not change),
  * ``mc``     — copy in ``<folder>/.mc/`` (after the move).

Numbers in the docstrings refer to the build plan's R1 test list. Folder
layout and header shapes are copied from a live sessions tree; ids, names
and texts are made up.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import omp_chat

CHAT = "01a15762-0000-7000-8000-000000000001"
TASK = "01a1559a-0000-7000-8000-000000000002"
TASK_ID = "48468c92-0000-4000-8000-000000000009"

CONTAINER_SESSIONS = "/home/agent/.omp/profiles/mc-agent/agent/sessions"

LAYOUTS = ("legacy", "mc")


def _msg(uid: str, ts: str, text: str = "hello") -> dict:
    return {"type": "message", "id": uid, "parentId": None, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _native(folder: Path, stamp: str, sid: str, cwd: str, *entries_ts: str) -> Path:
    """omp's own file: ``<ms>Z_<sid>.jsonl``, title line first, real cwd."""
    lines = [
        {"type": "title", "v": 1, "title": "", "updatedAt": "2026-10-20T03:36:53.100Z"},
        {"type": "session", "version": 3, "id": sid, "timestamp": "2026-10-20T03:36:53.100Z",
         "cwd": cwd},
    ]
    lines += [_msg(f"n{sid[:4]}{i:03d}", ts) for i, ts in enumerate(entries_ts)]
    return _write(folder / f"{stamp}Z_{sid}.jsonl", lines)


def _sink_dir(folder: Path, layout: str) -> Path:
    return folder / ".mc" if layout == "mc" else folder


def _sink(folder: Path, layout: str, stamp: str, sid: str, *entries_ts: str,
          task_id: str | None = None) -> Path:
    """MC's display copy: ``<s>_<sid>.jsonl``, header with empty cwd and
    ``bridge: "acp"`` (``taskId`` only on a task run)."""
    header = {"type": "session", "version": 3, "id": sid,
              "timestamp": "2026-10-20T03:36:53.200Z", "cwd": "", "bridge": "acp"}
    if task_id:
        header["taskId"] = task_id
    lines = [header] + [_msg(f"acp{sid[:4]}{i:03d}", ts) for i, ts in enumerate(entries_ts)]
    return _write(_sink_dir(folder, layout) / f"{stamp}_{sid}.jsonl", lines)


def _pointer(state_folder: Path, transcript: str, session_id: str) -> Path:
    """The chat daemon's state mirror; ``transcript`` as the daemon writes
    it (a container path)."""
    state = state_folder / omp_chat.CHAT_STATE_FILENAME
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "version": 1, "driver": "omp", "sessionId": session_id, "busy": False,
        "transcript": transcript,
    }))
    return state


def _container_path(path: Path, root: Path) -> str:
    return f"{CONTAINER_SESSIONS}/{path.relative_to(root).as_posix()}"


def _age(path: Path, epoch: int) -> None:
    os.utime(path, (epoch, epoch))


def _find(root: Path):
    return omp_chat.find_active_session(root, acp_chat=True)


OTHER = "01a15800-0000-7000-8000-000000000004"


def _tree(tmp_path: Path, layout: str, *, other: bool = False):
    """The live 20.10. shape: chat in /workspace, a task in a worktree that
    writes NEWER (by content and mtime).

    ``other`` adds the newest session of all: a parallel run in /workspace
    WITHOUT a task marker (older bridge image, a head run). Without a
    pointer the fallback would pick it — so with it in the tree only the
    pointer can name the chat, and a pointer test cannot pass by luck."""
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    wt = root / "--workspace-worktrees-bench-x--"
    chat_native = _native(ws, "2026-10-20T03-36-53-100", CHAT, "/workspace",
                          "2026-10-20T08:00:00.000Z")
    chat_sink = _sink(ws, layout, "2026-10-20T03-36-53", CHAT, "2026-10-20T08:00:01.000Z")
    task_native = _native(wt, "2026-10-20T09-19-10-646", TASK, "/workspace/worktrees/bench-x",
                          "2026-10-20T10:11:00.000Z")
    task_sink = _sink(wt, layout, "2026-10-20T09-19-11", TASK, "2026-10-20T10:11:01.000Z",
                      task_id=TASK_ID)
    for p in (chat_native, chat_sink):
        _age(p, 1_700_000_000)
    for p in (task_native, task_sink):
        _age(p, 1_700_000_100)
    if other:
        _age(_native(ws, "2026-10-20T11-00-00-000", OTHER, "/workspace",
                     "2026-10-20T11:30:00.000Z"), 1_700_000_200)
        assert _find(root)[0].name.endswith(f"_{OTHER}.jsonl")  # fallback = the other run
    return root, ws, chat_native, chat_sink, task_sink


# ── 1, 2 · the daemon's pointer ─────────────────────────────────────────────


@pytest.mark.parametrize("layout", LAYOUTS)
def test_1_pointer_to_the_copy_resolves_it_in_either_place(tmp_path, layout):
    """Plan R1 test 1 (+ test 9 for ``legacy``): the pointer names the copy
    where it lies -> the chat shows exactly that copy, not omp's file and
    not the newer task."""
    root, ws, _native_file, chat_sink, _ = _tree(tmp_path, layout, other=True)
    _pointer(ws, _container_path(chat_sink, root), CHAT)
    found = _find(root)
    assert found is not None
    assert found[0] == chat_sink
    assert found[1]["sessionId"] == chat_sink.stem


def test_2_old_pointer_without_mc_finds_the_moved_copy(tmp_path):
    """Plan R1 test 2: the move happened, the pointer still names the OLD
    place (``<folder>/<file>``) — the copy must be found in ``.mc/``. This
    is the hand-over during the deploy."""
    root, ws, _native_file, chat_sink, _ = _tree(tmp_path, "mc", other=True)
    old_place = ws / chat_sink.name
    assert not old_place.exists()
    _pointer(ws, _container_path(old_place, root), CHAT)
    assert _find(root)[0] == chat_sink


def test_2b_pointer_from_another_view_falls_back_to_mc_beside_the_state_file(tmp_path):
    """The fourth candidate: a pointer whose folder is not a folder of this
    tree (another view of the same files) is resolved beside the state
    file — in the folder and then in its ``.mc/``."""
    root, ws, _native_file, chat_sink, _ = _tree(tmp_path, "mc", other=True)
    _pointer(ws, f"/elsewhere/{chat_sink.name}", CHAT)
    assert _find(root)[0] == chat_sink


def test_pointer_into_mc_while_the_file_is_back_at_the_old_place(tmp_path):
    """Rollback of the move: the pointer says ``.mc/``, the file lies in the
    folder again. Old place first, then ``.mc/`` — it is found either way."""
    root, ws, _native_file, chat_sink, _ = _tree(tmp_path, "legacy", other=True)
    _pointer(ws, f"{CONTAINER_SESSIONS}/{ws.name}/.mc/{chat_sink.name}", CHAT)
    assert _find(root)[0] == chat_sink


def test_pointer_into_mc_with_the_copy_in_both_places_is_stable(tmp_path):
    """A move that skipped a file (target already there) leaves the copy in
    both places. The answer must be the same every time (old place first,
    as the candidate order says) — a flip-flop would wipe the open chat."""
    root, ws, _native_file, legacy, _ = _tree(tmp_path, "legacy", other=True)
    moved = _sink(ws, "mc", "2026-10-20T03-36-53", CHAT, "2026-10-20T08:00:01.000Z")
    _pointer(ws, _container_path(moved, root), CHAT)
    assert {_find(root)[0] for _ in range(5)} == {legacy}


@pytest.mark.parametrize("state_at", ("root", "other-folder"))
@pytest.mark.parametrize("layout", LAYOUTS)
def test_pointer_into_mc_takes_the_folder_above_mc_when_the_state_file_is_elsewhere(
        tmp_path, layout, state_at):
    """Contract point (4): when the pointer already names
    ``<folder>/.mc/<file>``, ``<folder>`` is the name ABOVE ``.mc``. The
    state file lies NOT in that folder (tree root, or another cwd folder),
    so the two candidates beside the state file cannot hide a wrong
    ``<folder>``. ``mc``: the copy in ``.mc/`` is found. ``legacy`` (the
    move was rolled back): the copy beside omp's file. A newer non-task
    session is present: reading ``.mc`` as the folder name would drop
    candidates 1 and 2, and the fallback would show that other
    conversation."""
    root, ws, _native_file, chat_sink, _ = _tree(tmp_path, layout, other=True)
    state_folder = root if state_at == "root" else root / "--workspace-worktrees-bench-x--"
    _pointer(state_folder, f"{CONTAINER_SESSIONS}/{ws.name}/.mc/{chat_sink.name}", CHAT)
    assert omp_chat.chat_state_file(root).parent == state_folder
    assert not (state_folder / chat_sink.name).exists()
    assert not (state_folder / ".mc" / chat_sink.name).exists()
    assert omp_chat._pointed_chat_transcript(root) == chat_sink
    assert _find(root)[0] == chat_sink


@pytest.mark.parametrize("broken", ("name", "folder"))
def test_a_pointer_with_a_nul_byte_never_raises(tmp_path, broken):
    """The state file is written inside the agent's container. A pointer
    with a NUL byte makes ``Path.resolve()`` raise ``ValueError`` (not
    ``OSError``); that must not escape into ``/chat/history`` or the tailer.
    Such a candidate is skipped like a missing one: a bad file name leaves
    no candidate (fallback = the newest non-task session); a bad folder
    name still finds the copy beside the state file."""
    root, ws, _native_file, chat_sink, _ = _tree(tmp_path, "mc", other=True)
    if broken == "name":
        transcript = f"{CONTAINER_SESSIONS}/{ws.name}/.mc/bad\x00{chat_sink.name}"
        expected = None
    else:
        transcript = f"{CONTAINER_SESSIONS}/--work\x00space--/{chat_sink.name}"
        expected = chat_sink
    _pointer(ws, transcript, CHAT)
    assert omp_chat._pointed_chat_transcript(root) == expected
    found = _find(root)[0]
    if expected is None:
        assert found.name.endswith(f"_{OTHER}.jsonl")
    else:
        assert found == chat_sink


def test_a_pointer_into_mc_cannot_leave_the_tree(tmp_path):
    """The tree check stays for the new candidates: a ``.mc/`` entry that is
    a link out of the sessions tree is not taken from the pointer. (What
    the fallback then picks is gated by ``transcript_allowed`` at every
    reader, as for a link directly in the folder.)"""
    root, ws, _native_file, _chat_sink, _ = _tree(tmp_path, "mc")
    secret = tmp_path / "outside.jsonl"
    _write(secret, [{"type": "session", "version": 3, "id": "x", "cwd": "", "bridge": "acp"}])
    link = ws / ".mc" / "2026-10-20T11-00-00_outside.jsonl"
    link.symlink_to(secret)
    for transcript in (
        f"{CONTAINER_SESSIONS}/{ws.name}/.mc/{link.name}",  # candidate 2
        f"{CONTAINER_SESSIONS}/{ws.name}/{link.name}",      # old pointer -> candidate 2
        f"/elsewhere/{link.name}",                           # candidate 4
    ):
        _pointer(ws, transcript, "x")
        assert omp_chat._pointed_chat_transcript(root) is None, transcript


# ── 3 · no pointer: a task never becomes the chat ──────────────────────────


@pytest.mark.parametrize("layout", LAYOUTS)
def test_3_without_pointer_a_task_whose_copy_is_in_mc_is_skipped(tmp_path, layout):
    """Plan R1 test 3 (+ 9): task and chat in the same folder, task newer.
    Only the task's COPY carries ``taskId``; omp's file of the task has a
    plain ``/workspace`` cwd. The task must be recognised through its copy
    wherever it lies, and the chat lands on the chat's copy."""
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    chat_native = _native(ws, "2026-10-20T03-36-53-100", CHAT, "/workspace",
                          "2026-10-20T08:00:00.000Z")
    chat_sink = _sink(ws, layout, "2026-10-20T03-36-53", CHAT, "2026-10-20T08:00:01.000Z")
    task_native = _native(ws, "2026-10-20T09-15-28-176", TASK, "/workspace",
                          "2026-10-20T10:00:00.000Z")
    task_sink = _sink(ws, layout, "2026-10-20T09-15-28", TASK, "2026-10-20T10:00:01.000Z",
                      task_id=TASK_ID)
    for p in (chat_native, chat_sink):
        _age(p, 1_700_000_000)
    for p in (task_native, task_sink):
        _age(p, 1_700_000_900)
    assert omp_chat.is_task_session(task_native) is True
    assert _find(root)[0] == chat_sink


@pytest.mark.parametrize("layout", LAYOUTS)
def test_3b_copy_only_chat_beside_a_newer_task(tmp_path, layout):
    """Plan R1 test 3, the copy-only case (the host chat daemon writes no omp
    file at all; live: 3 copies, 0 pairs): the chat exists ONLY as MC's
    copy. Without a pointer it must still be found — and the newer task
    must not take its place."""
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    chat_sink = _sink(ws, layout, "2026-10-20T03-36-53", CHAT, "2026-10-20T08:00:01.000Z")
    task_native = _native(ws, "2026-10-20T09-15-28-176", TASK, "/workspace",
                          "2026-10-20T10:00:00.000Z")
    task_sink = _sink(ws, layout, "2026-10-20T09-15-28", TASK, "2026-10-20T10:00:01.000Z",
                      task_id=TASK_ID)
    _age(chat_sink, 1_700_000_000)
    for p in (task_native, task_sink):
        _age(p, 1_700_000_900)
    found = _find(root)
    assert found is not None and found[0] == chat_sink


# ── 4, 5, 6 · the same session's files, both places ────────────────────────


@pytest.mark.parametrize("layout", LAYOUTS)
def test_4_bridge_sibling_of_omps_file_is_the_copy(tmp_path, layout):
    """Plan R1 test 4 (+ 9)."""
    _root, _ws, chat_native, chat_sink, _ = _tree(tmp_path, layout)
    assert omp_chat.bridge_sibling(chat_native) == chat_sink


@pytest.mark.parametrize("layout", LAYOUTS)
def test_5_session_cwd_of_the_copy_comes_from_omps_file(tmp_path, layout):
    """Plan R1 test 5 (+ 9): the copy records an empty cwd; omp's file of the
    same session (in the folder above ``.mc/``) has the real one."""
    _root, _ws, _native_file, chat_sink, _ = _tree(tmp_path, layout)
    assert omp_chat.session_cwd(chat_sink) == "/workspace"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_6_session_mtime_counts_the_newer_copy(tmp_path, layout):
    """Plan R1 test 6 (+ 9): ``live`` means the session, not one file."""
    _root, _ws, chat_native, chat_sink, _ = _tree(tmp_path, layout)
    _age(chat_native, 1_700_000_000)
    _age(chat_sink, 1_700_000_500)
    assert omp_chat._session_mtime(chat_native) == 1_700_000_500
    assert omp_chat._session_mtime(chat_sink) == 1_700_000_500


@pytest.mark.parametrize("layout", LAYOUTS)
def test_session_files_lists_both_places_and_nothing_else(tmp_path, layout):
    """The shared helper behind 3–6: every file of THIS session in the folder
    and in its ``.mc/`` — from either end — and never another session's
    file, a preview or a file of the same id in another folder."""
    root, ws, chat_native, chat_sink, _ = _tree(tmp_path, layout)
    # same id in another folder: not this folder's session
    _sink(root / "--other--", layout, "2026-10-20T03-36-53", CHAT)
    # a preview of this session (lives one level deeper)
    _write(_sink_dir(ws, layout) / "previews" / f"2026-10-20T03-37-00_{CHAT}_ab12.jsonl",
           [{"type": "custom_message", "customType": "acp-preview"}])
    # another session in the same folder
    _sink(ws, layout, "2026-10-20T04-00-00", TASK)
    want = sorted([chat_native, chat_sink])
    assert sorted(omp_chat.session_files(chat_native)) == want
    assert sorted(omp_chat.session_files(chat_sink)) == want


# ── 7 · a task's own run (#902 "From the task") ────────────────────────────


@pytest.mark.parametrize("layout", LAYOUTS)
def test_7_find_task_session_finds_the_task_copy(tmp_path, layout):
    """Plan R1 test 7 (+ 9): only the copy carries ``taskId``."""
    root, _ws, _native_file, _chat_sink, task_sink = _tree(tmp_path, layout)
    assert omp_chat.find_task_session(root, TASK_ID) == task_sink
    assert omp_chat.find_task_session(root, TASK_ID.upper()) == task_sink


# ── 10 · preview and pictures sit beside the copy ──────────────────────────


@pytest.mark.parametrize("layout", LAYOUTS)
def test_10_preview_channel_is_beside_the_copy(tmp_path, layout):
    """Plan R1 test 10: unchanged code, new place. After the move the bridge
    writes previews to ``.mc/previews/`` — exactly beside the copy."""
    _root, ws, _native_file, chat_sink, _ = _tree(tmp_path, layout)
    preview = _write(
        _sink_dir(ws, layout) / "previews" / f"2026-10-20T08-00-02_{CHAT}_ab12.jsonl",
        [{"type": "custom_message", "customType": "acp-preview", "content": "..."}],
    )
    assert omp_chat.preview_channel(chat_sink) == preview


def test_10_media_context_reads_pictures_beside_the_copy(tmp_path, monkeypatch):
    """Plan R1 test 10: ``MediaContext`` reads ``<copy's folder>/media/`` —
    after the move that is ``.mc/media/``."""
    from PIL import Image

    from app.services import chat_media

    monkeypatch.setattr(chat_media.settings, "chat_media_root", tmp_path / "chat-media")
    _root, ws, _native_file, chat_sink, _ = _tree(tmp_path, "mc")
    out = io.BytesIO()
    Image.new("RGB", (32, 24), (40, 40, 40)).save(out, format="PNG")
    raw = out.getvalue()
    sha = hashlib.sha256(raw).hexdigest()
    (ws / ".mc" / "media").mkdir(parents=True)
    (ws / ".mc" / "media" / f"{sha}.png").write_bytes(raw)

    agent = SimpleNamespace(id=uuid.UUID("11111111-2222-3333-4444-555555555555"))
    ctx = chat_media.context_for(agent, chat_sink)
    assert ctx is not None and ctx.session_dir == ws / ".mc"
    refs = ctx.from_refs([{"sha256": sha, "mimeType": "image/png", "bytes": len(raw)}])
    assert [r["id"] for r in refs] == [sha]


# ── 11 · the open chat during the move ─────────────────────────────────────


def _set_acp(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)


async def test_11_tailer_moves_once_to_the_moved_copy_and_never_back(tmp_path, monkeypatch):
    """Plan R1 test 11: the copy is moved from the folder into ``.mc/`` while
    the chat is open and the pointer still names the old place. The live
    stream must switch exactly once to the moved copy (one
    ``session_changed`` — the frontend then empties history and usage and
    reloads), never switch back, and send no entry twice."""
    import app.services.transcript_chat as tc
    from app.services import transcript_adapters

    _set_acp(monkeypatch)
    root, ws, _native_file, legacy, _ = _tree(tmp_path, "legacy")
    _pointer(ws, _container_path(legacy, root), CHAT)
    omp = transcript_adapters.adapter_for(SimpleNamespace(harness="omp"))
    monkeypatch.setattr(transcript_adapters, "adapter_for", lambda agent: omp)
    published: list[dict] = []

    async def _fake(channel, event_type, data):
        published.append(json.loads(json.dumps(data)))

    monkeypatch.setattr(tc.sse, "broadcast", _fake)

    def _changes() -> int:
        return sum(1 for d in published if d.get("kind") == "session_changed")

    m = tc.ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    m.STATE_PROBE_INTERVAL_SECONDS = 0.02
    await m.acquire("a", legacy)
    try:
        await asyncio.sleep(0.1)
        moved = ws / ".mc" / legacy.name
        moved.parent.mkdir()
        os.rename(legacy, moved)  # what the move does: same name, one level down
        for _ in range(150):
            if _changes():
                break
            await asyncio.sleep(0.02)
        with moved.open("a") as fh:
            fh.write(json.dumps(_msg("acpnew001", "2026-10-20T12:00:00.000Z", "after")) + "\n")
        for _ in range(150):
            if any(d.get("uuid") == "acpnew001" for d in published):
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)  # more ticks: it must not switch back
    finally:
        await m.release("a")
    assert _changes() == 1, published
    uuids = [d["uuid"] for d in published if d.get("kind") == "message" and d.get("uuid")]
    assert "acpnew001" in uuids
    assert len(uuids) == len(set(uuids)), uuids


# ── 14 · the token harvester keeps counting ─────────────────────────────────


@pytest.fixture
async def _usage_db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    import app.models.model_usage  # noqa: F401 — registers the tables

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_14_token_harvester_reads_files_in_mc(tmp_path, _usage_db):
    """Plan R1 test 14 (protection): the harvester's recursive
    ``*/omp-sessions/**/*.jsonl`` also walks the dot folder ``.mc/``, so
    moving a file there changes nothing it counts."""
    from sqlmodel import select

    from app.models.model_usage import ModelUsageEvent
    from app.services.token_harvester import run_harvest

    agents = tmp_path / "agents"
    target = agents / "agent-one" / "omp-sessions" / "--workspace--" / ".mc"
    line = {
        "type": "message", "id": "u0000001", "parentId": None,
        "timestamp": "2026-10-20T08:00:05.000Z",
        "message": {
            "role": "assistant", "api": "openai-completions", "provider": "mc-openai",
            "model": "test-model",
            "usage": {"input": 120, "output": 30, "cacheRead": 0, "cacheWrite": 0,
                      "totalTokens": 150},
            "content": [{"type": "text", "text": "ok"}], "stopReason": "stop",
        },
    }
    _write(target / f"2026-10-20T08-00-00_{CHAT}.jsonl", [line])
    stats = await run_harvest(
        _usage_db, agent_base_paths=[str(agents)], boss_base_paths=[], agent_slug_map={},
    )
    assert stats["files_scanned"] >= 1
    events = (await _usage_db.exec(select(ModelUsageEvent))).all()
    assert [e.output_tokens for e in events] == [30]
