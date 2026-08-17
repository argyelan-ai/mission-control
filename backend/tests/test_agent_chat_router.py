"""Task A4 — live transcript tailer + `/agents/{id}/chat/history|stream` router.

Two layers:

1. Router tests (history endpoint only — the SSE endpoint's response never
   terminates on its own, so it isn't exercised end-to-end here, matching
   how every other `*/stream` endpoint in this suite is (not) tested):
   - 200 with a fixture transcript (``resolve_transcript_dir`` monkeypatched
     to a tmp dir with a real ``.jsonl``)
   - 404 ``{"reason": "no_transcript"}`` for a host agent with no transcript
     dir at all (Hermes — not in the Boss allowlist, so
     ``resolve_transcript_dir`` returns None with zero monkeypatching)
   - 401 without a token

2. ``ChatTailerManager`` unit tests: refcounted acquire/release, new-bytes
   publishing (with a partial trailing line held back), session rollover to
   a newer ``.jsonl``, and a disappeared file not crashing the poll loop.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/history
# ══════════════════════════════════════════════════════════════════════════


def _user_line(text: str, msg_uuid: str = "u1", ts: str = "2026-08-13T00:00:00Z") -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": msg_uuid,
            "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


async def test_history_200_for_agent_with_fixture_transcript(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    tdir = tmp_path / "rex-transcripts"
    tdir.mkdir()
    (tdir / "sess1.jsonl").write_text(_user_line("hello from fixture") + "\n")

    import app.routers.agent_chat as agent_chat_mod
    import app.services.agent_chat_input as agent_chat_input_mod

    monkeypatch.setattr(agent_chat_mod, "resolve_transcript_dir", lambda a: tdir)
    # This test is about the transcript page + effort capabilities, not skill
    # discovery — point the skills scan at a dir that doesn't exist so the
    # result is deterministic (builtins only) regardless of whatever real
    # skills happen to be synced for this agent slug on the host running the
    # test suite (real skills DID leak in here once — "rex" is a real fleet
    # agent slug with a real ~/.mc/agents/rex/claude-config/skills/ dir).
    # The cache is ALSO cleared — it's keyed by slug and a previous test run
    # within the same pytest process could have cached "rex"'s real skills
    # before this monkeypatch even applies, since a cache hit short-circuits
    # before _agent_skills_dir is ever called again.
    monkeypatch.setattr(
        agent_chat_input_mod, "_agent_skills_dir", lambda slug: tmp_path / "no-skills-dir"
    )
    monkeypatch.setattr(agent_chat_input_mod, "_slash_commands_cache", {})

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session"]["sessionId"] == "sess1"
    assert len(body["events"]) == 1
    assert body["events"][0]["kind"] == "message"
    assert body["events"][0]["text"] == "hello from fixture"
    # session.live stays backward-compatible; aliveness is the new signal
    # (the fixture file was just written -> both agree it's "active").
    assert body["session"]["live"] is True
    assert body["session"]["aliveness"] == "active"
    # Dynamic effort-level capabilities (docker/cli-bridge agent): the
    # discovered 6-level list, switching allowed. slashCommands: builtins
    # only (skills dir monkeypatched to not exist, see above).
    assert body["capabilities"] == {
        "effortLevels": ["low", "medium", "high", "xhigh", "max", "ultracode"],
        "canSwitchEffort": True,
        "slashCommands": list(agent_chat_input_mod._BUILTIN_SLASH_COMMANDS),
    }


async def test_history_200_capabilities_boss_cannot_switch_effort(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """Boss (host runtime) has no pane probe — capabilities must say so
    explicitly rather than the frontend guessing from agent_runtime alone.
    slashCommands still shows the builtins (those aren't docker-gated),
    just no skill discovery (host has no claude-config mount to scan)."""
    agent = await make_agent(name="Boss", agent_runtime="host", slug="boss")

    tdir = tmp_path / "boss-transcripts"
    tdir.mkdir()
    (tdir / "sess1.jsonl").write_text(_user_line("hi from boss") + "\n")

    import app.routers.agent_chat as agent_chat_mod
    import app.services.agent_chat_input as agent_chat_input_mod

    monkeypatch.setattr(agent_chat_mod, "resolve_transcript_dir", lambda a: tdir)
    # Only the capabilities derivation is under test here — bypass the A2
    # Boss privacy heuristic (cwd/branch sniffing) entirely rather than
    # constructing a transcript line that would satisfy it.
    monkeypatch.setattr(agent_chat_mod, "transcript_allowed", lambda a, p: True)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 200, resp.text
    assert resp.json()["capabilities"] == {
        "effortLevels": [],
        "canSwitchEffort": False,
        "slashCommands": list(agent_chat_input_mod._BUILTIN_SLASH_COMMANDS),
    }


async def test_history_404_no_transcript_for_host_agent_without_dir(auth_client: AsyncClient, make_agent):
    # "hermes" is a host-runtime agent but not in the Boss allowlist —
    # resolve_transcript_dir() returns None for it with no monkeypatching.
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_transcript"}


async def test_history_404_no_transcript_when_dir_has_no_sessions(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "resolve_transcript_dir", lambda a: empty_dir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_transcript"}


async def test_history_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 401


async def test_history_404_for_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/history")
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# ChatTailerManager
# ══════════════════════════════════════════════════════════════════════════


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return predicate()


@pytest.fixture
def fake_broadcast(monkeypatch):
    """Patches ``transcript_chat.sse.broadcast`` and returns the list it
    appends (channel, event_type, data) tuples to."""
    import app.services.transcript_chat as transcript_chat

    published: list[tuple[str, str, dict]] = []

    async def _fake(channel: str, event_type: str, data: dict) -> None:
        published.append((channel, event_type, data))

    monkeypatch.setattr(transcript_chat.sse, "broadcast", _fake)
    return published


@pytest.fixture
def manager():
    from app.services.transcript_chat import ChatTailerManager

    m = ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    return m


async def test_tailer_publishes_appended_line(manager, fake_broadcast, tmp_path):
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        session_file.write_text(_user_line("hello", msg_uuid="u1") + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
    finally:
        await manager.release("agent-1")

    channel, event_type, data = fake_broadcast[0]
    assert channel == "mc:agent:agent-1:chat"
    assert event_type == "chat_event"
    assert data["kind"] == "message"
    assert data["text"] == "hello"


async def test_tailer_holds_back_partial_trailing_line(manager, fake_broadcast, tmp_path):
    """A write that lands mid-line must not be parsed until the newline
    arrives — the tailer buffers the remainder across polls."""
    session_file = tmp_path / "sess1.jsonl"
    full_line = _user_line("complete once flushed", msg_uuid="u1")
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        # Write only the first half of the line (no trailing newline).
        session_file.write_text(full_line[: len(full_line) // 2])
        await asyncio.sleep(0.1)
        assert fake_broadcast == []  # nothing published yet — line incomplete

        # Now complete it.
        session_file.write_text(full_line + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
    finally:
        await manager.release("agent-1")

    assert fake_broadcast[0][2]["text"] == "complete once flushed"


async def test_tailer_session_changed_on_newer_jsonl(manager, fake_broadcast, tmp_path):
    old_session = tmp_path / "sess-old.jsonl"
    old_session.write_text(_user_line("in the old session", msg_uuid="u-old") + "\n")

    await manager.acquire("agent-1", old_session)
    try:
        # A newer file lands in the same directory — simulate a fresh
        # Claude Code session starting.
        await asyncio.sleep(0.05)
        new_session = tmp_path / "sess-new.jsonl"
        new_session.write_text("")
        # Ensure a strictly newer mtime than old_session.
        import os
        os.utime(new_session, None)

        assert await _wait_until(
            lambda: any(d.get("kind") == "session_changed" for _, _, d in fake_broadcast)
        )

        # Events appended to the new file are now published too.
        fake_broadcast.clear()
        new_session.write_text(_user_line("in the new session", msg_uuid="u-new") + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
    finally:
        await manager.release("agent-1")

    assert fake_broadcast[-1][2]["text"] == "in the new session"


async def test_tailer_rollover_respects_boss_privacy_gate(manager, fake_broadcast, tmp_path):
    """A session rollover to a newer .jsonl must re-run the same Boss privacy
    gate the SSE handshake enforces at connect time (``transcript_allowed``,
    ``agent_chat.py:80``) — not just at ``acquire()`` time. A disallowed
    newest-mtime file (e.g. Mark's own personal session sitting in Boss's
    transcript dir, distinguishable only by ``cwd``) must be neither adopted
    nor published (review finding I-1)."""
    old_session = tmp_path / "sess-old.jsonl"
    old_session.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u-old",
                "timestamp": "2026-08-13T00:00:00Z",
                "cwd": "/Users/mark/.mc/checkouts/mission-control",
                "message": {"content": [{"type": "text", "text": "boss work"}]},
            }
        )
        + "\n"
    )

    agent = _StubAgent(agent_runtime="host", slug="boss")

    await manager.acquire("agent-1", old_session, agent)
    try:
        await asyncio.sleep(0.05)
        new_session = tmp_path / "sess-new.jsonl"
        new_session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u-new",
                    "timestamp": "2026-08-13T00:00:01Z",
                    "cwd": "/Users/mark/personal-project",
                    "message": {"content": [{"type": "text", "text": "private session"}]},
                }
            )
            + "\n"
        )
        # Ensure a strictly newer mtime than old_session.
        import os
        os.utime(new_session, None)

        # Give the tailer several poll ticks — enough for a wrongful rollover
        # to have already happened if the gate weren't re-checked.
        await asyncio.sleep(0.15)
    finally:
        await manager.release("agent-1")

    assert not any(d.get("kind") == "session_changed" for _, _, d in fake_broadcast)
    assert not any(d.get("text") == "private session" for _, _, d in fake_broadcast)


async def test_tailer_survives_disappearing_file(manager, fake_broadcast, tmp_path):
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        # Append AFTER acquire — offset is seeded from the (empty) file at
        # acquire time, so a line written before acquire would never publish.
        with session_file.open("a") as f:
            f.write(_user_line("before delete", msg_uuid="u1") + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
        session_file.unlink()
        # Give the loop a few ticks to poll a missing file — must not raise
        # or kill the task.
        await asyncio.sleep(0.15)
        assert not manager._tasks["agent-1"].done()
    finally:
        await manager.release("agent-1")


async def test_tailer_seeds_offset_skips_preexisting_content(manager, fake_broadcast, tmp_path):
    """Acquiring against an already-populated transcript must not re-read and
    re-broadcast its existing content as live chat_events (that would
    duplicate what /chat/history already returned) — only lines appended
    after acquire should publish."""
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text(_user_line("already here before acquire", msg_uuid="u0") + "\n")

    await manager.acquire("agent-1", session_file)
    try:
        await asyncio.sleep(0.1)
        assert fake_broadcast == []  # nothing published for pre-existing content

        with session_file.open("a") as f:
            f.write(_user_line("new after acquire", msg_uuid="u1") + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
    finally:
        await manager.release("agent-1")

    assert fake_broadcast[0][2]["text"] == "new after acquire"


async def test_tailer_dedups_repeated_uuid(manager, fake_broadcast, tmp_path):
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")
    line = _user_line("repeated line", msg_uuid="u1")

    await manager.acquire("agent-1", session_file)
    try:
        with session_file.open("a") as f:
            f.write(line + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)

        with session_file.open("a") as f:
            f.write(line + "\n")  # same uuid again — resumed session repeats a line
        await asyncio.sleep(0.15)
    finally:
        await manager.release("agent-1")

    assert len(fake_broadcast) == 1


def _assistant_usage_line(msg_uuid: str = "u1") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": msg_uuid,
            "timestamp": "2026-08-13T00:00:00Z",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "id": f"msg_{msg_uuid}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "text", "text": "usage carrier"}],
            },
        }
    )


async def test_tailer_usage_event_source_cli_when_statusline_fresh(manager, fake_broadcast, tmp_path):
    """A live usage event picks up docker/shared/statusline-mc.sh's fresh
    state file — same claude-config-root derivation read_history uses, see
    test_transcript_chat_history.py's _session_file fixture. session_file
    must sit 3 levels below claude-config/ (.../projects/<enc>/<sess>.jsonl,
    matching resolve_transcript_dir's real shape) for the derivation to land
    on the right statusline-state/ directory."""
    tdir = tmp_path / "claude-config" / "projects" / "-home-agent"
    tdir.mkdir(parents=True)
    session_file = tdir / "sess1.jsonl"
    session_file.write_text("")

    state_dir = tmp_path / "claude-config" / "statusline-state"
    state_dir.mkdir(parents=True)
    (state_dir / "sess1.json").write_text(
        json.dumps(
            {
                "context_window": {
                    "context_window_size": 1_000_000,
                    "used_percentage": 61.0,
                    "current_usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 1,
                        "cache_creation_input_tokens": 1,
                    },
                }
            }
        )
    )

    await manager.acquire("agent-1", session_file)
    try:
        with session_file.open("a") as f:
            f.write(_assistant_usage_line() + "\n")
        assert await _wait_until(
            lambda: any(d["kind"] == "usage" for _, _, d in fake_broadcast)
        )
    finally:
        await manager.release("agent-1")

    usage = next(d for _, _, d in fake_broadcast if d["kind"] == "usage")
    assert usage["usedPct"] == 61.0
    assert usage["source"] == "cli"
    # CLI's own context_window_size (1M) overrides the model-map estimate
    # (claude-sonnet-4-6 -> 200_000) — ground truth wins over the guess.
    assert usage["contextWindow"] == 1_000_000


async def test_tailer_usage_event_source_estimate_without_statusline_state(manager, fake_broadcast, tmp_path):
    """No statusline-mc.sh write ever happened (fresh agent, Boss, or the
    script failed) — usage events still publish, just with the static
    contextWindow estimate and no usedPct."""
    tdir = tmp_path / "claude-config" / "projects" / "-home-agent"
    tdir.mkdir(parents=True)
    session_file = tdir / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        with session_file.open("a") as f:
            f.write(_assistant_usage_line() + "\n")
        assert await _wait_until(
            lambda: any(d["kind"] == "usage" for _, _, d in fake_broadcast)
        )
    finally:
        await manager.release("agent-1")

    usage = next(d for _, _, d in fake_broadcast if d["kind"] == "usage")
    assert usage["usedPct"] is None
    assert usage["source"] == "estimate"


async def test_tailer_survives_broadcast_exception(manager, tmp_path, monkeypatch):
    """A transient failure inside sse.broadcast (e.g. Redis hiccup) must not
    silently kill the poll task while clients stay connected."""
    import app.services.transcript_chat as transcript_chat

    published: list[tuple[str, str, dict]] = []
    call_count = 0

    async def _flaky_broadcast(channel, event_type, data):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        published.append((channel, event_type, data))

    monkeypatch.setattr(transcript_chat.sse, "broadcast", _flaky_broadcast)

    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        with session_file.open("a") as f:
            f.write(_user_line("first (broadcast raises)", msg_uuid="u1") + "\n")
        await asyncio.sleep(0.15)  # let the failing iteration happen and pass
        assert not manager._tasks["agent-1"].done()  # task survived the exception

        with session_file.open("a") as f:
            f.write(_user_line("second (should publish)", msg_uuid="u2") + "\n")
        assert await _wait_until(lambda: len(published) > 0)
    finally:
        await manager.release("agent-1")

    assert published[0][2]["text"] == "second (should publish)"


async def test_tailer_handles_multibyte_utf8_across_polls(manager, fake_broadcast, tmp_path):
    """Regression: offsets must be tracked in bytes against a binary read,
    not text-mode characters against a byte-based stat size — the old
    mismatch could truncate or re-read multi-byte UTF-8 content spanning a
    poll boundary."""
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        line1 = _user_line("héllo wörld 🎉", msg_uuid="u1")
        with session_file.open("a", encoding="utf-8") as f:
            f.write(line1 + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) >= 1)

        line2 = _user_line("犬も歩けば棒に当たる", msg_uuid="u2")
        with session_file.open("a", encoding="utf-8") as f:
            f.write(line2 + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) >= 2)
    finally:
        await manager.release("agent-1")

    texts = [d["text"] for _, _, d in fake_broadcast]
    assert texts == ["héllo wörld 🎉", "犬も歩けば棒に当たる"]


async def test_tailer_republishes_tool_event_on_result_merge(manager, fake_broadcast, tmp_path):
    """A tool_use published live, whose tool_result arrives on a later poll,
    must be republished under the SAME uuid/toolUseId with the result
    merged in — a raw _tool_result must never be published on its own."""
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    tool_use_line = json.dumps(
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-08-13T00:00:00Z",
            "message": {
                "model": "claude-x",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "ls"}}
                ],
            },
        }
    )
    tool_result_line = json.dumps(
        {
            "type": "user",
            "uuid": "u2",
            "timestamp": "2026-08-13T00:00:01Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "file1\nfile2",
                        "is_error": False,
                    }
                ]
            },
        }
    )

    await manager.acquire("agent-1", session_file)
    try:
        with session_file.open("a") as f:
            f.write(tool_use_line + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) >= 1)
        assert fake_broadcast[0][2]["kind"] == "tool"
        assert fake_broadcast[0][2]["result"] is None

        with session_file.open("a") as f:
            f.write(tool_result_line + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) >= 2)
    finally:
        await manager.release("agent-1")

    kinds = [d["kind"] for _, _, d in fake_broadcast]
    assert kinds == ["tool", "tool"]  # never a raw "_tool_result"

    republished = fake_broadcast[1][2]
    assert republished["uuid"] == "a1"
    assert republished["toolUseId"] == "tool-1"
    assert republished["result"] == "file1\nfile2"
    assert republished["status"] == "done"


async def test_tailer_refcount_shares_one_task(manager, fake_broadcast, tmp_path):
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    task_after_first = manager._tasks["agent-1"]
    await manager.acquire("agent-1", session_file)
    assert manager._tasks["agent-1"] is task_after_first  # no second task spawned

    await manager.release("agent-1")
    assert manager._tasks["agent-1"] is task_after_first  # still running — one ref left

    await manager.release("agent-1")
    assert "agent-1" not in manager._tasks  # last release cancels + drops it


# ══════════════════════════════════════════════════════════════════════════
# ChatTailerManager — pane-state probe (A6)
# ══════════════════════════════════════════════════════════════════════════


class _StubAgent:
    def __init__(self, agent_runtime: str, slug: str):
        self.agent_runtime = agent_runtime
        self.slug = slug


async def test_tailer_no_state_probe_when_agent_absent(manager, fake_broadcast, tmp_path):
    """acquire() without an agent (as every other ChatTailerManager test in
    this file does) must never attempt a pane-state probe — no
    ``capture_pane`` call, no "state" chat_event, ever. This is what keeps
    every other test in this file docker-free."""
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)  # no agent arg
    try:
        await asyncio.sleep(0.2)  # several probe ticks would have fired by now
    finally:
        await manager.release("agent-1")

    assert not any(d.get("kind") == "state" for _, _, d in fake_broadcast)


async def test_tailer_publishes_state_only_on_change(manager, fake_broadcast, tmp_path, monkeypatch):
    """The probe fires every 2nd tick, but a "state" chat_event must only be
    published when the computed state differs from the previously published
    one — repeats of the same pane text (including repeats of the fallback
    after the fixture iterator is exhausted) must not spam the channel."""
    import app.services.transcript_chat as transcript_chat

    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    pane_texts = iter(["idle pane, no markers\n", "idle pane, no markers\n"])
    working_text = "✻ Thinking… (esc to interrupt)\n"

    async def _fake_capture_pane(agent):
        return next(pane_texts, working_text)

    monkeypatch.setattr(transcript_chat, "capture_pane", _fake_capture_pane)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    await manager.acquire("agent-1", session_file, agent)
    try:
        assert await _wait_until(
            lambda: any(
                d.get("kind") == "state" and d.get("status") == "working"
                for _, _, d in fake_broadcast
            )
        )
        # Give a few more ticks a chance to (wrongly) re-publish "working".
        await asyncio.sleep(0.1)
    finally:
        await manager.release("agent-1")

    state_events = [d for _, _, d in fake_broadcast if d.get("kind") == "state"]
    # One event for the initial state (first probe tick, "unknown" — no
    # markers in the fixture text — differs from the unset previous state),
    # one for the transition to "working". Every later tick recomputes the
    # same "working" state and must be suppressed.
    assert len(state_events) == 2
    assert state_events[0]["status"] != "working"
    assert state_events[1]["status"] == "working"


async def test_tailer_boss_state_from_mtime_never_permission_prompt(manager, fake_broadcast, tmp_path):
    """Boss/host agents have no capturable pane (capture_pane returns None
    for any non-cli-bridge runtime, unmocked here — real implementation) —
    state must fall back to the transcript-mtime heuristic and must never
    report permission_prompt."""
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    manager.STATE_ACTIVE_WINDOW_SECONDS = 0.05  # shrink so idle is reachable in-test

    agent = _StubAgent(agent_runtime="host", slug="boss")
    await manager.acquire("agent-1", session_file, agent)
    try:
        # Freshly (re)written file is within the active window -> working.
        assert await _wait_until(
            lambda: any(
                d.get("kind") == "state" and d.get("status") == "working"
                for _, _, d in fake_broadcast
            )
        )

        # Let the (shrunk) active window lapse without touching the file.
        assert await _wait_until(
            lambda: any(
                d.get("kind") == "state" and d.get("status") == "idle"
                for _, _, d in fake_broadcast
            ),
            timeout=2.0,
        )
    finally:
        await manager.release("agent-1")

    assert not any(d.get("status") == "permission_prompt" for _, _, d in fake_broadcast)
