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
import dataclasses
import json
import os
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

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
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    tdir = tmp_path / "rex-transcripts"
    tdir.mkdir()
    (tdir / "sess1.jsonl").write_text(_user_line("hello from fixture") + "\n")

    import app.services.transcript_chat as transcript_chat_mod
    import app.services.agent_chat_input as agent_chat_input_mod

    monkeypatch.setattr(transcript_chat_mod, "resolve_transcript_dir", lambda a: tdir)
    # "rex" existiert wirklich in der Flotte — capabilities.model/effort nie
    # aus der LIVE settings.json des Hosts lesen (Real-Host-Leak).
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_model", lambda slug: None)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_effort_level", lambda slug, levels=(): None)
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
    # Same real-host-leak concern as the skills dir above, but for the model
    # catalog: a cli-bridge agent triggers harness_catalog.discover_model_catalog
    # -> resolve_cli_version -> a REAL `docker exec ... claude --version`
    # subprocess call unless mocked — "rex" being a real fleet agent slug
    # means this would hit an actual container on the host running the
    # suite. Force the empty-catalog (static-alias-fallback) path instead;
    # the catalog's own behavior is covered by test_harness_catalog.py.
    async def _empty_catalog(agent, model=None):
        return []

    monkeypatch.setattr(agent_chat_input_mod, "discover_model_catalog", _empty_catalog)
    # effort_capabilities' version-drift check ALSO calls resolve_cli_version
    # independently (not through discover_model_catalog) — same real-docker-
    # exec risk, mocked out the same way.
    async def _no_version(agent):
        return None

    monkeypatch.setattr(agent_chat_input_mod, "resolve_cli_version", _no_version)
    # Dritter Weg in dieselbe Falle (19.08.2026): effort_capabilities fragt
    # jetzt den /model-Picker, ob das MODELL des Agenten Effort-Stufen kennt —
    # das oeffnet ohne Stub ein echtes Wegwerf-Fenster im Container von "rex".
    # ``supported=None`` = "nicht ermittelt", der bisherige Zustand.
    async def _effort_unknown(agent, model=None):
        return {"supported": None, "model": None, "level": None}

    monkeypatch.setattr(agent_chat_input_mod, "discover_effort_support", _effort_unknown)

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
    # only (skills dir monkeypatched to not exist, see above). modelOptions:
    # static-alias fallback (catalog forced empty, see above).
    assert body["capabilities"] == {
        "effortLevels": ["low", "medium", "high", "xhigh", "max", "ultracode"],
        "canSwitchEffort": True,
        # Startwerte fuer den Composer, solange die Session noch kein
        # usage-Ereignis hat. Der Fixture-Agent hat keine settings.json -> None.
        "effort": None,
        "effortShared": False,
        # Schaltbar -> kein Grund noetig (openclaude-Runde 19.08.2026).
        "effortReason": None,
        "model": None,
        "slashCommands": list(agent_chat_input_mod._BUILTIN_SLASH_COMMANDS),
        "modelOptions": (await agent_chat_input_mod.model_options_capabilities(agent))[
            "modelOptions"
        ],
    }


async def test_history_200_capabilities_boss_cannot_switch_effort(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """Boss (host runtime) has no pane probe — SWITCHING must stay off. Seit
    18.08.2026 liefert das Backend fuer host+harness=claude trotzdem die
    Stufenleiter (canSwitchEffort=false = "kennt der Harness, darfst du aber
    nicht druecken"): das Frontend proportioniert damit die Saeule des
    read-only Brain-Chips, statt Boss das nackte Alt-Label zu zeigen.
    slashCommands still shows the builtins (those aren't docker-gated),
    just no skill discovery (host has no claude-config mount to scan)."""
    agent = await make_agent(name="Boss", agent_runtime="host", slug="boss", harness="claude")

    tdir = tmp_path / "boss-transcripts"
    tdir.mkdir()
    (tdir / "sess1.jsonl").write_text(_user_line("hi from boss") + "\n")

    import app.services.transcript_chat as transcript_chat_mod
    import app.services.agent_chat_input as agent_chat_input_mod

    monkeypatch.setattr(transcript_chat_mod, "resolve_transcript_dir", lambda a: tdir)
    # Only the capabilities derivation is under test here — bypass the A2
    # Boss privacy heuristic (cwd/branch sniffing) entirely rather than
    # constructing a transcript line that would satisfy it.
    monkeypatch.setattr(transcript_chat_mod, "transcript_allowed", lambda a, p: True)
    # Echte Fleet-Slugs — der capabilities.model/effort-Zweig darf nie die
    # settings.json des LAUFENDEN Agenten vom Host lesen (Real-Host-Leak).
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_model", lambda slug: None)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_effort_level", lambda slug, levels=(): None)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_effort_level_at", lambda path, levels=(): None)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_model_at", lambda path: None)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 200, resp.text
    assert resp.json()["capabilities"] == {
        "effortLevels": ["low", "medium", "high", "xhigh", "max", "ultracode"],
        # Seit 19.08.2026 schaltbar: die Bridge tippt, das Transkript
        # verifiziert. effortShared sagt dem UI, dass eine persistierende
        # Stufe auch die lokalen Claude-Sessions des Operators umstellt
        # (geteilte ~/.claude/settings.json).
        "canSwitchEffort": True,
        "effort": None,
        "effortShared": True,
        # Schaltbar -> kein Grund noetig. Das Feld traegt nur das WARUM,
        # wenn nichts geht (openclaude-Runde 19.08.2026).
        "effortReason": None,
        "model": None,
        "slashCommands": list(agent_chat_input_mod._BUILTIN_SLASH_COMMANDS),
        # modelOptions: Boss has no harness (host runtime) -> catalog is
        # empty, no subprocess attempted at all -> static-alias fallback,
        # same list a docker agent gets on a cold cache.
        "modelOptions": (await agent_chat_input_mod.model_options_capabilities(agent))[
            "modelOptions"
        ],
    }


async def test_history_404_no_transcript_for_host_agent_without_dir(auth_client: AsyncClient, make_agent):
    # "hermes" is a host-runtime agent but not in the Boss allowlist —
    # resolve_transcript_dir() returns None for it with no monkeypatching.
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_transcript"}


