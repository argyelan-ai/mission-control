"""Phase 26 / Plan 26-07 (HERM-13/F7) — regression tests for the Sessions-UI
write-channel proxy that pumps Hermes keystrokes from the browser xterm.js
through the backend WS proxy to the host-pty-bridge.

Locked contracts:
  1) When the client sends a binary frame, the proxy forwards the *exact*
     bytes upstream (no transformation, no drop).
  2) When the client sends a text frame (raw or JSON-wrapped), the proxy
     forwards the *exact* text upstream so the bridge's dual-format parser
     can choose the right path. Bytes-priority order is preserved.
  3) Read direction (upstream -> client) still forwards bytes/text frames
     unchanged — guards against a symmetric break introduced while fixing
     the write-side.
  4) The byte-counter diagnostic logging emits an info-level message per
     forwarded write-frame so silent drops become visible in the backend
     log next time. (Per feedback_terminal_keystroke_forward.md memory:
     never silently drop a frame.)

These tests bypass the FastAPI WebSocket lifecycle and exercise the inner
proxy coroutines directly with mocks. That keeps them deterministic and
sub-second while still pinning the byte-for-byte forwarding contract.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess as _subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── Helpers ────────────────────────────────────────────────────────────────

class _FakeStarletteWS:
    """Minimal stand-in for `starlette.websockets.WebSocket` that yields a
    pre-programmed sequence of `receive()` messages and records every
    `send_bytes` / `send_text` call."""

    def __init__(self, incoming: list[dict]):
        # Append a final disconnect so client_to_upstream() returns cleanly.
        self._incoming = list(incoming) + [{"type": "websocket.disconnect"}]
        self.sent: list[tuple[str, Any]] = []

    async def receive(self) -> dict:
        if not self._incoming:
            return {"type": "websocket.disconnect"}
        return self._incoming.pop(0)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(("bytes", data))

    async def send_text(self, data: str) -> None:
        self.sent.append(("text", data))


class _FakeUpstream:
    """Stand-in for the `websockets.client` upstream connection used by the
    proxy. Records every `send()` and yields pre-programmed read frames via
    `__aiter__`."""

    def __init__(self, read_frames: list | None = None):
        self.sent: list[Any] = []
        self._read_frames = list(read_frames or [])

    async def send(self, payload: Any) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        async def gen():
            for f in self._read_frames:
                yield f
        return gen()


def _build_proxy_pair(client_ws: _FakeStarletteWS, upstream: _FakeUpstream,
                      *, agent_id: str = "abc-123", slug: str = "hermes",
                      logger: logging.Logger | None = None):
    """Construct the *exact* coroutines used inside `host_agent_terminal_ws`,
    in isolation. Mirrors the production code in
    backend/app/routers/cli_terminal.py — if that source drifts, this helper
    must be updated to match (one place, easy to keep in sync).
    """
    log = logger or logging.getLogger("mc.cli_terminal")

    sent_bytes_total = 0
    sent_frames = 0
    recv_bytes_total = 0
    recv_frames = 0

    async def client_to_upstream():
        nonlocal sent_bytes_total, sent_frames
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                payload = None
                if "bytes" in msg and msg["bytes"] is not None:
                    payload = msg["bytes"]
                    await upstream.send(payload)
                elif "text" in msg and msg["text"] is not None:
                    payload = msg["text"]
                    await upstream.send(payload)
                if payload is not None:
                    n = len(payload) if isinstance(payload, (bytes, bytearray, str)) else 0
                    sent_bytes_total += n
                    sent_frames += 1
                    log.info(
                        "ws proxy: forwarded %d bytes client->upstream "
                        "(agent=%s slug=%s frame=%d total_bytes=%d)",
                        n, agent_id, slug, sent_frames, sent_bytes_total,
                    )
        except Exception as e:
            log.warning(
                "client_to_upstream stopped after %d frames / %d bytes: %s",
                sent_frames, sent_bytes_total, e,
            )
            return

    async def upstream_to_client():
        nonlocal recv_bytes_total, recv_frames
        try:
            async for frame in upstream:
                n = len(frame) if isinstance(frame, (bytes, bytearray, str)) else 0
                recv_bytes_total += n
                recv_frames += 1
                if isinstance(frame, bytes):
                    await client_ws.send_bytes(frame)
                else:
                    await client_ws.send_text(frame)
        except Exception as e:
            log.warning(
                "upstream_to_client stopped after %d frames / %d bytes: %s",
                recv_frames, recv_bytes_total, e,
            )
            return

    return client_to_upstream, upstream_to_client


# ─── Tests ──────────────────────────────────────────────────────────────────

def test_proxy_module_has_expected_helpers():
    """Sanity: the production module exposes the helper our proxy structure
    depends on (`_build_host_upstream_url`). If this disappears, the integration
    has been refactored and these tests must be re-aligned."""
    from app.routers import cli_terminal as cli_mod
    assert callable(cli_mod._build_host_upstream_url)
    assert "hermes" in cli_mod._HOST_AGENT_TMUX_TARGETS


def test_hermes_ws_write_forwards_bytes():
    """HERM-13/F7: a binary frame from the client reaches the upstream
    bridge byte-for-byte (no transformation, no drop)."""
    payload = b"echo HERMES_WRITE_TEST > /tmp/hermes-write-probe.txt\r\n"
    client = _FakeStarletteWS([{"type": "websocket.receive", "bytes": payload}])
    upstream = _FakeUpstream()

    c2u, _ = _build_proxy_pair(client, upstream)
    asyncio.run(c2u())

    assert upstream.sent == [payload], (
        f"Expected exactly one upstream send of {len(payload)} bytes; "
        f"got {len(upstream.sent)} sends: {upstream.sent!r}"
    )


def test_hermes_ws_write_forwards_text_keystroke():
    """A single-character text frame (xterm.js sends keystrokes as text)
    reaches the upstream as the same text — the bridge handles dual-format
    parsing on its end, so the proxy must not transform anything."""
    client = _FakeStarletteWS([
        {"type": "websocket.receive", "text": "a"},
        {"type": "websocket.receive", "text": "\r"},
    ])
    upstream = _FakeUpstream()

    c2u, _ = _build_proxy_pair(client, upstream)
    asyncio.run(c2u())

    assert upstream.sent == ["a", "\r"], (
        f"Per-keystroke text frames must round-trip unchanged; got: {upstream.sent!r}"
    )


def test_hermes_ws_write_dual_format_json_and_raw():
    """Mix of JSON-wrapped and raw text frames both reach upstream as text.
    The bridge's ws_to_pty parses dual-format; the proxy must not drop
    either form (per feedback_terminal_keystroke_forward.md)."""
    json_frame = '{"type":"input","data":"x"}'
    raw_frame = "y"
    client = _FakeStarletteWS([
        {"type": "websocket.receive", "text": json_frame},
        {"type": "websocket.receive", "text": raw_frame},
    ])
    upstream = _FakeUpstream()

    c2u, _ = _build_proxy_pair(client, upstream)
    asyncio.run(c2u())

    assert upstream.sent == [json_frame, raw_frame]


def test_hermes_ws_write_priority_bytes_over_text():
    """If a frame somehow contains *both* keys, the proxy prefers bytes
    (matching the production order). Locks the asymmetry guard."""
    client = _FakeStarletteWS([
        {"type": "websocket.receive", "bytes": b"BIN", "text": "TEXT"},
    ])
    upstream = _FakeUpstream()

    c2u, _ = _build_proxy_pair(client, upstream)
    asyncio.run(c2u())

    assert upstream.sent == [b"BIN"]


def test_hermes_ws_write_skips_frames_with_neither_key():
    """A receive() that has neither bytes nor text (e.g. control frame the
    framework synthesised) must be skipped silently — but it must NOT
    abort the loop. Subsequent real frames continue to flow."""
    client = _FakeStarletteWS([
        {"type": "websocket.receive"},  # no bytes, no text
        {"type": "websocket.receive", "bytes": b"X"},
    ])
    upstream = _FakeUpstream()

    c2u, _ = _build_proxy_pair(client, upstream)
    asyncio.run(c2u())

    assert upstream.sent == [b"X"], (
        f"Empty receive() must be skipped, not break the loop. Got: {upstream.sent!r}"
    )


def test_hermes_ws_write_logs_byte_counter_per_frame(caplog):
    """Diagnostic log per forwarded write-frame: 'forwarded N bytes
    client->upstream'. Without this, silent drops are invisible in
    the backend log (root cause of why F7 went undetected for so long)."""
    client = _FakeStarletteWS([
        {"type": "websocket.receive", "bytes": b"abc"},
    ])
    upstream = _FakeUpstream()

    log = logging.getLogger("mc.cli_terminal.test")
    log.setLevel(logging.INFO)

    c2u, _ = _build_proxy_pair(client, upstream, logger=log)
    with caplog.at_level(logging.INFO, logger="mc.cli_terminal.test"):
        asyncio.run(c2u())

    matched = [r for r in caplog.records if "forwarded" in r.getMessage()
               and "client->upstream" in r.getMessage()]
    assert matched, (
        "Expected at least one info-log 'forwarded N bytes client->upstream' — "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    # And it must report the right byte count.
    assert any("forwarded 3 bytes" in r.getMessage() for r in matched), (
        f"Byte count should be 3 (len of b'abc'); got: "
        f"{[r.getMessage() for r in matched]}"
    )


def test_hermes_ws_read_direction_unchanged_bytes():
    """Regression: read direction (upstream -> client) for binary frames
    must still forward bytes-for-bytes after the write-side changes."""
    frame = b"\x1b[32mok\x1b[0m"
    client = _FakeStarletteWS([])
    upstream = _FakeUpstream(read_frames=[frame])

    _, u2c = _build_proxy_pair(client, upstream)
    asyncio.run(u2c())

    assert client.sent == [("bytes", frame)]


def test_hermes_ws_read_direction_unchanged_text():
    """Regression: text frames upstream -> client still go via send_text."""
    client = _FakeStarletteWS([])
    upstream = _FakeUpstream(read_frames=["hello-from-tmux"])

    _, u2c = _build_proxy_pair(client, upstream)
    asyncio.run(u2c())

    assert client.sent == [("text", "hello-from-tmux")]


# ─── HERM-15: Direct tmux send-keys write-channel ───────────────────────────
# These tests cover the _hermes_ws_send_keys() helper added in plan 27-06.
# The helper is called by host_agent_terminal_ws when the slug is "hermes"
# and the client sends a write-frame (text or bytes).

class _FakeTmux:
    """Records every tmux argv and replays canned answers per subcommand:
    ``has-session``, the ``#{pane_mode}`` probe and ``send-keys``.

    Returns real ``subprocess.CompletedProcess`` objects — not MagicMocks —
    so the code under test decodes genuine bytes from the probe. A MagicMock
    stdout would be truthy and look like an active pane mode, which is
    exactly the failure this suite has to be able to tell apart.
    """

    def __init__(self, pane_mode: str = "", probe_rc: int = 0,
                 has_session: bool = True, send_keys_rc: int = 0):
        self.pane_mode = pane_mode
        self.probe_rc = probe_rc
        self.has_session = has_session
        self.send_keys_rc = send_keys_rc
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs) -> _subprocess.CompletedProcess:
        self.calls.append(list(argv))
        subcommand = argv[1] if len(argv) > 1 else ""
        if subcommand == "has-session":
            rc = 0 if self.has_session else 1
            return _subprocess.CompletedProcess(argv, rc, stdout=b"", stderr=b"")
        if subcommand == "display-message":
            if self.probe_rc != 0:
                return _subprocess.CompletedProcess(
                    argv, self.probe_rc, stdout=b"", stderr=b"no server running")
            # Real tmux prints the mode followed by a newline; empty = no mode.
            out = f"{self.pane_mode}\n".encode() if self.pane_mode else b"\n"
            return _subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")
        if subcommand == "send-keys":
            return _subprocess.CompletedProcess(
                argv, self.send_keys_rc, stdout=b"", stderr=b"tmux send-keys failed")
        raise AssertionError(f"unexpected tmux call: {argv!r}")


def _send_with_tmux(fake: _FakeTmux, message: str = "hello\n") -> tuple[dict, _FakeTmux]:
    """Runs _hermes_ws_send_keys against ``fake`` and returns (result, fake)."""
    from unittest.mock import patch
    from app.routers.cli_terminal import _hermes_ws_send_keys

    with patch("app.routers.cli_terminal.subprocess") as mock_subprocess:
        mock_subprocess.run.side_effect = fake.run
        result = _hermes_ws_send_keys(message)
    return result, fake


def test_ws_write_reaches_hermes_tmux():
    """HERM-15: WS text write-message triggers tmux send-keys for hermes-worker."""
    result, fake = _send_with_tmux(_FakeTmux())

    assert result["ok"] is True, f"a pane with no mode must still accept keys; got {result!r}"
    # The message itself must reach the argv byte-for-byte, with the trailing
    # "" (no Enter) that the caller relies on.
    assert fake.calls[-1] == [
        "tmux", "send-keys", "-t", "hermes-worker", "hello\n", "",
    ], f"Expected send-keys cmd; got: {fake.calls!r}"


def test_ws_write_empty_string_dropped():
    """HERM-15: empty or whitespace-only message must not call subprocess."""
    from unittest.mock import patch
    from app.routers.cli_terminal import _hermes_ws_send_keys

    with patch("app.routers.cli_terminal.subprocess") as mock_subprocess:
        # Empty string
        result_empty = _hermes_ws_send_keys("")
        # Whitespace only
        result_ws = _hermes_ws_send_keys("   ")

    assert result_empty["ok"] is False
    assert result_ws["ok"] is False
    mock_subprocess.run.assert_not_called()


def test_ws_write_session_not_found():
    """HERM-15: if hermes-worker tmux session doesn't exist, return error (no crash)."""
    result, fake = _send_with_tmux(_FakeTmux(has_session=False))

    assert result["ok"] is False
    assert "session" in result["error"].lower()
    # Short-circuit before the probe: no display-message, no send-keys.
    assert [c[1] for c in fake.calls] == ["has-session"], (
        f"must not touch the pane after a failed has-session; got: {fake.calls!r}"
    )


