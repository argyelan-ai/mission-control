#!/usr/bin/env python3
"""Generate synthetic NDJSON fixtures for the omp-bridge golden tests.

Each fixture mirrors a REAL event shape captured in ../rpc/*.ndjson but is
trimmed to the minimum lifecycle skeleton so the fixtures stay reviewable.
Provenance of each shape is noted inline. Run: python3 make_fixtures.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
os.makedirs(FIX, exist_ok=True)

REFLECTION = (
    "## Was wurde gemacht\n"
    "buggy.py mit NameError erstellt, mit python3 ausgefuehrt, Fehler beobachtet, "
    "x=42 ergaenzt und erneut ausgefuehrt.\n"
    "## Was hat funktioniert\n"
    "Der Fix war eindeutig; zweiter Lauf druckt OK mit exit 0.\n"
    "## Was war unklar\n"
    "Nichts Wesentliches — Aufgabe war deterministisch.\n"
    "## Lesson fuer Agent-Memory\n"
    "NameError immer erst reproduzieren, dann die fehlende Bindung ergaenzen."
)


def write(name: str, events: list[dict]) -> None:
    path = os.path.join(FIX, name)
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    print(f"wrote {name} ({len(events)} events)")


def session():
    return {"type": "session", "version": 3, "id": "fixture-0001",
            "timestamp": "2026-07-01T00:00:00.000Z", "cwd": "/work"}


def turn_end(stop_reason, text=None, thinking=True, tool_error=False):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": "…"})
    if text is not None:
        content.append({"type": "text", "text": text})
    ev = {"type": "turn_end",
          "message": {"role": "assistant", "content": content, "stopReason": stop_reason},
          "toolResults": ([{"isError": True}] if tool_error else [])}
    return ev


def agent_end():
    return {"type": "agent_end", "messages": [{"role": "assistant", "stopReason": "stop"}]}


# 1) GENUINE FINISH — stopReason stop + reflection block + trailing sentinel.
#    (Real json-stream.ndjson has this exact final structure but WITHOUT the
#    reflection+sentinel, because it was run with a bare prompt. Here we add the
#    prompt-wrapping contract from design §3.4.)
final_text = "\n\nDone.\n\n" + REFLECTION + "\n" + "TASK_COMPLETE"
write("finish-with-sentinel.ndjson", [
    session(),
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash",
     "args": {"command": "python3 buggy.py"}, "intent": "run"},
    {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash",
     "result": {"content": [{"type": "text", "text": "Result is 42\nOK\n"}],
                "details": {"exitCode": 0, "wallTimeMs": 40.0}}, "isError": False},
    turn_end("stop", text=final_text),
    agent_end(),
])

# 2) INCOMPLETE ABORT (crash) — a turn is cut off; NO turn_end, NO agent_end.
#    Mirrors the "no agent_end at process exit" abort shape (design §2, case 2).
write("incomplete-abort-crash.ndjson", [
    session(),
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash",
     "args": {"command": "python3 buggy.py"}, "intent": "run"},
    # <-- stream truncated here (SIGKILL / OOM). No terminal marker.
])

# 3) TRANSIENT API ERROR — the ORIGINAL openclaude failure, first-class.
#    Shape: stopReason==error with a transient errorMessage (design §2.1: exact
#    shape is UNVERIFIED for omp+Claude; we detect the abort-class heuristically).
write("transient-api-error.ndjson", [
    session(),
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "message_end",
     "message": {"role": "assistant", "content": [], "stopReason": "error",
                 "errorId": 502,
                 "errorMessage": "API Error: fetch failed (Connection error to upstream, 503)"}},
    turn_end("error", text=None, thinking=False),
    agent_end(),
])

# 4) MALFORMED REFLECTION — sentinel present, but reflection missing a header.
bad_reflection = ("## Was wurde gemacht\nkurz\n## Was hat funktioniert\nok\nTASK_COMPLETE")
write("malformed-reflection.ndjson", [
    session(), {"type": "agent_start"}, {"type": "turn_start"},
    turn_end("stop", text="Fertig.\n\n" + bad_reflection),
    agent_end(),
])

# 5) ANTI-ECHO — TASK_COMPLETE echoed mid-text, real last line is a give-up.
echo_text = ("I was told to end with TASK_COMPLETE when done.\n"
             "I couldn't finish everything, I'll continue later.")
write("anti-echo-giveup.ndjson", [
    session(), {"type": "agent_start"}, {"type": "turn_start"},
    turn_end("stop", text=echo_text),
    agent_end(),
])

# 6) HANG (synthetic) — the stream ADVANCES then simply stops (deadlocked
#    provider read / TLS stall): a turn_start, a tool that begins, and then
#    NOTHING — no tool_execution_end, no turn_end, no agent_end (design §2 case 3,
#    §3.4 "hang fixture"). On disk this is indistinguishable from a crash; what
#    makes it a HANG is that omp is still *alive but wedged*, so the out-of-band
#    watchdog (not any stream field) is what resolves it. The live-path test in
#    test_bridge.py replays this through supervise_stream over a blocking pipe and
#    asserts the watchdog fires -> RunOutcome.watchdog_killed -> Kind.ABORT_HANG.
write("hang-truncated.ndjson", [
    session(),
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash",
     "args": {"command": "curl https://api.example/slow"}, "intent": "run"},
    # <-- omp wedged here: still running, but no further NDJSON ever arrives.
])

print("done")
