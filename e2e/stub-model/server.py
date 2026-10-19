"""Stub model server for the journey tests (docs/journeys.md).

A stand-in for a local model server (Ollama, LM Studio, vLLM): it speaks the
OpenAI-compatible endpoints MC's probes and agents use — ``GET /v1/models``
and ``POST /v1/chat/completions`` (plain or streamed) — with one fixed model
and one fixed answer. No model, no GPU, no network, standard library only, so
it runs in a bare ``python:3-alpine`` next to the journey stack.

The first-run journey adds it as the operator's AI (step S2) and stops its
container for the "model gone" sabotage. Tested by
backend/tests/test_e2e_stub_model.py.

    python3 server.py [--host 0.0.0.0] [--port 8080]
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "stub-model"
CONTEXT_LENGTH = 8192
REPLY = "Stub model here. I answer every prompt with this sentence."


def _models() -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_ID,
                "object": "model",
                "created": 0,
                "owned_by": "mc-journeys",
                "max_model_len": CONTEXT_LENGTH,
            }
        ],
    }


def _usage(messages: list) -> dict:
    prompt = sum(len(str(m.get("content", "")).split()) for m in messages if isinstance(m, dict))
    completion = len(REPLY.split())
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


class Handler(BaseHTTPRequestHandler):
    server_version = "mc-stub-model/1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102 — one short line per request
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": "invalid_request_error"}})

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._json(200, _models())
        elif path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._error(404, f"no route {path}")

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._error(404, f"no route {path}")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body is not an object")
        except ValueError as exc:
            self._error(400, f"bad JSON: {exc}")
            return
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        created = int(time.time())
        if body.get("stream"):
            self._stream(created)
            return
        self._json(
            200,
            {
                "id": f"chatcmpl-stub-{created}",
                "object": "chat.completion",
                "created": created,
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": REPLY},
                        "finish_reason": "stop",
                    }
                ],
                "usage": _usage(messages),
            },
        )

    def _stream(self, created: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        words = REPLY.split(" ")
        for i, word in enumerate(words):
            delta = {"content": word if i == 0 else f" {word}"}
            if i == 0:
                delta["role"] = "assistant"
            self._event(created, delta, None)
        self._event(created, {}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _event(self, created: int, delta: dict, finish: str | None) -> None:
        chunk = {
            "id": f"chatcmpl-stub-{created}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    httpd = make_server(args.host, args.port)
    print(f"stub model {MODEL_ID} on {args.host}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
