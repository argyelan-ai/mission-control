"""Hermes shows the RUNTIME's context window, not its own model-name guess.

Live 22.10.2026: Hermes' chat ring read ~17.5k of 203k (9 %) while the runtime
it runs on serves 1,048,576 tokens (truth: 2 %). An omp agent on the SAME
runtime was right. Cause: MC told omp the window (``OMP_CONTEXT_WINDOW`` from
``runtime.max_context_len``) but told Hermes nothing, so Hermes guessed from a
stale model name ("glm" -> 202,752) and the backend took the harness' number
over the runtime catalog without a second look.

Pinned here (backend half — the bridge + config patcher halves live in
test_hermes_bridge_config_sync.py / test_hermes_config_patcher.py):

1. ``build_runtime_env`` emits ``MC_CONTEXT_WINDOW`` for every
   OpenAI-protocol harness (hermes included), keeps ``OMP_CONTEXT_WINDOW``,
   and leaves Claude Code alone.
2. Hermes' agent.env gets the window on bootstrap AND on a runtime sync, and
   is still rewritten only when it changes.
3. PARITY: an omp and a Hermes agent on one runtime (1,048,576) both show
   ``contextWindow == 1048576`` in their chat history — whatever their
   harness reported.
4. The guard: runtime window wins over a disagreeing harness window and
   ``usedPct`` follows; no runtime window = harness number untouched; the
   Claude Code parser never sees it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime

WINDOW = 1_048_576
HERMES_GUESS = 202_752  # hermes-agent's "glm" entry (agent/model_metadata.py)
MODEL = "DeepSeek-V4.1-Flash-1017"


def _runtime(**kw) -> Runtime:
    base = dict(
        slug="dgx-spark-slot",
        display_name="Spark slot",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier=MODEL,
        max_context_len=WINDOW,
        enabled=True,
    )
    base.update(kw)
    return Runtime(**base)


def _acp_usage_line(*, entry_id: str, window: int, used: int) -> str:
    """One final assistant line exactly as the ACP bridge writes it
    (docker/omp-bridge/acp_chat_events.py ``_usage_block``)."""
    return json.dumps(
        {
            "type": "message",
            "id": entry_id,
            "timestamp": "2026-10-22T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "fertig"}],
                "model": MODEL,
                "usage": {
                    "input": 63_710,
                    "cacheRead": 256_000,
                    "cacheWrite": 0,
                    "output": 120,
                    "contextWindow": window,
                    "usedTokens": used,
                },
                "stopReason": "stop",
            },
        }
    )


# ── 1. build_runtime_env ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hermes_harness_gets_the_runtime_window(async_session):
    from app.routers.internal import build_runtime_env

    agent = Agent(name="Hermes", slug="hermes", agent_runtime="host", harness="hermes")
    env = await build_runtime_env(_runtime(), async_session, agent=agent)

    assert env["OPENAI_MODEL"] == MODEL
    assert env["MC_CONTEXT_WINDOW"] == str(WINDOW)


@pytest.mark.asyncio
async def test_omp_keeps_its_window_var_and_gets_the_generic_one(async_session):
    from app.routers.internal import build_runtime_env

    agent = Agent(name="OmpAgent", slug="omp-agent", agent_runtime="cli-bridge", harness="omp")
    env = await build_runtime_env(_runtime(), async_session, agent=agent)

    assert env["OMP_CONTEXT_WINDOW"] == str(WINDOW)
    assert env["MC_CONTEXT_WINDOW"] == env["OMP_CONTEXT_WINDOW"]


@pytest.mark.asyncio
async def test_bootstrap_path_without_agent_carries_the_window(async_session):
    """``build_hermes_agent_env`` (Hermes bootstrap) and the generic host
    staging call ``build_runtime_env`` WITHOUT the agent — the window must
    reach agent.env that way too."""
    from app.services.agent_bootstrap import build_hermes_agent_env

    env = await build_hermes_agent_env(_runtime(), "tok", session=async_session)
    assert env["MC_CONTEXT_WINDOW"] == str(WINDOW)


@pytest.mark.asyncio
async def test_hermes_runtime_type_branch_carries_the_window(async_session):
    from app.routers.internal import build_runtime_env

    env = await build_runtime_env(_runtime(runtime_type="hermes"), async_session)
    assert env["MC_CONTEXT_WINDOW"] == str(WINDOW)


@pytest.mark.asyncio
async def test_preferred_len_is_the_fallback_like_for_omp(async_session):
    from app.routers.internal import build_runtime_env

    agent = Agent(name="Hermes", slug="hermes", agent_runtime="host", harness="hermes")
    env = await build_runtime_env(
        _runtime(max_context_len=None, preferred_context_len=500_000), async_session, agent=agent
    )
    assert env["MC_CONTEXT_WINDOW"] == "500000"


@pytest.mark.asyncio
async def test_no_window_on_the_runtime_means_no_variable(async_session):
    """No invented number: the patcher then leaves Hermes' own value alone."""
    from app.routers.internal import build_runtime_env

    agent = Agent(name="Hermes", slug="hermes", agent_runtime="host", harness="hermes")
    env = await build_runtime_env(_runtime(max_context_len=None), async_session, agent=agent)
    assert "MC_CONTEXT_WINDOW" not in env