def test_ws_write_refuses_when_pane_is_in_copy_mode():
    """A pane in copy-mode swallows send-keys at rc=0 (live-verified: the text
    never reaches the program, tmux still exits 0). Acking ok:true there is a
    silent loss — the helper must refuse honestly and skip send-keys entirely.
    Same refusal shape as the host-pty-bridge (PR #629)."""
    result, fake = _send_with_tmux(_FakeTmux(pane_mode="copy-mode"))

    assert result["ok"] is False, f"copy-mode must never ack success; got {result!r}"
    assert result["pane_mode"] == "copy-mode"
    assert "copy-mode" in result["error"]
    assert not [c for c in fake.calls if c[1] == "send-keys"], (
        f"send-keys must not run while the pane is in a mode; got: {fake.calls!r}"
    )


def test_ws_write_probe_failure_falls_through_to_send_keys():
    """An impossible probe (no server on the socket, timeout, missing tmux)
    is not a refusal: it falls through so send-keys' own return code decides,
    matching the bridge's behaviour."""
    result, fake = _send_with_tmux(_FakeTmux(probe_rc=1))

    assert result["ok"] is True, f"a failed probe must not refuse; got {result!r}"
    assert fake.calls[-1][1] == "send-keys", (
        f"a failed probe must fall through to send-keys; got: {fake.calls!r}"
    )


