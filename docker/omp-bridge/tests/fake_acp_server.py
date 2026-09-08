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
                i = _cursor[0]
                # Wait until the walk emitted everything before this reply's
                # transcript position (bounded — a stalled walk must not hang
                # the client past its own request timeout).
                want = _reply_positions.get(i, 0)
                _deadline = time.time() + 30
                while _walk_at[0] < want and time.time() < _deadline:
                    time.sleep(0.005)
                with _lock:
                    _cursor[0] += 1
                if i < len(_replies):
                    _send(_replies[i])

    rt = threading.Thread(target=_reader, daemon=True)
    rt.start()
    for _i, m in enumerate(msgs_in):
        method = m.get("method")
        if "id" in m and "method" not in m:
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

