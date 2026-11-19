#!/usr/bin/env python3
"""A fake `omp acp` that REMEMBERS each session (chats slice S2 acceptance).

Unlike fake_acp_server.py (a replay of a recorded run), this one keeps every
session's conversation in `<FAKE_OMP_STORE>/<sessionId>.json`, the way omp
keeps its own session files: `session/load` in a NEW process finds what an
earlier process stored. Every reply names everything the session has been
told so far — so a test can see whose context a turn ran in.

JSON-RPC over stdio: initialize, session/new, session/load (unknown id =
error, like omp), session/prompt (one `agent_message_chunk`, then
`end_turn`), session/set_config_option, session/close.
"""
import json
import os
import sys
import uuid
from pathlib import Path

STORE = Path(os.environ["FAKE_OMP_STORE"])
STORE.mkdir(parents=True, exist_ok=True)


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _history(sid):
    path = STORE / f"{sid}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _store(sid, said):
    (STORE / f"{sid}.json").write_text(json.dumps(said))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "method" not in msg or "id" not in msg:
            continue
        rid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}})
        elif method == "session/new":
            sid = "fake-" + uuid.uuid4().hex[:12]
            _store(sid, [])
            _send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid}})
        elif method == "session/load":
            sid = params.get("sessionId")
            if _history(sid) is None:
                _send({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32002, "message": f"Session not found: {sid}"}})
            else:
                _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            sid = params.get("sessionId")
            said = _history(sid)
            if said is None:
                _send({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32002, "message": f"Session not found: {sid}"}})
                continue
            text = "".join(b.get("text", "") for b in params.get("prompt") or [])
            said.append(text)
            _store(sid, said)
            _send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": sid,
                "update": {"sessionUpdate": "agent_message_chunk", "messageId": f"m{len(said)}",
                           "content": {"type": "text", "text": "I know: " + ", ".join(said)}},
            }})
            _send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        else:
            _send({"jsonrpc": "2.0", "id": rid, "result": {}})


if __name__ == "__main__":
    main()
