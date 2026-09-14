#!/usr/bin/env python3
"""Minimal CDP-shaped mock server for the healthcheck.sh sabotage tests.

Stdlib-only (socket/hashlib/base64/struct) so it runs on bare alpine:3.19
with just `apk add python3` — no websockets package needed. Serves the same
two things Chromium's real debug port does on the same TCP port:
  - a plain HTTP GET /json/version returning webSocketDebuggerUrl
  - a hand-rolled WS server for the follow-up Target.getTargets roundtrip

MODE selects what the WS side sends, to reproduce the cases from Rex'
review on PR #544:
  healthy      -> id:1 response immediately
  event-first  -> an unsolicited event, THEN (after a delay) the response
                  (the exact W2 case: exit 1 after 0.16s while the real
                  answer was 0.2s away, because old `-1` stopped on the
                  first message)
  no-response  -> only events, forever — must still time out red
"""
import base64
import hashlib
import socket
import struct
import sys
import time

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

EVENT = b'{"method":"Target.targetCreated","params":{}}'
RESPONSE = b'{"id":1,"result":{"targetInfos":[]}}'


def ws_frame(payload: bytes) -> bytes:
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x81, length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x81, 126, length)
    else:
        header = struct.pack("!BBQ", 0x81, 127, length)
    return header + payload


def handle_ws(conn: socket.socket, request: bytes, mode: str) -> None:
    key = b""
    for line in request.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1(key + WS_MAGIC.encode()).digest())
    conn.sendall(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )

    if mode == "healthy":
        conn.sendall(ws_frame(RESPONSE))
    elif mode == "event-first":
        conn.sendall(ws_frame(EVENT))
        time.sleep(0.2)
        conn.sendall(ws_frame(RESPONSE))
    elif mode == "no-response":
        for _ in range(30):
            conn.sendall(ws_frame(EVENT))
            time.sleep(0.3)
    else:
        raise SystemExit(f"unknown MODE {mode!r}")

    time.sleep(0.5)  # keep the socket open a beat so slow readers still see it


def handle_http(conn: socket.socket, port: int) -> None:
    body = ('{"webSocketDebuggerUrl":"ws://127.0.0.1:%d/ws"}' % port).encode()
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )


def main() -> None:
    port = int(sys.argv[1])
    mode = sys.argv[2]

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)

    # healthcheck.sh makes two sequential connections (HTTP, then WS) — serve
    # a handful so a test can also retry its own readiness probe beforehand.
    for _ in range(10):
        conn, _ = srv.accept()
        try:
            data = conn.recv(4096)
            if b"upgrade: websocket" in data.lower():
                handle_ws(conn, data, mode)
            else:
                handle_http(conn, port)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