@pytest.mark.asyncio
async def test_claude_code_env_is_unchanged(async_session):
    """Protocol-fixed vendors know their own window — a claude agent's env
    must not grow a key (agent.env rewrite = bridge restart on host)."""
    from app.routers.internal import build_runtime_env

    rt = _runtime(
        slug="claude-max",
        runtime_type="anthropic",
        endpoint=None,
        model_identifier="claude-opus-5",
        max_context_len=1_000_000,
    )
    agent = Agent(name="Boss", slug="boss", agent_runtime="host", harness="claude")
    env = await build_runtime_env(rt, async_session, agent=agent)
    assert "MC_CONTEXT_WINDOW" not in env


# ── 2. agent.env on a runtime sync ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_sync_writes_the_window_into_hermes_agent_env(
    async_session, tmp_path, monkeypatch
):
    from app.services.host_harness_adapter import sync_host_agent_model

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    env_path = tmp_path / ".mc" / "agents" / "hermes" / "agent.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("MC_AGENT_TOKEN='tok'\nOPENAI_MODEL='GLM-5.3-Flash-EXL3'\n")
    rt = _runtime()
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes", runtime_id=rt.id
    )

    await sync_host_agent_model(agent, rt, session=async_session)
    text = env_path.read_text()
    assert f"MC_CONTEXT_WINDOW='{WINDOW}'" in text
    assert f"OPENAI_MODEL='{MODEL}'" in text
    assert "MC_AGENT_TOKEN='tok'" in text

    # Still write-only-on-change: a second identical sync leaves the file.
    old = time.time() - 3600
    os.utime(env_path, (old, old))
    before = env_path.stat().st_mtime_ns
    await sync_host_agent_model(agent, rt, session=async_session)
    assert env_path.stat().st_mtime_ns == before

    # A window change (recipe switch on the same model) IS a change.
    rt.max_context_len = 524_288
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    await sync_host_agent_model(agent, rt, session=async_session)
    assert "MC_CONTEXT_WINDOW='524288'" in env_path.read_text()


# ── 3. parity: omp + Hermes on one runtime ──────────────────────────────────


def _write_session(home: Path, slug: str, line: str) -> Path:
    tdir = home / ".mc" / "agents" / slug / "omp-sessions" / "--workspace--"
    tdir.mkdir(parents=True)
    path = tdir / "2026-10-22T10-00-00-000Z_sess0001.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": "sess0001", "timestamp": "2026-10-22T10:00:00.000Z"})
        + "\n"
        + line
        + "\n"
    )
    return path


