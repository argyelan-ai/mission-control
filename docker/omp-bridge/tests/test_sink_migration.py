#!/usr/bin/env python3
"""S1-R3 — moving MC's old display copies into ``<folder>/.mc/``.

``sink_migration.py`` moves every MC copy (a session header with
``bridge: "acp"``) that still sits next to omp's own file into the folder's
``.mc/`` — and nothing else. It never deletes, never overwrites, never
creates a ``.bak`` name (omp renames ``*.jsonl.<x>.bak`` back into sessions),
writes a list of every move (``.mc-sink-migration.json`` at the sessions
root, paths relative to it) and can undo it (``rollback``).

It runs on the host by hand in the deploy window (``--dry-run``, then
``--apply``, agent stopped) and, as a safety net only, in
``ChatSession.start()`` before the pointer is read.

Tests 4-11 and 14-16 of the plan, plus remap, the CLI and the daemon net.
Every tree here is built in ``tmp_path`` with neutral names.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT.parent.parent / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import omp_listing_rule  # noqa: E402
import sink_migration  # noqa: E402
from test_acp_chat import SESSION_RESULT, FakeClient  # noqa: E402

STAMP = "2026-01-02T03:04:05.678Z"
OMP_TS = "2026-01-02T03-04-05-678Z"   # omp's file names carry ms and a Z
MC_TS = "2026-01-02T03-04-05"          # MC's carry neither

SCRIPT = ROOT / "sink_migration.py"


# ── building blocks ─────────────────────────────────────────────────────────


def _jsonl(path: Path, lines: list[dict], mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def omp_file(folder: Path, sid: str, mtime: float, ts: str = OMP_TS) -> Path:
    return _jsonl(folder / f"{ts}_{sid}.jsonl", [
        {"type": "session", "version": 3, "id": sid, "timestamp": STAMP,
         "cwd": "/workspace"},
        {"type": "message", "id": "0a1b2c3d",
         "message": {"role": "user", "content": "omp's own line"}},
    ], mtime)


def mc_copy(folder: Path, sid: str, mtime: float, ts: str = MC_TS,
            task_id: str | None = None, extra: list[dict] | None = None) -> Path:
    header = {"type": "session", "version": 3, "id": sid, "timestamp": STAMP,
              "cwd": "", "bridge": "acp"}
    if task_id:
        header["taskId"] = task_id
    lines = [header, {"type": "message", "id": "acp00001",
                      "message": {"role": "user", "content": "a question"}}]
    return _jsonl(folder / f"{ts}_{sid}.jsonl", lines + list(extra or []), mtime)


def build_tree(root: Path) -> dict[str, Path]:
    """The live shapes (20.10.2026) in one sessions root.

    ``--workspace--``: two pairs (one whose MC copy is the younger file —
    the one omp would load), a task copy, a mixed file (omp wrote into MC's
    copy, 16.09.), an omp-only and a copy-only session, ``media/``,
    ``previews/``, the state mirror, the pointer, omp's lock dot-file, omp's
    artifact folder ``<ts>Z_<id>/`` and the MC-NAMED artifact folder of the
    mixed file (no ``Z``, live ``2026-09-16T01-06-13_<id>/``).
    A second folder holds one more copy; a dot folder at the root and a
    ``*.jsonl`` directly in the root stay out of scope."""
    ws = root / "--workspace--"
    other = root / "--workspace-other--"
    t = 1_700_000_000.0
    p: dict[str, Path] = {}
    p["omp_a"] = omp_file(ws, "sid-a", t + 10)
    p["mc_a"] = mc_copy(ws, "sid-a", t + 5)                      # older copy
    p["omp_b"] = omp_file(ws, "sid-b", t + 20, ts="2026-01-02T04-00-00-000Z")
    p["mc_b"] = mc_copy(ws, "sid-b", t + 25, ts="2026-01-02T04-00-00")  # younger
    p["omp_task"] = omp_file(ws, "sid-task", t + 30, ts="2026-01-02T05-00-00-000Z")
    p["mc_task"] = mc_copy(ws, "sid-task", t + 31, ts="2026-01-02T05-00-00",
                           task_id="0f0e0d0c-0000-4000-8000-000000000001")
    p["omp_mixed"] = omp_file(ws, "sid-mixed", t + 40, ts="2026-01-02T06-00-00-000Z")
    p["mc_mixed"] = mc_copy(ws, "sid-mixed", t + 41, ts="2026-01-02T06-00-00", extra=[
        {"type": "message", "id": "1a2b3c4d",
         "message": {"role": "assistant", "content": "omp wrote this here"}},
        {"type": "model_change", "id": "2b3c4d5e", "model": "some-model"},
    ])
    p["omp_only"] = omp_file(ws, "sid-omp-only", t + 50, ts="2026-01-02T07-00-00-000Z")
    p["mc_only"] = mc_copy(ws, "sid-copy-only", t + 60, ts="2026-01-02T08-00-00")
    p["media_1"] = ws / "media" / ("c" * 64 + ".png")
    p["media_2"] = ws / "media" / ("d" * 64 + ".webp")
    for key in ("media_1", "media_2"):
        p[key].parent.mkdir(parents=True, exist_ok=True)
        p[key].write_bytes(key.encode() * 10)
        os.utime(p[key], (t + 70, t + 70))
    p["media_tmp"] = ws / "media" / ".partial.tmp"
    p["media_tmp"].write_bytes(b"half")
    p["preview"] = _jsonl(ws / "previews" / f"{MC_TS}_sid-b_abc123.jsonl",
                          [{"type": "custom_message", "customType": "acp-preview"}], t + 80)
    p["state"] = ws / "acp-chat-state.json"
    p["state"].write_text(json.dumps({"sessionId": "sid-b"}), encoding="utf-8")
    p["pointer"] = ws / "acp-chat.json"
    p["pointer"].write_text(json.dumps({"sessionId": "sid-b"}), encoding="utf-8")
    p["omp_lock"] = ws / f".{OMP_TS}_sid-a.jsonl.lock.os"
    p["omp_lock"].write_text("pid", encoding="utf-8")
    p["omp_artifact"] = ws / f"{OMP_TS}_sid-a" / "1.bash.log"
    p["omp_artifact"].parent.mkdir()
    p["omp_artifact"].write_text("output", encoding="utf-8")
    p["mc_named_artifact"] = ws / "2026-01-02T06-00-00_sid-mixed" / "11.bash.log"
    p["mc_named_artifact"].parent.mkdir()
    p["mc_named_artifact"].write_text("output", encoding="utf-8")
    # A copy-shaped file INSIDE a subfolder: never touched (all subfolders).
    p["copy_in_subfolder"] = mc_copy(ws / "2026-01-02T06-00-00_sid-mixed", "sid-sub", t + 90)
    p["mc_other"] = mc_copy(other, "sid-other", t + 100)
    p["dot_folder_copy"] = mc_copy(root / ".cache", "sid-dot", t + 110)
    p["root_level"] = mc_copy(root, "sid-root", t + 120)
    return p


#: Copies a migration must move (key -> folder they end up in).
MOVED = ("mc_a", "mc_b", "mc_task", "mc_mixed", "mc_only", "mc_other")
META = (sink_migration.LIST_NAME, sink_migration.LOCK_NAME)


def tree_state(root: Path, *, skip_meta: bool = False) -> dict[str, object]:
    """Every file (content hash + mtime) and every directory under ``root``.
    ``skip_meta`` leaves out the list and the lock and all directories —
    what a rollback cannot (and need not) undo."""
    state: dict[str, object] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if not skip_meta:
            for d in dirnames:
                state[f"dir:{(rel_dir / d).as_posix()}"] = True
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if skip_meta and rel in META:
                continue
            full = Path(dirpath) / name
            state[rel] = (hashlib.sha256(full.read_bytes()).hexdigest(),
                          full.stat().st_mtime_ns)
    return state


def read_list(root: Path) -> dict:
    return json.loads((root / sink_migration.LIST_NAME).read_text(encoding="utf-8"))


def entries(report: "sink_migration.Report") -> list[tuple]:
    return sorted((e["from"], e["to"], e["sessionId"], e["task"], json.dumps(e["mixed"]))
                  for e in report.moved)


# ── 4. the migration moves exactly MC's copies ──────────────────────────────


def test_4_migrate_moves_only_mc_copies_and_keeps_everything_else(tmp_path):
    root = tmp_path / "sessions"
    p = build_tree(root)
    before = {k: (v.read_bytes(), v.stat().st_mtime_ns) for k, v in p.items()}

    report = sink_migration.migrate(root)

    assert report.status == "ok", report.detail
    ws = root / "--workspace--"
    for key in MOVED:
        src = p[key]
        dst = src.parent / ".mc" / src.name
        assert not src.exists(), f"{key} still next to omp's file"
        assert (dst.read_bytes(), dst.stat().st_mtime_ns) == before[key], key
    untouched = [k for k in p if k not in MOVED and not k.startswith("media_")
                 or k == "media_tmp"]
    for key in untouched:
        assert p[key].exists(), f"{key} was moved"
        assert (p[key].read_bytes(), p[key].stat().st_mtime_ns) == before[key], key
    # Pictures follow the copies: media/ -> .mc/media/, same name, same bytes.
    for key in ("media_1", "media_2"):
        dst = ws / ".mc" / "media" / p[key].name
        assert not p[key].exists() and dst.read_bytes() == before[key][0]
    assert not (ws / ".mc" / "previews").exists(), "previews/ stays where it is"
    # The list: one run, six moves, relative paths, flags right.
    listing = read_list(root)
    assert listing["version"] == 1 and len(listing["runs"]) == 1
    run = listing["runs"][0]
    assert run["action"] == "migrate" and run["status"] == "done"
    assert len(run["moved"]) == len(MOVED) == len(report.moved)
    assert sorted(e["sessionId"] for e in run["moved"]) == sorted(
        ["sid-a", "sid-b", "sid-task", "sid-mixed", "sid-copy-only", "sid-other"])
    assert [e["sessionId"] for e in run["moved"] if e["task"]] == ["sid-task"]
    mixed = [e for e in run["moved"] if e["mixed"]]
    assert [e["sessionId"] for e in mixed] == ["sid-mixed"]
    assert mixed[0]["mixed"] == {"foreignLines": 2, "types": ["message", "model_change"]}
    for e in run["moved"]:
        assert not Path(e["from"]).is_absolute() and not Path(e["to"]).is_absolute()
        assert Path(e["to"]).parent.name == ".mc"
        assert (root / e["to"]).is_file()
    assert sorted(Path(e["to"]).name for e in run["media"]) == sorted(
        [p["media_1"].name, p["media_2"].name])
    counts = report.counts()
    assert counts == {"moved": 6, "mixed": 1, "task": 1, "media": 2, "skipped": 0}
    assert report.omp_files == 5
    # omp's view afterwards: its own five sessions, no MC copy.
    listed = omp_listing_rule.omp_would_list(root)
    assert [x for x in listed if omp_listing_rule.is_mc_copy(x)] == []
    assert len(listed) == 5


# ── 5. idempotent ───────────────────────────────────────────────────────────


def test_5_a_second_run_has_nothing_to_do(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    sink_migration.migrate(root)
    after_first = tree_state(root)

    second = sink_migration.migrate(root)

    assert second.status == "ok"
    assert second.moved == [] and second.media == [] and second.skipped == []
    assert tree_state(root) == after_first, "a no-op run must not even touch the list"
    assert len(read_list(root)["runs"]) == 1


# ── 6. never overwrite ──────────────────────────────────────────────────────


def test_6_an_existing_target_is_skipped_with_a_reason(tmp_path):
    root = tmp_path / "sessions"
    p = build_tree(root)
    ws = root / "--workspace--"
    clash = ws / ".mc" / p["mc_a"].name
    clash.parent.mkdir()
    clash.write_text("already here", encoding="utf-8")
    picture_clash = ws / ".mc" / "media" / p["media_1"].name
    picture_clash.parent.mkdir()
    picture_clash.write_bytes(b"same name, kept")
    source_before = p["mc_a"].read_bytes()

    report = sink_migration.migrate(root)

    assert p["mc_a"].read_bytes() == source_before, "source must stay untouched"
    assert clash.read_text(encoding="utf-8") == "already here", "target overwritten"
    assert picture_clash.read_bytes() == b"same name, kept"
    assert p["media_1"].exists()
    reasons = {(e["path"], e["reason"]) for e in report.skipped}
    assert ("--workspace--/" + p["mc_a"].name, "target_exists") in reasons
    assert ("--workspace--/media/" + p["media_1"].name, "target_exists") in reasons
    assert len(report.moved) == len(MOVED) - 1
    assert read_list(root)["runs"][0]["skipped"] == report.skipped


def test_6b_a_target_that_appears_after_planning_is_not_overwritten(tmp_path, monkeypatch):
    """The move itself refuses too: a file that shows up in ``.mc/`` between
    planning and moving (a sink opening right then) is never replaced."""
    root = tmp_path / "sessions"
    p = build_tree(root)
    clash = root / "--workspace--" / ".mc" / p["mc_b"].name
    real_plan = sink_migration._plan

    def plan_then_clash(r):
        planned = real_plan(r)
        clash.parent.mkdir(exist_ok=True)
        clash.write_text("written meanwhile", encoding="utf-8")
        return planned

    monkeypatch.setattr(sink_migration, "_plan", plan_then_clash)
    report = sink_migration.migrate(root)

    assert clash.read_text(encoding="utf-8") == "written meanwhile"
    assert p["mc_b"].exists()
    assert {"path": "--workspace--/" + p["mc_b"].name, "reason": "target_exists"} in report.skipped
    run = read_list(root)["runs"][0]
    assert run["status"] == "done"
    assert "sid-b" not in [e["sessionId"] for e in run["moved"]]


# ── 7. a dry run writes nothing and says the same ───────────────────────────


def test_7_dry_run_changes_nothing_and_reports_like_the_real_run(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    before = tree_state(root)

    dry = sink_migration.migrate(root, dry_run=True)

    assert tree_state(root) == before, "the dry run wrote something"
    assert dry.status == "ok" and dry.dry_run is True
    real = sink_migration.migrate(root)
    assert entries(dry) == entries(real)
    assert sorted(map(json.dumps, dry.media)) == sorted(map(json.dumps, real.media))
    assert dry.skipped == real.skipped and dry.omp_files == real.omp_files
    assert dry.counts() == real.counts()


# ── 8. rollback ─────────────────────────────────────────────────────────────


def test_8_rollback_restores_the_tree_exactly(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    before = tree_state(root, skip_meta=True)
    sink_migration.migrate(root)
    assert tree_state(root, skip_meta=True) != before

    back = sink_migration.rollback(root)

    assert back.status == "ok", back.detail
    assert tree_state(root, skip_meta=True) == before
    runs = read_list(root)["runs"]
    assert [r["action"] for r in runs] == ["migrate", "rollback"]
    assert len(runs[1]["moved"]) == len(MOVED)
    # Undone is undone: a second rollback moves nothing.
    again = sink_migration.rollback(root)
    assert again.moved == [] and tree_state(root, skip_meta=True) == before


def test_8b_rollback_all_also_returns_copies_written_after_the_move(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    sink_migration.migrate(root)
    ws = root / "--workspace--"
    later = acp_chat_events.ChatEventSink(ws, "sid-later")   # new code -> .mc/
    later.write(['{"type": "message", "id": "acp00009"}'])
    assert later.path is not None and later.path.parent == ws / ".mc"
    later_picture = ws / ".mc" / "media" / ("e" * 64 + ".png")
    later_picture.write_bytes(b"later")
    omp_in_mc = omp_file(ws / ".mc", "sid-not-ours", 1_700_000_500.0)

    plain = sink_migration.rollback(root)
    assert later.path.exists(), "without --all only listed moves go back"
    assert later_picture.exists()

    full = sink_migration.rollback(root, all_mc=True)
    assert full.status == "ok"
    assert not later.path.exists() and (ws / later.path.name).is_file()
    assert (ws / "media" / later_picture.name).read_bytes() == b"later"
    assert omp_in_mc.exists(), "--all moves MC copies only"
    assert plain.moved and [e["sessionId"] for e in full.moved] == ["sid-later"]
    assert read_list(root)["runs"][-1]["all"] is True


# ── 9. a lock held by a live run ────────────────────────────────────────────


def _lock(root: Path, *, pid: int, age_s: float, host: str | None = None) -> Path:
    path = root / sink_migration.LOCK_NAME
    path.write_text(json.dumps({"pid": pid, "at": time.time() - age_s,
                                "host": host or socket.gethostname()}), encoding="utf-8")
    return path


def test_9_a_fresh_lock_of_a_live_process_stops_the_run(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    lock = _lock(root, pid=os.getppid(), age_s=5)
    before = tree_state(root)

    report = sink_migration.migrate(root)

    assert report.status == "locked"
    assert tree_state(root) == before, "a locked run changed the tree"
    assert lock.exists()
    assert sink_migration.rollback(root).status == "locked"
    assert sink_migration.migrate(root, dry_run=True).status == "locked"


# ── 10. omp's .bak trap ─────────────────────────────────────────────────────


def test_10_no_bak_name_anywhere_after_migrate_and_rollback(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    sink_migration.migrate(root)
    sink_migration.rollback(root, all_mc=True)
    sink_migration.migrate(root)
    names = [p.name for p in root.rglob("*")]
    assert [n for n in names if ".bak" in n] == [], names
    assert [n for n in names if n.endswith(".tmp") and n != ".partial.tmp"] == []


# ── 11. the daemon finds its moved copy through the old pointer ─────────────


def _daemon(tmp_path: Path, client: FakeClient) -> acp_chat.ChatSession:
    folder = tmp_path / "agent" / "sessions" / "--workspace--"
    folder.mkdir(parents=True, exist_ok=True)
    return acp_chat.ChatSession(
        client_factory=lambda: client, cwd="/workspace",
        state_dir=tmp_path / "state", sessions_dir=folder,
    )


def _pointer(tmp_path: Path, sid: str, transcript: Path) -> None:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "acp-chat.json").write_text(
        json.dumps({"sessionId": sid, "transcript": str(transcript)}), encoding="utf-8")


def _lines(path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x]


def _cards(path, custom: str) -> list[str]:
    return [(x.get("data") or {}).get("code") for x in _lines(path)
            if x.get("customType") == custom]


def test_11_start_follows_an_old_pointer_into_the_mc_folder(tmp_path):
    """The deploy case: the host run moved the copy, the pointer still names
    the old path. Before S1-R3 the pointer led nowhere and the daemon
    silently opened a new session."""
    folder = tmp_path / "agent" / "sessions" / "--workspace--"
    old_path = folder / f"{MC_TS}_sid-1.jsonl"
    moved = mc_copy(folder / ".mc", "sid-1", 1_700_000_000.0)
    assert moved.name == old_path.name
    _pointer(tmp_path, "sid-1", old_path)
    client = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-new"},
                        turns=[{"chunks": ["again"]}])
    sess = _daemon(tmp_path, client)

    sess.start()
    sess.prompt("after the deploy")
    assert sess.wait_idle(timeout=5)
    sess.close()

    assert ("load_session", "sid-1", "/workspace") in client.calls
    assert not [c for c in client.calls if c[0] == "new_session"]
    assert sess.transcript_path == moved
    assert not old_path.exists()
    texts = [(x.get("message") or {}).get("content") for x in _lines(moved)]
    assert "after the deploy" in json.dumps(texts)
    assert _cards(moved, acp_chat_events.CHAT_ERROR_CUSTOM_TYPE) == []
    pointer = json.loads((tmp_path / "state" / "acp-chat.json").read_text())
    assert pointer == {"sessionId": "sid-1", "transcript": str(moved)}


def test_11b_start_moves_a_copy_left_in_place_then_loads_it(tmp_path):
    """The safety net: a tree nobody moved yet is moved at start, and the
    pointer follows."""
    folder = tmp_path / "agent" / "sessions" / "--workspace--"
    legacy = mc_copy(folder, "sid-1", 1_700_000_000.0)
    omp_own = omp_file(folder, "sid-1", 1_699_999_000.0)
    _pointer(tmp_path, "sid-1", legacy)
    client = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-new"})
    sess = _daemon(tmp_path, client)

    sess.start()
    sess.close()

    assert sess.transcript_path == folder / ".mc" / legacy.name
    assert not legacy.exists() and omp_own.exists()
    assert ("load_session", "sid-1", "/workspace") in client.calls
    run = read_list(tmp_path / "agent" / "sessions")["runs"][0]
    assert run["runner"] == "daemon" and len(run["moved"]) == 1


# ── 14. stale locks are taken over; a fresh one is reported ─────────────────


def test_14_a_lock_older_than_ten_minutes_is_taken_over(tmp_path):
    root = tmp_path / "sessions"
    p = build_tree(root)
    _lock(root, pid=os.getppid(), age_s=sink_migration.LOCK_STALE_S + 60)

    report = sink_migration.migrate(root)

    assert report.status == "ok", report.detail
    assert not p["mc_a"].exists()
    assert not (root / sink_migration.LOCK_NAME).exists(), "the lock must be released"
    assert "stale" in report.detail


def test_14b_a_lock_of_a_dead_process_is_taken_over(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _lock(root, pid=dead.pid, age_s=1)
    assert sink_migration.migrate(root).status == "ok"


def test_14c_a_fresh_lock_from_another_host_waits_an_old_one_does_not(tmp_path):
    """Host and container share the tree but not the process table: a lock
    from the other side can only be judged by its age."""
    root = tmp_path / "sessions"
    build_tree(root)
    _lock(root, pid=999_999, age_s=5, host="another-machine")
    assert sink_migration.migrate(root).status == "locked"
    _lock(root, pid=999_999, age_s=sink_migration.LOCK_STALE_S + 1, host="another-machine")
    assert sink_migration.migrate(root).status == "ok"


def test_14d_an_unreadable_lock_is_judged_by_its_file_age(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    lock = root / sink_migration.LOCK_NAME
    lock.write_text("", encoding="utf-8")
    assert sink_migration.migrate(root).status == "locked"
    old = time.time() - sink_migration.LOCK_STALE_S - 60
    os.utime(lock, (old, old))
    assert sink_migration.migrate(root).status == "ok"


def test_14e_the_lock_names_pid_and_time_while_the_run_holds_it(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    build_tree(root)
    seen: list[dict] = []
    real_rename = os.rename

    def spying_rename(src, dst, *a, **kw):
        lock = root / sink_migration.LOCK_NAME
        if lock.exists() and not seen:
            seen.append(json.loads(lock.read_text(encoding="utf-8")))
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(sink_migration.os, "rename", spying_rename)
    sink_migration.migrate(root)
    assert seen and seen[0]["pid"] == os.getpid()
    assert abs(seen[0]["at"] - time.time()) < 60
    assert seen[0]["host"] == socket.gethostname()


def test_14f_daemon_start_under_a_fresh_lock_shows_a_notice_and_logs(tmp_path, caplog):
    root = tmp_path / "agent" / "sessions"
    folder = root / "--workspace--"
    legacy = mc_copy(folder, "sid-old", 1_700_000_000.0)
    _lock(root, pid=os.getppid(), age_s=5)
    client = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-1"})
    sess = _daemon(tmp_path, client)
    caplog.set_level(logging.WARNING, logger="acp_chat")

    sess.start()
    sess.close()

    assert legacy.exists(), "a locked run must not move anything"
    # S1-R4: no pointer, but an earlier chat copy in the tree -> the chat
    # also says that a new conversation started.
    assert _cards(sess.transcript_path, acp_chat_events.CHAT_NOTICE_CUSTOM_TYPE) == [
        "chat_started_fresh", "sink_migration_skipped"]
    assert any("sink migration" in r.getMessage() and "locked" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    assert sess.state()["sessionId"] == "sid-1", "the chat itself keeps working"


def test_14g_daemon_start_survives_a_failing_migration(tmp_path, monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("disk trouble")

    monkeypatch.setattr(sink_migration, "migrate", boom)
    client = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-1"})
    sess = _daemon(tmp_path, client)
    caplog.set_level(logging.WARNING, logger="acp_chat")
    sess.start()
    sess.close()
    assert _cards(sess.transcript_path, acp_chat_events.CHAT_NOTICE_CUSTOM_TYPE) == [
        "sink_migration_skipped"]
    assert any("disk trouble" in r.getMessage() for r in caplog.records)


def test_14h_a_clean_start_shows_no_notice(tmp_path):
    client = FakeClient(session_result={**SESSION_RESULT, "sessionId": "sid-1"})
    sess = _daemon(tmp_path, client)
    sess.start()
    sess.close()
    assert _cards(sess.transcript_path, acp_chat_events.CHAT_NOTICE_CUSTOM_TYPE) == []
    assert not (tmp_path / "agent" / "sessions" / sink_migration.LIST_NAME).exists()


# ── 15. every subfolder stays untouched ─────────────────────────────────────


def test_15_an_mc_named_artifact_folder_stays_where_it_is(tmp_path):
    """Live: the mixed file has an artifact folder with MC's name pattern
    (no ``Z``). It is omp's, not MC's — it must not move with the copy."""
    root = tmp_path / "sessions"
    p = build_tree(root)
    folder = p["mc_named_artifact"].parent
    inside = {x.name: (x.read_bytes(), x.stat().st_mtime_ns) for x in folder.iterdir()}

    sink_migration.migrate(root)

    assert folder.is_dir()
    assert {x.name: (x.read_bytes(), x.stat().st_mtime_ns) for x in folder.iterdir()} == inside
    assert not (folder.parent / ".mc" / folder.name).exists()
    assert p["omp_artifact"].exists() and p["copy_in_subfolder"].exists()


# ── 16. the list is no session for anybody ─────────────────────────────────


def test_16_the_list_and_the_lock_are_never_read_as_a_session(tmp_path):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.services import omp_chat  # the backend's own search patterns

    root = tmp_path / "sessions"
    build_tree(root)
    sink_migration.migrate(root)
    (root / sink_migration.LOCK_NAME).write_text("{}", encoding="utf-8")
    assert sink_migration.LIST_NAME.endswith(".json")
    found = {str(x.relative_to(root)) for pattern in omp_chat._SESSION_GLOBS
             for x in root.glob(pattern)}
    assert sink_migration.LIST_NAME not in found
    assert sink_migration.LOCK_NAME not in found
    assert not [x for x in omp_listing_rule.omp_would_list(root)
                if x.name.startswith(".mc-sink")]


# ── remap: an old pointer finds the moved file ──────────────────────────────


def test_remap_follows_a_move_in_both_directions(tmp_path):
    folder = tmp_path / "--workspace--"
    moved = mc_copy(folder / ".mc", "sid-1", 1_700_000_000.0)
    old = folder / moved.name
    assert sink_migration.remap(old) == moved
    assert sink_migration.remap(str(old)) == moved
    assert sink_migration.remap(moved) == moved, "an existing path stays"
    # After a rollback the pointer names .mc/ — the old place is found again.
    sink_migration.os.rename(moved, old)
    assert sink_migration.remap(moved) == old
    # Neither place: the path comes back unchanged (the caller resets).
    gone = folder / "2026-01-01T00-00-00_sid-gone.jsonl"
    assert sink_migration.remap(gone) == gone


def test_dirname_matches_the_bridge_constant():
    assert sink_migration.MC_SINK_DIRNAME == acp_chat_events.MC_SINK_DIRNAME == ".mc"


# ── the command line (host run in the deploy window) ────────────────────────


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=60)


def test_cli_dry_run_apply_rollback(tmp_path):
    root = tmp_path / "sessions"
    p = build_tree(root)
    before = tree_state(root)

    dry = _cli("--root", str(root), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert tree_state(root) == before
    assert "dry run" in dry.stdout and "moves: 6" in dry.stdout
    assert "mixed: 1" in dry.stdout and "task copies: 1" in dry.stdout

    applied = _cli("--root", str(root), "--apply")
    assert applied.returncode == 0, applied.stderr
    assert not p["mc_a"].exists() and "moves: 6" in applied.stdout
    assert read_list(root)["runs"][0]["runner"] == "cli"

    back = _cli("--root", str(root), "--rollback", "--all")
    assert back.returncode == 0, back.stderr
    assert p["mc_a"].exists()
    assert tree_state(root, skip_meta=True) == {
        k: v for k, v in before.items() if not k.startswith("dir:")}


def test_cli_refuses_unclear_calls_and_reports_a_lock(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    assert _cli("--root", str(root)).returncode == 2               # no action
    assert _cli("--root", str(root), "--dry-run", "--apply").returncode == 2
    assert _cli("--root", str(root), "--apply", "--all").returncode == 2
    assert _cli("--root", str(tmp_path / "missing"), "--dry-run").returncode == 1
    _lock(root, pid=os.getppid(), age_s=5)
    locked = _cli("--root", str(root), "--apply")
    assert locked.returncode == 3 and "locked" in locked.stdout


def test_cli_json_report(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    out = _cli("--root", str(root), "--dry-run", "--json")
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["status"] == "ok" and report["dryRun"] is True
    assert report["counts"] == {"moved": 6, "mixed": 1, "task": 1, "media": 2, "skipped": 0}


@pytest.mark.parametrize("bad", ["not json", "[]", '{"version": 1, "runs": "x"}'])
def test_a_broken_list_is_kept_aside_never_overwritten(tmp_path, bad):
    """The list is the only record of what moved. If it cannot be read, the
    run refuses instead of starting a new list over it."""
    root = tmp_path / "sessions"
    p = build_tree(root)
    (root / sink_migration.LIST_NAME).write_text(bad, encoding="utf-8")
    report = sink_migration.migrate(root)
    assert report.status == "error" and "list" in report.detail
    assert p["mc_a"].exists()
    assert (root / sink_migration.LIST_NAME).read_text(encoding="utf-8") == bad


# ── review: the move never leaves the root ──────────────────────────────────
#
# The session folders are the agent's own bind mount: from inside its
# container the agent can plant a symlink there. The operator runs the move
# on the HOST, with the host's rights — a followed symlink would move MC
# copies (or files the agent crafted with MC's header) into any host folder,
# and a crafted list would let ``rollback`` pull host files into the tree.


def _outside(tmp_path: Path) -> Path:
    """A folder next to the sessions root, never inside it."""
    target = tmp_path / "elsewhere" / "--workspace--"
    target.mkdir(parents=True)
    return target


def _files_under(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*"))


def test_17_a_symlinked_mc_folder_sends_nothing_out_of_the_root(tmp_path):
    root = tmp_path / "sessions"
    ws = root / "--workspace--"
    real = mc_copy(ws, "sid-x1", 1_700_000_000.0, ts="2026-01-01T00-00-00")
    planted = mc_copy(ws, "sid-inj", 1_700_000_001.0, ts="2026-01-01T00-00-01")
    outside = _outside(tmp_path)
    os.symlink("../../elsewhere/--workspace--", ws / ".mc")
    assert (ws / ".mc").resolve() == outside.resolve()

    report = sink_migration.migrate(root)

    assert _files_under(outside) == [], "a copy left the sessions root"
    assert real.exists() and planted.exists()
    assert report.moved == []
    assert {"path": "--workspace--/.mc", "reason": "unsafe_target"} in report.skipped
    # The host run in the deploy window: same answer, and it says so.
    applied = _cli("--root", str(root), "--apply")
    assert _files_under(outside) == []
    assert "unsafe_target" in applied.stdout
    assert "WARNING" in applied.stdout.splitlines()[2], applied.stdout


def test_17b_a_symlinked_media_folder_keeps_the_pictures_home(tmp_path):
    root = tmp_path / "sessions"
    ws = root / "--workspace--"
    copy = mc_copy(ws, "sid-1", 1_700_000_000.0)
    picture = ws / "media" / ("c" * 64 + ".png")
    picture.parent.mkdir()
    picture.write_bytes(b"png")
    outside = _outside(tmp_path)
    (ws / ".mc").mkdir()
    os.symlink(str(outside), ws / ".mc" / "media")

    report = sink_migration.migrate(root)

    assert _files_under(outside) == [], "a picture left the sessions root"
    assert picture.exists()
    assert not copy.exists() and (ws / ".mc" / copy.name).is_file(), \
        "the copy itself still moves into the real .mc/"
    assert {"path": "--workspace--/.mc/media", "reason": "unsafe_target"} in report.skipped


def test_17c_a_file_in_place_of_the_mc_folder_skips_the_folder(tmp_path):
    root = tmp_path / "sessions"
    ws = root / "--workspace--"
    copy = mc_copy(ws, "sid-1", 1_700_000_000.0)
    (ws / ".mc").write_text("not a folder", encoding="utf-8")

    report = sink_migration.migrate(root)

    assert report.status == "ok" and copy.exists()
    assert {"path": "--workspace--/.mc", "reason": "unsafe_target"} in report.skipped


def test_17d_a_folder_turned_symlink_after_planning_is_refused_at_the_move(
        tmp_path, monkeypatch):
    """The move itself checks again: every folder between the root and the
    target must be a real folder at the moment of the rename."""
    root = tmp_path / "sessions"
    ws = root / "--workspace--"
    copy = mc_copy(ws, "sid-1", 1_700_000_000.0)
    outside = _outside(tmp_path)
    real_plan = sink_migration._plan

    def plan_then_swap(r):
        planned = real_plan(r)
        os.symlink(str(outside), ws / ".mc")
        return planned

    monkeypatch.setattr(sink_migration, "_plan", plan_then_swap)
    report = sink_migration.migrate(root)

    assert _files_under(outside) == []
    assert copy.exists()
    assert {"path": "--workspace--/" + copy.name, "reason": "unsafe_target"} in report.skipped


def _crafted_list(root: Path, moved: list[dict]) -> None:
    (root / sink_migration.LIST_NAME).write_text(json.dumps({
        "version": 1, "runs": [{"action": "migrate", "status": "done",
                                "moved": moved, "media": []}]}), encoding="utf-8")


def test_17e_a_crafted_list_cannot_pull_host_files_in_or_push_copies_out(tmp_path):
    root = tmp_path / "sessions"
    ws = root / "--workspace--"
    ws.mkdir(parents=True)
    outside = _outside(tmp_path)
    secret = outside / "secret.txt"
    secret.write_text("host only", encoding="utf-8")
    os.symlink(str(outside), ws / "link")
    copy = mc_copy(ws / ".mc", "sid-1", 1_700_000_000.0)
    _crafted_list(root, [
        # in: "move back" a host file through the symlink into the tree
        {"from": "--workspace--/stolen.txt", "to": "--workspace--/link/secret.txt"},
        # out: "move back" a copy through the symlink out of the tree
        {"from": "--workspace--/link/" + copy.name, "to": "--workspace--/.mc/" + copy.name},
    ])

    report = sink_migration.rollback(root)

    assert secret.read_text(encoding="utf-8") == "host only"
    assert not (ws / "stolen.txt").exists()
    assert copy.exists() and _files_under(outside) == ["secret.txt"]
    reasons = {(e["path"], e["reason"]) for e in report.skipped}
    assert ("--workspace--/link/secret.txt", "unsafe_target") in reasons
    assert ("--workspace--/.mc/" + copy.name, "unsafe_target") in reasons


def test_17f_rollback_all_never_follows_a_symlinked_media_folder(tmp_path):
    root = tmp_path / "sessions"
    ws = root / "--workspace--"
    picture = ws / ".mc" / "media" / ("e" * 64 + ".png")
    picture.parent.mkdir(parents=True)
    picture.write_bytes(b"png")
    mc_copy(ws / ".mc", "sid-1", 1_700_000_000.0)
    outside = _outside(tmp_path)
    os.symlink(str(outside), ws / "media")

    report = sink_migration.rollback(root, all_mc=True)

    assert _files_under(outside) == []
    assert picture.exists()
    assert ("--workspace--/.mc/media/" + picture.name, "unsafe_target") in {
        (e["path"], e["reason"]) for e in report.skipped}


def test_17g_the_list_never_writes_through_a_planted_temp_name(tmp_path):
    """The list is rewritten through a temp file at the root. A symlink the
    agent planted under a guessable temp name must not be followed."""
    root = tmp_path / "sessions"
    build_tree(root)
    outside = _outside(tmp_path)
    victim = outside / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    for name in (f"{sink_migration.LIST_NAME}.{os.getpid()}.tmp",
                 f"{sink_migration.LIST_NAME}.tmp"):
        os.symlink(str(victim), root / name)

    report = sink_migration.migrate(root)

    assert report.status == "ok", report.detail
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert len(read_list(root)["runs"]) == 1


def test_17h_a_symlinked_list_is_refused_like_a_broken_one(tmp_path):
    root = tmp_path / "sessions"
    p = build_tree(root)
    outside = _outside(tmp_path)
    foreign = outside / "foreign.json"
    foreign.write_text(json.dumps({"version": 1, "runs": []}), encoding="utf-8")
    os.symlink(str(foreign), root / sink_migration.LIST_NAME)

    report = sink_migration.migrate(root)

    assert report.status == "error" and "list" in report.detail
    assert p["mc_a"].exists()
    assert json.loads(foreign.read_text(encoding="utf-8")) == {"version": 1, "runs": []}


# ── review: nothing named like a session can hang the move ──────────────────


def _returns_within(fn, seconds: float = 2.0):
    """Run ``fn`` in a thread; fail when it is still running after
    ``seconds`` (a blocking open of a FIFO never returns)."""
    import threading

    box: dict = {}

    def run():
        box["value"] = fn()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{fn} still blocked after {seconds} s"
    return box.get("value")


def _unblock(path: Path) -> None:
    """Open the FIFO's write end once so a reader stuck in open() returns
    (keeps a failing test from leaving a thread behind)."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_18_a_fifo_lock_or_list_never_hangs_the_run(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    lock = root / sink_migration.LOCK_NAME
    os.mkfifo(lock)
    try:
        report = _returns_within(lambda: sink_migration.migrate(root))
        assert report.status in ("locked", "ok")
    finally:
        _unblock(lock)
    if lock.exists():
        lock.unlink()
    listing = root / sink_migration.LIST_NAME
    if listing.exists():
        listing.unlink()
    os.mkfifo(listing)
    try:
        report = _returns_within(lambda: sink_migration.migrate(root, dry_run=True))
        assert report.status == "error"
    finally:
        _unblock(listing)