async def test_history_404_no_transcript_when_dir_has_no_sessions(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    import app.services.transcript_chat as transcript_chat_mod

    monkeypatch.setattr(transcript_chat_mod, "resolve_transcript_dir", lambda a: empty_dir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_transcript"}


async def test_history_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

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


def _async_return(value):
    """Kleiner Helfer: eine Coroutine, die immer denselben Wert liefert."""
    async def _inner(*_args, **_kwargs):
        return value
    return _inner


@pytest.fixture
def manager():
    from app.services.transcript_chat import ChatTailerManager

    m = ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    # Der Sondentakt zaehlt in Sekunden (nicht mehr in Poll-Durchlaeufen), also
    # muss er hier genauso schrumpfen wie der Poll-Takt — sonst wartet jeder
    # Test, der eine ZWEITE Zustandsmeldung braucht, volle 2 Sekunden auf sie.
    m.STATE_PROBE_INTERVAL_SECONDS = 0.02
    return m


async def test_tailer_seeds_the_parser_off_the_event_loop(
    manager, fake_broadcast, tmp_path, monkeypatch
):
    """``new_parser(initial_path)`` darf nicht auf der Schleife laufen.

    Fuer omp ist das ``OmpLineParser.seed_from`` — es liest die GANZE
    Sitzungsdatei mit ``open()``. Einmal je Agent beim ersten SSE-Verbinden,
    und solange steht die komplette FastAPI-Schleife. Dieselbe Regel, die
    ``_run`` weiter unten fuer ``transcript_allowed`` selbst formuliert
    („same rule as every other disk read in this loop")."""
    import dataclasses
    import threading

    import app.services.transcript_adapters as transcript_adapters

    loop_thread = threading.get_ident()
    seeded_in: list[int] = []
    base = transcript_adapters.adapter_for(None)

    def _spy_new_parser(session_path=None):
        seeded_in.append(threading.get_ident())
        return base.new_parser(session_path)

    spy = dataclasses.replace(base, new_parser=_spy_new_parser)
    monkeypatch.setattr(transcript_adapters, "adapter_for", lambda agent: spy)

    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        assert await _wait_until(lambda: bool(seeded_in))
    finally:
        await manager.release("agent-1")

    assert seeded_in and loop_thread not in seeded_in


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


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/attachment  (Chat-Anhänge, 19.08.2026)
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def attachment_root(tmp_path, monkeypatch):
    """Anhang-Root nach tmp_path — nie in den echten ~/.mc schreiben.

    Es ist derselbe Root, den auch Task-/Projekt-Referenzen benutzen: der
    Chat legt seine Anhaenge als AGENTEN-Referenzen ab (reference_files.
    agent_id, Migration 0172), nicht in einer zweiten Ablage daneben."""
    from app.config import settings
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    return tmp_path / ".mc" / "references"


async def test_attachment_upload_returns_the_absolute_path(
    auth_client: AsyncClient, make_agent, attachment_root
):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("foto.png", b"\x89PNG-bytes", "image/png")},
    )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "foto.png"
    assert body["isImage"] is True
    assert body["bytes"] == len(b"\x89PNG-bytes")
    # Der zurueckgegebene Pfad ist absolut und zeigt auf eine echte Datei —
    # genau diesen String haengt der Composer an die Nachricht, und genau ihn
    # oeffnet die CLI (Host- und Container-Pfad sind identisch, 1:1-Mount).
    assert body["path"].startswith(str(attachment_root))
    assert os.path.isfile(body["path"])
    assert open(body["path"], "rb").read() == b"\x89PNG-bytes"


async def test_attachment_belongs_to_the_agent(
    auth_client: AsyncClient, make_agent, attachment_root
):
    """Besitzer ist der Agent — die Ownership-Art, die es fuer genau diesen
    Fall schon gibt. Damit raeumt das Loeschen des Agenten die Datei mit ab,
    statt sie verwaist liegen zu lassen."""
    from app.models.reference_file import ReferenceFile

    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("foto.png", b"bytes", "image/png")},
    )
    assert res.status_code == 201, res.text
    body = res.json()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(
            select(ReferenceFile).where(ReferenceFile.agent_id == agent.id)
        )).all()
    assert len(rows) == 1
    assert rows[0].original_name == "foto.png"
    assert rows[0].uploaded_by == "chat"
    # Root + Unterpfad kommen aus der Ablage, damit das Frontend sie nicht
    # aus dem absoluten Pfad zurueckrechnen muss.
    assert body["root"] == "references"
    assert body["subpath"] == rows[0].rel_path
    assert body["path"] == os.path.join(str(attachment_root), rows[0].rel_path)


async def test_deleting_the_agent_removes_his_attachments(
    auth_client: AsyncClient, make_agent, attachment_root
):
    """Der eigentliche Grund fuer die Agenten-Ownership: Anhaenge bleiben
    nicht verwaist liegen. `delete_agent` ruft `delete_references_for(
    agent_id=…)` — Zeile UND Datei verschwinden mit ihrem Agenten.

    Vorher lagen Chat-Anhaenge in einer eigenen Ablage ohne DB-Zeile; niemand
    hat sie je wieder angefasst."""
    from unittest.mock import patch

    from app.utils import utcnow

    agent = await make_agent(
        name="Rex", agent_runtime="cli-bridge", harness="claude",
        archived_at=utcnow(),  # der Delete-Gate verlangt archiviert
    )
    up = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("foto.png", b"bytes", "image/png")},
    )
    assert up.status_code == 201, up.text
    path = up.json()["path"]
    assert os.path.isfile(path)

    with patch("app.services.docker_agent_sync.remove_docker_agent_container",
               return_value={"ok": "true"}):
        res = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert res.status_code == 204, res.text

    assert not os.path.exists(path), "Anhang ueberlebt seinen Agenten"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.reference_file import ReferenceFile
        rows = (await s.exec(
            select(ReferenceFile).where(ReferenceFile.agent_id == agent.id)
        )).all()
    assert rows == []