@pytest.mark.asyncio
async def test_parity_omp_and_hermes_show_the_runtime_window(
    async_session, tmp_path, monkeypatch
):
    """THE bug: same runtime, two harnesses, two different rings. Through the
    real router helper (DB agent -> runtime row -> adapter -> parser)."""
    from app.routers.agent_chat import _resolve_transcript_path
    from app.services.transcript_chat import read_history

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    rt = _runtime()
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    omp_agent = Agent(
        name="OmpAgent", slug="omp-agent", agent_runtime="cli-bridge", harness="omp", runtime_id=rt.id
    )
    hermes = Agent(
        name="Hermes", slug="hermes", agent_runtime="host", harness="hermes", runtime_id=rt.id
    )
    async_session.add(omp_agent)
    async_session.add(hermes)
    await async_session.commit()

    # omp reports what MC rendered for it; Hermes reports its name-table guess.
    _write_session(tmp_path, "omp-agent", _acp_usage_line(entry_id="s1", window=WINDOW, used=17_500))
    _write_session(
        tmp_path, "hermes", _acp_usage_line(entry_id="h1", window=HERMES_GUESS, used=17_500)
    )

    windows = {}
    pcts = {}
    for agent in (omp_agent, hermes):
        resolved = await _resolve_transcript_path(agent.id, async_session)
        assert not hasattr(resolved, "status_code"), f"{agent.name}: no transcript"
        _agent, path, adapter = resolved
        history = read_history(path, adapter)
        usage = [e for e in history["events"] if e["kind"] == "usage"][-1]
        windows[agent.name] = usage["contextWindow"]
        pcts[agent.name] = usage["usedPct"]

    assert windows == {"OmpAgent": WINDOW, "Hermes": WINDOW}
    assert pcts["Hermes"] == pcts["OmpAgent"] == round(17_500 / WINDOW * 100, 1)


# ── 4. the guard itself ─────────────────────────────────────────────────────


def _parse_last_usage(parser, line: str) -> dict:
    return [e for e in parser(line) if e["kind"] == "usage"][-1]


def test_runtime_window_wins_over_a_disagreeing_harness_window():
    from app.services import omp_chat

    parser = omp_chat.new_parser(runtime_window=WINDOW)
    usage = _parse_last_usage(parser, _acp_usage_line(entry_id="g1", window=HERMES_GUESS, used=17_500))
    assert usage["contextWindow"] == WINDOW
    assert usage["usedPct"] == round(17_500 / WINDOW * 100, 1)
    assert usage["inputTokens"] == 17_500  # the fill itself is the harness' measurement
    assert usage["source"] == "cli"


def test_without_runtime_window_the_harness_number_stands():
    """Cloud / unbound agents: nothing to compare against -> unchanged."""
    from app.services import omp_chat

    usage = _parse_last_usage(
        omp_chat.new_parser(), _acp_usage_line(entry_id="g2", window=HERMES_GUESS, used=17_500)
    )
    assert usage["contextWindow"] == HERMES_GUESS
    assert usage["usedPct"] == round(17_500 / HERMES_GUESS * 100, 1)


def test_matching_windows_change_nothing(caplog):
    from app.services import omp_chat

    with caplog.at_level(logging.WARNING, logger="app.services.omp_chat"):
        usage = _parse_last_usage(
            omp_chat.new_parser(runtime_window=WINDOW),
            _acp_usage_line(entry_id="g3", window=WINDOW, used=1000),
        )
    assert usage["contextWindow"] == WINDOW
    assert not [r for r in caplog.records if "drift" in r.getMessage()]


def test_drift_is_logged_once_not_per_line(caplog):
    from app.services import omp_chat

    omp_chat._WINDOW_DRIFT_LOGGED.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.omp_chat"):
        for i in range(3):
            parser = omp_chat.new_parser(runtime_window=WINDOW)
            _parse_last_usage(parser, _acp_usage_line(entry_id=f"d{i}", window=HERMES_GUESS, used=1))
    drift = [r for r in caplog.records if "drift" in r.getMessage()]
    assert len(drift) == 1
    assert str(HERMES_GUESS) in drift[0].getMessage() and str(WINDOW) in drift[0].getMessage()


def test_runtime_window_replaces_the_model_name_guess_without_fill_truth():
    """A native omp line (no contextWindow) used to fall back to a guess from
    the model name; the bound runtime knows better."""
    from app.services import omp_chat

    line = json.loads(_acp_usage_line(entry_id="n1", window=1, used=1))
    del line["message"]["usage"]["contextWindow"]
    del line["message"]["usage"]["usedTokens"]
    usage = _parse_last_usage(omp_chat.new_parser(runtime_window=WINDOW), json.dumps(line))
    assert usage["contextWindow"] == WINDOW
    assert "usedPct" not in usage  # no fill truth -> no invented percentage


