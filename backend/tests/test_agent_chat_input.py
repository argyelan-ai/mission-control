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

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class _StubAgent:
    """Duck-typed stand-in for the DB-backed Agent row — mirrors the
    ``agent.slug`` / ``agent.agent_runtime`` contract ``_target_kind`` reads,
    same convention as ``transcript_chat.resolve_transcript_dir``'s tests."""

    def __init__(self, slug: str, agent_runtime: str):
        self.slug = slug
        self.agent_runtime = agent_runtime


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
    unsubmitted); this asserts the exact 3-call sequence: literal text,
    THEN a separate Enter, THEN the recycler-marker touch."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "hello agent")

    assert len(calls) == 3  # send-keys -l text + send-keys Enter + touch marker
    first, second, third = calls
    assert first[:2] == ["docker", "exec"]
    assert "-e" in first and "LANG=C.UTF-8" in first
    assert "-u" in first and "agent" in first
    assert "mc-agent-rex" in first
    assert first[-3:] == ["-l", "--", "hello agent"]
    assert second[-1] == "Enter"
    assert "-l" not in second
    assert third == [
        "docker", "exec", "-u", "agent", "mc-agent-rex",
        "touch", "/home/agent/.claude/last-task.marker",
    ]


async def test_send_text_docker_multiline_two_calls_bracketed_paste(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "line one\nline two")

    assert len(calls) == 3  # paste + Enter + recycler-marker touch
    first, second, third = calls
    assert first[-3] == "-l"
    assert first[-2] == "--"
    assert first[-1] == "\x1b[200~line one\nline two\x1b[201~"
    assert second[-1] == "Enter"
    assert "-l" not in second
    assert third == ["docker", "exec", "-u", "agent", "mc-agent-rex", "touch", "/home/agent/.claude/last-task.marker"]


async def test_send_text_docker_touches_recycler_marker(monkeypatch):
    """Fix round 3 (live-gate finding): the fleet's agent-recycler kills idle
    claude sessions based on last-task.marker's mtime — chat input must
    refresh it or an idle agent gets recycled mid chat-conversation."""
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

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

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "-h")

    assert calls[0][-3:] == ["-l", "--", "-h"]


async def test_send_text_docker_dash_bullet_single_line_gets_double_dash(monkeypatch):
    from app.services import agent_chat_input

    calls: list[list[str]] = []

    async def _fake_run(argv):
        calls.append(argv)

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    await agent_chat_input.send_text(agent, "- bullet")

    assert calls[0][-3:] == ["-l", "--", "- bullet"]


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
            pane_sequence=[f"  ⎿  Set effort level to {level} (...)"],
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


async def test_set_effort_boss_raises_input_not_supported(monkeypatch):
    """v1 scope: Boss has no pane probe (mirrors pane_state.capture_pane's
    own v1 scope) — effort switching stays docker-only even though Boss has
    an input channel for send_text/send_keys."""
    from app.services import agent_chat_input

    calls = await _patch_effort_deps(monkeypatch, agent_chat_input, pane_sequence=[None])

    agent = _StubAgent(slug="boss", agent_runtime="host")
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_effort(agent, "high")

    assert calls == []


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
        pane_sequence=[_IDLE_PANE, "  ⎿  Set effort level to low (saved as your default for new sessions): ..."],
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


async def test_effort_capabilities_docker_agent_gets_full_level_list():
    from app.services import agent_chat_input

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge")
    caps = agent_chat_input.effort_capabilities(agent)

    assert caps == {
        "effortLevels": ["low", "medium", "high", "xhigh", "max", "ultracode"],
        "canSwitchEffort": True,
    }
    # Same single-source constant set_effort validates against — no drift.
    assert caps["effortLevels"] == list(agent_chat_input.ALLOWED_EFFORT_LEVELS)


async def test_effort_capabilities_boss_cannot_switch():
    from app.services import agent_chat_input

    agent = _StubAgent(slug="boss", agent_runtime="host")
    caps = agent_chat_input.effort_capabilities(agent)

    assert caps == {"effortLevels": [], "canSwitchEffort": False}


async def test_effort_capabilities_other_host_agent_cannot_switch():
    from app.services import agent_chat_input

    agent = _StubAgent(slug="hermes", agent_runtime="host")
    caps = agent_chat_input.effort_capabilities(agent)

    assert caps == {"effortLevels": [], "canSwitchEffort": False}


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


async def test_post_chat_effort_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/effort", json={"level": "high"}
    )

    assert resp.status_code == 401
