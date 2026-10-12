"""omp 18.10.1 over ACP: plan + usage through the bridge's own client and mapper.

Fixture rpc/acp-plan-usage-18.10.1.ndjson is the verbatim wire stream of a
REAL `omp acp` 18.10.1 run (throwaway container without network, 12.10.2026),
driven by the bridge's ACPClient. The model endpoint was a local
OpenAI-compatible stub that answered the first call with a `todo` tool call
(op=init, two items) and later calls with fixed text, so the run exercises
exactly what MC consumes since the upgrade:

* `plan` session updates (omp maps its todo tool onto ACP plans) ->
  `custom`/`acp-plan` transcript lines (PR #787's todo panel),
* `usage_update` (size/used = context window, not tokens) -> the final
  assistant line's `contextWindow`/`usedTokens`,
* the `session/prompt` result's `usage` -> per-TURN token deltas (omp's
  `#buildTurnUsage`, unchanged since 18.1.10 — not session-cumulative).

Three model calls happened in that one turn: the tool call, the text answer,
and omp's own "You stopped with 2 incomplete todo item(s)" nudge — that is
why the text arrives twice and the prompt usage is 1200 + 1300 + 1300.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat_events  # noqa: E402
from test_acp_adapter import InProcessFake  # noqa: E402

FIXTURE = ROOT / "rpc" / "acp-plan-usage-18.10.1.ndjson"


def _wire() -> list[dict]:
    return [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]


def test_fixture_is_from_omp_18_10_1():
    init = _wire()[0]["result"]
    assert init["agentInfo"]["version"] == "18.10.1"
    assert init["protocolVersion"] == 1
    caps = init["agentCapabilities"]
    assert caps["loadSession"] is True
    assert "close" in caps["sessionCapabilities"]


def _run_turn():
    fake = InProcessFake(FIXTURE, [], fake_delay=0.0)
    client = fake.client
    mapper = acp_chat_events.ACPEventMapper()
    mapped: list[dict] = []
    events: list[dict] = []

    def on_event(params):
        events.append(params)
        mapped.extend(mapper.map_update(params, stream=True))

    client.on_event(on_event)
    client.on_permission(lambda _req: "allow_once")
    try:
        client.initialize(timeout=10)
        sid = client.new_session(cwd=str(HERE), timeout=10)
        client.set_config_option(sid, "model", "mc-openai/mock-model", timeout=10)
        result = client.prompt(sid, "make a plan", timeout=10)
    finally:
        fake.close()
    return sid, mapper, mapped, events, result


def test_plan_update_becomes_an_acp_plan_line():
    _sid, _mapper, mapped, events, _result = _run_turn()
    kinds = [(e.get("update") or {}).get("sessionUpdate") for e in events]
    assert "plan" in kinds
    plans = [m for m in mapped if m.get("customType") == acp_chat_events.PLAN_CUSTOM_TYPE]
    assert plans, "omp 18.10.1 plan update was dropped by the mapper"
    entries = plans[-1]["data"]["entries"]
    assert [e["content"] for e in entries] == ["first step", "second step"]
    assert [e["status"] for e in entries] == ["in_progress", "pending"]


def test_usage_update_and_prompt_usage_land_on_the_final_line():
    _sid, mapper, _mapped, _events, result = _run_turn()
    assert result.stopReason == "end_turn"
    # per-turn delta of three model calls (1200 + 1300 + 1300), not cumulative
    assert result.usage == {"inputTokens": 3800, "outputTokens": 44, "totalTokens": 3844}
    mapper.set_prompt_usage(result.usage)
    final = mapper.map_final_assistant_message("MOCK-OK 18.10.1 probe")
    usage = final[0]["message"]["usage"]
    assert usage["contextWindow"] == 131072
    assert usage["usedTokens"] == 1300
    assert usage["input"] == 3800
    assert usage["output"] == 44


def test_available_commands_are_advertised_over_acp():
    """The ACP chat palette is fed live from this update (no hard-coded list
    for headless agents). 18.2.1 renamed /drop to /delete — neither is an
    ACP command; /delete stays TUI-only."""
    _sid, _mapper, _mapped, events, _result = _run_turn()
    cmds = [
        e["update"]["availableCommands"]
        for e in events
        if (e.get("update") or {}).get("sessionUpdate") == "available_commands_update"
    ]
    assert cmds
    names = {c["name"] for c in cmds[-1]}
    assert {"compact", "model", "effort", "todo"} <= names
    assert "drop" not in names
