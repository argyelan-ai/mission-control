#!/usr/bin/env python3
"""S1-R4 — the pointer to the open chat survives a recreate.

The chat daemon remembers its ACP session in a pointer `acp-chat.json`
(`{"sessionId", "transcript"}`). Before S1-R4 the container kept it in
`$OMP_HOME` — not mounted, so every recreate (each new image) threw it away
and the chat silently started over (live 20.10.2026). The state mirror
`acp-chat-state.json` names the same session and always sat on the mount.

Contract (docs/specs/chat-over-acp.md, "What survives what"):

* `build_session()` (container) keeps the pointer in the sessions folder,
  next to the state mirror, on the mount. Hermes keeps its own state_dir.
* `_read_persisted()` is a chain; the first source naming a session id AND
  a transcript wins: (1) the pointer, (2) the state mirror — the way over
  the deploy, because the old image already wrote it. The old place
  `$OMP_HOME/acp-chat.json` is never read again (a new image always means
  a recreate, so it is empty there; a stale one must never win).
* The pointer is written BEFORE the state mirror names a new session, so
  the first source of the chain is never older than the second.
* Unknown pointer fields survive a rewrite.
* Visible instead of silent: pointer whose transcript is gone (also after
  `sink_migration.remap`) -> `session_reset` / `transcript_missing`; omp
  refuses the load because the session's model is not configured ("Could
  not restore model X", omp >= 18.6.3 under ACP) -> `session_reset` /
  `model_missing`, naming the model; no pointer but an earlier chat copy
  -> `chat_notice` `chat_started_fresh`. A brand-new agent, or one with
  only task copies, gets no card.

Every assertion reads what the backend reads: the pointer and state files,
and the JSONL transcript the chat view tails.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_events  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import FakeClient, SESSION_RESULT, entries_of_type, read_lines  # noqa: E402

CWD = "/workspace"
FOLDER = "--workspace--"  # acp_chat_events._encode_cwd("/workspace")
S = "01a1-current"
OLD = "01a0-older"
TS = "2026-10-20T03-36-53"
MODEL = "model-a"  # offered by SESSION_RESULT, so _pin_model sets it

FRESH_TEXT = (
    "The chat could not tell which conversation was open — a new one "
    "started. Earlier conversations are kept."
)


# ── a sessions tree as the container sees it ────────────────────────────────


def layout(tmp: Path) -> tuple[Path, Path]:
    """`<root>/--workspace--/`: the sessions root (the mount) and the chat's
    session folder."""
    root = tmp / "omp-sessions"
    folder = root / FOLDER
    folder.mkdir(parents=True)
    return root, folder


def container_session(folder: Path, *clients, **kw) -> acp_chat.ChatSession:
    """A ChatSession wired like build_session() after S1-R4: the pointer
    lives in the sessions folder. Each `_connect` takes the next client."""
    queue = list(clients)
    kw.setdefault("model", MODEL)
    return acp_chat.ChatSession(
        client_factory=lambda: queue.pop(0),
        cwd=CWD,
        state_dir=folder,
        sessions_dir=folder,
        **kw,
    )


def mc_copy(folder: Path, sid: str, *, task_id: str = "", in_mc: bool = True,
            ts: str = TS) -> Path:
    """MC's display copy as ChatEventSink writes it (header + one line)."""
    place = folder / acp_chat_events.MC_SINK_DIRNAME if in_mc else folder
    place.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "version": 3, "id": sid,
              "timestamp": "2026-10-20T03:36:53Z", "cwd": "", "bridge": "acp"}
    if task_id:
        header["taskId"] = task_id
    path = place / f"{ts}_{sid}.jsonl"
    path.write_text(
        json.dumps(header) + "\n"
        + json.dumps({"type": "message", "id": "u1",
                      "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}})
        + "\n",
        encoding="utf-8",
    )
    return path


def omp_file(folder: Path, sid: str) -> Path:
    """omp's own session file: no bridge key, a real cwd."""
    path = folder / f"{TS}-427Z_{sid}.jsonl"
    path.write_text(json.dumps({"type": "session", "version": 3, "id": sid,
                                "timestamp": "2026-10-20T03:36:53Z", "cwd": CWD}) + "\n",
                    encoding="utf-8")
    return path


def write_pointer(folder: Path, sid: str, transcript, **extra) -> Path:
    path = folder / "acp-chat.json"
    path.write_text(json.dumps({"sessionId": sid, "transcript": str(transcript), **extra}),
                    encoding="utf-8")
    return path


def write_state(folder: Path, sid, transcript) -> Path:
    """The state mirror as a daemon leaves it (an older image too)."""
    path = folder / "acp-chat-state.json"
    path.write_text(json.dumps({
        "version": 1, "driver": "omp", "sessionId": sid, "busy": False, "turn": 4,
        "transcript": None if transcript is None else str(transcript),
        "configOptions": [], "commands": [], "queue": [], "elicitations": [],
    }), encoding="utf-8")
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def loads(client: FakeClient) -> list[str]:
    return [c[1] for c in client.calls if c[0] == "load_session"]


def news(client: FakeClient) -> int:
    return sum(1 for c in client.calls if c[0] == "new_session")


def cards(path) -> tuple[list[dict], list[dict]]:
    """(chat_error lines, chat_notice lines) of one transcript."""
    lines = read_lines(path)
    return (entries_of_type(lines, "custom_message", "chat_error"),
            entries_of_type(lines, "custom_message", "chat_notice"))


def codes(entries: list[dict]) -> list[tuple]:
    return [((e.get("data") or {}).get("code"), (e.get("data") or {}).get("detail"))
            for e in entries]


def client(**kw) -> FakeClient:
    kw.setdefault("session_result", dict(SESSION_RESULT, sessionId="sid-new"))
    return FakeClient(**kw)


# ── 1. the recreate: no pointer anywhere, the state mirror names S ──────────


@pytest.mark.parametrize("moved", [True, False], ids=["copy-in-.mc", "copy-at-old-place"])
def test_recreate_resumes_the_session_the_state_mirror_names(tmp_path, moved):
    """The deploy itself: the old image kept its pointer in $OMP_HOME (gone
    with the container) and its copy next to omp's file; the state mirror
    on the mount names both. The new daemon moves the copy (S1-R3), follows
    the state mirror, remaps the old path and LOADS — no new session, no
    card, and the chat keeps growing in the same file."""
    _, folder = layout(tmp_path)
    copy = mc_copy(folder, S, in_mc=moved)
    omp_file(folder, S)
    write_state(folder, S, copy)  # pre-move path when not moved
    c = client()
    sess = container_session(folder, c)
    try:
        sess.start()
        assert loads(c) == [S]
        assert news(c) == 0
        target = folder / ".mc" / copy.name
        assert sess.transcript_path == target
        errors, notices = cards(target)
        assert errors == [] and notices == []
        assert sess._resume_source == "state"
        # From now on the pointer itself is on the mount.
        pointer = read_json(folder / "acp-chat.json")
        assert pointer["sessionId"] == S
        assert pointer["transcript"] == str(target)
    finally:
        sess.close()


def test_the_pointer_wins_over_the_state_mirror(tmp_path):
    """Source 1 of the chain: a pointer with id and transcript is taken
    even when the state mirror names another session."""
    _, folder = layout(tmp_path)
    write_pointer(folder, S, mc_copy(folder, S))
    write_state(folder, OLD, mc_copy(folder, OLD, ts="2026-10-19T01-00-00"))
    c = client()
    sess = container_session(folder, c)
    try:
        sess.start()
        assert loads(c) == [S]
        assert sess._resume_source == "pointer"
    finally:
        sess.close()


@pytest.mark.parametrize("pointer", [
    {"sessionId": S},                        # no transcript
    {"transcript": "x.jsonl"},               # no session id
    {"sessionId": "", "transcript": ""},     # empty
    "not json",                              # broken file
])
def test_a_pointer_without_id_and_transcript_falls_to_the_state_mirror(tmp_path, pointer):
    _, folder = layout(tmp_path)
    path = folder / "acp-chat.json"
    path.write_text(pointer if isinstance(pointer, str) else json.dumps(pointer),
                    encoding="utf-8")
    write_state(folder, OLD, mc_copy(folder, OLD))
    c = client()
    sess = container_session(folder, c)
    try:
        sess.start()
        assert loads(c) == [OLD]
        assert sess._resume_source == "state"
    finally:
        sess.close()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_in_place_of_the_pointer_never_stalls_the_start(tmp_path):
    """The pointer now sits in the agent's own mount: a FIFO named like it
    must not hang the chat's start — it counts as no pointer."""
    import threading

    _, folder = layout(tmp_path)
    os.mkfifo(folder / "acp-chat.json")
    write_state(folder, S, mc_copy(folder, S))
    c = client()
    sess = container_session(folder, c)
    worker = threading.Thread(target=sess.start, daemon=True)
    worker.start()
    worker.join(timeout=5)
    try:
        assert not worker.is_alive(), "start() hung on a FIFO pointer"
        assert loads(c) == [S]
        assert sess._resume_source == "state"
    finally:
        sess.close()


# ── 2. + 3. production wiring: the pointer is on the mount ──────────────────


@pytest.fixture
def container_env(tmp_path, monkeypatch):
    """The image's environment: $OMP_HOME (NOT mounted) and the profile
    tree whose sessions folder IS the mount."""
    home = tmp_path / "omp-home"
    home.mkdir()
    monkeypatch.setenv("OMP_HOME", str(home))
    monkeypatch.setenv("OMP_PROFILE", "mc-agent")
    monkeypatch.setenv("OMP_ACP_CWD", CWD)
    monkeypatch.setenv("OMP_ACP_MODEL", MODEL)
    monkeypatch.delenv("OMP_ENV_FILE", raising=False)
    folder = acp_chat_events.session_dir(cwd=CWD)
    assert folder == home / "profiles" / "mc-agent" / "agent" / "sessions" / FOLDER
    return home, folder


def built(*clients) -> acp_chat.ChatSession:
    sess = acp_chat.build_session()
    queue = list(clients)
    sess._client_factory = lambda: queue.pop(0)
    return sess


def test_every_session_switch_writes_the_pointer_on_the_mount(container_env):
    """start, /new and a reset after a failed load: each writes the pointer
    into the sessions folder (the mount), never into $OMP_HOME."""
    home, folder = container_env
    first = client(new_ids=["sid-A", "sid-B"])
    sess = built(first)
    assert sess.persist_file == folder / "acp-chat.json"
    try:
        sess.start()
        assert read_json(folder / "acp-chat.json")["sessionId"] == "sid-A"
        assert sess.new_session()["ok"]
        assert sess.wait_idle(5)
        assert read_json(folder / "acp-chat.json")["sessionId"] == "sid-B"
    finally:
        sess.close()
    # The next daemon cannot load sid-B -> reset -> new pointer, same place.
    second = client(new_ids=["sid-C"],
                    load_raises=acp_client.ACPError("session/load failed: load was cancelled"))
    sess = built(second)
    try:
        sess.start()
        assert loads(second) == ["sid-B"]
        pointer = read_json(folder / "acp-chat.json")
        assert pointer["sessionId"] == "sid-C"
        assert pointer["transcript"] == str(sess.transcript_path)
    finally:
        sess.close()
    assert not (home / "acp-chat.json").exists()


def test_an_old_pointer_in_omp_home_is_never_read(container_env):
    """F11: the old place is only ever stale after the deploy — the state
    mirror on the mount wins."""
    home, folder = container_env
    write_pointer(home, OLD, mc_copy(folder, OLD, ts="2026-10-19T01-00-00"))
    write_state(folder, S, mc_copy(folder, S))
    c = client()
    sess = built(c)
    try:
        sess.start()
        assert loads(c) == [S]
        assert sess._resume_source == "state"
    finally:
        sess.close()


def test_an_old_pointer_alone_is_no_pointer(container_env):
    """Only the old place names a session: it is not loaded. The chat
    starts fresh and says so (the tree holds an earlier chat copy)."""
    home, folder = container_env
    write_pointer(home, OLD, mc_copy(folder, OLD))
    c = client()
    sess = built(c)
    try:
        sess.start()
        assert loads(c) == []
        assert news(c) == 1
        assert sess._resume_source == "none"
        errors, notices = cards(sess.transcript_path)
        assert errors == []
        assert codes(notices) == [("chat_started_fresh", None)]
    finally:
        sess.close()


# ── 4. pointer there, transcript gone ───────────────────────────────────────


@pytest.mark.parametrize("source", ["pointer", "state"])
def test_a_pointer_whose_transcript_is_gone_is_one_visible_reset(tmp_path, source):
    """Not at the named place, not in `.mc/` (remap): no load, a new
    session and ONE card — before S1-R4 the chat started over silently."""
    _, folder = layout(tmp_path)
    gone = folder / f"{TS}_{S}.jsonl"
    (write_pointer if source == "pointer" else write_state)(folder, S, gone)
    c = client(new_ids=["sid-new"])
    sess = container_session(folder, c)
    try:
        sess.start()
        assert loads(c) == []
        assert news(c) == 1
        errors, notices = cards(sess.transcript_path)
        assert codes(errors) == [("session_reset", "transcript_missing")]
        assert notices == []
        assert read_json(folder / "acp-chat.json")["sessionId"] == "sid-new"
    finally:
        sess.close()


# ── 5. + 6. + 8. no pointer at all ──────────────────────────────────────────


@pytest.mark.parametrize("where", ["own-folder-.mc", "own-folder-old-place", "other-folder"])
def test_no_pointer_but_an_earlier_chat_copy_says_a_new_chat_started(tmp_path, where):
    root, folder = layout(tmp_path)
    if where == "other-folder":
        mc_copy(root / "--workspace-repo--", OLD)
    else:
        mc_copy(folder, OLD, in_mc=(where == "own-folder-.mc"))
    c = client(new_ids=["sid-new"])
    sess = container_session(folder, c)
    try:
        sess.start()
        assert loads(c) == []
        errors, notices = cards(sess.transcript_path)
        assert errors == []
        assert codes(notices) == [("chat_started_fresh", None)]
        assert notices[0]["content"] == FRESH_TEXT
    finally:
        sess.close()


@pytest.mark.parametrize("tree", ["empty", "only-omp-files"])
def test_no_pointer_and_no_earlier_chat_shows_no_card(tmp_path, tree):
    """A brand-new agent (or one whose only sessions are omp's own, e.g.
    from the native console) — no noise."""
    _, folder = layout(tmp_path)
    if tree == "only-omp-files":
        omp_file(folder, OLD)
    c = client(new_ids=["sid-new"])
    sess = container_session(folder, c)
    try:
        sess.start()
        errors, notices = cards(sess.transcript_path)
        assert errors == [] and notices == []
    finally:
        sess.close()


def test_only_task_copies_and_no_pointer_shows_no_card(tmp_path):
    """A task's copy (header `taskId`) is no earlier chat."""
    root, folder = layout(tmp_path)
    mc_copy(folder, "task-1", task_id="t-1")
    mc_copy(folder, "task-2", task_id="t-2", in_mc=False)
    mc_copy(root / "--workspace-repo--", "task-3", task_id="t-3")
    c = client(new_ids=["sid-new"])
    sess = container_session(folder, c)
    try:
        sess.start()
        errors, notices = cards(sess.transcript_path)
        assert errors == [] and notices == []
    finally:
        sess.close()


# ── 7. the pointer before the state mirror ──────────────────────────────────


def test_the_pointer_names_a_new_session_before_the_state_mirror(tmp_path, monkeypatch):
    """The chain reads the pointer first, so it must never be OLDER than the
    state mirror: for every new session (start, /new, reset) the first of
    the two files that names it is `acp-chat.json`. The model pin writes
    the state mirror too — it must come after the pointer."""
    _, folder = layout(tmp_path)
    write_pointer(folder, OLD, mc_copy(folder, OLD))
    seen: list[tuple[str, object]] = []
    real_replace = os.replace

    def spy(src, dst):
        real_replace(src, dst)
        name = Path(dst).name
        if name in ("acp-chat.json", "acp-chat-state.json"):
            seen.append((name, read_json(Path(dst)).get("sessionId")))

    monkeypatch.setattr(acp_chat.os, "replace", spy)
    first = client(new_ids=["sid-A", "sid-B"],
                   load_raises=acp_client.ACPError("session/load failed: load was cancelled"))
    sess = container_session(folder, first)
    try:
        sess.start()  # load of OLD fails -> reset -> sid-A
        assert sess.new_session()["ok"]
        assert sess.wait_idle(5)  # /new -> sid-B
    finally:
        sess.close()
    second = client(new_ids=["sid-C"])
    os.unlink(folder / "acp-chat.json")
    write_state(folder, None, None)
    sess = container_session(folder, second)
    try:
        sess.start()  # nothing to resume -> sid-C
    finally:
        sess.close()
    for sid in ("sid-A", "sid-B", "sid-C"):
        first_writer = next((name for name, named in seen if named == sid), None)
        assert first_writer == "acp-chat.json", (sid, seen)


# ── 9. omp cannot restore the session's model ───────────────────────────────


MISSING = acp_client.ACPError(
    "session/load failed: Internal error "
    "(Could not restore model mc-openai/Old-Model-4.1-Flash)"
)


@pytest.mark.parametrize("path", ["start", "child-restart"])
def test_load_refused_for_a_missing_model_is_one_model_missing_card(tmp_path, path):
    """omp >= 18.6.3 refuses `session/load` under ACP when the session's
    last model is not in models.yml (a runtime switch since the last turn).
    One card naming the model, one new session — not `rpc_error`, not a
    raw exception."""
    _, folder = layout(tmp_path)
    if path == "start":
        write_pointer(folder, S, mc_copy(folder, S))
        c = client(new_ids=["sid-new"], load_raises=MISSING)
        sess = container_session(folder, c)
    else:
        healthy = client(new_ids=["sid-live"])
        c = client(new_ids=["sid-new"], load_raises=MISSING)
        sess = container_session(folder, healthy, c)
    try:
        sess.start()
        if path == "child-restart":
            sess._restart_child()  # the omp child died; the respawn cannot load
            assert loads(c) == ["sid-live"]
        else:
            assert loads(c) == [S]
        assert news(c) == 1
        errors, notices = cards(sess.transcript_path)
        assert codes(errors) == [("session_reset", "model_missing")]
        assert "Old-Model-4.1-Flash" in errors[0]["content"]
        assert "mc-openai/" not in errors[0]["content"]
        assert notices == []
        assert read_json(folder / "acp-chat.json")["sessionId"] == "sid-new"
    finally:
        sess.close()


def test_model_missing_without_a_name_still_says_model_missing(tmp_path):
    _, folder = layout(tmp_path)
    write_pointer(folder, S, mc_copy(folder, S))
    c = client(load_raises=acp_client.ACPError("Could not restore model"))
    sess = container_session(folder, c)
    try:
        sess.start()
        errors, _ = cards(sess.transcript_path)
        assert codes(errors) == [("session_reset", "model_missing")]
        assert "model" in errors[0]["content"]
    finally:
        sess.close()


def test_any_other_load_failure_keeps_its_own_detail(tmp_path):
    """Only omp's model sentence is `model_missing`; anything else keeps the
    exception as detail, as before."""
    _, folder = layout(tmp_path)
    write_pointer(folder, S, mc_copy(folder, S))
    c = client(load_raises=acp_client.ACPError("session/load failed: load was cancelled"))
    sess = container_session(folder, c)
    try:
        sess.start()
        errors, _ = cards(sess.transcript_path)
        assert [code for code, _ in codes(errors)] == ["session_reset"]
        assert errors[0]["data"]["detail"] == (
            "ACPError: session/load failed: load was cancelled")
    finally:
        sess.close()


# ── 10. unknown fields of the pointer survive ───────────────────────────────


def test_unknown_pointer_fields_survive_every_rewrite(tmp_path):
    """P4 adds fields (e.g. `homeSessionId`); a daemon that does not know
    them must not drop them when it rewrites the pointer."""
    _, folder = layout(tmp_path)
    write_pointer(folder, S, mc_copy(folder, S), homeSessionId="home-1",
                  future={"a": 1})
    c = client(new_ids=["sid-B"])
    sess = container_session(folder, c)
    try:
        sess.start()  # load -> rewrite
        pointer = read_json(folder / "acp-chat.json")
        assert pointer["sessionId"] == S
        assert pointer["homeSessionId"] == "home-1" and pointer["future"] == {"a": 1}
        assert sess.new_session()["ok"]
        assert sess.wait_idle(5)
        pointer = read_json(folder / "acp-chat.json")
        assert pointer["sessionId"] == "sid-B"
        assert pointer["transcript"] == str(sess.transcript_path)
        assert pointer["homeSessionId"] == "home-1" and pointer["future"] == {"a": 1}
    finally:
        sess.close()


def test_the_state_mirror_never_leaks_into_the_pointer(tmp_path):
    """Resumed from the state mirror: the new pointer carries id and
    transcript only — never the mirror's queue, turn or config."""
    _, folder = layout(tmp_path)
    write_state(folder, S, mc_copy(folder, S))
    sess = container_session(folder, client())
    try:
        sess.start()
        assert set(read_json(folder / "acp-chat.json")) == {"sessionId", "transcript"}
    finally:
        sess.close()
