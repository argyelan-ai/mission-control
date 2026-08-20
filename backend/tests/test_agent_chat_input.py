"""Task A5 — chat input (text + keys) delivery + router.

Two layers:

1. ``services/agent_chat_input.py`` unit tests: docker-exec argv construction
   for cli-bridge agents (single-line literal vs. multi-line bracketed
   paste), key allowlist validation, Boss WS byte delivery, and the 409-
   triggering ``InputNotSupportedError`` for every other host agent.
2. Router tests: ``POST /agents/{id}/chat/input|keys`` — 204 on success, 422
   on bad input, 404 for an unknown agent, 409 for unsupported runtimes —
   with the service functions monkeypatched so router tests never shell out
   to docker or open a real WebSocket.
"""
from __future__ import annotations

import uuid

import json

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_effort_probe(monkeypatch):
    """``effort_capabilities`` fragt seit 19.08.2026 den ``/model``-Picker, ob
    das MODELL des Agenten ueberhaupt Effort-Stufen kennt. Ohne Stub wuerde
    jeder Test hier ein echtes ``docker exec`` absetzen (dieselbe
    Real-Host-Klasse wie die gemockten Subprozess-Aufrufe weiter unten) —
    und zwar fuer eine Frage, die keiner dieser Tests stellt.

    ``supported=None`` ist die ehrliche Vorgabe: "nicht ermittelt" — genau
    der Zustand, in dem der Regler wie bisher schaltbar bleibt. Die
    modellabhaengigen Faelle stehen in tests/test_openclaude_capabilities.py."""
    from app.services import agent_chat_input

    async def _unknown(agent, model=None):
        return {"supported": None, "model": None, "level": None}

    monkeypatch.setattr(agent_chat_input, "discover_effort_support", _unknown)


class _StubAgent:
    """Duck-typed stand-in for the DB-backed Agent row — mirrors the
    ``agent.slug`` / ``agent.agent_runtime`` contract ``_target_kind`` reads,
    same convention as ``transcript_chat.resolve_transcript_dir``'s tests."""

    def __init__(self, slug: str, agent_runtime: str, harness: str = "claude"):
        # harness default "claude" = der Flotten-Mainstream; die Harness-Gates
        # (18.08.2026: kein /effort//model in fremde CLIs) testen Abweichler
        # explizit mit harness="kimi"/"omp".
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness


class _FakeWSConn:
    def __init__(self, sent: list[bytes], events: list[tuple] | None = None):
        self._sent = sent
        self._events = events

    async def send(self, payload: bytes) -> None:
        self._sent.append(payload)
        if self._events is not None:
            self._events.append(("send", payload))

    async def __aenter__(self) -> "_FakeWSConn":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeWSClient:
    """``events`` interleaves ``("send", payload)`` entries with whatever a
    test also appends to it (e.g. a monkeypatched ``asyncio.sleep`` recording
    ``("sleep", delay)``) — needed to assert frame/delay ordering, not just
    the final set of bytes sent."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.connected_urls: list[str] = []
        self.events: list[tuple] = []

    def connect(self, url: str, **kwargs):
        self.connected_urls.append(url)
        return _FakeWSConn(self.sent, self.events)


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — docker (cli-bridge) delivery
# ══════════════════════════════════════════════════════════════════════════


async def test_send_text_docker_single_line_submits_via_separate_enter(monkeypatch):
    """Fix round 4: a literal ``-l`` send only types text into the TUI's
    input box — it never submits on its own. The single-line path was
    missing a follow-up Enter call entirely (root cause of messages sitting
    unsubmitted); this asserts the exact 3-call sequence: the recycler-
    marker touch FIRST (send-readiness-gate hardening round — touched
    before the readiness gate so a slow poll can't race a recycle), THEN
    literal text, THEN a separate Enter. ``capture_pane`` is stubbed ready
    immediately so the gate never blocks."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        return _IDLE_PANE

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "hello agent")

    assert len(calls) == 3  # touch marker + send-keys -l text + send-keys Enter
    first, second, third = calls
    assert first == [
        "docker", "exec", "-u", "agent", "mc-agent-rex",
        "touch", "/home/agent/.claude/last-task.marker",
    ]
    assert second[:2] == ["docker", "exec"]
    assert "-e" in second and "LANG=C.UTF-8" in second
    assert "-u" in second and "agent" in second
    assert "mc-agent-rex" in second
    assert second[-3:] == ["-l", "--", "hello agent"]
    assert third[-1] == "Enter"
    assert "-l" not in third


async def test_send_text_docker_multiline_two_calls_bracketed_paste(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        return _IDLE_PANE

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "line one\nline two")

    assert len(calls) == 3  # recycler-marker touch + paste + Enter
    first, second, third = calls
    assert first == ["docker", "exec", "-u", "agent", "mc-agent-rex", "touch", "/home/agent/.claude/last-task.marker"]
    assert second[-3] == "-l"
    assert second[-2] == "--"
    assert second[-1] == "\x1b[200~line one\nline two\x1b[201~"
    assert third[-1] == "Enter"
    assert "-l" not in third


async def test_send_text_docker_touches_recycler_marker(monkeypatch):
    """Fix round 3 (live-gate finding): the fleet's agent-recycler kills idle
    claude sessions based on last-task.marker's mtime — chat input must
    refresh it or an idle agent gets recycled mid chat-conversation."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        return _IDLE_PANE

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "hello agent")

    touch_calls = [c for c in calls if "touch" in c]
    assert len(touch_calls) == 1
    assert touch_calls[0] == [
        "docker", "exec", "-u", "agent", "mc-agent-rex",
        "touch", "/home/agent/.claude/last-task.marker",
    ]


async def test_send_keys_does_not_touch_recycler_marker(monkeypatch):
    """Only send_text refreshes the marker — send_keys (Escape/Enter/digits/
    y-n) is control input, not the kind of activity the recycler should
    treat as a live conversation continuing."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_keys(agent, ["Escape"])

    assert not any("touch" in c for c in calls)


async def test_send_text_docker_dash_prefixed_single_line_gets_double_dash(monkeypatch):
    """Reproduces the review finding: text starting with '-' would otherwise
    be parsed by tmux as a flag (even after -l) and silently dropped."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        return _IDLE_PANE

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "-h")

    assert calls[1][-3:] == ["-l", "--", "-h"]  # calls[0] is the recycler-marker touch


async def test_send_text_docker_dash_bullet_single_line_gets_double_dash(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        return _IDLE_PANE

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "- bullet")

    assert calls[1][-3:] == ["-l", "--", "- bullet"]  # calls[0] is the recycler-marker touch


async def test_send_text_docker_readiness_gate_types_after_becoming_ready_mid_poll(monkeypatch):
    """Send-readiness-gate hardening: capture_pane returns an unrecognizable
    (booting) pane for the first two polls, then a ready pane — the gate
    must keep polling (not give up early) and let the actual keystrokes
    through once the pane resolves to a real status."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []
    capture_count = {"n": 0}
    sleep_calls: list[float] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        capture_count["n"] += 1
        # First two polls: still booting (unrecognized pane) -> "unknown".
        if capture_count["n"] <= 2:
            return _UNMATCHED_PANE
        return _IDLE_PANE

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "hello agent")

    assert capture_count["n"] == 3  # 2 booting polls + 1 that finally reads ready
    assert len(sleep_calls) == 2  # slept between each of the two booting polls
    assert len(calls) == 3  # touch + literal text + Enter — typing DID happen
    assert calls[1][-3:] == ["-l", "--", "hello agent"]
    assert calls[2][-1] == "Enter"


async def test_send_text_docker_readiness_gate_never_ready_raises_and_types_nothing(monkeypatch):
    """The other half of the same finding: a pane that NEVER becomes
    recognizable within the poll budget (persistent boot failure, stuck
    respawn) must raise AgentStartingError and must NOT type anything — the
    whole point of the gate is that a half-booted TUI never sees
    send-keys."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []
    capture_count = {"n": 0}

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        capture_count["n"] += 1
        return _UNMATCHED_PANE  # never resolves to a recognized status

    async def _fake_sleep(delay):
        pass

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(agent_chat_input.AgentStartingError):
        await agent_chat_input.send_text(agent, "hello agent")

    assert capture_count["n"] == agent_chat_input._SEND_READINESS_POLL_ATTEMPTS
    # ONLY the recycler-marker touch went out — zero send-keys, exactly the
    # "never type into a half-booted TUI" contract the router turns into
    # 409 {"reason": "agent_starting"}.
    assert len(calls) == 1
    assert calls[0] == [
        "docker", "exec", "-u", "agent", "mc-agent-rex",
        "touch", "/home/agent/.claude/last-task.marker",
    ]


async def test_send_text_docker_readiness_gate_working_pane_types_immediately_no_poll(monkeypatch):
    """A pane already mid-turn (spinner visible) is NOT a boot state — the
    gate must resolve on the very first capture and type right away.
    Queueing text into a working agent is legitimate (see the queued-draft
    idle-detection fix); this gate only protects against booting/respawning
    panes, never against a busy-but-alive one."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []
    capture_count = {"n": 0}
    sleep_calls: list[float] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        capture_count["n"] += 1
        return _WORKING_PANE

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "queued message")

    assert capture_count["n"] == 1  # resolved on the very first poll
    assert sleep_calls == []  # never had to wait
    assert len(calls) == 3  # touch + literal text + Enter
    assert calls[1][-3:] == ["-l", "--", "queued message"]
    assert calls[2][-1] == "Enter"


