#!/usr/bin/env python3
"""
acp_chat_ctl.py — CLI shim in front of the chat daemon's Unix socket.

The backend reaches an ACP agent's chat with
`docker exec mc-agent-<slug> python3 /opt/omp-bridge/acp_chat_ctl.py <op>`,
so the transport is a process exit code plus one JSON line on stdout:

    0  the daemon answered `{"ok": true, ...}`
    2  the daemon answered `{"ok": false, ...}` (busy, rpc_error, ...)
    3  the socket is unreachable — prints `{"ok": false, "error": "unreachable"}`

Examples:
    acp_chat_ctl.py state
    acp_chat_ctl.py prompt --json '{"text": "hello"}'
    acp_chat_ctl.py config --json '{"id": "thinking", "value": "high"}'
    acp_chat_ctl.py cancel
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import acp_chat  # noqa: E402

OPS = ("prompt", "cancel", "config", "state")

EXIT_OK = 0
EXIT_NOT_OK = 2
EXIT_UNREACHABLE = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="control the ACP chat daemon")
    parser.add_argument("op", choices=OPS)
    parser.add_argument("--json", dest="payload", default=None,
                        help='request fields as JSON, e.g. \'{"text": "hi"}\'')
    parser.add_argument("--socket", default=None,
                        help="socket path (default: $OMP_HOME/acp-chat.sock)")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    request = {"op": args.op}
    if args.payload:
        try:
            extra = json.loads(args.payload)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": "bad_json", "detail": str(exc)}))
            return EXIT_NOT_OK
        if not isinstance(extra, dict):
            print(json.dumps({"ok": False, "error": "bad_json",
                              "detail": "--json must be an object"}))
            return EXIT_NOT_OK
        request.update(extra)

    path = args.socket or acp_chat.default_socket_path()
    try:
        response = acp_chat.request(path, request, timeout=args.timeout)
    except (OSError, ValueError):
        # Daemon down, socket missing, half-open connection — one honest
        # answer, never a traceback the backend would have to parse.
        print(json.dumps({"ok": False, "error": "unreachable"}))
        return EXIT_UNREACHABLE
    print(json.dumps(response, ensure_ascii=False))
    return EXIT_OK if response.get("ok") else EXIT_NOT_OK


if __name__ == "__main__":
    sys.exit(main())