def test_bind_runtime_window_leaves_the_claude_parser_alone():
    from app.services import transcript_adapters, transcript_chat

    claude = transcript_adapters.adapter_for(Agent(name="Rex", harness="claude"))
    bound = transcript_adapters.bind_runtime_window(claude, WINDOW)
    assert bound.new_parser() is transcript_chat.parse_transcript_line

    omp = transcript_adapters.adapter_for(Agent(name="OmpAgent", harness="omp"))
    assert transcript_adapters.bind_runtime_window(omp, None) is omp
    parser = transcript_adapters.bind_runtime_window(omp, WINDOW).new_parser()
    assert parser.runtime_window == WINDOW


@pytest.mark.asyncio
async def test_agent_runtime_window_lookup(async_session):
    from app.services.runtime_window import agent_runtime_window

    rt = _runtime()
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    bound = Agent(name="Hermes", harness="hermes", runtime_id=rt.id)
    unbound = Agent(name="Cloud", harness="claude")
    assert await agent_runtime_window(bound, async_session) == WINDOW
    assert await agent_runtime_window(unbound, async_session) is None


# ── 5. the live stream (tailer) shows the same number as the history page ──


@pytest.mark.asyncio
async def test_tailer_streams_the_runtime_window_and_observes_it(tmp_path, monkeypatch):
    """The ring updates from the SSE stream, not only from the history page —
    and the tailer feeds a ``source == "cli"`` window into the SHARED
    observed-window map. Without the binding Hermes' wrong 202,752 landed
    there under the DeepSeek model name, for every other reader too."""
    import asyncio

    import app.services.runtime_window as runtime_window
    import app.services.transcript_adapters as transcript_adapters
    import app.services.transcript_chat as transcript_chat
    from app.services.transcript_chat import ChatTailerManager

    published: list[dict] = []
    observed: list[tuple[str, int]] = []

    async def _broadcast(channel, event_type, data):
        published.append(data)

    async def _observe(model, window):
        observed.append((model, window))

    async def _window(agent, session=None):
        return WINDOW

    async def _no_observed():
        return {}

    monkeypatch.setattr(transcript_chat.sse, "broadcast", _broadcast)
    monkeypatch.setattr(transcript_chat, "observe_model_window", _observe)
    monkeypatch.setattr(transcript_chat, "get_observed_model_windows", _no_observed)
    monkeypatch.setattr(runtime_window, "agent_runtime_window", _window)
    monkeypatch.setattr(
        transcript_adapters, "adapter_for", lambda _agent: transcript_adapters._omp_adapter("hermes")
    )

    session_file = tmp_path / "omp-sessions" / "--workspace--" / "sess1.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("")

    manager = ChatTailerManager()
    manager.POLL_INTERVAL = 0.02
    manager.STATE_PROBE_INTERVAL_SECONDS = 0.02
    await manager.acquire("hermes-1", session_file)
    try:
        await asyncio.sleep(0.1)
        with session_file.open("a") as f:
            f.write(_acp_usage_line(entry_id="live1", window=HERMES_GUESS, used=17_500) + "\n")
        for _ in range(150):
            if any(d.get("kind") == "usage" for d in published):
                break
            await asyncio.sleep(0.02)
    finally:
        await manager.release("hermes-1")

    usage = [d for d in published if d.get("kind") == "usage"]
    assert usage, "no usage event streamed"
    assert usage[-1]["contextWindow"] == WINDOW
    assert observed == [(MODEL, WINDOW)]