async def test_send_text_docker_marker_touched_before_readiness_gate_starts_polling(monkeypatch):
    """Marker-touch-before-gate ordering (explicit hardening requirement):
    the recycler marker must be refreshed BEFORE the gate's first
    capture_pane call, not after — a gate poll can run for up to ~20s, and
    without an early touch the recycler could decide the session is idle
    and kill it while this function is still waiting on the gate."""
    from app.services import agent_chat_input

    events: list[tuple[str, object]] = []

    async def _fake_run(argv):
        events.append(("exec", argv))

    async def _fake_capture_pane(agent):
        events.append(("capture", None))
        return _IDLE_PANE

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "hello agent")

    assert events[0] == (
        "exec",
        ["docker", "exec", "-u", "agent", "mc-agent-rex", "touch", "/home/agent/.claude/last-task.marker"],
    )
    assert events[1][0] == "capture"  # gate's first poll happens right after the touch


async def test_send_keys_docker_named_key_no_literal_flag(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_keys(agent, ["Escape"])

    assert len(calls) == 1
    assert calls[0][-1] == "Escape"
    assert "-l" not in calls[0]


async def test_send_keys_docker_digit_key_uses_literal_flag(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_keys(agent, ["1", "y"])

    assert len(calls) == 2
    assert calls[0][-3:] == ["-l", "--", "1"]
    assert calls[1][-3:] == ["-l", "--", "y"]


async def test_send_keys_rejects_non_allowlisted_key(monkeypatch):
    from app.services import agent_chat_input

    called = False

    async def _fake_run(argv):
        nonlocal called
        called = True

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(ValueError):
        await agent_chat_input.send_keys(agent, ["Escape", "F5"])

    assert not called  # nothing delivered once one key fails validation


async def test_run_docker_exec_timeout_does_not_raise(monkeypatch):
    """A wedged ``docker exec`` (daemon stall, container in uninterruptible
    state) must not raise — same fail-silent contract as every other
    delivery failure this module swallows, and the same ``timeout=5`` its
    sibling ``pane_state.capture_pane`` already uses (review finding I-2).
    Without the timeout, a hang here pins a thread from the shared executor
    pool forever and ``POST /chat/input`` never returns."""
    import subprocess

    from app.services import agent_chat_input

    def _fake_run(argv, capture_output=True, timeout=None):
        assert timeout == 5
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(agent_chat_input.subprocess, "run", _fake_run)

    await agent_chat_input._run_docker_exec(["docker", "exec", "mc-agent-rex", "true"])
    # No exception -> pass.


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — Boss (host) delivery
# ══════════════════════════════════════════════════════════════════════════


async def test_send_text_boss_sends_bytes_over_ws(monkeypatch):
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.send_text(agent, "deploy the thing")

    assert fake_client.connected_urls == ["ws://host.docker.internal:7682/"]
    assert fake_client.sent == [b"deploy the thing", b"\r"]


async def test_send_text_boss_does_not_touch_recycler_marker(monkeypatch):
    """The recycler marker lives inside the cli-bridge container's
    filesystem — Boss has no docker exec path at all, so send_text must
    never attempt a docker-exec touch for it."""
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    docker_calls: list[list[str]] = []

    async def _fake_run(argv):
        docker_calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.send_text(agent, "deploy the thing")

    assert docker_calls == []


async def test_send_text_boss_sends_text_then_enter_as_separate_frames_with_delay(monkeypatch):
    """Fix round 2 — reproduced live: text + '\\r' sent as one frame (or two
    frames with no gap) makes the Claude TUI treat the Enter as part of a
    paste and never submit. Text must land, THEN a delay, THEN Enter as its
    own frame."""
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    async def _fake_sleep(delay):
        fake_client.events.append(("sleep", delay))

    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.send_text(agent, "deploy the thing")

    assert fake_client.events == [
        ("send", b"deploy the thing"),
        ("sleep", 0.15),
        ("send", b"\r"),
    ]


async def test_send_text_boss_host_alias_slug(monkeypatch):
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    agent = _StubAgent(slug="boss-host", agent_runtime="host")
    await agent_chat_input.send_text(agent, "hi")

    assert fake_client.sent == [b"hi", b"\r"]


async def test_send_keys_boss_sends_mapped_bytes(monkeypatch):
    from app.services import agent_chat_input

    fake_client = _FakeWSClient()
    monkeypatch.setattr(agent_chat_input, "ws_client", fake_client)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.send_keys(agent, ["Up", "Enter"])

    assert fake_client.sent == [b"\x1b[A", b"\r"]


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — unsupported runtimes
# ══════════════════════════════════════════════════════════════════════════


async def test_send_text_other_host_agent_raises_input_not_supported():
    from app.services.agent_chat_input import InputNotSupportedError, send_text

    agent = _StubAgent(slug="hermes", agent_runtime="host")
    with pytest.raises(InputNotSupportedError):
        await send_text(agent, "hi")


async def test_send_keys_other_host_agent_raises_input_not_supported():
    from app.services.agent_chat_input import InputNotSupportedError, send_keys

    agent = _StubAgent(slug="hermes", agent_runtime="host")
    with pytest.raises(InputNotSupportedError):
        await send_keys(agent, ["Enter"])


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — effort switching (set_effort)
#
# Phase-0 discovery (live, throwaway tmux window on mc-agent-freecode,
# Claude Code 2.1.233): `/effort <level>` as a direct argument switches
# effort instantly (confirmed via the pane's own "<level> · /effort"
# status-line indicator AND the statusline-state file's effort.level).
# The /model picker's Left/Right/"s" (session-only) sequence was also tested
# and rejected as the implementation path: "s" correctly scopes a MODEL
# choice to the session (settings.json's "model" stayed unchanged) but does
# NOT extend that scoping to effort — settings.json's "effortLevel" changed
# identically via BOTH the direct-argument form and the picker's "s" path
# (verified twice, cleanly isolated from the model-selection side effect the
# first attempt accidentally introduced). Since the picker buys no
# behavioral difference over the direct argument for effort, the simpler
# path was implemented; ALLOWED_KEYS was NOT extended with Left/Right/s.
#
# Wave-review I-1 (fix round): Escape is this app's INTERRUPT key, not a
# neutral cleanup — set_effort must never send anything into a working turn
# or an open permission prompt, and a verify-timeout Escape cleanup must only
# fire when a FRESH pane capture confirms the pane is no longer busy.
#
# Dynamic effort levels (fix round): the CLI's OWN invalid-argument error
# (zero persistence risk) is the authoritative list — "low, medium, high,
# xhigh, max, ultracode, auto". "auto" is deliberately excluded from
# ALLOWED_EFFORT_LEVELS (clears the override rather than setting one, no
# stable displayed state). "max"/"ultracode" turned out to be session-only
# BY CLI DESIGN ("this session only" in their own confirmation text,
# settings.json genuinely untouched) — unlike the other 4, which persist —
# and neither renders the compact "<level> · /effort" status-line badge the
# original verification polled for. Verification now polls for the CLI's
# inline confirmation line instead ("effort level to <level>", present in
# both phrasings), so the marker text in these fixtures reads that way.
# ══════════════════════════════════════════════════════════════════════════

# Synthetic pane snapshots exercising the real parse_pane_state heuristics
# (not mocked) — same fixture shapes as test_pane_state.py's own, kept local
# here so this file's busy/idle assumptions don't silently drift with edits
# over there.
_WORKING_PANE = "✻ Thinking… (esc to interrupt)\n\n  Reading demo.py\n"

_PERMISSION_PROMPT_PANE = (
    "Do you want to make this edit to demo.py?\n"
    "❯ 1. Yes\n"
    "  2. Yes, and don't ask again this session\n"
)

_IDLE_PANE = "╭──────────╮\n│ ❯          │\n╰──────────╯\n  ? for shortcuts\n"

# Doesn't match any parse_pane_state rule (no spinner, no options, no ❯/>
# marker) -> "unknown", which is NOT a busy status. Used where the test only
# cares that the pane is *not* busy, not that it's affirmatively idle.
_UNMATCHED_PANE = "nothing matching here"


async def _patch_effort_deps(monkeypatch, agent_chat_input, *, pane_sequence: list):
    """Shared fixture wiring for set_effort tests: captures docker-exec argv
    calls, stubs capture_pane to walk through ``pane_sequence`` on successive
    calls (repeating the last entry once exhausted — so a single-element
    sequence behaves like a fixed snapshot for every poll), and stubs
    asyncio.sleep so verification polling doesn't actually sleep."""
    calls: list[list[str]] = []
    call_count = {"n": 0}

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        idx = min(call_count["n"], len(pane_sequence) - 1)
        call_count["n"] += 1
        return pane_sequence[idx]

    async def _fake_sleep(delay):
        pass

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)
    return calls


async def test_set_effort_success_sends_command_and_verifies(monkeypatch):
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input,
        pane_sequence=[
            "❯ /effort high\n"
            "  ⎿  Set effort level to high (saved as your default for new sessions): ..."
        ],
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.set_effort(agent, "high")

    assert len(calls) == 2  # /effort high (literal) + Enter — no Escape
    first, second = calls
    assert first[-3:] == ["-l", "--", "/effort high"]
    assert second[-1] == "Enter"


async def test_set_effort_success_session_only_level(monkeypatch):
    """max/ultracode are session-only by CLI design and never show the
    compact status-line badge — verification must succeed via the inline
    confirmation line alone (its "(this session only)" phrasing)."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input,
        pane_sequence=[
            "❯ /effort max\n"
            "  ⎿  Set effort level to max (this session only): ..."
        ],
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.set_effort(agent, "max")

    assert len(calls) == 2
    assert calls[0][-3:] == ["-l", "--", "/effort max"]
    assert calls[1][-1] == "Enter"


async def test_set_effort_accepts_all_six_discovered_levels(monkeypatch):
    """The full authoritative list from the CLI's own invalid-argument error
    message — low/medium/high/xhigh persist, max/ultracode are session-only,
    but all 6 must be accepted and verifiable via the confirmation line."""
    from app.services import agent_chat_input

    for level in agent_chat_input.ALLOWED_EFFORT_LEVELS:
        calls = await _patch_effort_deps(
            monkeypatch, agent_chat_input,
            # Mit Kommando-Echo wie im echten Pane: seit dem Stale-Zeilen-Fix
            # (18.08.2026) zaehlt eine Bestaetigung nur HINTER dem eigenen Echo.
            pane_sequence=[f"❯ /effort {level}\n  ⎿  Set effort level to {level} (...)"],
        )
        agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
        await agent_chat_input.set_effort(agent, level)
        assert calls[0][-3:] == ["-l", "--", f"/effort {level}"]


async def test_set_effort_rejects_non_allowlisted_level(monkeypatch):
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(monkeypatch, agent_chat_input, pane_sequence=[None])

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(ValueError):
        # "auto" IS a CLI-accepted /effort argument (confirmed live) but is
        # deliberately excluded from ALLOWED_EFFORT_LEVELS — see the module
        # docstring for why (clears the override, no stable displayed state).
        await agent_chat_input.set_effort(agent, "auto")

    assert calls == []  # nothing delivered — validated before any docker call


async def test_set_effort_non_docker_agent_raises_input_not_supported(monkeypatch):
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(monkeypatch, agent_chat_input, pane_sequence=[None])

    agent = _StubAgent(slug="hermes", agent_runtime="host")
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_effort(agent, "high")

    assert calls == []  # no docker exec attempted for an unsupported runtime


async def test_set_effort_host_without_claude_harness_not_supported(monkeypatch):
    """Host-Agent OHNE Claude-Harness bleibt unschaltbar — der alte
    Boss-Fall (harness=claude) ist seit 19.08.2026 ein eigener Schaltweg
    (Bridge + Transkript-Verify, eigene Tests oben)."""
    from app.services import agent_chat_input

    agent = _StubAgent(slug="hermes", agent_runtime="host", harness="hermes")
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_effort(agent, "high")


async def test_set_effort_preflight_blocks_when_working(monkeypatch):
    """I-1: a working pane must reject BEFORE anything is sent — not queue
    /effort behind the running turn."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input, pane_sequence=[_WORKING_PANE]
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(agent_chat_input.AgentBusyError):
        await agent_chat_input.set_effort(agent, "high")

    assert calls == []  # not even the send-keys call — TUI never touched


async def test_set_effort_preflight_blocks_when_permission_prompt(monkeypatch):
    """I-1: an open permission prompt must also reject before anything is
    sent — /effort would otherwise queue behind an unrelated approval."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input, pane_sequence=[_PERMISSION_PROMPT_PANE]
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(agent_chat_input.AgentBusyError):
        await agent_chat_input.set_effort(agent, "high")

    assert calls == []


async def test_set_effort_preflight_allows_idle_pane(monkeypatch):
    """A plain idle prompt (❯ with no spinner, no options) must NOT be
    misclassified as busy — regression guard for the transcript_active
    choice in _pane_is_busy (see its docstring: True would make every idle
    pane look "working" and permanently block this endpoint)."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input,
        pane_sequence=[_IDLE_PANE, "❯ /effort low\n  ⎿  Set effort level to low (saved as your default for new sessions): ..."],
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.set_effort(agent, "low")

    assert len(calls) == 2  # reached send-keys + Enter — preflight passed


async def test_set_effort_verification_timeout_sends_escape_and_raises(monkeypatch):
    """The abort path: the pane never shows the expected confirmation (CLI
    version drifted, an unexpected picker/autocomplete opened instead, ...),
    and the pane is confirmed idle (not busy) on the fresh re-check — so the
    Escape cleanup IS safe to send. Must raise EffortSwitchFailedError
    (-> router 409 effort_switch_failed) rather than silently claim success."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input, pane_sequence=[_UNMATCHED_PANE]
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(agent_chat_input.EffortSwitchFailedError):
        await agent_chat_input.set_effort(agent, "high")

    # /effort high + Enter + Escape (cleanup) — no false-positive success.
    assert len(calls) == 3
    assert calls[0][-3:] == ["-l", "--", "/effort high"]
    assert calls[1][-1] == "Enter"
    assert calls[2][-1] == "Escape"


async def test_set_effort_verification_timeout_working_pane_skips_escape(monkeypatch):
    """I-1's core scenario: preflight passes on an idle pane, /effort is
    sent, but by the time verification polls the pane a real turn has
    started (e.g. a queued task, or the /effort itself got interpreted as
    part of an in-flight prompt). The verify-timeout cleanup must NOT send
    Escape in that case — that would abort the now-running turn instead of
    tidying up a stray UI state."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input,
        # index 0 (preflight): idle/unmatched -> not busy, proceeds.
        # index 1+ (verify loop + fresh re-check): working -> never matches
        # the effort marker AND confirms busy on the re-check.
        pane_sequence=[_UNMATCHED_PANE, _WORKING_PANE],
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(agent_chat_input.EffortSwitchFailedError):
        await agent_chat_input.set_effort(agent, "high")

    # /effort high + Enter — NO Escape, because the fresh re-check saw "working".
    assert len(calls) == 2
    assert calls[0][-3:] == ["-l", "--", "/effort high"]
    assert calls[1][-1] == "Enter"


async def test_set_effort_confirms_cached_conversation_dialog(monkeypatch):
    """Sessions mit gecachtem Verlauf fragen zurueck ("Change effort level?",
    Option "Yes" vorgewaehlt) — gefunden 19.08.2026 am Boss, erklaert auch das
    R12b-Raetsel: "Kept effort level as X" ist die Antwort auf ein verneintes
    Dialogfeld. Der Verify bestaetigt den Dialog EINMAL per Enter und pollt
    dann normal weiter."""
    from app.services import agent_chat_input

    dialog = (
        "❯ /effort low\n"
        "   Change effort level?\n"
        "   ❯ 1. Yes, switch to low\n"
        "     2. No, go back"
    )
    confirmed = dialog + "\n  ⎿  Set effort level to low (saved as your default for new sessions): ..."
    polls = {"n": 0}
    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    idle = "⏺ ok\n\n❯ \n"

    async def _fake_capture_pane(agent):
        polls["n"] += 1
        # Poll 1 = Preflight (ruhiger Prompt — der Dialog erscheint erst als
        # ANTWORT auf unser /effort); Poll 2 sieht den Dialog; nach dem
        # Enter (Poll 3) steht die Bestaetigung.
        if polls["n"] == 1:
            return idle
        return dialog if polls["n"] == 2 else confirmed

    async def _sleep(d): pass
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _sleep)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.set_effort(agent, "low")
    # /effort low + Enter + genau EIN Dialog-Enter
    enters = [c for c in calls if c[-1] == "Enter"]
    assert len(enters) == 2
    assert len(calls) == 3


