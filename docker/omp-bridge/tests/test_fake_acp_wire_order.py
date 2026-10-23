"""The fake ACP server sends its recording in recorded order — never ahead of
the client (CI flake on PR #954 and on main's merge queue for #945, 22.10.).

Real omp cannot send a ``session/update`` before the request it follows:
the session it belongs to does not exist yet. The fake's walk did exactly
that — it skipped every recorded reply and streamed the updates the moment
its thread started, with ``fake_delay=0.0`` before the code under test had
even registered its update callback. ``ACPClient._fire_event_cbs`` with no
callback drops the update, so the turn's FIRST line went missing
(``test_11d``: no ``thinking`` line; ``test_7``: ``final_text`` without its
first thought). How often depended on how slow the runner was between
``InProcessFake(...)`` and ``client.on_event(...)``: 3 of 40 fresh pytest
runs of test_acp_text_segments.py on main locally, rarer in a warm process.

Rule now: a recorded reply leaves only once the client sent the request it
answers, and nothing recorded after it leaves before it.
"""
from __future__ import annotations

import io
import json
import os
import select
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fake_acp_server  # noqa: E402

SID = "sess-wire-order"
INIT = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}
NEW = {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": SID}}
UPDATE = {"jsonrpc": "2.0", "method": "session/update", "params": {
    "sessionId": SID, "update": {"sessionUpdate": "agent_thought_chunk",
                                 "content": {"type": "text", "text": "first"}}}}
PROMPT = {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}


class _Wire:
    """The client end of a replay: write requests, read the fake's lines."""

    def __init__(self, recording: list[dict]):
        c_r, self._c_w = os.pipe()      # client -> fake
        self._f_r, f_w = os.pipe()      # fake -> client
        self._buf = b""
        self._fin = io.open(c_r, "r", encoding="utf-8", newline="")
        self._fout = io.open(f_w, "w", encoding="utf-8", newline="")
        self.walk = threading.Thread(
            target=fake_acp_server.replay,
            args=(recording, self._fin, self._fout), kwargs={"delay_s": 0.0},
            daemon=True,
        )
        self.walk.start()

    def send(self, rid: int, method: str) -> None:
        os.write(self._c_w, (json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                         "params": {}}) + "\n").encode())

    def lines(self, quiet_for: float = 0.3) -> list[dict]:
        """Everything that arrives until the wire stays quiet ``quiet_for`` s."""
        out: list[dict] = []
        while True:
            ready, _, _ = select.select([self._f_r], [], [], quiet_for)
            if not ready:
                return out
            chunk = os.read(self._f_r, 65536)
            if not chunk:
                return out
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if line.strip():
                    out.append(json.loads(line))

    def close(self) -> None:
        os.close(self._c_w)


def _tag(msgs: list[dict]) -> list[str]:
    return [f"reply:{m['id']}" if "id" in m and "method" not in m else m["method"]
            for m in msgs]


def test_nothing_reaches_the_client_before_it_asked():
    wire = _Wire([INIT, NEW, UPDATE, PROMPT])
    try:
        assert _tag(wire.lines()) == []
    finally:
        wire.close()


def test_the_recording_goes_out_in_recorded_order_one_request_at_a_time():
    wire = _Wire([INIT, NEW, UPDATE, PROMPT])
    try:
        wire.send(1, "initialize")
        assert _tag(wire.lines()) == ["reply:1"]
        wire.send(2, "session/new")
        # The update was recorded AFTER the session/new reply: it follows it,
        # never precedes it, and goes out without waiting for the prompt.
        assert _tag(wire.lines()) == ["reply:2", "session/update"]
        wire.send(3, "session/prompt")
        assert _tag(wire.lines()) == ["reply:3"]
    finally:
        wire.close()


def test_a_client_that_hangs_up_before_asking_ends_the_walk():
    """The walk waits at a reply for its request — but not for a client
    that is gone (real omp ends when its stdin closes). Without this every
    replay test that stops after session/new held the subprocess fake open
    until the client's 5 s close timeout (4 tests, +20 s per run)."""
    wire = _Wire([INIT, NEW, UPDATE, PROMPT])
    wire.send(1, "initialize")
    assert _tag(wire.lines()) == ["reply:1"]
    wire.close()
    wire.walk.join(timeout=2)
    assert not wire.walk.is_alive()


def test_a_slow_runner_before_the_callback_is_registered_loses_no_update(
        tmp_path, monkeypatch):
    """The CI failure itself, made deterministic: 50 ms between starting the
    in-process fake and the bridge registering ``on_event`` (a cold, busy
    runner). Before, every update the walk sent in that window was dropped."""
    import acp_client
    from test_acp_text_segments import _run_task, _transcript, shape, text, thought, \
        tool_call, tool_done

    real_on_event = acp_client.ACPClient.on_event

    def slow_on_event(self, cb):
        time.sleep(0.05)
        return real_on_event(self, cb)

    monkeypatch.setattr(acp_client.ACPClient, "on_event", slow_on_event)
    outcome, order = _run_task(tmp_path, [thought("pondering"), text("Before."), tool_call(),
                                          tool_done(), text("After.", mid="m-2")])
    assert shape(_transcript(order)) == ["thinking", "text:Before.", "toolCall",
                                         "text:After."], shape(_transcript(order))
    assert outcome.final_text == "ponderingBefore.After."