@pytest.mark.asyncio
async def test_tailer_follows_a_runtime_switch_without_reconnect(tmp_path, monkeypatch):
    """Review #939 SHOULD 2: Hermes keeps its ACP session across the bridge
    restart of a runtime switch, so the tailer is NOT restarted. It must
    re-read the window on its probe tick — otherwise the live ring kept the
    OLD runtime's window until the last viewer disconnected."""
    import asyncio
    from types import SimpleNamespace

    import app.services.pane_stream as pane_stream
    import app.services.runtime_window as runtime_window
    import app.services.transcript_adapters as transcript_adapters
    import app.services.transcript_chat as transcript_chat
    from app.services.transcript_chat import ChatTailerManager

    old_window = 202_752
    current = {"window": old_window}
    published: list[dict] = []

    async def _broadcast(channel, event_type, data):
        published.append(data)

    async def _observe(model, window):
        return None

    async def _no_observed():
        return {}

    async def _snapshot_window(agent, session=None):
        return old_window

    async def _current_window(agent):
        return current["window"]

    async def _none(*_a, **_k):
        return None

    async def _pane(*_a, **_k):
        return ""

    async def _state(self, *_a, **_k):
        return {"state": "idle"}

    monkeypatch.setattr(transcript_chat.sse, "broadcast", _broadcast)
    monkeypatch.setattr(transcript_chat, "observe_model_window", _observe)
    monkeypatch.setattr(transcript_chat, "get_observed_model_windows", _no_observed)
    monkeypatch.setattr(transcript_chat, "capture_pane", _pane)
    monkeypatch.setattr(pane_stream, "start", _none)
    monkeypatch.setattr(pane_stream, "stop", _none)
    monkeypatch.setattr(ChatTailerManager, "_compute_pane_state", _state)
    monkeypatch.setattr(runtime_window, "agent_runtime_window", _snapshot_window)
    monkeypatch.setattr(runtime_window, "current_agent_runtime_window", _current_window)
    monkeypatch.setattr(
        transcript_adapters, "adapter_for", lambda _agent: transcript_adapters._omp_adapter("hermes")
    )

    session_file = tmp_path / "omp-sessions" / "--workspace--" / "sess1.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("")
    agent = SimpleNamespace(id="hermes-1", runtime_id="rt-1", harness="hermes")

    async def _usage_after(entry_id: str) -> dict:
        with session_file.open("a") as f:
            f.write(_acp_usage_line(entry_id=entry_id, window=HERMES_GUESS, used=17_500) + "\n")
        for _ in range(200):
            hits = [d for d in published if d.get("kind") == "usage" and d.get("uuid") == entry_id]
            if hits:
                return hits[-1]
            await asyncio.sleep(0.02)
        raise AssertionError(f"no usage event for {entry_id}")

    manager = ChatTailerManager()
    manager.POLL_INTERVAL = 0.02
    manager.STATE_PROBE_INTERVAL_SECONDS = 0.02
    manager.RUNTIME_WINDOW_REFRESH_SECONDS = 0.02
    await manager.acquire("hermes-1", session_file, agent)
    try:
        await asyncio.sleep(0.1)
        before = await _usage_after("before-switch")
        assert before["contextWindow"] == old_window
        current["window"] = WINDOW  # the runtime switch lands in the DB
        await asyncio.sleep(0.2)
        after = await _usage_after("after-switch")
    finally:
        await manager.release("hermes-1")
    assert after["contextWindow"] == WINDOW
    assert after["usedPct"] == round(17_500 / WINDOW * 100, 1)


@pytest.mark.asyncio
async def test_current_agent_runtime_window_rereads_the_binding(async_session, monkeypatch):
    """The tailer's agent row is a snapshot; the re-read follows the agent's
    CURRENT runtime_id (agent_runtime_switch rebinds the agent)."""
    import app.services.runtime_window as runtime_window

    old = _runtime(slug="glm-slot", max_context_len=HERMES_GUESS)
    new = _runtime(slug="deepseek-slot", max_context_len=WINDOW)
    async_session.add_all([old, new])
    await async_session.commit()
    agent = Agent(name="Hermes", harness="hermes", runtime_id=old.id)
    async_session.add(agent)
    await async_session.commit()
    snapshot = Agent(id=agent.id, name="Hermes", harness="hermes", runtime_id=old.id)

    agent.runtime_id = new.id
    await async_session.commit()

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *exc):
            return False

    import app.database as database

    monkeypatch.setattr(database, "async_session_maker", _Maker())
    assert await runtime_window.current_agent_runtime_window(snapshot) == WINDOW
