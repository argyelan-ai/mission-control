"""Phase 26 / Plan 26-01 → 26-07 (HERM-13/F7): Sessions-UI WebSocket
write-channel forwards keystrokes to the Hermes tmux session.

History:
- 26-01: RED stub (assert False) so 26-07 has a failing target to flip GREEN.
- 26-07: Diagnostic logging + structural fix + per-frame forward contract
  test. The full contract suite lives in
  `tests/test_cli_terminal_hermes_write.py`. This file keeps the named
  RED→GREEN audit trail by re-asserting the most important byte-for-byte
  invariant.
"""
from __future__ import annotations

import asyncio


def test_hermes_ws_write_forwards_bytes():
    """GREEN — HERM-13/F7: WS proxy forwards keystroke bytes upstream
    to the Hermes tmux session byte-for-byte.

    Verified post Plan 26-07 with both:
    1. The byte-counter diagnostic log in
       `backend/app/routers/cli_terminal.py` host_agent_terminal_ws
       (logs 'forwarded N bytes client->upstream' per frame), and
    2. This in-process unit test that exercises the proxy coroutine
       directly with a fake upstream and asserts the exact byte payload
       arrived without transformation.
    """
    # Re-use the helper from the canonical contract suite to avoid
    # duplicating proxy reconstruction logic.
    from tests.test_cli_terminal_hermes_write import (
        _FakeStarletteWS,
        _FakeUpstream,
        _build_proxy_pair,
    )

    payload = b"echo HERMES_WRITE_TEST > /tmp/hermes-write-probe.txt\r\n"
    client = _FakeStarletteWS([{"type": "websocket.receive", "bytes": payload}])
    upstream = _FakeUpstream()

    c2u, _ = _build_proxy_pair(client, upstream)
    asyncio.run(c2u())

    assert upstream.sent == [payload], (
        f"WS proxy must forward keystroke bytes byte-for-byte; "
        f"expected {payload!r}, got {upstream.sent!r}"
    )