async def test_attachment_accepts_any_file_type(
    auth_client: AsyncClient, make_agent, attachment_root
):
    """Operator-Entscheid 19.08.2026: keine Typen-Liste. Ob der Agent die
    Datei lesen kann, ist nicht unsere Zusage. Die strenge Allowlist von
    reference_ingest bleibt fuer References-Upload und Slack unveraendert —
    nur dieser Aufrufer schaltet sie ab."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    for name, mime in (("a.heic", "image/heic"), ("b.mov", "video/quicktime"),
                       ("c.html", "text/html"), ("d.xyz", "application/x-unknown")):
        res = await auth_client.post(
            f"/api/v1/agents/{agent.id}/chat/attachment",
            files={"file": (name, b"payload", mime)},
        )
        assert res.status_code == 201, f"{name}: {res.text}"


async def test_attachment_has_no_files_per_agent_cap(
    auth_client: AsyncClient, make_agent, attachment_root
):
    """Der 20er-Deckel von reference_ingest passt fuer einen laufenden Chat
    nicht — nach 20 Screenshots waere Schluss."""
    from app.services.reference_ingest import MAX_FILES_PER_ENTITY

    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    for i in range(MAX_FILES_PER_ENTITY + 2):
        res = await auth_client.post(
            f"/api/v1/agents/{agent.id}/chat/attachment",
            files={"file": (f"n{i}.png", f"inhalt-{i}".encode(), "image/png")},
        )
        assert res.status_code == 201, f"#{i}: {res.text}"


async def test_attachment_works_for_every_harness(
    auth_client: AsyncClient, make_agent, attachment_root
):
    """Operator-Entscheid 19.08.2026: alle Agenten, nicht nur Claude."""
    for harness in ("claude", "openclaude", "omp", "kimi"):
        agent = await make_agent(
            name=f"A-{harness}", agent_runtime="cli-bridge", harness=harness
        )
        res = await auth_client.post(
            f"/api/v1/agents/{agent.id}/chat/attachment",
            files={"file": ("x.png", b"x", "image/png")},
        )
        assert res.status_code == 201, f"{harness}: {res.text}"


async def test_attachment_413_when_too_large(
    auth_client: AsyncClient, make_agent, attachment_root
):
    from app.services.reference_ingest import MAX_BYTES
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("gross.bin", b"x" * (MAX_BYTES + 1), "application/octet-stream")},
    )

    assert res.status_code == 413
    # Die Meldung muss die Grenze nennen — stilles Verschlucken war der
    # ausdrueckliche Abnahme-Punkt.
    assert "25" in res.json()["detail"]


async def test_attachment_409_for_agents_that_cannot_receive_input(
    auth_client: AsyncClient, make_agent, attachment_root
):
    """Hermes ist ein Host-Agent ausserhalb der Boss-Allowlist — er nimmt
    ueberhaupt keinen Chat-Text an. Dann ist auch ein Anhang sinnlos, und das
    UI erfaehrt den Grund statt einer Datei, die nie jemand liest."""
    agent = await make_agent(name="Hermes", agent_runtime="host", harness="hermes")

    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("x.png", b"x", "image/png")},
    )

    assert res.status_code == 409
    assert res.json()["reason"] == "input_not_supported"


async def test_attachment_422_on_traversal_name(
    auth_client: AsyncClient, make_agent, attachment_root
):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("../../etc/passwd", b"x", "text/plain")},
    )

    assert res.status_code == 422


async def test_attachment_422_on_empty_file(
    auth_client: AsyncClient, make_agent, attachment_root
):
    """Eine leere Datei ist keine Datei — der Agent bekaeme einen Pfad auf 0
    Bytes und keinen Hinweis, warum nichts drinsteht."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("leer.png", b"", "image/png")},
    )

    assert res.status_code == 422


async def test_attachment_requires_auth(client: AsyncClient, make_agent, attachment_root):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    res = await client.post(
        f"/api/v1/agents/{agent.id}/chat/attachment",
        files={"file": ("x.png", b"x", "image/png")},
    )
    assert res.status_code == 401


async def test_attachment_404_for_unknown_agent(auth_client: AsyncClient, attachment_root):
    res = await auth_client.post(
        f"/api/v1/agents/{uuid.uuid4()}/chat/attachment",
        files={"file": ("x.png", b"x", "image/png")},
    )
    assert res.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/subagent/{run_id}
#
# Der Endpunkt greift als ERSTER ueberhaupt in die Unterordner einer Sitzung.
# ``find_active_session`` steigt dort bewusst nie ab — es gibt hier also keine
# Vorgaenger-Absicherung, die einen Fehler auffinge. Darum steht unter den
# Tests fuer den Normalfall eine ganze Reihe fuer den Missbrauchsfall.
#
# Angenehme Folge des schmalen Vertrags (kein ``capabilities``-Block): der
# ganze Mock-Block des History-Tests entfaellt, der sonst per ``docker exec``
# echte Container anfassen wuerde.
# ══════════════════════════════════════════════════════════════════════════


def _subagent_fixture(tmp_path, run_id="apruefer", meta=None, body=None):
    """Legt eine Sitzung mit genau einem Subagenten-Lauf an."""
    tdir = tmp_path / "transcripts"
    tdir.mkdir(exist_ok=True)
    (tdir / "sess1.jsonl").write_text(_user_line("hauptstrom") + "\n")
    subdir = tdir / "sess1" / "subagents"
    subdir.mkdir(parents=True, exist_ok=True)
    (subdir / f"agent-{run_id}.jsonl").write_text(
        (body if body is not None else _user_line("ich bin der subagent", "s1")) + "\n"
    )
    if meta is not False:
        (subdir / f"agent-{run_id}.meta.json").write_text(
            json.dumps(meta or {
                "agentType": "reviewer",
                "name": "pruefer",
                "description": "Prueft den Zweig",
                "model": "claude-opus-5",
                "color": "green",
                "teamName": "session-abc123",
            })
        )
    return tdir, subdir


