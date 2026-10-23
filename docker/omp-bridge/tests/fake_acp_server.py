#!/usr/bin/env python3
"""
Fake `omp acp` server for replay tests (test_acp_replay.py).

Replays a golden transcript (captured from a REAL omp run, rpc/acp-*.ndjson)
over stdin/stdout, protocol-aware:

* Client->server requests (initialize, session/new, ...) are answered with
  the NEXT recorded reply in capture order — exactly what real omp did.
* Server->client requests (session/request_permission, fs/*) are sent with
  their recorded ids, then the fake WAITS for the live client's actual
  reply before continuing — the permission round-trip is exercised for
  real. For fs/* requests the client under test answers from disk.
* Notifications (session/update) replay verbatim with a small delay.
* Strict recorded order: nothing recorded after a reply goes out before that
  reply, and a reply goes out only once the client sent its request — real
  omp cannot send a session/update before the request it follows.
* Every client->server message is recorded to stderr as JSON lines
  {"__client": {...}} for tests that assert the wire side.

Usage: python3 fake_acp_server.py <transcript.ndjson>
Env:   FAKE_DELAY=<seconds between notifications> (default 0.01)
"""
import json
import os
import sys
import threading
import time

delay = float(os.environ.get("FAKE_DELAY", "0.01"))


def _load_transcript(path: str | None) -> list:
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def replay(msgs_in: list, fin, fout, *, sink: list | None = None, delay_s: float | None = None) -> None:
    """Importable replay core: walk `msgs_in` (server-side transcript) over the
    given text pipes. `fin` = client->fake, `fout` = fake->client. `sink`
    records everything the fake SENDS (wire-side, for assertions).
    Used by the subprocess entrypoint below AND by in-process adapter tests."""
    d = delay_s if delay_s is not None else float(os.environ.get("FAKE_DELAY", "0.01"))
    _lock = threading.Lock()
    _pending = {}
    # Wire-order gate: a recorded reply may only hit the wire once the walk has
    # emitted every transcript message before it — real omp streams updates
    # DURING the turn, before the terminal reply. Without this the reply
    # cursor fires on request arrival and updates land after prompt() returned
    # (synthetic reordering a live server never produces).
    # The other half (CI flake, 22.10.2026): the walk must not run AHEAD of the
    # client either. It used to skip every recorded reply and stream the
    # updates the moment its thread started — with delay 0 before the code
    # under test had registered its update callback, which then dropped the
    # turn's first update. Now the walk stops at each recorded reply until
    # that reply went out (which needs the client's request), so the wire
    # carries the recording in recorded order, one request at a time.
    _walk_at = [0]               # how many transcript positions the walk emitted
    _reply_positions = {}        # reply index -> walk position in msgs_in
    _pos = 0
    for _idx, _m in enumerate(msgs_in):
        if "id" in _m and "method" not in _m:
            _reply_positions[len(_reply_positions)] = _pos
        _pos += 1

    def _send(obj):
        if sink is not None:
            sink.append(obj)
        with _lock:
            fout.write(json.dumps(obj) + "\n")
            fout.flush()

    _replies = [m for m in msgs_in if "id" in m and "method" not in m]
    _reply_sent = [threading.Event() for _ in _replies]
    _cursor = [0]

    def _reader():
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sink is not None:
                sink.append({"__client": msg})
            ev = _pending.get(msg.get("id"))
            if ev is not None:
                ev.set()
                continue
            if "method" in msg and "id" in msg:
                with _lock:
                    i = _cursor[0]
                    _cursor[0] += 1
                # Wait until the walk emitted everything before this reply's
                # transcript position (bounded — a stalled walk must not hang
                # the client past its own request timeout). The wait runs on
                # its OWN thread: blocking this reader here deadlocked a
                # fixture whose walk sends a server->client request (e.g.
                # fs/write_text_file) mid-turn — the walk waited for the
                # client's answer, which this reader could no longer read
                # while it waited for the walk (P1 fixture, 1 in ~20 runs).
                want = _reply_positions.get(i, 0)

                def _gated_reply(i=i, want=want):
                    _deadline = time.time() + 30
                    while _walk_at[0] < want and time.time() < _deadline:
                        time.sleep(0.005)
                    if i < len(_replies):
                        _send(_replies[i])
                        _reply_sent[i].set()

                threading.Thread(target=_gated_reply, daemon=True).start()

    rt = threading.Thread(target=_reader, daemon=True)
    rt.start()
    _reply_idx = 0
    for _i, m in enumerate(msgs_in):
        method = m.get("method")
        if "id" in m and "method" not in m:
            # A recorded reply: let its gated sender go (everything before it
            # is out), then wait until it is on the wire — bounded like the
            # reply gate, so a client that never asks cannot hang the walk.
            # A client that hung up (reader at EOF) will never ask: the walk
            # ends there, as real omp does when its stdin closes.
            _walk_at[0] = _i
            if _reply_idx < len(_reply_sent):
                sent = _reply_sent[_reply_idx]
                _deadline = time.time() + 30
                while not sent.wait(0.01) and rt.is_alive() and time.time() < _deadline:
                    pass
                if not sent.is_set() and not rt.is_alive():
                    break
            _reply_idx += 1
            _walk_at[0] = _i + 1
            continue
        if "id" in m and method:
            _pending[m["id"]] = threading.Event()
            _send(m)
            _pending[m["id"]].wait(timeout=60)
            _walk_at[0] = _i + 1
            continue
        time.sleep(d)
        _send(m)
        _walk_at[0] = _i + 1
    rt.join(timeout=5)


def main() -> None:
    msgs = _load_transcript(sys.argv[1] if len(sys.argv) > 1 else None)
    replay(msgs, sys.stdin, sys.stdout, delay_s=delay)
    # Stay alive until the client closes stdin (EOF) — like real omp, which
    # keeps running until the parent terminates it. Without this the process
    # exits right after the transcript walk and a client mid-conversation
    # would hit a dead pipe / respawn attempt.
    for _line in sys.stdin:
        pass


if __name__ == "__main__":
    main()