async def test_set_effort_ignores_stale_rejection_from_earlier_attempt(monkeypatch):
    """Operator-Live-Bug (18.08.2026 abends): eine "Kept effort level as
    auto"-Zeile eines FRUEHEREN Versuchs stand noch sichtbar im Pane. Der
    Verify-Poll durchsuchte den GANZEN Pane und meldete jeden neuen Versuch
    sofort als abgelehnt — jeder "Erneut versuchen"-Klick scheiterte
    identisch. Die Auswertung darf nur lesen, was hinter dem Echo des
    EIGENEN Kommandos steht."""
    from app.services import agent_chat_input

    # Exakt die Bug-Form: die alte Ablehnung steht sichtbar da, die Antwort
    # auf UNSER Kommando ist noch nicht gerendert. Der ungescopte Poll las
    # hier sofort "abgelehnt" — noch bevor die CLI ueberhaupt antworten
    # konnte. Erst der Folge-Poll bringt die echte Bestaetigung.
    stale_pending = (
        "❯ /effort low\n"
        "  ⎿  Kept effort level as auto\n"        # Leiche des alten Versuchs
        "❯ /effort low"                             # UNSER Echo, noch unbeantwortet
    )
    answered = stale_pending + "\n  ⎿  Set effort level to low (saved as your default for new sessions): ..."
    calls = await _patch_effort_deps(
        monkeypatch, agent_chat_input, pane_sequence=[stale_pending, stale_pending, answered],
    )
    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.set_effort(agent, "low")  # darf NICHT ablehnen
    assert len(calls) == 2  # kein Escape — normaler Erfolg


async def test_set_effort_ignores_stale_confirmation_from_earlier_attempt(monkeypatch):
    """Gegenprobe zum Stale-Fix: auch eine ALTE Bestaetigung derselben Stufe
    darf keinen sofortigen Falsch-Erfolg liefern. Erst wenn hinter dem
    letzten Echo wirklich die Bestaetigung erscheint, gilt der Wechsel."""
    from app.services import agent_chat_input

    stale_conf_only = (
        "❯ /effort high\n"
        "  ⎿  Set effort level to high (saved as your default for new sessions): ...\n"
        "❯ /effort high"                            # neuer Versuch, noch ohne Antwort
    )
    real_conf = stale_conf_only + "\n  ⎿  Set effort level to high (saved as your default for new sessions): ..."
    polls = {"n": 0}
    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        polls["n"] += 1
        # Poll 1 = Preflight (busy-Check), Poll 2 = Verify sieht nur das alte
        # Ergebnis + unser unbeantwortetes Echo, Poll 3 = echte Bestaetigung.
        return stale_conf_only if polls["n"] <= 2 else real_conf

    async def _fake_sleep(delay):
        pass

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.set_effort(agent, "high")
    # Poll 2 durfte den alten Erfolg NICHT zaehlen -> es brauchte Poll 3.
    assert polls["n"] == 3


