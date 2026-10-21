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

The deliverables root (second half of this file): MC tells a container
agent to put its results under /deliverables/<task_id>/ (ADR-022 mount,
backend deliverable_paths). omp's write tool goes through fs/write_text_file,
so the jail serves that root too — with the same realpath check, so a
symlink or `..` out of /deliverables is refused like one out of the cwd.

Run:  python3 test_fs_jail.py   (standalone)   OR   pytest -v
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from typing import Iterator, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import acp_client.py

import acp_client  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))


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
# The deliverables root
# ---------------------------------------------------------------------------
# The roots are read when the client is built (bridge.py run_acp_once and
# acp_chat.py both build it with ACPClient(cwd=...)), so every test builds
# its client inside _deliverables_config(). Plain helpers instead of pytest
# fixtures: the standalone runner below calls each test without arguments.

TASK_ID = "11467ea6-4749-47be-901d-6acf900efb2d"
_UNSET = object()


@contextlib.contextmanager
def _deliverables_config(env=_UNSET, default=_UNSET) -> Iterator[None]:
    """Set MC_DELIVERABLES_DIR (None = unset) and the built-in default root
    for the duration of the block, then put both back."""
    old_env = os.environ.get("MC_DELIVERABLES_DIR")
    had_default = hasattr(acp_client, "DEFAULT_DELIVERABLES_DIR")
    old_default = getattr(acp_client, "DEFAULT_DELIVERABLES_DIR", None)
    try:
        if env is not _UNSET:
            if env is None:
                os.environ.pop("MC_DELIVERABLES_DIR", None)
            else:
                os.environ["MC_DELIVERABLES_DIR"] = env
        if default is not _UNSET:
            acp_client.DEFAULT_DELIVERABLES_DIR = default
        yield
    finally:
        if old_env is None:
            os.environ.pop("MC_DELIVERABLES_DIR", None)
        else:
            os.environ["MC_DELIVERABLES_DIR"] = old_env
        if had_default:
            acp_client.DEFAULT_DELIVERABLES_DIR = old_default
        elif hasattr(acp_client, "DEFAULT_DELIVERABLES_DIR"):
            delattr(acp_client, "DEFAULT_DELIVERABLES_DIR")


@contextlib.contextmanager
def _dirs(*prefixes: str) -> Iterator[list]:
    """Fresh temp folders (realpath'd — macOS /var is a symlink), removed after."""
    made = [os.path.realpath(tempfile.mkdtemp(prefix=p)) for p in prefixes]
    try:
        yield made
    finally:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def _error(sent: list) -> Optional[dict]:
    assert len(sent) == 1, sent
    assert sent[0]["id"] == 7
    return sent[0].get("error")


def test_fs_write_into_deliverables_root_is_served():
    """The live failure (bench task, 2026-10-21): omp's write tool put the
    result at /deliverables/<task_id>/index.html, as the dispatch prompt
    says, and got -32002 "outside session cwd '/workspace'". With the root
    configured the write lands — also when the task folder does not exist
    yet (the first file of a task)."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)
        target = os.path.join(deliv, TASK_ID, "index.html")

        _fs_request(client, "fs/write_text_file",
                    {"path": target, "content": "<!DOCTYPE html>"})

        assert _error(sent) is None, sent
        assert sent[0]["result"] == {}
        with open(target, encoding="utf-8") as f:
            assert f.read() == "<!DOCTYPE html>"


def test_fs_read_from_deliverables_root_is_served():
    """Reading back a result (e.g. to check it before registering) works too."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        os.makedirs(os.path.join(deliv, TASK_ID))
        target = os.path.join(deliv, TASK_ID, "report.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("# report")
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/read_text_file", {"path": target})

        assert sent == [{"jsonrpc": "2.0", "id": 7,
                         "result": {"content": "# report"}}]


def test_default_root_is_the_container_mount_when_it_exists():
    """No env var: the built-in default (the container's /deliverables mount)
    is served when that folder exists."""
    with _dirs("omp-fs-cwd-", "omp-fs-mount-") as (cwd, mount):
        with _deliverables_config(env=None, default=mount):
            client, sent = _jailed_client(cwd)
        target = os.path.join(mount, TASK_ID, "index.html")

        _fs_request(client, "fs/write_text_file",
                    {"path": target, "content": "ok"})

        assert _error(sent) is None, sent
        assert os.path.isfile(target)


def test_symlink_from_deliverables_to_etc_is_rejected():
    """A folder link inside /deliverables pointing at /etc resolves outside
    every allowed root — refused, nothing read."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        os.symlink("/etc", os.path.join(deliv, "etc"))
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/read_text_file",
                    {"path": os.path.join(deliv, "etc", "hosts")})

        err = _error(sent)
        assert err is not None and err["code"] == -32002
        assert "outside session cwd" in err["message"]


def test_symlink_write_out_of_deliverables_is_rejected():
    """A file link inside /deliverables pointing at a file outside: the write
    is refused and the outside file keeps its content."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-", "omp-fs-outside-") as (cwd, deliv, outside):
        secret = os.path.join(outside, "secret.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("original")
        os.symlink(secret, os.path.join(deliv, "link.txt"))
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/write_text_file",
                    {"path": os.path.join(deliv, "link.txt"), "content": "hacked"})

        err = _error(sent)
        assert err is not None and err["code"] == -32002
        with open(secret, encoding="utf-8") as f:
            assert f.read() == "original"