# ── review: the way back works with a damaged list ──────────────────────────


@pytest.mark.parametrize("bad", ["{broken", "[]"])
def test_19_rollback_all_works_without_a_readable_list(tmp_path, bad):
    """``--rollback --all`` needs no list: it finds MC's copies in ``.mc/``
    by their header. A damaged list must not block the way back; it stays
    as it is, and the run goes into a fresh list beside it."""
    root = tmp_path / "sessions"
    build_tree(root)
    before = tree_state(root, skip_meta=True)
    sink_migration.migrate(root)
    (root / sink_migration.LIST_NAME).write_text(bad, encoding="utf-8")

    assert sink_migration.rollback(root).status == "error", \
        "without --all the list is needed"
    back = sink_migration.rollback(root, all_mc=True)

    assert back.status == "ok", back.detail
    assert {k: v for k, v in tree_state(root, skip_meta=True).items()
            if not k.startswith(".mc-sink-migration.")} == before
    assert (root / sink_migration.LIST_NAME).read_text(encoding="utf-8") == bad
    assert {"path": sink_migration.LIST_NAME, "reason": "list_unreadable"} in back.skipped
    fresh = [p for p in root.iterdir()
             if p.name.startswith(".mc-sink-migration.") and p.name.endswith(".json")
             and p.name != sink_migration.LIST_NAME]
    assert len(fresh) == 1 and fresh[0].name in back.detail
    runs = json.loads(fresh[0].read_text(encoding="utf-8"))["runs"]
    assert [r["action"] for r in runs] == ["rollback"] and runs[0]["all"] is True
    assert len(runs[0]["moved"]) == len(MOVED)
    # Never a session for the backend or omp either.
    assert not fresh[0].name.endswith(".jsonl")
    # A new move still refuses until the operator has looked at the list.
    assert sink_migration.migrate(root).status == "error"