async def test_set_effort_explicit_rejection_raises_distinct_error_no_escape(monkeypatch):
    """Live-verified on Davinci (2026-08-18): the CLI can answer a switch
    attempt with "Kept effort level as <X>" instead of "Set effort level to
    <X>" — an explicit decline, not a verification timeout. Must raise
    EffortSwitchRejectedError (-> router 409 effort_switch_rejected) WITH
    the CLI's own message, stop polling immediately (not burn the rest of
    the attempt budget), and send NO Escape — the CLI already answered and
    left the pane in a normal ready state, nothing to clean up."""
    from app.services import agent_chat_input

    poll_count = {"n": 0}
    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        poll_count["n"] += 1
        return "❯ /effort low\n  ⎿  Kept effort level as auto"

    async def _fake_sleep(delay):
        pass

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _fake_sleep)

    agent = _StubAgent(slug="davinci", agent_runtime="cli-bridge")
    with pytest.raises(agent_chat_input.EffortSwitchRejectedError) as exc_info:
        await agent_chat_input.set_effort(agent, "low")

    assert exc_info.value.cli_message == "Kept effort level as auto"
    # /effort low + Enter only — NO Escape (nothing to clean up).
    assert len(calls) == 2
    assert calls[0][-3:] == ["-l", "--", "/effort low"]
    assert calls[1][-1] == "Enter"
    # capture_pane was called exactly twice — once for the preflight
    # busy-check (passes: this pane text has a "❯" input marker, no
    # spinner) and once for the FIRST verify-loop poll, which already sees
    # the rejection and stops immediately — not after burning through all 5
    # verify attempts.
    assert poll_count["n"] == 2


async def test_set_effort_verification_absent_pane_counts_as_not_applied(monkeypatch):
    """capture_pane returning None (container gone, tmux window missing —
    pane_state's own documented None case) must be treated as "not verified
    yet" AND "not busy" (nothing to protect from interrupting), not crash
    the polling loop."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(monkeypatch, agent_chat_input, pane_sequence=[None])

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with pytest.raises(agent_chat_input.EffortSwitchFailedError):
        await agent_chat_input.set_effort(agent, "medium")

    assert calls[-1][-1] == "Escape"


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — effort_capabilities
# ══════════════════════════════════════════════════════════════════════════


async def test_effort_capabilities_docker_agent_gets_full_level_list(monkeypatch, tmp_path):
    from app.services import agent_chat_input

    # Docker agent -> triggers the version-drift check -> resolve_cli_version
    # -> a real docker-exec subprocess call unless mocked (same real-host
    # concern as elsewhere in this file/round).
    async def _no_version(agent):
        return None

    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _no_version)
    # Siehe unten: "rex" existiert wirklich — nie gegen das echte ~/.mc lesen.
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.effort_capabilities(agent)

    assert caps == {
        "effortLevels": ["low", "medium", "high", "xhigh", "max", "ultracode"],
        "canSwitchEffort": True,
        # Kein settings.json im tmp-Home -> ehrliches None statt Ratewert.
        "effort": None,
        "effortShared": False,
        # Schaltbar -> kein Grund noetig. Der Grund traegt nur den Fall
        # "geht nicht, und zwar deswegen" (s. test_openclaude_capabilities).
        "effortReason": None,
    }
    # Same single-source constant set_effort validates against — no drift.
    assert caps["effortLevels"] == list(agent_chat_input.ALLOWED_EFFORT_LEVELS)


async def test_effort_capabilities_reports_persisted_default(monkeypatch, tmp_path):
    """Der Chip im Composer hing frueher allein am usage-Ereignis des
    Transkripts — eine frisch gestartete Session hat noch keines, also fehlte das
    Bedienelement komplett und der Effort war nicht schaltbar (Operator-Befund
    18.08.2026). Die settings.json des Agenten ist die ehrliche Zweitquelle."""
    from app.services import agent_chat_input

    async def _no_version(agent):
        return None

    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _no_version)

    cfg = tmp_path / ".mc" / "agents" / "rex" / "claude-config"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({"effortLevel": "xhigh", "model": "opus"}))
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)

    caps = await agent_chat_input.effort_capabilities(_StubAgent(slug="rex", agent_runtime="cli-bridge"))
    assert caps["effort"] == "xhigh"


async def test_effort_capabilities_ignores_unusable_settings(monkeypatch, tmp_path):
    """Fail-silent statt Behauptung: fehlende Datei, kaputtes JSON und ein
    unbekannter Wert muessen alle None ergeben — das UI zeigt dann `auto`."""
    from app.services import agent_chat_input

    async def _no_version(agent):
        return None

    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _no_version)
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)
    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")

    # 1) Datei fehlt ganz
    assert (await agent_chat_input.effort_capabilities(agent))["effort"] is None

    cfg = tmp_path / ".mc" / "agents" / "rex" / "claude-config"
    cfg.mkdir(parents=True)

    # 2) kaputtes JSON
    (cfg / "settings.json").write_text("{ das ist kein json")
    assert (await agent_chat_input.effort_capabilities(agent))["effort"] is None

    # 3) Wert, den set_effort nie akzeptieren wuerde
    (cfg / "settings.json").write_text(json.dumps({"effortLevel": "turbo"}))
    assert (await agent_chat_input.effort_capabilities(agent))["effort"] is None

    # 4) gueltiger Wert -> kommt durch
    (cfg / "settings.json").write_text(json.dumps({"effortLevel": "low"}))
    assert (await agent_chat_input.effort_capabilities(agent))["effort"] == "low"


async def test_capabilities_foreign_cli_gets_nothing_claude_specific():
    """Kimi/Sparky sind cli-bridge, aber KEIN Claude: /effort- und
    /model-Vokabular darf dort weder angeboten noch getippt werden
    (kritischer Test-Durchgang 18.08.2026 — vorher galt jeder
    cli-bridge-Agent als Claude)."""
    from app.services import agent_chat_input

    for harness in ("kimi", "omp"):
        agent = _StubAgent(slug="kimi", agent_runtime="cli-bridge", harness=harness)
        caps = await agent_chat_input.effort_capabilities(agent)
        assert caps == {"effortLevels": [], "canSwitchEffort": False, "effort": None,
                        "effortShared": False, "effortReason": caps["effortReason"],
                        "effortModel": None}
        assert caps["effortReason"] in ("foreign_harness", "no_pane")
        slash = await agent_chat_input.slash_command_capabilities(agent)
        assert slash == {"slashCommands": []}
        models = await agent_chat_input.model_options_capabilities(agent)
        assert models == {"modelOptions": [], "model": None}
        with pytest.raises(agent_chat_input.InputNotSupportedError):
            await agent_chat_input.set_effort(agent, "high")


async def test_effort_capabilities_host_claude_gets_ladder_but_no_switch(monkeypatch, tmp_path):
    """Boss-Gestalt (host + harness=claude, 18.08.2026): die Stufenleiter des
    Harness kommt mit, das Schaltrecht nicht — das Frontend proportioniert
    damit die Saeule des read-only Brain-Chips statt Boss das nackte
    Alt-Label zu zeigen."""
    from app.services import agent_chat_input

    # Vierte Sichtung des Real-Host-Leak-Musters: der Boss-Zweig liest
    # ~/.claude/settings.json — ohne tmp-Home stuende hier Marks ECHTE Stufe.
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)
    agent = _StubAgent(slug="boss", agent_runtime="host")
    agent.harness = "claude"
    caps = await agent_chat_input.effort_capabilities(agent)
    assert caps == {
        "effortLevels": list(agent_chat_input.ALLOWED_EFFORT_LEVELS),
        "canSwitchEffort": True,   # seit 19.08.2026: Bridge tippt, Transkript verifiziert
        "effort": None,
        "effortShared": True,      # Boss teilt ~/.claude/settings.json mit dem Operator
        "effortReason": None,
    }


async def test_set_effort_boss_types_via_bridge_and_verifies_in_transcript(monkeypatch, tmp_path):
    """Boss-Schaltweg (19.08.2026): Bridge tippt, das TRANSKRIPT bestaetigt —
    /effort schreibt seine stdout-Zeile in die Session-Datei. Gelesen wird nur,
    was nach der vorab notierten Dateigroesse dazukommt (Stale-Lektion)."""
    import json as _json
    from app.services import agent_chat_input

    f = tmp_path / "sess.jsonl"
    stale = _json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Set effort level to high (alt)"}]}}) + "\n"
    f.write_text(stale)
    import os, time as _t
    os.utime(f, (_t.time() - 300, _t.time() - 300))  # alt -> Preflight nicht busy

    sent: list[bytes] = []
    async def _fake_bridge(*payloads, delay_before_last=0.0):
        sent.extend(payloads)
        # CLI "antwortet" ins Transkript
        with open(f, "a") as fh:
            fh.write(_json.dumps({"type": "user", "message": {"content": "cmd"}}) + "\n")
            fh.write(_json.dumps({"type": "system", "text": "Set effort level to max (this session only)"}) + "\n")

    async def _sleep(d): pass
    monkeypatch.setattr(agent_chat_input, "_send_boss_bytes", _fake_bridge)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _sleep)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tmp_path)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.set_effort(agent, "max")
    assert sent and sent[0] == b"/effort max"


async def test_set_effort_boss_ignores_stale_confirmation(monkeypatch, tmp_path):
    """Steht die (identische) Bestaetigung nur VOR der Baseline, ist das kein
    Erfolg — ohne neue Zeilen laeuft das Budget ab, OHNE Escape (kein Pane,
    ein Escape koennte echte Boss-Arbeit abbrechen)."""
    import json as _json
    from app.services import agent_chat_input

    f = tmp_path / "sess.jsonl"
    f.write_text(_json.dumps({"type": "system", "text": "Set effort level to max (this session only)"}) + "\n")
    import os, time as _t
    os.utime(f, (_t.time() - 300, _t.time() - 300))

    sent: list[bytes] = []
    async def _fake_bridge(*payloads, delay_before_last=0.0):
        sent.extend(payloads)
    async def _sleep(d): pass
    monkeypatch.setattr(agent_chat_input, "_send_boss_bytes", _fake_bridge)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _sleep)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tmp_path)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    with pytest.raises(agent_chat_input.EffortSwitchFailedError):
        await agent_chat_input.set_effort(agent, "max")
    # Kommando + Enter + EIN blinder Dialog-Enter — aber KEIN Escape-Byte
    assert b"\x1b" not in b"".join(sent)
    assert sent.count(b"\r") == 2  # Submit-Enter + einmalige Dialog-Bestaetigung


async def test_set_effort_boss_busy_preflight_blocks(monkeypatch, tmp_path):
    """Frisches Transkript + Zug laeuft (letzte inhaltliche Zeile kein reiner
    Antwort-Text) -> AgentBusyError, es wird NICHTS getippt."""
    import json as _json
    from app.services import agent_chat_input

    f = tmp_path / "sess.jsonl"
    f.write_text(_json.dumps({"type": "user", "message": {"content": "mach was"}}) + "\n")

    sent: list[bytes] = []
    async def _fake_bridge(*payloads, delay_before_last=0.0):
        sent.extend(payloads)
    monkeypatch.setattr(agent_chat_input, "_send_boss_bytes", _fake_bridge)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tmp_path)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    with pytest.raises(agent_chat_input.AgentBusyError):
        await agent_chat_input.set_effort(agent, "max")
    assert sent == []


async def test_send_text_skips_readiness_gate_for_foreign_cli(monkeypatch):
    """Sparky-Befund (19.08.2026): das Readiness-Gate liest mit Claude-Regeln —
    eine omp/kimi-TUI erfuellt sie nie, jeder Send endete 409 agent_starting.
    Fuer fremde Harnesses wird blind zugestellt."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []
    async def _fake_run(argv): calls.append(argv)
    async def _fake_marker(slug): pass
    async def _boom(agent):
        raise AssertionError("readiness gate darf fuer fremde CLIs nicht laufen")
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "_touch_recycler_marker", _fake_marker)
    monkeypatch.setattr(agent_chat_input, "_wait_for_send_readiness", _boom)

    agent = _StubAgent(slug="sparky", agent_runtime="cli-bridge", harness="omp")
    await agent_chat_input.send_text(agent, "hallo sparky")
    assert any("hallo sparky" in " ".join(c) for c in calls)