async def test_subagent_401_without_auth(client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    resp = await client.get(f"/api/v1/agents/{agent.id}/chat/subagent/apruefer")
    assert resp.status_code == 401


async def test_subagent_200_returns_that_runs_events(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, _ = _subagent_fixture(tmp_path)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/apruefer")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["kind"] for e in body["events"]] == ["message"]
    assert body["events"][0]["text"] == "ich bin der subagent"
    # Der Steckbrief kommt mit — die Karte braucht ihn ohne zweiten Abruf.
    assert body["subagent"]["runId"] == "apruefer"
    assert body["subagent"]["name"] == "pruefer"
    assert body["subagent"]["model"] == "claude-opus-5"


async def test_subagent_response_carries_no_capabilities(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """``capabilities`` beschreibt einen steuerbaren Live-Agenten. Ein
    Subagenten-Lauf ist ein abgeschlossenes Protokoll — wer dort einen
    Effort-Regler saehe, saehe eine Luege. (Und der Block zoege echte
    docker-exec-Aufrufe in diesen Test.)"""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, _ = _subagent_fixture(tmp_path)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/apruefer")).json()

    assert "capabilities" not in body
    assert "aliveness" not in body["session"]
    assert "subagentRuns" not in body


async def test_subagent_404_for_unknown_run(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, _ = _subagent_fixture(tmp_path)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/agibtsnicht")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_transcript"}


@pytest.mark.parametrize(
    "evil",
    ["..%2F..%2Fetc%2Fpasswd", "..", "%2e%2e%2fsess1", "a%00b", "a/b"],
)
async def test_subagent_404_on_a_traversal_run_id(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch, evil):
    """Der Pfad entsteht NIE aus der Eingabe — er wird in der gescannten
    Lauf-Liste nachgeschlagen. Zusaetzlich faellt die Gestalt schon vorher
    durch. Beides zusammen, weil dieser Baum bei einem Host-Agenten auch die
    persoenlichen Sitzungen des Operators enthaelt."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, _ = _subagent_fixture(tmp_path)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/{evil}")

    assert resp.status_code == 404, resp.text
    assert "passwd" not in resp.text


async def test_subagent_404_when_a_symlink_escapes_the_folder(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """Ein Symlink IM Ordner zeigt nach draussen. Gestalt-Pruefung und
    Nachschlag sehen ihn beide nicht — nur die Eindaemmung nach ``resolve()``
    faengt ihn."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, subdir = _subagent_fixture(tmp_path)
    geheim = tmp_path / "geheim.jsonl"
    geheim.write_text(_user_line("privat", "p1") + "\n")
    (subdir / "agent-apruefer.jsonl").unlink()
    (subdir / "agent-apruefer.jsonl").symlink_to(geheim)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/apruefer")

    assert resp.status_code == 404, resp.text
    assert "privat" not in resp.text


async def test_subagent_404_when_the_child_file_fails_the_privacy_gate(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """DER wichtigste Test hier.

    Boss teilt sein Transkript-Verzeichnis mit den PERSOENLICHEN Sitzungen des
    Operators; welche dazugehoert, entscheidet ``transcript_allowed`` am
    Arbeitsverzeichnis. Eltern- und Kindurteil koennen auseinandergehen, weil
    ein Subagent das Verzeichnis wechseln kann. Die Eltern-Pruefung allein
    reicht darum NICHT.
    """
    agent = await make_agent(name="Boss", agent_runtime="cli-bridge", harness="claude")
    tdir, _ = _subagent_fixture(tmp_path)
    import app.services.transcript_chat as tc
    import app.services.transcript_adapters as ta
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    base = ta.adapter_for(agent)

    # Eltern erlaubt, Kind verboten — genau die Divergenz.
    def _gate(a, path):
        return "subagents" not in str(path)

    spy = dataclasses.replace(base, transcript_allowed=_gate)
    # Am VERWENDUNGSORT patchen: der Router hat ``adapter_for`` direkt
    # importiert, eine Aenderung am Modul erreicht ihn nicht mehr.
    import app.routers.agent_chat as router_mod
    monkeypatch.setattr(router_mod, "adapter_for", lambda a: spy)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/apruefer")

    assert resp.status_code == 404, resp.text
    assert "subagent" not in resp.text.lower() or resp.json() == {"reason": "no_transcript"}


async def test_subagent_usage_never_reads_a_foreign_statusline(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """``stamp_usage`` leitet die Config-Wurzel ueber ``parent.parent.parent``
    aus dem Pfad ab. Eine Subagenten-Datei liegt zwei Ebenen tiefer — der
    Zeiger landete in einem FREMDEN Verzeichnis. Darum ist es fuer diesen
    Aufruf stillgelegt."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    # Ein Zug MIT Verbrauchszahlen — sonst gibt es kein usage-Ereignis, und
    # der Spy bliebe auch ohne Stilllegung leer (die erste Fassung dieses
    # Tests war genau so und prueft nichts; Sabotage-Probe 22.08.2026).
    assistant = json.dumps({
        "type": "assistant",
        "uuid": "a1",
        "timestamp": "2026-08-13T00:00:01Z",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "fertig"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    })
    tdir, _ = _subagent_fixture(tmp_path, body=assistant)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    gesehen: list = []
    echt = tc._stamp_usage_source

    def _spy(ev, root, session_id):
        gesehen.append((root, session_id))
        return echt(ev, root, session_id)

    monkeypatch.setattr(tc, "_stamp_usage_source", _spy)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/apruefer")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Gegenprobe, dass die Vorbedingung stimmt: es GIBT ein usage-Ereignis,
    # stamp_usage haette also etwas zu tun gehabt.
    assert any(e["kind"] == "usage" for e in body["events"]), body["events"]
    assert gesehen == [], f"stamp_usage lief doch: {gesehen}"


async def test_subagent_without_a_profile_still_works(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """Der Steckbrief fehlt in der Haelfte der Faelle. Der Verlauf ist trotzdem
    da und wird gezeigt — nur ohne Namen."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, _ = _subagent_fixture(tmp_path, meta=False)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/apruefer")).json()

    assert body["subagent"]["name"] is None
    assert len(body["events"]) == 1


async def test_a_malformed_run_id_never_reaches_the_directory_scan(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """Die Gestalt-Pruefung ist die AEUSSERE der beiden Schranken und deckt
    sich in der Wirkung mit dem Nachschlag. Ohne diesen Test waere sie
    trotzdem ungeprueft — und beim naechsten Umbau still entfernbar.

    Gepinnt wird darum die REIHENFOLGE: ein missgestalteter Wert wird
    abgewiesen, BEVOR ueberhaupt ein Ordner gelesen wird."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, _ = _subagent_fixture(tmp_path)
    import app.services.transcript_chat as tc
    import app.routers.agent_chat as router_mod
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    scans: list = []
    base = router_mod.adapter_for(agent)

    def _spy_runs(session_path):
        scans.append(session_path)
        return base.subagent_runs(session_path)

    monkeypatch.setattr(
        router_mod, "adapter_for",
        lambda a: dataclasses.replace(base, subagent_runs=_spy_runs),
    )

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/hat%20leerzeichen")

    assert resp.status_code == 404
    assert scans == [], "der Ordner wurde trotz missgestalteter runId gelesen"


async def test_subagent_404_when_the_whole_folder_is_a_symlink(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    """Der Ordner-Symlink — die Luecke, die das Review gefunden hat.

    Die Eindaemmung bildete ihre Grenze mit ``(… / "subagents").resolve()``,
    loeste also den ORDNER selbst mit auf. Zeigt ``subagents`` per Symlink
    woandershin, wandert die Grenze mit und der Vergleich ist danach trivial
    wahr. Die anderen drei Schranken sehen ihn ebenfalls nicht: die
    Gestalt-Pruefung schaut nur auf Zeichen, der Nachschlag benutzt
    ``glob()`` (folgt dem Symlink, findet den fremden Lauf und bestaetigt
    ihn), und ``transcript_allowed`` gibt fuer cli-bridge blind True zurueck.

    Der frueher einzige Symlink-Test haengte einen Symlink auf eine DATEI ein
    — genau den Fall, den die Eindaemmung faengt. Der Ordner-Fall war
    ungeprueft.
    """
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    (tdir / "sess1.jsonl").write_text(_user_line("hauptstrom") + "\n")

    # Ein fremder Baum — im Betrieb das Config-Verzeichnis eines ANDEREN
    # Agenten oder der persoenliche ~/.claude-Baum des Operators.
    fremd = tmp_path / "fremd" / "subagents"
    fremd.mkdir(parents=True)
    (fremd / "agent-ageheim.jsonl").write_text(_user_line("MEIN PRIVATER INHALT", "g1") + "\n")
    (fremd / "agent-ageheim.meta.json").write_text('{"agentType":"privat","name":"privat"}')

    # Der Agent legt den Symlink in seiner EIGENEN Sitzung an — er hat sein
    # Config-Verzeichnis rw gemountet.
    (tdir / "sess1").mkdir()
    (tdir / "sess1" / "subagents").symlink_to(fremd, target_is_directory=True)

    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/ageheim")

    assert resp.status_code == 404, resp.text
    assert "PRIVATER" not in resp.text


async def test_a_symlinked_folder_contributes_no_runs_at_all(tmp_path):
    """Zweite Haelfte desselben Fixes: der fremde Lauf darf gar nicht erst in
    der Liste auftauchen. Sonst zeigte die Oberflaeche eine Karte fuer ein
    Protokoll, das der Endpunkt danach zu Recht verweigert — eine Karte, die
    ins Leere fuehrt."""
    import app.services.transcript_chat as tc

    session = tmp_path / "sess.jsonl"
    session.write_text("{}\n", encoding="utf-8")
    fremd = tmp_path / "fremd" / "subagents"
    fremd.mkdir(parents=True)
    (fremd / "agent-ageheim.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "sess").mkdir()
    (tmp_path / "sess" / "subagents").symlink_to(fremd, target_is_directory=True)

    assert tc.subagent_runs(session) == []


async def test_the_endpoint_refuses_a_symlinked_folder_even_if_the_run_list_offers_it(
    auth_client: AsyncClient, make_agent, tmp_path, monkeypatch
):
    """Die Schranke des ENDPUNKTS, unabhaengig von der Lauf-Liste geprueft.

    Sabotage-Probe 22.08.2026: Nimmt man die Symlink-Abweisung aus dem
    Endpunkt heraus, bleiben alle Tests gruen — weil ``subagent_runs`` den
    fremden Lauf schon gar nicht mehr liefert und der Nachschlag scheitert.
    Die zweite Schicht war damit vorhanden, aber ungeprueft, und beim
    naechsten Umbau spurlos entfernbar.

    Hier liefert die Lauf-Liste den fremden Lauf ABSICHTLICH — und der
    Endpunkt muss ihn trotzdem verweigern.
    """
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    (tdir / "sess1.jsonl").write_text(_user_line("hauptstrom") + "\n")
    fremd = tmp_path / "fremd" / "subagents"
    fremd.mkdir(parents=True)
    (fremd / "agent-ageheim.jsonl").write_text(_user_line("MEIN PRIVATER INHALT", "g1") + "\n")
    (tdir / "sess1").mkdir()
    (tdir / "sess1" / "subagents").symlink_to(fremd, target_is_directory=True)

    import app.services.transcript_chat as tc
    import app.routers.agent_chat as router_mod
    import app.services.transcript_adapters as ta
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    base = ta.adapter_for(agent)
    fake_run = {
        "runId": "ageheim", "name": "privat", "agentType": "privat",
        "description": None, "model": None, "color": None,
        "teamName": None, "startedAt": None,
    }
    spy = dataclasses.replace(base, subagent_runs=lambda _p: [fake_run])
    monkeypatch.setattr(router_mod, "adapter_for", lambda a: spy)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/ageheim")

    assert resp.status_code == 404, resp.text
    assert "PRIVATER" not in resp.text


async def test_the_endpoint_serves_only_real_files_from_that_folder(
    auth_client: AsyncClient, make_agent, tmp_path, monkeypatch
):
    """Auch ein Symlink INNERHALB des Ordners wird nicht bedient.

    Die Eindaemmung allein liesse ihn durch — er zeigt ja nach drinnen. Er ist
    trotzdem nicht das, was die Lauf-Liste beschreibt, und ein Verweis, dem
    wir folgen, ist ein Verweis, den jemand umbiegen kann. Ohne diesen Test
    waere die ``is_symlink``-Pruefung auf die Zieldatei ungeprueft (Sabotage
    D blieb gruen).
    """
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    tdir, subdir = _subagent_fixture(tmp_path, run_id="aecht")
    (subdir / "agent-averweis.jsonl").symlink_to(subdir / "agent-aecht.jsonl")
    (subdir / "agent-averweis.meta.json").write_text('{"agentType":"x"}')

    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tdir)

    # Der echte Lauf geht.
    assert (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/aecht")).status_code == 200
    # Der Verweis darauf nicht.
    assert (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/subagent/averweis")).status_code == 404


async def test_first_state_arrives_immediately_not_after_the_probe_interval(
    manager, fake_broadcast, tmp_path
):
    """Wer den Chat oeffnet, sieht den Zustand sofort — nicht erst nach dem
    Sondentakt.

    Der Sondentakt war in POLL-TICKS gezaehlt, nicht in Sekunden: die erste
    Zustandsmeldung kam damit erst nach ``N × POLL_INTERVAL``, und jede
    Aenderung am Poll-Takt verschob sie mit (01.09.2026 beim Absenken des
    Takts auf 0,3 s aufgefallen). Die Sonde ist ein 50-ms-Rundlauf; die erste
    darf sofort laufen, danach reicht der Zeittakt.
    """
    session_file = tmp_path / "sess-first.jsonl"
    session_file.write_text("")
    agent = _StubAgent(agent_runtime="host", slug="boss")
    # Beide Takte auf Betriebswerte: die Fixture verkleinert sie fuer andere
    # Tests, hier muessen sie echt sein — sonst kaeme die erste Meldung auch
    # ohne die Sofort-Sonde rechtzeitig, und der Test prueft nichts.
    manager.POLL_INTERVAL = 0.1
    manager.STATE_PROBE_INTERVAL_SECONDS = 2.0

    await manager.acquire("agent-first", session_file, agent)
    try:
        assert await _wait_until(
            lambda: any(d.get("kind") == "state" for _, _, d in fake_broadcast),
            timeout=0.25,
        ), "nach einer Viertelsekunde lag noch keine Zustandsmeldung vor"
    finally:
        await manager.release("agent-first")


# ── Live-Vorschau aus dem Terminal-Strom (P1b) ─────────────────────────────


async def test_tailer_publishes_a_preview_while_the_answer_is_still_being_written(
    manager, fake_broadcast, tmp_path, monkeypatch
):
    """Der Kern der Live-Schicht: Text zeigen, bevor das Transkript ihn hat.

    Gemessen am laufenden Stack schreibt die CLI ihren Assistenten-Block auf
    einen Schlag — bis dahin steht in der Datei nichts, waehrend im Terminal
    der Text laeuft. Der Tailer liest deshalb zusaetzlich den Pane-Strom und
    schickt daraus ein ``preview``-Ereignis.
    """
    import app.services.transcript_chat as transcript_chat_mod
    from app.services import pane_stream

    session_file = tmp_path / "sess-prev.jsonl"
    session_file.write_text("")
    stream_file = tmp_path / "pane.log"
    stream_file.write_bytes(b"")

    monkeypatch.setattr(pane_stream, "start", _async_return(stream_file))
    monkeypatch.setattr(pane_stream, "stop", _async_return(None))
    monkeypatch.setattr(transcript_chat_mod, "capture_pane", _async_return("❯ "))

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    await manager.acquire("agent-prev", session_file, agent)
    try:
        stream_file.write_bytes("● Der Uetliberg ist 869 Meter hoch.\r\n".encode())
        assert await _wait_until(
            lambda: any(
                d.get("kind") == "preview" and "869 Meter" in d.get("text", "")
                for _, _, d in fake_broadcast
            ),
            timeout=3.0,
        ), "keine Vorschau aus dem Terminal-Strom"
    finally:
        await manager.release("agent-prev")


async def test_preview_events_carry_no_uuid_and_never_enter_the_history(
    manager, fake_broadcast, tmp_path, monkeypatch
):
    """Eine Vorschau ist keine Wahrheit.

    Sie traegt nie eine Dedup-Kennung — sonst konkurrierte sie im Reducer mit
    dem echten Transkript-Ereignis, und der Verlauf haette am Ende zwei
    Fassungen derselben Antwort."""
    import app.services.transcript_chat as transcript_chat_mod
    from app.services import pane_stream

    session_file = tmp_path / "sess-prev2.jsonl"
    session_file.write_text("")
    stream_file = tmp_path / "pane2.log"
    stream_file.write_bytes(b"")
    monkeypatch.setattr(pane_stream, "start", _async_return(stream_file))
    monkeypatch.setattr(pane_stream, "stop", _async_return(None))
    monkeypatch.setattr(transcript_chat_mod, "capture_pane", _async_return("❯ "))

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    await manager.acquire("agent-prev2", session_file, agent)
    try:
        stream_file.write_bytes("● Antwort im Entstehen, lang genug zum Senden.\r\n".encode())
        await _wait_until(
            lambda: any(d.get("kind") == "preview" for _, _, d in fake_broadcast), timeout=3.0
        )
    finally:
        await manager.release("agent-prev2")

    previews = [d for _, _, d in fake_broadcast if d.get("kind") == "preview"]
    assert previews, "keine Vorschau erzeugt"
    for event in previews:
        assert event.get("uuid") is None
        assert event.get("source") == "pane"


async def test_releasing_the_last_client_switches_the_stream_off(
    manager, fake_broadcast, tmp_path, monkeypatch
):
    """Der Strom laeuft nur, solange jemand zusieht — sonst waechst eine Datei
    im Container, die niemand liest."""
    import app.services.transcript_chat as transcript_chat_mod
    from app.services import pane_stream

    session_file = tmp_path / "sess-prev3.jsonl"
    session_file.write_text("")
    stopped: list[str] = []

    async def _stop(agent):
        stopped.append(getattr(agent, "slug", "?"))

    monkeypatch.setattr(pane_stream, "start", _async_return(tmp_path / "pane3.log"))
    monkeypatch.setattr(pane_stream, "stop", _stop)
    monkeypatch.setattr(transcript_chat_mod, "capture_pane", _async_return("❯ "))

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    await manager.acquire("agent-prev3", session_file, agent)
    await manager.release("agent-prev3")

    assert stopped == ["rex"], "der Strom wurde nicht abgeschaltet"


async def test_preview_is_only_sent_once_two_readings_agree(tmp_path, fake_broadcast, manager):
    """Flacker-Daempfung: ein einzelner Blick genuegt nicht.

    Der Poll friert den Terminal-Strom an einer beliebigen Byte-Grenze ein.
    Trifft er mitten ins Neuzeichnen, steht dort kurz eine halb ueberschriebene
    Zeile — in der Machbarkeitsprobe live gesehen (ein Textstueck doppelt).
    Gesendet wird deshalb erst, wenn zwei Durchlaeufe DENSELBEN Text ergeben;
    das kostet einen Tick (0,3 s) und erspart dem Leser das Zucken.
    """
    from app.services.pane_preview import PanePreview

    stream = tmp_path / "flicker.log"
    stream.write_bytes("● Eine Antwort, die lang genug ist.\r\n".encode())
    state = {"path": stream, "offset": 0, "screen": PanePreview(), "last_sent": "", "pending": None}

    await manager._pump_preview("chan", state)
    assert not [d for _, _, d in fake_broadcast if d.get("kind") == "preview"], (
        "schon der erste Blick wurde gesendet — die Daempfung fehlt"
    )

    await manager._pump_preview("chan", state)
    previews = [d for _, _, d in fake_broadcast if d.get("kind") == "preview"]
    assert len(previews) == 1
    assert "Eine Antwort" in previews[0]["text"]

    # Unveraenderter Text wird nicht erneut gesendet.
    await manager._pump_preview("chan", state)
    assert len([d for _, _, d in fake_broadcast if d.get("kind") == "preview"]) == 1


async def test_preview_screen_takes_the_real_pane_size(manager, tmp_path, monkeypatch):
    """Live-Gate 01.09.2026: Pane 168x45, Emulator 80x24 -> Zeilen bei 80 ab."""
    from app.services import pane_stream

    monkeypatch.setattr(pane_stream, "start", _async_return(tmp_path / "p.log"))
    monkeypatch.setattr(pane_stream, "pane_size", _async_return((168, 45)))
    state = await manager._start_preview(_StubAgent(agent_runtime="cli-bridge", slug="rex"))
    assert (state["screen"].cols, state["screen"].rows) == (168, 45)


async def test_preview_keeps_its_size_when_the_stream_file_is_truncated(
    tmp_path, fake_broadcast, manager
):
    from app.services.pane_preview import PanePreview

    stream = tmp_path / "trunc.log"
    stream.write_bytes(b"x" * 100)
    state = {"path": stream, "offset": 0, "screen": PanePreview(cols=168, rows=45), "last_sent": "", "pending": None}
    await manager._pump_preview("chan", state)
    stream.write_bytes(b"neu")          # geleert und kuerzer -> von vorn
    await manager._pump_preview("chan", state)
    assert (state["screen"].cols, state["screen"].rows) == (168, 45)


async def test_preview_shows_only_what_follows_the_last_transcript_line(
    manager, fake_broadcast, tmp_path, monkeypatch
):
    """Live-Gate 01.09.2026: die Vorschau trug den GANZEN Bildschirm — alte
    Zuege, die eigene Frage, und nach der fertigen Antwort dieselbe Antwort
    nochmal. Die Vorschau soll nur zeigen, was das Transkript noch nicht hat:
    alles NACH der letzten Transkript-Zeile."""
    import json

    import app.services.transcript_chat as transcript_chat_mod
    from app.services import pane_stream

    session_file = tmp_path / "sess-anchor.jsonl"
    session_file.write_text("")
    stream_file = tmp_path / "anchor.log"
    stream_file.write_bytes(b"")
    monkeypatch.setattr(pane_stream, "start", _async_return(stream_file))
    monkeypatch.setattr(pane_stream, "stop", _async_return(None))
    monkeypatch.setattr(transcript_chat_mod, "capture_pane", _async_return("❯ "))

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    await manager.acquire("agent-anchor", session_file, agent)
    try:
        question = "Schreib mir bitte in 4-5 Saetzen, was ein Fjord ist."
        with open(session_file, "a") as fh:
            fh.write(json.dumps({
                "type": "user", "uuid": "u-1", "timestamp": "2026-09-01T21:00:21Z",
                "message": {"role": "user", "content": question},
            }) + "\n")
        assert await _wait_until(
            lambda: any(d.get("kind") == "message" for _, _, d in fake_broadcast), timeout=3.0
        )
        stream_file.write_bytes(
            f"● Alte Antwort von vorhin, lang genug fuer die Dedup-Grenze.\r\n> {question}\r\n● Ein Fjord ist ein Meeresarm.\r\n".encode()
        )
        assert await _wait_until(
            lambda: any(
                d.get("kind") == "preview" and "Meeresarm" in d.get("text", "")
                for _, _, d in fake_broadcast
            ),
            timeout=3.0,
        ), "keine Vorschau"
    finally:
        await manager.release("agent-anchor")

    preview = [d for _, _, d in fake_broadcast if d.get("kind") == "preview"][-1]
    assert "Alte Antwort" not in preview["text"], "alter Zug in der Vorschau"
    assert "Fjord ist" not in preview["text"] or "Schreib mir" not in preview["text"], (
        "die eigene Frage steht in der Vorschau"
    )
    assert preview["text"] == "● Ein Fjord ist ein Meeresarm."


async def test_preview_sends_the_stable_prefix_while_the_text_keeps_growing(
    tmp_path, fake_broadcast, manager
):
    """Live 02.09.2026: ein Modell mit 50 t/s aendert den Bildschirm bei JEDEM
    Poll — zwei gleiche Lesungen gab es in 20 s Antwort kein einziges Mal, die
    Vorschau blieb stumm. Gesendet wird darum, was zwei Lesungen GEMEINSAM
    haben (bis zur letzten ganzen Wortgrenze): flackerfrei, aber nie stumm."""
    from app.services.pane_preview import PanePreview

    stream = tmp_path / "grow.log"
    stream.write_bytes("Erste Zeile der Antwort, lang genug.\r\nZweite".encode())
    state = {"path": stream, "offset": 0, "screen": PanePreview(), "last_sent": "", "pending": None}
    await manager._pump_preview("chan", state)
    with open(stream, "ab") as fh:
        fh.write(" Zeile waechst noch weiter.\r\nDritte".encode())
    await manager._pump_preview("chan", state)
    previews = [d for _, _, d in fake_broadcast if d.get("kind") == "preview"]
    assert [p["text"] for p in previews] == ["Erste Zeile der Antwort, lang genug.\nZweite"], (
        "'Zweite' steht in beiden Lesungen als ganzes Wort — es gehoert dazu; "
        "'Dritte' ist noch im Entstehen und bleibt draussen"
    )


# ── Frische Sitzung ohne Datei (omp ``/new``) ───────────────────────────────


class _OmpStubAgent(_StubAgent):
    harness = "omp"


def _omp_pane(with_marker: bool) -> str:
    body = " ✔ New session started\n" if with_marker else ""
    return (
        " Tip: Press alt+p (or /switch) to switch provider\n"
        f"{body}"
        "╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 3.7%/500K ⟲ ▶──╮\n"
        "╰─                                                        ─╯\n"
    )


async def test_tailer_fresh_session_marker_clears_the_chat(
    manager, fake_broadcast, tmp_path, monkeypatch
):
    """Taucht ``New session started`` NEU im Terminal auf, gilt die alte Datei
    als beendet: ``session_changed`` geht raus, die Historie meldet sich
    leer, und Zeilen, die noch in die alte Datei fallen, erreichen den Chat
    nicht mehr."""
    import app.services.transcript_chat as transcript_chat
    from app.services import fresh_session

    fresh_session.reset_for_tests()
    session_file = tmp_path / "2026-09-01T12-39-02-054Z_old.jsonl"
    session_file.write_text("")

    panes = iter([_omp_pane(False), _omp_pane(False), _omp_pane(True)])

    async def _fake_capture_pane(agent):
        return next(panes, _omp_pane(True))

    monkeypatch.setattr(transcript_chat, "capture_pane", _fake_capture_pane)

    agent = _OmpStubAgent(agent_runtime="cli-bridge", slug="omp-agent")
    await manager.acquire("agent-omp", session_file, agent)
    try:
        assert await _wait_until(
            lambda: any(d.get("kind") == "session_changed" for _, _, d in fake_broadcast)
        )
        assert fresh_session.is_stale("agent-omp", session_file) is True

        with session_file.open("a") as fh:
            fh.write(
                '{"type":"message","id":"m9","timestamp":"2026-09-01T12:40:00Z",'
                '"message":{"role":"user","content":[{"type":"text","text":"alt"}]}}\n'
            )
        await asyncio.sleep(0.15)
    finally:
        await manager.release("agent-omp")
        fresh_session.reset_for_tests()

    assert not any(d.get("kind") == "message" for _, _, d in fake_broadcast)
    changed = [d for _, _, d in fake_broadcast if d.get("kind") == "session_changed"]
    assert len(changed) == 1


async def test_tailer_preexisting_marker_does_not_fire(
    manager, fake_broadcast, tmp_path, monkeypatch
):
    """Der Marker steht nach einem frueheren ``/new`` noch lange im Terminal.
    Nur ein ZUWACHS zaehlt — sonst leerte sich der Chat bei jedem Oeffnen."""
    import app.services.transcript_chat as transcript_chat
    from app.services import fresh_session

    fresh_session.reset_for_tests()
    session_file = tmp_path / "old.jsonl"
    session_file.write_text("")

    async def _fake_capture_pane(agent):
        return _omp_pane(True)

    monkeypatch.setattr(transcript_chat, "capture_pane", _fake_capture_pane)

    agent = _OmpStubAgent(agent_runtime="cli-bridge", slug="omp-agent")
    await manager.acquire("agent-omp", session_file, agent)
    try:
        await asyncio.sleep(0.2)
    finally:
        await manager.release("agent-omp")
        fresh_session.reset_for_tests()

    assert not any(d.get("kind") == "session_changed" for _, _, d in fake_broadcast)
    assert fresh_session.is_stale("agent-omp", session_file) is False


async def test_history_is_empty_while_the_fresh_session_has_no_file_yet(
    auth_client: AsyncClient, make_agent, tmp_path, monkeypatch
):
    """Nach ``session_changed`` holt das Frontend die Historie neu. Liefert
    die Route dann die ALTE Datei, ist der alte Verlauf sofort wieder da —
    genau der gemeldete Fehler. Solange keine neuere Datei existiert, muss
    sie leer antworten, mit einer Sitzungs-ID, die nicht die alte ist."""
    import app.services.transcript_chat as transcript_chat_mod
    from app.services import agent_chat_input as agent_chat_input_mod
    from app.services import fresh_session

    fresh_session.reset_for_tests()
    tdir = tmp_path / "t"
    tdir.mkdir()
    old = tdir / "old.jsonl"
    old.write_text(_user_line("alter verlauf") + "\n")
    os.utime(old, (1_000, 1_000))

    monkeypatch.setattr(transcript_chat_mod, "resolve_transcript_dir", lambda a: tdir)
    monkeypatch.setattr(transcript_chat_mod, "transcript_allowed", lambda a, p: True)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_model", lambda slug: None)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_effort_level", lambda slug, levels=(): None)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_effort_level_at", lambda path, levels=(): None)
    monkeypatch.setattr(agent_chat_input_mod, "_persisted_model_at", lambda path: None)

    agent = await make_agent(name="Omp Agent", slug="omp-agent", agent_runtime="cli-bridge")
    try:
        r0 = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")
        assert r0.status_code == 200
        assert len(r0.json()["events"]) == 1

        fresh_session.mark(str(agent.id), at=2_000)
        r1 = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")
        assert r1.status_code == 200
        body = r1.json()
        assert body["events"] == []
        assert body["session"]["sessionId"] != r0.json()["session"]["sessionId"]
        assert body["session"]["aliveness"] == "active"
        assert "capabilities" in body
    finally:
        fresh_session.reset_for_tests()