# ── review: a lock from the future, a lock that cannot be made ──────────────


def test_20_a_lock_dated_in_the_future_does_not_hold_forever(tmp_path):
    """A lock time ten days ahead (a clock jump, garbage) is no time: the
    lock is judged by its file time — at most ten minutes, never ten days."""
    root = tmp_path / "sessions"
    p = build_tree(root)
    lock = _lock(root, pid=1, age_s=-10 * 86400, host="another-machine")

    fresh = sink_migration.migrate(root)
    assert fresh.status == "locked" and "future" in fresh.detail, \
        "a lock file written just now still guards a possibly live run"
    old = time.time() - sink_migration.LOCK_STALE_S - 60
    os.utime(lock, (old, old))
    report = sink_migration.migrate(root)

    assert report.status == "ok", report.detail
    assert not p["mc_a"].exists()
    assert "future" in report.detail


def test_20b_a_contentless_lock_with_a_future_mtime_is_stale(tmp_path):
    root = tmp_path / "sessions"
    build_tree(root)
    lock = root / sink_migration.LOCK_NAME
    lock.write_text("", encoding="utf-8")
    future = time.time() + 10 * 86400
    os.utime(lock, (future, future))
    assert sink_migration.migrate(root).status == "ok"


def test_20c_a_lock_within_the_clock_slack_still_holds(tmp_path):
    """Host and container clocks may differ by a few seconds — a lock a
    little in the future is a live run, not garbage."""
    root = tmp_path / "sessions"
    build_tree(root)
    _lock(root, pid=999_999, age_s=-5, host="another-machine")
    assert sink_migration.migrate(root).status == "locked"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores folder permissions")