async def test_effort_capabilities_host_without_claude_harness_gets_nothing():
    """Host-Agent OHNE Claude-Harness (unset/legacy): weder Leiter noch
    Schaltrecht — der Boss-Fall mit harness=claude bekommt die Leiter
    (eigener Test oben)."""
    from app.services import agent_chat_input

    agent = _StubAgent(slug="boss", agent_runtime="host", harness=None)
    caps = await agent_chat_input.effort_capabilities(agent)

    assert caps == {"effortLevels": [], "canSwitchEffort": False, "effort": None,
                    "effortShared": False, "effortReason": caps["effortReason"],
                    # ``effortModel`` steht in JEDER Antwort mit Grund, damit das
                    # Frontend eine feste Form hat — genannt hat die CLI hier
                    # aber kein Modell.
                    "effortModel": None}
    assert caps["effortReason"] in ("foreign_harness", "no_pane")


async def test_effort_capabilities_other_host_agent_cannot_switch():
    from app.services import agent_chat_input

    agent = _StubAgent(slug="hermes", agent_runtime="host", harness="hermes")
    caps = await agent_chat_input.effort_capabilities(agent)

    assert caps == {"effortLevels": [], "canSwitchEffort": False, "effort": None,
                    "effortShared": False, "effortReason": caps["effortReason"],
                    # ``effortModel`` steht in JEDER Antwort mit Grund, damit das
                    # Frontend eine feste Form hat — genannt hat die CLI hier
                    # aber kein Modell.
                    "effortModel": None}
    assert caps["effortReason"] in ("foreign_harness", "no_pane")


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — effort-levels version-drift check
#
# ALLOWED_EFFORT_LEVELS is NEVER auto-reprobed on a version mismatch
# (/effort persists to settings.json — an unattended reprobe would silently
# change a real agent's default) — this only logs, once per cli_version
# fleet-wide via a Redis SET NX EX dedup.
# ══════════════════════════════════════════════════════════════════════════


async def test_effort_capabilities_matching_version_does_not_log(monkeypatch, fake_redis, caplog):
    import app.redis_client as redis_client_mod
    from app.services import agent_chat_input

    monkeypatch.setattr(redis_client_mod, "_redis", fake_redis)

    async def _matching_version(agent):
        return agent_chat_input._EFFORT_LEVELS_VERIFIED_CLI_VERSION["claude"]

    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _matching_version)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with caplog.at_level("WARNING", logger="mc.agent_chat_input"):
        await agent_chat_input.effort_capabilities(agent)

    assert "Phase-0-Nachlauf" not in caplog.text


async def test_effort_capabilities_version_drift_logs_once_per_version(monkeypatch, fake_redis, caplog):
    import app.redis_client as redis_client_mod
    from app.services import agent_chat_input

    monkeypatch.setattr(redis_client_mod, "_redis", fake_redis)

    async def _newer_version(agent):
        return "2.9.999"

    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _newer_version)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with caplog.at_level("WARNING", logger="mc.agent_chat_input"):
        await agent_chat_input.effort_capabilities(agent)
        first_count = caplog.text.count("Phase-0-Nachlauf")
        caplog.clear()
        await agent_chat_input.effort_capabilities(agent)  # same version again
        second_count = caplog.text.count("Phase-0-Nachlauf")

    assert first_count == 1
    assert second_count == 0  # deduped via the Redis SET NX


async def test_effort_capabilities_no_version_does_not_log(monkeypatch, fake_redis, caplog):
    """resolve_cli_version returning None (container gone, check failed) is
    a normal "can't tell" outcome, not evidence of drift — must not log."""
    import app.redis_client as redis_client_mod
    from app.services import agent_chat_input

    monkeypatch.setattr(redis_client_mod, "_redis", fake_redis)

    async def _no_version(agent):
        return None

    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _no_version)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    with caplog.at_level("WARNING", logger="mc.agent_chat_input"):
        caps = await agent_chat_input.effort_capabilities(agent)

    assert "Phase-0-Nachlauf" not in caplog.text
    # Still returns the normal capability payload — the drift check is
    # purely observability, never affects the response.
    assert caps["canSwitchEffort"] is True


async def test_effort_capabilities_still_returns_levels_despite_drift(monkeypatch, fake_redis):
    """The whole point: a version mismatch changes nothing about what's
    served — same static, verified level list either way."""
    import app.redis_client as redis_client_mod
    from app.services import agent_chat_input

    monkeypatch.setattr(redis_client_mod, "_redis", fake_redis)

    async def _newer_version(agent):
        return "2.9.999"

    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _newer_version)
    # "rex" ist ein echter Fleet-Slug: ohne dieses Patch liest der Test die
    # settings.json des LAUFENDEN Agenten vom Host und wird von dessen aktueller
    # Effort-Stufe abhaengig (gleiche Real-Host-Leak-Klasse wie die gemockten
    # Subprozess-Aufrufe weiter oben).
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.effort_capabilities(agent)

    assert caps == {
        "effortLevels": ["low", "medium", "high", "xhigh", "max", "ultracode"],
        "canSwitchEffort": True,
        "effort": None,
        "effortShared": False,
        "effortReason": None,
    }


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — model_options_capabilities
#
# Config-driven (settings.model_aliases + transcript_chat.resolve_context_window
# via settings.context_windows) — not agent/runtime-gated, unlike effort/
# slash-command capabilities.
# ══════════════════════════════════════════════════════════════════════════


async def _patch_model_options_deps(monkeypatch, agent_chat_input, *, catalog, observed, model=None):
    monkeypatch.setattr(
        agent_chat_input, "discover_model_catalog", _async_return(catalog)
    )
    monkeypatch.setattr(
        agent_chat_input, "get_observed_model_windows", _async_return(observed)
    )
    # "rex" ist ein echter Fleet-Slug: ohne dieses Patch laese der neue
    # capabilities.model-Zweig die settings.json des LAUFENDEN Agenten vom Host
    # (Real-Host-Leak, dritte Sichtung heute — gleiche Klasse wie bei den
    # gemockten Subprozessen und den Effort-Tests).
    monkeypatch.setattr(agent_chat_input, "_persisted_model", lambda slug: model)


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


async def test_model_options_capabilities_falls_back_to_static_aliases_when_catalog_empty(monkeypatch):
    """Empty catalog (cold cache / no harness for this runtime / discovery
    failed) -> settings.model_aliases is the fallback, exactly like before
    harness-catalog discovery existed."""
    from app.services import agent_chat_input

    await _patch_model_options_deps(monkeypatch, agent_chat_input, catalog=[], observed={})

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.model_options_capabilities(agent)

    options = {o["command"]: o for o in caps["modelOptions"]}
    assert set(options) == {"default", "opus", "sonnet", "haiku"}
    assert options["opus"] == {
        "command": "opus", "label": "Opus", "contextWindow": 1_000_000,
    }
    assert options["sonnet"] == {
        "command": "sonnet", "label": "Sonnet", "contextWindow": 1_000_000,
    }
    assert options["haiku"] == {
        "command": "haiku", "label": "Haiku", "contextWindow": 200_000,
    }
    # "default" resolves to whatever settings.model_aliases["default"]
    # points at (claude-sonnet-5) — same 1M window, not a special case.
    assert options["default"]["contextWindow"] == 1_000_000