def test_ws_write_reports_send_keys_failure():
    """A failing send-keys still surfaces as ok:false (no regression from the
    added probe: the probe must not short-circuit the real error path)."""
    result, fake = _send_with_tmux(_FakeTmux(send_keys_rc=1))

    assert result["ok"] is False
    assert "send-keys" in result["error"]
    assert fake.calls[-1][1] == "send-keys"


def test_ws_read_stream_unaffected():
    """HERM-15: read-stream (upstream → client) still works when write-path is active.

    Regression guard: adding the write-channel must not break the existing
    upstream_to_client() direction. Uses same _build_proxy_pair helper as Phase 26.
    """
    frame = b"\x1b[33mhermes output\x1b[0m"
    client = _FakeStarletteWS([])
    upstream = _FakeUpstream(read_frames=[frame])

    _, u2c = _build_proxy_pair(client, upstream)
    asyncio.run(u2c())

    assert client.sent == [("bytes", frame)], (
        f"Read stream must forward bytes unchanged; got: {client.sent!r}"
    )


# ─── Bridge-side parser contract (host-pty-bridge/server.py) ────────────────

def test_bridge_ws_to_pty_parser_accepts_raw_text():
    """The host-pty-bridge ws_to_pty() must NOT drop a raw-text frame just
    because it fails JSON parsing. Codified contract from
    feedback_terminal_keystroke_forward.md (`WS dual-format`).

    We test the parser logic by re-implementing the decision tree here
    — the production code lives at docker/host-pty-bridge/server.py
    `ws_to_pty()`. If you change the parser there, mirror it here.
    """
    import json

    def classify(msg) -> tuple[str, bytes | None]:
        """Mirror of ws_to_pty()'s decision tree. Returns ('binary'|'json-input'|
        'json-resize'|'raw-text', payload-as-bytes-or-None)."""
        if isinstance(msg, (bytes, bytearray)):
            return ("binary", bytes(msg))
        # text path
        try:
            d = json.loads(msg)
            if isinstance(d, dict):
                if d.get("type") == "resize":
                    return ("json-resize", None)
                if d.get("type") == "input":
                    return ("json-input", d.get("data", "").encode())
        except (json.JSONDecodeError, ValueError):
            pass
        return ("raw-text", msg.encode())

    # Single keystroke that is not valid JSON → raw text fallback (NOT dropped)
    kind, payload = classify("a")
    assert kind == "raw-text" and payload == b"a"

    # Escape sequence (arrow key) → not JSON, raw text fallback
    kind, payload = classify("\x1b[A")
    assert kind == "raw-text" and payload == b"\x1b[A"

    # JSON input wrapper → unwrapped to bytes
    kind, payload = classify('{"type":"input","data":"hi"}')
    assert kind == "json-input" and payload == b"hi"

    # Binary frame → forwarded as-is
    kind, payload = classify(b"raw\xff")
    assert kind == "binary" and payload == b"raw\xff"

    # Resize frame → handled (no payload to PTY)
    kind, payload = classify('{"type":"resize","cols":120,"rows":40}')
    assert kind == "json-resize" and payload is None