def test_20d_a_lock_that_cannot_be_created_is_an_error_not_locked(tmp_path):
    root = tmp_path / "sessions"
    p = build_tree(root)
    root.chmod(0o555)
    try:
        report = sink_migration.migrate(root)
        cli = _cli("--root", str(root), "--apply")
    finally:
        root.chmod(0o755)
    assert report.status == "error", (report.status, report.detail)
    assert "lock" in report.detail
    assert cli.returncode == 1 and "status: error" in cli.stdout
    assert p["mc_a"].exists()


# ── review: what the list records ───────────────────────────────────────────


def test_21_the_list_records_moves_and_command_line_skips_only(tmp_path):
    """The rule the spec states: a run that moved something is recorded; a
    command-line run that only skipped something too (a refused move stays
    on record); a run with nothing to do, or a daemon run that only
    skipped, is not (it would repeat at every start)."""
    root = tmp_path / "sessions"
    p = build_tree(root)
    sink_migration.migrate(root, runner="cli")
    assert len(read_list(root)["runs"]) == 1
    assert _cli("--root", str(root), "--apply").returncode == 0
    assert len(read_list(root)["runs"]) == 1, "nothing to do: not recorded"
    clash = mc_copy(root / "--workspace--", "sid-a", 1_700_000_900.0)  # same name again
    assert clash.name == p["mc_a"].name
    sink_migration.migrate(root, runner="daemon")
    assert len(read_list(root)["runs"]) == 1, "a daemon skip repeats at every start"
    sink_migration.migrate(root, runner="cli")
    runs = read_list(root)["runs"]
    assert len(runs) == 2 and runs[1]["skipped"] == [
        {"path": "--workspace--/" + clash.name, "reason": "target_exists"}]