async def test_model_options_capabilities_boss_reads_shared_operator_config(monkeypatch, tmp_path):
    """Boss liest ~/.claude/settings.json (CLAUDE_CONFIG_DIR unset). Ohne
    diesen Zweig stand nach /clear ein "—" im Composer, bis die erste
    Nachricht ein usage-Ereignis erzeugte (Operator-Screenshot 19.08.2026).
    Gegenstueck zum Geister-Modell-Test darunter: DIESER Pfad ist der
    richtige, das mc-Agenten-Muster der falsche."""
    import json as _json
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "discover_model_catalog", _async_return([]))
    monkeypatch.setattr(agent_chat_input, "get_observed_model_windows", _async_return({}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(_json.dumps({"model": "opus"}))
    # Geister-Config im mc-Muster daneben — darf NICHT gewinnen.
    ghost = tmp_path / ".mc" / "agents" / "boss" / "claude-config"
    ghost.mkdir(parents=True)
    (ghost / "settings.json").write_text(_json.dumps({"model": "glm-5.1:cloud"}))
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    caps = await agent_chat_input.model_options_capabilities(agent)
    assert caps["model"] == "opus"


async def test_model_options_capabilities_host_agent_never_reads_stale_config(monkeypatch, tmp_path):
    """Live-Fund direkt nach dem Deploy (18.08.2026): Boss' capabilities.model
    war "glm-5.1:cloud" — aus ~/.mc/agents/boss/claude-config, einem seit
    April brachliegenden Ordner, den Boss (host, CLAUDE_CONFIG_DIR unset)
    nie liest. Persistiertes Modell darf NUR fuer docker/cli-bridge gelesen
    werden; fuer Host-Agenten ist None die einzige ehrliche Antwort."""
    from app.services import agent_chat_input

    # BEWUSST nicht _patch_model_options_deps: das wuerde _persisted_model
    # stubben — und genau dessen echtes Verhalten ist hier der Pruefling.
    # (Erste Fassung dieses Tests tappte in die Falle: der "Restore" der
    # echten Funktion griff den bereits gesetzten Stub — Sabotage-Probe
    # bestand nicht. Darum nur die beiden Discovery-Abhaengigkeiten stubben.)
    monkeypatch.setattr(agent_chat_input, "discover_model_catalog", _async_return([]))
    monkeypatch.setattr(agent_chat_input, "get_observed_model_windows", _async_return({}))
    # Geister-Config anlegen — sie DARF nicht gelesen werden.
    cfg = tmp_path / ".mc" / "agents" / "boss" / "claude-config"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text('{"model": "glm-5.1:cloud"}')
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    caps = await agent_chat_input.model_options_capabilities(agent)
    assert caps["model"] is None


async def test_model_options_capabilities_unknown_model_id_yields_null_window(monkeypatch):
    from app.services import agent_chat_input
    from app.config import settings

    monkeypatch.setattr(
        settings, "model_aliases", {"mystery": "some-future-model-nobody-configured"}
    )
    await _patch_model_options_deps(monkeypatch, agent_chat_input, catalog=[], observed={})

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.model_options_capabilities(agent)

    assert caps == {
        "model": None,
        "modelOptions": [
            {"command": "mystery", "label": "Mystery", "contextWindow": None},
        ]
    }


async def test_model_options_capabilities_prefers_discovered_catalog_over_static_aliases(monkeypatch):
    """A non-empty catalog (the agent's OWN /model picker rows) wins over
    settings.model_aliases entirely — including surfacing a LOCAL model row
    the static alias map has no entry for at all."""
    from app.services import agent_chat_input

    catalog = [
        {"command": "default", "label": "Default"},
        {"command": "opus", "label": "Opus"},
        {"command": "Qwen/Qwen3.6-35B-A3B-FP8", "label": "Qwen/Qwen3.6-35B-A3B-FP8"},
    ]
    await _patch_model_options_deps(monkeypatch, agent_chat_input, catalog=catalog, observed={})

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.model_options_capabilities(agent)

    commands = [o["command"] for o in caps["modelOptions"]]
    assert commands == ["default", "opus", "Qwen/Qwen3.6-35B-A3B-FP8"]
    # Known aliases in the catalog still resolve a window via model_aliases.
    options = {o["command"]: o for o in caps["modelOptions"]}
    assert options["opus"]["contextWindow"] == 1_000_000
    # The local model isn't in model_aliases at all — no window guess exists
    # for it yet (would come from the observed map once a real turn runs on
    # it), so null rather than a fabricated number.
    assert options["Qwen/Qwen3.6-35B-A3B-FP8"]["contextWindow"] is None


async def test_model_options_capabilities_observed_map_overrides_config_seed(monkeypatch):
    """The observed-window tier (real statusline reads from the fleet)
    outranks the static settings.context_windows seed — same precedence
    resolve_context_window itself documents."""
    from app.services import agent_chat_input

    await _patch_model_options_deps(
        monkeypatch, agent_chat_input,
        catalog=[],
        observed={"claude-haiku-4-5": 999_999},  # config seed says 200_000
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.model_options_capabilities(agent)

    options = {o["command"]: o for o in caps["modelOptions"]}
    assert options["haiku"]["contextWindow"] == 999_999


# ══════════════════════════════════════════════════════════════════════════
# services/agent_chat_input.py — slash_command_capabilities
#
# Skills are discovered from <claude-config>/skills/*/SKILL.md — the SAME
# per-agent directory plugin_manager.sync_agent_skills_to_disk populates for
# both plain custom skills and resolved plugin-provided symlinks, scanned
# via plugin_manager.list_skills_in_dir (reused, not reimplemented). Every
# test here clears the module-level ~60s cache first (keyed by slug) so
# results can't leak across tests.
# ══════════════════════════════════════════════════════════════════════════


def _write_skill(skills_dir, name: str, description: str | None) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    if description is None:
        (skill_dir / "SKILL.md").write_text("# no frontmatter here\njust prose\n")
    else:
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nbody\n"
        )


async def test_slash_command_capabilities_docker_merges_builtins_and_skills(monkeypatch, tmp_path):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_slash_commands_cache", {})
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "mc-debug", "Debug MC issues systematically")
    _write_skill(skills_dir, "mc-review-geben", "Give a structured code review")
    monkeypatch.setattr(agent_chat_input, "_agent_skills_dir", lambda slug: skills_dir)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.slash_command_capabilities(agent)

    names = [c["name"] for c in caps["slashCommands"]]
    assert names == [
        "model", "effort", "clear", "compact", "context", "status", "help", "resume",
        "mc-debug", "mc-review-geben",
    ]
    skill_entries = {c["name"]: c["description"] for c in caps["slashCommands"][8:]}
    assert skill_entries == {
        "mc-debug": "Debug MC issues systematically",
        "mc-review-geben": "Give a structured code review",
    }


async def test_slash_command_capabilities_missing_skills_dir_builtins_only(monkeypatch, tmp_path):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_slash_commands_cache", {})
    monkeypatch.setattr(
        agent_chat_input, "_agent_skills_dir", lambda slug: tmp_path / "does-not-exist"
    )

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.slash_command_capabilities(agent)

    assert caps == {"slashCommands": list(agent_chat_input._BUILTIN_SLASH_COMMANDS)}


async def test_slash_command_capabilities_malformed_skill_md_included_with_no_description(monkeypatch, tmp_path):
    """A SKILL.md with no parseable description: line doesn't break
    discovery or get dropped — it shows up with description=None, and a
    sibling well-formed skill in the same dir is discovered normally."""
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_slash_commands_cache", {})
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "broken-skill", None)
    _write_skill(skills_dir, "good-skill", "A perfectly normal skill")
    # A directory entry with no SKILL.md at all must be silently excluded.
    (skills_dir / "not-a-skill").mkdir(parents=True)
    monkeypatch.setattr(agent_chat_input, "_agent_skills_dir", lambda slug: skills_dir)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = await agent_chat_input.slash_command_capabilities(agent)

    skill_entries = {c["name"]: c["description"] for c in caps["slashCommands"][8:]}
    assert skill_entries == {
        "broken-skill": None,
        "good-skill": "A perfectly normal skill",
    }


async def test_slash_command_capabilities_host_agent_skips_skill_scan_entirely(monkeypatch, tmp_path):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_slash_commands_cache", {})

    def _boom(slug):
        raise AssertionError("skills scan must not be attempted for a host agent")

    monkeypatch.setattr(agent_chat_input, "_agent_skills_dir", _boom)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    caps = await agent_chat_input.slash_command_capabilities(agent)

    assert caps == {"slashCommands": list(agent_chat_input._BUILTIN_SLASH_COMMANDS)}


async def test_discover_skill_commands_result_is_cached(monkeypatch, tmp_path):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_slash_commands_cache", {})
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "mc-debug", "Debug MC issues")
    call_count = {"n": 0}

    real_list_skills_in_dir = agent_chat_input.list_skills_in_dir

    def _counting_list_skills_in_dir(d):
        call_count["n"] += 1
        return real_list_skills_in_dir(d)

    monkeypatch.setattr(agent_chat_input, "list_skills_in_dir", _counting_list_skills_in_dir)
    monkeypatch.setattr(agent_chat_input, "_agent_skills_dir", lambda slug: skills_dir)

    await agent_chat_input._discover_skill_commands("rex")
    await agent_chat_input._discover_skill_commands("rex")

    assert call_count["n"] == 1  # second call served from the ~60s cache


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/input
# ══════════════════════════════════════════════════════════════════════════


