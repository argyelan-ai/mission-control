#!/usr/bin/env python3
"""Chats slice S2 acceptance (architecture study §5, S2): "Two chats on the
omp agent, alternate messages, both continue with their own context; daemon
restart -> both resume via session/load".

End to end without mocks inside the daemon: real `ACPClient` children
(a fake `omp acp` that remembers each session on disk, fake_omp_memory.py),
the real socket server, the real `acp_chat_ctl.py` shim the backend calls,
and the real `McChats` HTTP client against a small fake of MC's two
agent-token routes (`/api/v1/chat-record/agent/me/chats/{id}/resume` and
`/native-session`, rules of #1041's resume_plan).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_chat_pool  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import assistant_texts, read_lines  # noqa: E402

A = "aaaaaaaa-0000-4000-8000-00000000000a"
B = "bbbbbbbb-0000-4000-8000-00000000000b"
TOKEN = "agent-token-for-tests"
PREFIX = "/api/v1/chat-record/agent/me/chats/"


class FakeMcServer:
    def __init__(self, known):
        self.known = set(known)
        self.pins: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _answer(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _chat(self):
                outer.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._answer(401, {"detail": "no token"})
                    return None
                if not self.path.startswith(PREFIX):
                    self._answer(404, {"detail": "Not found"})
                    return None
                chat_id, _, rest = self.path[len(PREFIX):].partition("/")
                if chat_id not in outer.known:
                    self._answer(404, {"detail": "Chat not found"})
                    return None
                return chat_id, rest

            def do_GET(self):
                found = self._chat()
                if found is None:
                    return
                chat_id, _ = found
                native = outer.pins.get(chat_id)
                self._answer(200, {
                    "chatId": chat_id, "harness": "omp", "style": "acp", "agentDefault": False,
                    "nativeSessionId": native, "action": "resume" if native else "start",
                    "acp": {"method": "session/load", "sessionId": native} if native
                    else {"method": "session/new"},
                })

            def do_PUT(self):
                found = self._chat()
                if found is None:
                    return
                chat_id, _ = found
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                native = body["nativeSessionId"]
                if outer.pins.get(chat_id) not in (None, native):
                    self._answer(409, {"detail": {"reason": "already_pinned"}})
                    return
                outer.pins[chat_id] = native
                self._answer(200, {"id": chat_id, "nativeSessionId": native, "movedEvents": 0})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Daemon:
    """What `acp_chat.py --serve` builds, with the fake omp as the child."""

    def __init__(self, tmp: Path, mc_url: str):
        self.sessions_dir = tmp / "omp-sessions" / "--workspace--"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        work = tmp / "work"
        work.mkdir(exist_ok=True)
        env = {"FAKE_OMP_STORE": str(tmp / "omp-store"), "PATH": "/usr/bin:/bin"}
        command = [sys.executable, str(HERE / "fake_omp_memory.py")]
        self.default = acp_chat.ChatSession(
            client_factory=lambda: acp_client.ACPClient(command=command, cwd=str(work), env=env),
            cwd=str(work), state_dir=self.sessions_dir, sessions_dir=self.sessions_dir,
        )
        self.default.start()
        self.pool = acp_chat_pool.build_pool(
            self.default, mc=acp_chat_pool.McChats(mc_url, token=lambda: TOKEN))
        self.sock = Path(tempfile.mkdtemp(prefix="acpmany")) / "c.sock"
        self.server = acp_chat.ChatSocketServer(self.default, str(self.sock), pool=self.pool)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        deadline = time.time() + 3
        while not self.sock.exists() and time.time() < deadline:
            time.sleep(0.01)

    def ctl(self, op, payload=None):
        args = [sys.executable, str(ROOT / "acp_chat_ctl.py"), op, "--socket", str(self.sock)]
        if payload is not None:
            args[3:3] = ["--json", json.dumps(payload)]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return proc.returncode, json.loads(proc.stdout or "{}")

    def say(self, chat_id, text):
        code, body = self.ctl("prompt", {"text": text, "chatId": chat_id, "mode": "queue"})
        assert code == 0 and body["ok"] is True, body
        chat, _ = self.pool.get(chat_id, start=False)
        assert chat.wait_idle(20)
        return assistant_texts(read_lines(chat.transcript_path))[-1]

    def close(self):
        self.server.shutdown()
        self.pool.close()
        self.default.close()


def test_two_chats_keep_their_own_context_and_resume_after_a_restart(tmp_path):
    mc = FakeMcServer(known=(A, B))
    try:
        d = Daemon(tmp_path, mc.url)
        try:
            assert d.say(A, "my cat is Tom") == "I know: my cat is Tom"
            assert d.say(B, "my dog is Bello") == "I know: my dog is Bello"
            assert d.say(A, "what is my pet?") == "I know: my cat is Tom, what is my pet?"
            assert d.say(B, "what is my pet?") == "I know: my dog is Bello, what is my pet?"
            pins = dict(mc.pins)
            assert set(pins) == {A, B} and pins[A] != pins[B]
            # the agent's own chat never saw any of it
            default_state = json.loads((d.sessions_dir / "acp-chat-state.json").read_text())
            assert default_state["sessionId"] not in pins.values()
            assert assistant_texts(read_lines(d.default.transcript_path)) == []
            files = {c: d.pool.get(c, start=False)[0].transcript_path for c in (A, B)}
        finally:
            d.close()

        d2 = Daemon(tmp_path, mc.url)   # daemon restart: new children, new pool
        try:
            assert d2.say(B, "and now?") == "I know: my dog is Bello, what is my pet?, and now?"
            assert d2.say(A, "and now?") == "I know: my cat is Tom, what is my pet?, and now?"
            assert mc.pins == pins       # same native sessions, nothing re-pinned elsewhere
            for c in (A, B):
                assert d2.pool.get(c, start=False)[0].transcript_path == files[c]
            # the backend's view of the further chats through the ctl shim
            code, body = d2.ctl("chats")
            assert code == 0 and {c["chatId"] for c in body["chats"]} == {A, B}
        finally:
            d2.close()
        puts = [r for r in mc.requests if r[0] == "PUT"]
        assert len(puts) == 2, puts     # one pin per chat, at its first session/new
    finally:
        mc.close()


def test_a_chat_mc_does_not_know_is_refused_through_the_shim(tmp_path):
    mc = FakeMcServer(known=())
    try:
        d = Daemon(tmp_path, mc.url)
        try:
            code, body = d.ctl("prompt", {"text": "x", "chatId": A})
            assert code == 2 and body == {"ok": False, "error": "chat_unknown"}
            code, body = d.ctl("chats")
            assert body == {"ok": True, "chats": []}
        finally:
            d.close()
    finally:
        mc.close()