def test_21b_a_daemon_start_with_nothing_to_move_writes_nothing_at_the_root(
        tmp_path, monkeypatch):
    """Every chat-daemon start runs the safety net. With nothing to move it
    must not create (and remove) the lock at every start."""
    root = tmp_path / "agent" / "sessions"
    folder = root / "--workspace--"
    mc_copy(folder / ".mc", "sid-done", 1_700_000_000.0)   # moved long ago
    omp_file(folder, "sid-done", 1_700_000_000.0)
    taken: list = []
    real_acquire = sink_migration._acquire
    monkeypatch.setattr(sink_migration, "_acquire",
                        lambda *a, **kw: taken.append(a) or real_acquire(*a, **kw))
    report = sink_migration.migrate(root, runner="daemon")
    assert report.status == "ok" and taken == []
    assert sorted(p.name for p in root.iterdir()) == ["--workspace--"]
    # A real move still takes the lock.
    mc_copy(folder, "sid-new", 1_700_000_100.0)
    assert sink_migration.migrate(root, runner="daemon").counts()["moved"] == 1
    assert len(taken) == 1


# ── review: one header rule, not two ────────────────────────────────────────


def test_22_the_move_uses_the_bridge_header_rule_itself():
    """No second copy of omp's header rule in production code: the move
    imports it from ``acp_chat_events`` (tests/omp_listing_rule.py stays the
    independent twin)."""
    assert sink_migration.session_header is acp_chat_events.session_header
    assert sink_migration.is_mc_header is acp_chat_events.is_mc_header
    assert sink_migration.MC_SINK_DIRNAME is acp_chat_events.MC_SINK_DIRNAME
    source = SCRIPT.read_text(encoding="utf-8")
    assert "def session_header" not in source and "_HEADER_BYTES" not in source


# ── review: pictures move only next to MC copies ────────────────────────────


def test_23_media_moves_only_in_folders_that_hold_an_mc_copy(tmp_path):
    """A wrong ``--root`` (one level too high) must not move unrelated
    ``media/`` folders: pictures follow MC's copies, nothing else."""
    root = tmp_path / "sessions"
    lonely = root / "unrelated" / "media" / ("a" * 64 + ".png")
    omp_only = root / "--omp-only--" / "media" / ("b" * 64 + ".png")
    for picture in (lonely, omp_only):
        picture.parent.mkdir(parents=True)
        picture.write_bytes(b"png")
    omp_file(root / "--omp-only--", "sid-omp", 1_700_000_000.0)
    # Copies moved by an earlier run, a picture written after it: it follows.
    late = root / "--moved-before--" / "media" / ("c" * 64 + ".png")
    mc_copy(root / "--moved-before--" / ".mc", "sid-old", 1_700_000_000.0)
    late.parent.mkdir(parents=True)
    late.write_bytes(b"png")

    report = sink_migration.migrate(root)

    assert lonely.exists() and omp_only.exists()
    assert not late.exists()
    assert [e["from"] for e in report.media] == ["--moved-before--/media/" + late.name]