async def test_post_chat_input_204_and_forwards_text(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    calls = []

    async def _fake_send_text(a, text):
        calls.append((a.id, text))

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_text", _fake_send_text)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hello"}
    )

    assert resp.status_code == 204, resp.text
    assert calls == [(agent.id, "hello")]


async def test_post_chat_input_422_empty_text(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "   "}
    )

    assert resp.status_code == 422


async def test_post_chat_input_422_too_long(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "x" * 20001}
    )

    assert resp.status_code == 422


async def test_post_chat_input_422_nul_byte(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hello\x00world"}
    )

    assert resp.status_code == 422


async def test_post_chat_input_422_other_control_char(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hello\x1bworld"}
    )

    assert resp.status_code == 422


async def test_post_chat_input_allows_newline_and_tab(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    async def _fake_send_text(a, text):
        pass

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_text", _fake_send_text)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "line one\n\tline two"}
    )

    assert resp.status_code == 204, resp.text


async def test_post_chat_input_404_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.post(
        f"/api/v1/agents/{uuid.uuid4()}/chat/input", json={"text": "hi"}
    )
    assert resp.status_code == 404


async def test_post_chat_input_409_unsupported_runtime(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hi"}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "input_not_supported"}


async def test_post_chat_input_409_agent_starting(auth_client: AsyncClient, make_agent, monkeypatch):
    """Send-readiness-gate hardening: when send_text's gate never sees the
    pane become ready (booting/plugin-loading/recycler-respawn), the router
    must surface 409 {"reason": "agent_starting"} instead of a 500 or a
    silently-swallowed lost keystroke."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    import app.routers.agent_chat as agent_chat_mod

    async def _fake_send_text(a, text):
        raise agent_chat_mod.AgentStartingError()

    monkeypatch.setattr(agent_chat_mod, "send_text", _fake_send_text)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hi"}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "agent_starting"}


async def test_post_chat_input_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hi"}
    )

    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/keys
# ══════════════════════════════════════════════════════════════════════════


async def test_post_chat_keys_204_and_forwards_keys(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    calls = []

    async def _fake_send_keys(a, keys):
        calls.append((a.id, keys))

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_keys", _fake_send_keys)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Escape", "Enter"]}
    )

    assert resp.status_code == 204, resp.text
    assert calls == [(agent.id, ["Escape", "Enter"])]


async def test_post_chat_keys_422_non_allowlisted(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["F5"]}
    )

    assert resp.status_code == 422


async def test_post_chat_keys_422_too_many_keys(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"] * 17}
    )

    assert resp.status_code == 422


async def test_post_chat_keys_allows_max_keys(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    async def _fake_send_keys(a, keys):
        pass

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "send_keys", _fake_send_keys)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"] * 16}
    )

    assert resp.status_code == 204, resp.text


async def test_post_chat_keys_404_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.post(
        f"/api/v1/agents/{uuid.uuid4()}/chat/keys", json={"keys": ["Enter"]}
    )
    assert resp.status_code == 404


async def test_post_chat_keys_409_unsupported_runtime(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"]}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "input_not_supported"}


async def test_post_chat_keys_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/keys", json={"keys": ["Enter"]}
    )

    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/effort
# ══════════════════════════════════════════════════════════════════════════


async def test_post_chat_effort_204_and_forwards_level(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    calls = []

    async def _fake_set_effort(a, level):
        calls.append((a.id, level))

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "set_effort", _fake_set_effort)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "high"}
    )

    assert resp.status_code == 204, resp.text
    assert calls == [(agent.id, "high")]


async def test_post_chat_effort_accepts_all_six_discovered_levels(auth_client: AsyncClient, make_agent, monkeypatch):
    from app.services.agent_chat_input import ALLOWED_EFFORT_LEVELS

    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    async def _fake_set_effort(a, level):
        pass

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "set_effort", _fake_set_effort)

    for level in ALLOWED_EFFORT_LEVELS:
        resp = await auth_client.post(
            f"/api/v1/agents/{agent.id}/chat/effort", json={"level": level}
        )
        assert resp.status_code == 204, f"level={level!r}: {resp.text}"


async def test_post_chat_effort_422_non_allowlisted_level(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    import app.routers.agent_chat as agent_chat_mod
    from app.services.agent_chat_input import set_effort as real_set_effort

    monkeypatch.setattr(agent_chat_mod, "set_effort", real_set_effort)

    # "auto" is a real CLI-accepted /effort argument but deliberately
    # excluded from ALLOWED_EFFORT_LEVELS (see module docstring) — 422, not 204.
    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "auto"}
    )

    assert resp.status_code == 422


async def test_post_chat_effort_404_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.post(
        f"/api/v1/agents/{uuid.uuid4()}/chat/effort", json={"level": "high"}
    )
    assert resp.status_code == 404


async def test_post_chat_effort_409_unsupported_runtime(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "high"}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "input_not_supported"}


async def test_post_chat_effort_409_boss_unsupported_no_pane_probe(auth_client: AsyncClient, make_agent):
    """v1 scope: Boss has an input channel (send_text/send_keys) but no pane
    probe, so effort switching stays docker-only for it too."""
    agent = await make_agent(name="Boss", agent_runtime="host", slug="boss")

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "high"}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "input_not_supported"}


async def test_post_chat_effort_409_switch_failed(auth_client: AsyncClient, make_agent, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    import app.routers.agent_chat as agent_chat_mod
    from app.services.agent_chat_input import EffortSwitchFailedError

    async def _fake_set_effort(a, level):
        raise EffortSwitchFailedError()

    monkeypatch.setattr(agent_chat_mod, "set_effort", _fake_set_effort)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "high"}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "effort_switch_failed"}


async def test_post_chat_effort_409_agent_busy(auth_client: AsyncClient, make_agent, monkeypatch):
    """I-1: the router maps AgentBusyError to its own 409 reason, distinct
    from effort_switch_failed — the composer chip needs to tell "don't
    interrupt a working turn" apart from "the switch itself didn't verify"."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    import app.routers.agent_chat as agent_chat_mod
    from app.services.agent_chat_input import AgentBusyError

    async def _fake_set_effort(a, level):
        raise AgentBusyError()

    monkeypatch.setattr(agent_chat_mod, "set_effort", _fake_set_effort)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "high"}
    )

    assert resp.status_code == 409
    assert resp.json() == {"reason": "agent_busy"}


async def test_post_chat_effort_409_switch_rejected_with_cli_message(auth_client: AsyncClient, make_agent, monkeypatch):
    """Distinct from effort_switch_failed: the CLI explicitly declined the
    switch (live-verified on Davinci), and its own message text is surfaced
    so the UI can show the operator WHY."""
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    import app.routers.agent_chat as agent_chat_mod
    from app.services.agent_chat_input import EffortSwitchRejectedError

    async def _fake_set_effort(a, level):
        raise EffortSwitchRejectedError("Kept effort level as auto")

    monkeypatch.setattr(agent_chat_mod, "set_effort", _fake_set_effort)

    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "low"}
    )

    assert resp.status_code == 409
    assert resp.json() == {
        "reason": "effort_switch_rejected",
        "message": "Kept effort level as auto",
    }


async def test_post_chat_effort_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "high"}
    )

    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Review-Runde 20.08.2026 — Befunde aus dem PR-Review von #325
# ══════════════════════════════════════════════════════════════════════════

async def test_set_effort_dialog_marker_only_counts_behind_the_command_echo(monkeypatch):
    """Befund 2: der Dialog-Zweig durchsuchte den GANZEN Pane und lief VOR dem
    Zuschnitt hinter das Kommando-Echo — also genau der Alt-Scrollback-Fehler,
    den die Ablehnungszeile zwei Zeilen weiter unten schon behebt.

    Gestalt: zweimal Effort umschalten innerhalb eines Scrollbacks. Beim
    ERSTEN Verify-Poll steht die "Change effort level?"-Zeile des FRUEHEREN
    Wechsels noch sichtbar ueber dem neuen Echo. Vor dem Fix feuerte dort ein
    Streu-Enter ins LIVE-Fenster des Agenten (schickt ab, was der Operator
    gerade tippt) und verbrauchte ``confirmed_dialog`` — der ECHTE Dialog
    wurde nie beantwortet, das Budget lief ab, der Operator bekam 409."""
    from app.services import agent_chat_input

    alter_verlauf = (
        "❯ /effort low\n"
        "   Change effort level?\n"
        "   ❯ 1. Yes, switch to low\n"
        "     2. No, go back\n"
        "  ⎿  Set effort level to low (saved as your default for new sessions): ...\n"
    )
    # Poll 1: neues Echo gerendert, dahinter noch nichts.
    poll1 = alter_verlauf + "❯ /effort high\n"
    # Poll 2: die Bestaetigung des NEUEN Wechsels steht hinter dem Echo.
    poll2 = poll1 + "  ⎿  Set effort level to high (saved as your default for new sessions): ...\n"

    polls = {"n": 0}
    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _fake_capture_pane(agent):
        polls["n"] += 1
        if polls["n"] == 1:
            return _IDLE_PANE  # Preflight
        return poll1 if polls["n"] == 2 else poll2

    async def _sleep(d): pass
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _fake_capture_pane)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _sleep)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.set_effort(agent, "high")

    # NUR /effort high + das Submit-Enter. Kein Streu-Enter aus dem Alt-Dialog.
    assert len(calls) == 2, calls
    assert calls[0][-3:] == ["-l", "--", "/effort high"]
    assert calls[1][-1] == "Enter"