def test_dotdot_out_of_deliverables_is_rejected():
    """`..` out of /deliverables is refused, and refused BEFORE any folder is
    made: the missing folders along an escaping path are never created."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-", "omp-fs-outside-") as (cwd, deliv, outside):
        escape = os.path.join(deliv, TASK_ID, "..", "..",
                              os.path.basename(outside), "made", "x.txt")
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/write_text_file",
                    {"path": escape, "content": "x"})

        err = _error(sent)
        assert err is not None and err["code"] == -32002
        assert not os.path.exists(os.path.join(outside, "made"))
        assert not os.path.exists(os.path.join(deliv, TASK_ID))


def test_foreign_path_stays_rejected_with_a_deliverables_root():
    """The root opens /deliverables, nothing else: a path in neither the cwd
    nor the root is still refused, and the error names both."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-", "omp-fs-outside-") as (cwd, deliv, outside):
        foreign = os.path.join(outside, "x.txt")
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/write_text_file",
                    {"path": foreign, "content": "x"})

        err = _error(sent)
        assert err is not None and err["code"] == -32002
        assert cwd in err["message"] and deliv in err["message"]
        assert not os.path.exists(foreign)


def test_without_config_only_the_cwd_is_served():
    """No env var and no mount (the host, CI): the jail is the cwd alone —
    the behaviour before the deliverables root existed. An explicitly empty
    MC_DELIVERABLES_DIR switches the root off even where the mount exists."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        missing = os.path.join(deliv, "not-mounted")
        target = os.path.join(deliv, "x.txt")
        for env, default in ((None, missing), ("", deliv)):
            with _deliverables_config(env=env, default=default):
                client, sent = _jailed_client(cwd)

            _fs_request(client, "fs/write_text_file",
                        {"path": target, "content": "x"})

            err = _error(sent)
            assert err is not None and err["code"] == -32002, (env, sent)
            assert not os.path.exists(target)


def test_filesystem_root_is_never_a_deliverables_root():
    """A misconfigured MC_DELIVERABLES_DIR=/ would open every path — it is
    ignored, and so are relative entries."""
    with _dirs("omp-fs-cwd-") as (cwd,):
        with _deliverables_config(env=os.pathsep.join(["/", "relative/dir"])):
            client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/read_text_file", {"path": "/etc/hosts"})

        err = _error(sent)
        assert err is not None and err["code"] == -32002


def test_no_session_rejects_the_deliverables_root_too():
    """Before session/new there is no jail — every fs request is refused,
    the deliverables root does not open a way around that."""
    with _dirs("omp-fs-deliv-") as (deliv,):
        with _deliverables_config(env=deliv):
            client = acp_client.ACPClient(command=["never-spawned"])
        sent: list = []
        client._send_raw = sent.append  # type: ignore[method-assign]

        _fs_request(client, "fs/write_text_file",
                    {"path": os.path.join(deliv, "x.txt"), "content": "x"})

        err = _error(sent)
        assert err is not None and err["code"] == -32002
        assert not os.path.exists(os.path.join(deliv, "x.txt"))


def test_default_root_matches_the_backend_contract():
    """The default is not a second truth: it is the mount target the compose
    renderer gives every agent container (ADR-022) and the first path the
    backend accepts for a deliverable. A rename on either side trips here."""
    default = acp_client.DEFAULT_DELIVERABLES_DIR
    with open(os.path.join(REPO_ROOT, "backend", "app", "services",
                           "deliverable_paths.py"), encoding="utf-8") as f:
        contract = f.read()
    with open(os.path.join(REPO_ROOT, "backend", "app", "services",
                           "compose_renderer.py"), encoding="utf-8") as f:
        renderer = f.read()
    assert f'f"{default}/{{task_id}}/"' in contract
    assert f'.mc/deliverables/{{slug}}:{default}")' in renderer


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
