#!/usr/bin/env python3
"""fs-Jail tests (Review #464 Major follow-up, PR #471) — the security
boundary in acp_client._handle_fs_request: fs/read_text_file and
fs/write_text_file must be served INSIDE the session's realpath(cwd) jail
and rejected OUTSIDE it, with the escape checked on the RESOLVED path
(realpath), not the textual one.

Covers the two behaviors from the second-review checklist:
  1. a plain fs/read_text_file inside the session cwd is served from disk,
  2. an escape attempt is refused with -32002 "outside session cwd" —
     once via a `..` path component, once via a symlink that resolves
     outside the jail (both realpath escapes; a prefix-string check on the
     textual path would wrongly allow both).

Run:  python3 test_fs_jail.py   (standalone)   OR   pytest -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import acp_client.py

import acp_client  # noqa: E402


def _jailed_client(jail: str) -> tuple[acp_client.ACPClient, list[dict]]:
    """ACPClient wired for in-process dispatch: no subprocess — replies are
    captured instead of written to a (never-spawned) server's stdin. The jail
    is set exactly as new_session() does after a successful session/new."""
    client = acp_client.ACPClient(command=["never-spawned"])
    client._fs_jail = os.path.realpath(jail)
    sent: list[dict] = []
    client._send_raw = sent.append  # type: ignore[method-assign]
    return client, sent


def _fs_request(client: acp_client.ACPClient, method: str, params: dict) -> None:
    """Drive the REAL reader-loop dispatch for a server->client fs request."""
    client._dispatch({"jsonrpc": "2.0", "id": 7, "method": method,
                      "params": params})


def test_fs_read_inside_cwd_is_served():
    """A file inside the session cwd is read and returned as content."""
    with tempfile.TemporaryDirectory(prefix="omp-fs-jail-") as cwd:
        inner = os.path.join(cwd, "inner.txt")
        with open(inner, "w", encoding="utf-8") as f:
            f.write("hello jail")
        client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/read_text_file", {"path": inner})

        assert sent == [{"jsonrpc": "2.0", "id": 7,
                         "result": {"content": "hello jail"}}]


def test_fs_read_dotdot_escape_is_rejected():
    """`..` climbs out of the jail — realpath resolves it, the request is
    refused with -32002 even though the textual path starts with cwd."""
    with tempfile.TemporaryDirectory(prefix="omp-fs-jail-") as cwd:
        outside_dir = tempfile.mkdtemp(prefix="omp-fs-outside-")
        try:
            outside = os.path.join(outside_dir, "secret.txt")
            with open(outside, "w", encoding="utf-8") as f:
                f.write("outside")
            escape = os.path.join(cwd, "..", os.path.basename(outside_dir),
                                  "secret.txt")
            client, sent = _jailed_client(cwd)

            _fs_request(client, "fs/read_text_file", {"path": escape})

            assert len(sent) == 1
            err = sent[0]["error"]
            assert sent[0]["id"] == 7
            assert err["code"] == -32002
            assert "outside session cwd" in err["message"]
        finally:
            os.remove(os.path.join(outside_dir, "secret.txt"))
            os.rmdir(outside_dir)


def test_fs_read_symlink_escape_is_rejected():
    """A symlink inside the jail pointing outside resolves via realpath to a
    path outside the jail — refused, a prefix-string check on the textual
    path would have allowed it."""
    with tempfile.TemporaryDirectory(prefix="omp-fs-jail-") as cwd:
        outside_dir = tempfile.mkdtemp(prefix="omp-fs-outside-")
        try:
            outside = os.path.join(outside_dir, "secret.txt")
            with open(outside, "w", encoding="utf-8") as f:
                f.write("outside")
            link = os.path.join(cwd, "link.txt")
            os.symlink(outside, link)
            client, sent = _jailed_client(cwd)

            _fs_request(client, "fs/read_text_file", {"path": link})

            assert len(sent) == 1
            err = sent[0]["error"]
            assert sent[0]["id"] == 7
            assert err["code"] == -32002
            assert "outside session cwd" in err["message"]
        finally:
            os.remove(link)
            os.remove(outside)
            os.rmdir(outside_dir)


def test_fs_write_escape_is_rejected_too():
    """The jail guards fs/write_text_file identically (same check, both
    methods) — an escaping write must not touch the outside file."""
    with tempfile.TemporaryDirectory(prefix="omp-fs-jail-") as cwd:
        outside_dir = tempfile.mkdtemp(prefix="omp-fs-outside-")
        try:
            outside = os.path.join(outside_dir, "secret.txt")
            with open(outside, "w", encoding="utf-8") as f:
                f.write("original")
            link = os.path.join(cwd, "link.txt")
            os.symlink(outside, link)
            client, sent = _jailed_client(cwd)

            _fs_request(client, "fs/write_text_file",
                        {"path": link, "content": "hacked"})

            assert len(sent) == 1 and sent[0]["error"]["code"] == -32002
            with open(outside, encoding="utf-8") as f:
                assert f.read() == "original"
        finally:
            os.remove(link)
            os.remove(outside)
            os.rmdir(outside_dir)


# ---------------------------------------------------------------------------
# Standalone runner (matches test_heartbeat_context.py's pattern)
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
