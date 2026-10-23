#!/usr/bin/env python3
"""The chat daemon keeps the latest context fill in its state.

Round-2 user test (t4 S-neu-3, 23.10.2026): Hermes' agent page said
"Kontext —" while its chat said "2 % belegt". The chat ring reads the
`usage_update` the agent sends (size = window, used = tokens in use) from
the transcript; the agent page reads the heartbeat's `context_pct`. Hermes
runs over ACP and has no TUI pane, so its heartbeat scrape found nothing and
the page had no number.

The daemon now remembers the last valid `usage_update` and exposes it as
`state()["context"]` = {"used", "size", "pct"}. The host bridge reports
that `pct` on the heartbeat it already sends — the same figure the chat
shows. Garbage (non-int, size <= 0, used outside 0..size) is ignored, a
fresh `/new` session starts from "no reading" again.

Runs two ways:
  * pytest:      pytest test_acp_chat_context_fill.py -v
  * standalone:  python3 test_acp_chat_context_fill.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402,F401
from test_acp_chat import FakeClient, make_session  # noqa: E402


def _usage(client: FakeClient, size, used) -> None:
    client.fire({
        "sessionId": "sid-1",
        "update": {"sessionUpdate": "usage_update", "size": size, "used": used},
    })


def test_state_carries_the_last_usage_update(tmp_path):
    client = FakeClient()
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        assert sess.state()["context"] is None
        _usage(client, 1_048_576, 20_972)
        assert sess.state()["context"] == {"used": 20_972, "size": 1_048_576, "pct": 2.0}
        _usage(client, 1_048_576, 524_288)
        assert sess.state()["context"]["pct"] == 50.0
    finally:
        sess.close()


def test_garbage_never_overwrites_a_good_reading(tmp_path):
    client = FakeClient()
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        _usage(client, 200_000, 10_000)
        for size, used in ((0, 10), (100, 200), ("1000", 5), (1000, -1), (None, None)):
            _usage(client, size, used)
        assert sess.state()["context"] == {"used": 10_000, "size": 200_000, "pct": 5.0}
    finally:
        sess.close()


def test_a_new_session_forgets_the_old_fill(tmp_path):
    client = FakeClient()
    sess = make_session(tmp_path, client)
    sess.start()
    try:
        _usage(client, 200_000, 150_000)
        assert sess.state()["context"]["pct"] == 75.0
        sess.new_session()
        assert sess.wait_idle(5)
        assert sess.state()["context"] is None
    finally:
        sess.close()


if __name__ == "__main__":
    for fn in (
        test_state_carries_the_last_usage_update,
        test_garbage_never_overwrites_a_good_reading,
        test_a_new_session_forgets_the_old_fill,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            fn(Path(tmp))
        print(f"PASS {fn.__name__}")