async def _boss_effort_env(monkeypatch, tmp_path, agent_chat_input):
    """Gemeinsame Verdrahtung der Boss-Effort-Tests: Bridge-Bytes sammeln,
    nicht schlafen, Transkript-Verzeichnis auf tmp_path."""
    sent: list[bytes] = []

    async def _fake_bridge(*payloads, delay_before_last=0.0):
        sent.extend(payloads)

    async def _sleep(d): pass
    monkeypatch.setattr(agent_chat_input, "_send_boss_bytes", _fake_bridge)
    monkeypatch.setattr(agent_chat_input.asyncio, "sleep", _sleep)
    import app.services.transcript_chat as tc
    monkeypatch.setattr(tc, "resolve_transcript_dir", lambda a: tmp_path)
    return sent


async def test_set_effort_boss_never_reads_whole_files(monkeypatch, tmp_path):
    """Befund 3: lieferte ``find_active_session`` beim Eintritt None (Boss
    frisch gestartet, Verzeichnis noch leer, stat-Race), blieb baseline_path
    None — und der else-Zweig las in JEDER der 24 Runden die GANZE Datei.
    Boss' echtes Verzeichnis hat 53 Dateien / 981 MB, die groesste 124,9 MB:
    das waeren ~3 GB Lesen und 24 aufeinanderfolgende ~125-MB-Allokationen in
    EINEM POST /chat/effort — auf einer Docker-VM mit 5 GB Deckel.

    Der Test haelt die Eigenschaft fest, nicht die Zahl: es wird NIE eine
    ganze Datei gelesen. Vor dem Fix schlug er mit 24 read_text-Aufrufen
    auf einer 2-MB-Datei fehl."""
    import json as _json
    from pathlib import Path as _Path
    from app.services import agent_chat_input
    import app.services.transcript_chat as tc

    gross = tmp_path / "gross.jsonl"
    fuellzeile = _json.dumps({"type": "system", "text": "x" * 2000}) + "\n"
    gross.write_text(fuellzeile * 1000)  # ~2 MB

    read_text_calls: list[str] = []
    _orig_read_text = _Path.read_text

    def _spy_read_text(self, *a, **k):
        read_text_calls.append(str(self))
        return _orig_read_text(self, *a, **k)

    monkeypatch.setattr(_Path, "read_text", _spy_read_text)

    sent = await _boss_effort_env(monkeypatch, tmp_path, agent_chat_input)

    # Erst NACH dem Tippen wird die Datei als aktive Session sichtbar —
    # exakt die Lage, in der baseline_path None blieb.
    sessions = {"n": 0}

    def _fake_find(_dir):
        sessions["n"] += 1
        return None if sessions["n"] == 1 else (gross, 0.0)

    monkeypatch.setattr(tc, "find_active_session", _fake_find)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    with pytest.raises(agent_chat_input.EffortSwitchFailedError):
        await agent_chat_input.set_effort(agent, "max")

    assert sent and sent[0] == b"/effort max"
    assert [c for c in read_text_calls if str(gross) in c] == [], read_text_calls


async def test_set_effort_boss_finds_confirmation_appended_after_first_sighting(monkeypatch, tmp_path):
    """Gegenstueck zum Test darueber: der Zuwachs wird sehr wohl gelesen.
    Die Datei taucht erst in der Verify-Schleife auf; was DANACH angehaengt
    wird, muss die Bestaetigung tragen und zu 204 fuehren."""
    import json as _json
    from app.services import agent_chat_input
    import app.services.transcript_chat as tc

    f = tmp_path / "sess.jsonl"
    f.write_text(_json.dumps({"type": "system", "text": "alter Kram"}) + "\n")

    sent = await _boss_effort_env(monkeypatch, tmp_path, agent_chat_input)

    sessions = {"n": 0}

    def _fake_find(_dir):
        sessions["n"] += 1
        if sessions["n"] == 1:
            return None
        if sessions["n"] == 3:
            with open(f, "a") as fh:
                fh.write(_json.dumps({
                    "type": "system", "text": "Set effort level to max (this session only)"
                }) + "\n")
        return (f, 0.0)

    monkeypatch.setattr(tc, "find_active_session", _fake_find)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.set_effort(agent, "max")
    assert sent and sent[0] == b"/effort max"


async def test_set_effort_boss_stat_race_does_not_trust_old_lines(monkeypatch, tmp_path):
    """Befund 4: ``baseline_path`` wurde VOR dem try gesetzt, im ``except
    OSError`` aber nur ``baseline_size = 0`` zurueckgesetzt. Die Schleife nahm
    danach den ``path == baseline_path``-Zweig mit Offset 0 und las die GANZE
    Datei — eine "Set effort level to max"-Zeile von vor einer Stunde erfuellte
    den Marker und ergab 204 Erfolg, obwohl nichts angewendet wurde.

    Hier: das Baseline-stat schlaegt fehl, die Datei traegt nur eine ALTE
    Bestaetigung. Erwartung: kein falscher Erfolg."""
    import json as _json
    from pathlib import Path as _Path
    from app.services import agent_chat_input
    import app.services.transcript_chat as tc

    f = tmp_path / "sess.jsonl"
    f.write_text(_json.dumps({
        "type": "system", "text": "Set effort level to max (this session only)"
    }) + "\n")

    sent = await _boss_effort_env(monkeypatch, tmp_path, agent_chat_input)
    monkeypatch.setattr(tc, "find_active_session", lambda _dir: (f, 0.0))

    # Genau EIN stat-Fehlschlag: der des Baseline-Reads.
    _orig_stat = _Path.stat
    race = {"done": False}

    def _racing_stat(self, *a, **k):
        if not race["done"] and self == f:
            race["done"] = True
            raise OSError(2, "No such file or directory")
        return _orig_stat(self, *a, **k)

    monkeypatch.setattr(_Path, "stat", _racing_stat)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    with pytest.raises(agent_chat_input.EffortSwitchFailedError):
        await agent_chat_input.set_effort(agent, "max")
    assert sent and sent[0] == b"/effort max"


async def test_set_effort_boss_stat_race_does_not_fake_a_rejection(monkeypatch, tmp_path):
    """Kehrseite desselben Befunds: eine ALTE "Kept effort level as low"-Zeile
    darf einen Wechsel, der gerade funktioniert hat, nicht als abgelehnt
    melden. Nach dem stat-Race zaehlt nur, was danach dazukommt."""
    import json as _json
    from pathlib import Path as _Path
    from app.services import agent_chat_input
    import app.services.transcript_chat as tc

    f = tmp_path / "sess.jsonl"
    f.write_text(_json.dumps({
        "type": "system", "text": "Kept effort level as low"
    }) + "\n")

    sent = await _boss_effort_env(monkeypatch, tmp_path, agent_chat_input)

    zaehler = {"n": 0}

    def _fake_find(_dir):
        zaehler["n"] += 1
        if zaehler["n"] == 3:
            with open(f, "a") as fh:
                fh.write(_json.dumps({
                    "type": "system", "text": "Set effort level to max (this session only)"
                }) + "\n")
        return (f, 0.0)

    monkeypatch.setattr(tc, "find_active_session", _fake_find)

    _orig_stat = _Path.stat
    race = {"done": False}

    def _racing_stat(self, *a, **k):
        if not race["done"] and self == f:
            race["done"] = True
            raise OSError(2, "No such file or directory")
        return _orig_stat(self, *a, **k)

    monkeypatch.setattr(_Path, "stat", _racing_stat)

    agent = _StubAgent(slug="boss", agent_runtime="host")
    await agent_chat_input.set_effort(agent, "max")
    assert sent and sent[0] == b"/effort max"


async def test_effort_capabilities_non_boss_host_claude_agent_gets_no_switch(monkeypatch, tmp_path):
    """Befund 5: ``_target_kind`` liefert "boss" NUR fuer host + einen Slug aus
    _BOSS_SLUGS; alles andere wirft InputNotSupportedError, das
    ``effort_capabilities`` zu kind=None verschluckt — und dann fiel es in den
    harness=="claude"-Zweig. Migration 0163 setzt harness='claude' auf JEDEN
    Host-Agenten mit Anthropic-Runtime, nicht nur auf Boss. So ein Agent
    meldete canSwitchEffort=true, las die PERSOENLICHE ~/.claude/settings.json
    des Operators fuer den Chip — und jeder Zug am Regler gab 409
    input_not_supported: genau der "klicke drauf, passiert nichts"-Fehler, den
    #325 beheben wollte."""
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"effortLevel": "xhigh"}')

    agent = _StubAgent(slug="jarvis", agent_runtime="host")
    agent.harness = "claude"

    caps = await agent_chat_input.effort_capabilities(agent)
    assert caps == {
        "effortLevels": [], "canSwitchEffort": False, "effort": None,
        "effortShared": False,
        # Seit der openclaude-Runde traegt die Antwort eine BEGRUENDUNG. Hier
        # ist sie "no_pane" und nicht "foreign_harness": der Agent faehrt sehr
        # wohl Claude Code — ihm fehlt der Kanal, nicht die CLI-Faehigkeit. Das
        # UI kann das erklaeren, statt das Bedienelement wortlos wegzulassen.
        "effortReason": "no_pane",
        # Kein Modellname, weil hier kein Picker befragt wurde — behauptet
        # wird nichts.
        "effortModel": None,
    }
    # Und die persoenliche settings.json des Operators wurde NICHT gelesen —
    # der eigentliche Schaden des alten Verhaltens.
    assert caps["effort"] is None

    # Und der Endpunkt haette ihn ohnehin abgewiesen — das Versprechen war leer.
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_effort(agent, "high")
