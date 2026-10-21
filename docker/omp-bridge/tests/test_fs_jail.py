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

The scratch roots (last part): inside an agent container the jail also
serves the container's own /tmp and its /workspace mount — omp's bash
writes both anyway, only its read/write tools were refused. Same realpath
check again: a link out of /tmp or /workspace (to /etc, to ~/.omp, to the
mounts /workspace-ref and /vault) stays refused. /tmp shares the
container's own filesystem with ~/.omp, so a file served only through a
scratch root must also be a regular file with one link. The price of
/workspace: a task in its own worktree reaches the main clone and the
other tasks' worktrees with its file tools (operator's decision).
Every client in this file is built as on the host (no container marker,
no MC_FS_SCRATCH_DIRS) unless a test turns the scratch roots on, so the
machine's own /tmp never leaks into the older tests.

Run:  python3 test_fs_jail.py   (standalone)   OR   pytest -v
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
from typing import Iterator, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import acp_client.py

import acp_client  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

_UNSET = object()
#: Stand-ins for the container marker: a file that exists ("in a
#: container") and one that does not ("on the host").
_MARKER_PRESENT = os.path.abspath(__file__)
_MARKER_MISSING = os.path.join(HERE, "no-such-container-marker")


@contextlib.contextmanager
def _scratch_config(env=None, default=_UNSET, container=False,
                    checkouts=_UNSET) -> Iterator[None]:
    """MC_FS_SCRATCH_DIRS (None = unset), the built-in scratch roots, the
    checkouts mount (a stand-in for /workspace) and whether the container
    marker exists — for the block, then put back.

    The baseline (no arguments) is the host: no env var, no marker. So a
    test that does not ask for the scratch roots never gets the machine's
    /tmp — where tempfile makes every "outside" folder on Linux."""
    old_env = os.environ.get("MC_FS_SCRATCH_DIRS")
    saved = {name: getattr(acp_client, name, _UNSET)
             for name in ("DEFAULT_SCRATCH_DIRS", "CONTAINER_MARKER", "CHECKOUTS_DIR")}
    try:
        if env is None:
            os.environ.pop("MC_FS_SCRATCH_DIRS", None)
        else:
            os.environ["MC_FS_SCRATCH_DIRS"] = env
        if default is not _UNSET:
            acp_client.DEFAULT_SCRATCH_DIRS = default
        if checkouts is not _UNSET:
            acp_client.CHECKOUTS_DIR = checkouts
        acp_client.CONTAINER_MARKER = _MARKER_PRESENT if container else _MARKER_MISSING
        yield
    finally:
        if old_env is None:
            os.environ.pop("MC_FS_SCRATCH_DIRS", None)
        else:
            os.environ["MC_FS_SCRATCH_DIRS"] = old_env
        for name, value in saved.items():
            if value is not _UNSET:
                setattr(acp_client, name, value)
            elif hasattr(acp_client, name):
                delattr(acp_client, name)


def _jailed_client(jail: str, **scratch) -> tuple[acp_client.ACPClient, list[dict]]:
    """ACPClient wired for in-process dispatch: no subprocess — replies are
    captured instead of written to a (never-spawned) server's stdin. The jail
    is set exactly as new_session() does after a successful session/new.
    Built under _scratch_config(**scratch): as on the host unless the test
    asks for the scratch roots."""
    with _scratch_config(**scratch):
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
    ignored, and so are relative entries (`.` always exists, so only the
    relative-path rule keeps it out)."""
    configured = os.pathsep.join(["/", "."])
    assert acp_client.deliverables_roots({"MC_DELIVERABLES_DIR": configured}) == ()
    with _dirs("omp-fs-cwd-") as (cwd,):
        with _deliverables_config(env=configured):
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


def test_sibling_folder_with_a_shared_prefix_is_rejected():
    """The check is "inside the folder", not "starts with the same letters":
    `<cwd>-ref` (a real sibling mount next to /workspace in an agent
    container) and `<deliverables>2` share the root's name as a prefix but
    lie outside it. Read and write are refused there, nothing is made."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        siblings = [cwd + "-ref", deliv + "2"]
        try:
            for sib in siblings:
                os.makedirs(sib)
                with open(os.path.join(sib, "secret.txt"), "w", encoding="utf-8") as f:
                    f.write("original")
            with _deliverables_config(env=deliv):
                client, sent = _jailed_client(cwd)
            for sib in siblings:
                for method, params in (
                        ("fs/read_text_file", {"path": os.path.join(sib, "secret.txt")}),
                        ("fs/write_text_file", {"path": os.path.join(sib, "secret.txt"),
                                                "content": "hacked"}),
                        ("fs/write_text_file", {"path": os.path.join(sib, TASK_ID, "x.txt"),
                                                "content": "x"})):
                    sent.clear()
                    _fs_request(client, method, params)

                    err = _error(sent)
                    assert err is not None and err["code"] == -32002, (sib, method, sent)
                with open(os.path.join(sib, "secret.txt"), encoding="utf-8") as f:
                    assert f.read() == "original", sib
                assert not os.path.exists(os.path.join(sib, TASK_ID)), sib
        finally:
            for sib in siblings:
                shutil.rmtree(sib, ignore_errors=True)


def test_every_fs_request_gets_exactly_one_reply():
    """omp waits for the answer to each fs request; the reader loop only logs
    an exception and sends nothing, so an unanswered request hangs the tool
    call. A file that is not UTF-8 (omp's own binary check looks at the first
    bytes only), a NUL byte in the path, or params of the wrong shape each
    get one error reply — which makes omp fall back to its own disk read."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        for folder in (cwd, deliv):
            with open(os.path.join(folder, "bin.dat"), "wb") as f:
                f.write(b"ok so far \xff\xfe\x00\x80")
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)
        cases = [
            ("fs/read_text_file", {"path": os.path.join(deliv, "bin.dat")}),
            ("fs/read_text_file", {"path": os.path.join(cwd, "bin.dat")}),
            ("fs/read_text_file", {"path": os.path.join(deliv, "a\x00b.txt")}),
            ("fs/write_text_file", {"path": os.path.join(deliv, TASK_ID, "a\x00b.txt"),
                                    "content": "x"}),
            ("fs/read_text_file", ["not", "an", "object"]),
        ]
        for method, params in cases:
            sent.clear()
            client._dispatch({"jsonrpc": "2.0", "id": 7, "method": method,
                              "params": params})

            err = _error(sent)
            assert err is not None, (method, params, sent)
            assert isinstance(err["code"], int) and err["message"], err


def test_bad_write_content_gets_an_error_and_leaves_nothing_behind():
    """content that is not a string, or text that cannot be written as UTF-8
    (a lone surrogate passes json.loads), is refused before anything is
    made: no file, and no task folder left empty behind. A content that is
    not a string is named as such (-32602, invalid params), so the model
    can see what it got wrong."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd)
        for content, code in ((None, -32602), (5, -32602), (["x"], -32602),
                              ("half \ud800 text", -32000)):
            for root in (cwd, deliv):
                target = os.path.join(root, TASK_ID, "index.html")
                sent.clear()
                _fs_request(client, "fs/write_text_file",
                            {"path": target, "content": content})

                err = _error(sent)
                assert err is not None and err["code"] == code, (content, root, sent)
                assert not os.path.exists(os.path.join(root, TASK_ID)), (content, root)


def test_symlinked_deliverables_root_is_resolved():
    """The root is stored resolved: when MC_DELIVERABLES_DIR is a link to the
    real folder, a write through the link path lands in the real folder."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-", "omp-fs-links-") as (cwd, deliv, links):
        link = os.path.join(links, "deliverables")
        os.symlink(deliv, link)
        assert acp_client.deliverables_roots({"MC_DELIVERABLES_DIR": link}) == (deliv,)
        with _deliverables_config(env=link):
            client, sent = _jailed_client(cwd)

        _fs_request(client, "fs/write_text_file",
                    {"path": os.path.join(link, TASK_ID, "index.html"), "content": "ok"})

        assert _error(sent) is None, sent
        with open(os.path.join(deliv, TASK_ID, "index.html"), encoding="utf-8") as f:
            assert f.read() == "ok"


def test_missing_deliverables_root_is_not_served():
    """A configured root that does not exist (the host has no /deliverables)
    is no root: nothing is served there, and a write does not create it."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-") as (cwd, deliv):
        missing = os.path.join(deliv, "not-mounted")
        assert acp_client.deliverables_roots({"MC_DELIVERABLES_DIR": missing}) == ()
        for env, default in ((missing, "/never-used"), (None, missing)):
            with _deliverables_config(env=env, default=default):
                assert acp_client.deliverables_roots() == (), (env, default)
                client, sent = _jailed_client(cwd)

            _fs_request(client, "fs/write_text_file",
                        {"path": os.path.join(missing, TASK_ID, "x.txt"), "content": "x"})

            err = _error(sent)
            assert err is not None and err["code"] == -32002, (env, sent)
            assert not os.path.exists(missing)


def test_write_into_a_new_subfolder_of_the_cwd_makes_the_folders():
    """A write makes the missing folders on its way — inside the cwd too, not
    only in /deliverables (before, `/workspace/cube/index.html` failed with
    ENOENT when `cube/` did not exist yet)."""
    with _dirs("omp-fs-cwd-") as (cwd,):
        with _deliverables_config(env=""):
            client, sent = _jailed_client(cwd)
        target = os.path.join(cwd, "new", "deep", "x.txt")

        _fs_request(client, "fs/write_text_file", {"path": target, "content": "x"})

        assert _error(sent) is None, sent
        with open(target, encoding="utf-8") as f:
            assert f.read() == "x"


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
# The scratch roots: the container's /tmp and /workspace
# ---------------------------------------------------------------------------
# The refusals counted for #924 that were not /deliverables: /tmp (tasks in a
# worktree cwd, and chat turns in /workspace, up to 2026-10-13) and
# /workspace (up to 2026-09-18). Both belong to the container, and omp's
# bash writes there anyway. Most tests below use
# stand-in folders (`default=`) — for /tmp because on Linux every tempfile
# "outside" folder lies under the real /tmp, for /workspace because it
# exists only in a container. _container_defaults maps the REAL defaults
# onto such a stand-in layout, so a change to them shows up here.

@contextlib.contextmanager
def _real_tmp_dir() -> Iterator[str]:
    """A fresh folder under the literal /tmp (not realpath'd: on macOS /tmp
    is a link to /private/tmp, which the jail must resolve itself)."""
    made = tempfile.mkdtemp(prefix="omp-fs-scratch-", dir="/tmp")
    try:
        yield made
    finally:
        shutil.rmtree(made, ignore_errors=True)


def _container_defaults(tmp: str, ws: str) -> tuple:
    """acp_client.DEFAULT_SCRATCH_DIRS as it is, with /tmp and /workspace
    mapped onto stand-in folders (any other entry kept as written)."""
    stand_in = {"/tmp": tmp, "/workspace": ws}
    return tuple(stand_in.get(d, d) for d in acp_client.DEFAULT_SCRATCH_DIRS)


def _write(client, sent, path: str, content: str = "x") -> Optional[dict]:
    sent.clear()
    _fs_request(client, "fs/write_text_file", {"path": path, "content": content})
    return _error(sent)


def _read(client, sent, path: str) -> Optional[dict]:
    sent.clear()
    _fs_request(client, "fs/read_text_file", {"path": path})
    return _error(sent)


def test_write_and_read_under_tmp_are_served():
    """The live refusal: a task whose cwd is a worktree writes a scratch file
    to /tmp/x and gets -32002. In a container /tmp is served — the real /tmp,
    through the literal path, a new subfolder included."""
    with _dirs("omp-fs-cwd-") as (cwd,), _real_tmp_dir() as tmp:
        client, sent = _jailed_client(cwd, container=True)
        target = os.path.join(tmp, "new", "x.txt")

        assert _write(client, sent, target, "scratch") is None, sent
        assert _read(client, sent, target) is None, sent
        assert sent[0]["result"] == {"content": "scratch"}


def test_write_and_read_under_workspace_are_served():
    """A task whose cwd is a worktree writes a loose scratch file to
    /workspace/scratch-1/x.py (refused up to 2026-09-18). In a container the
    /workspace mount is served next to any cwd — read and write, a new
    subfolder included. The real defaults on a stand-in layout."""
    with _dirs("omp-fs-tmp-", "omp-fs-ws-") as (tmp, ws):
        cwd = os.path.join(ws, "worktrees", "task-a")
        os.makedirs(cwd)
        client, sent = _jailed_client(cwd, container=True,
                                      default=_container_defaults(tmp, ws))
        target = os.path.join(ws, "scratch-1", "x.py")

        assert _write(client, sent, target, "print(1)") is None, sent
        assert _read(client, sent, target) is None, sent
        assert sent[0]["result"] == {"content": "print(1)"}


def test_task_worktree_reaches_the_other_checkouts():
    """The price of the /workspace root, kept visible: a task's cwd is its
    own worktree under /workspace, and its read/edit/write tools now reach
    the main clone and the other tasks' worktrees next to it — no refusal.
    (Up to this change such a wrong-checkout edit was refused, e.g. a task
    in worktrees/pr-610 editing the main clone's repo_binding.py.) The
    operator chose this; whoever narrows /workspace again flips this test."""
    with _dirs("omp-fs-tmp-", "omp-fs-ws-") as (tmp, ws):
        cwd = os.path.join(ws, "worktrees", "task-a")
        os.makedirs(cwd)
        others = [os.path.join(ws, "mission-control", "backend", "app.py"),
                  os.path.join(ws, "worktrees", "task-b", "backend", "app.py")]
        for other in others:
            os.makedirs(os.path.dirname(other))
            with open(other, "w", encoding="utf-8") as f:
                f.write("original")
        client, sent = _jailed_client(cwd, container=True,
                                      default=_container_defaults(tmp, ws))

        for other in others:
            assert _read(client, sent, other) is None, (other, sent)
            assert _write(client, sent, other, "edited") is None, (other, sent)
            with open(other, encoding="utf-8") as f:
                assert f.read() == "edited", other


def test_symlink_from_tmp_to_etc_passwd_is_rejected():
    """/tmp/l -> /etc/passwd: the link sits in a served root, the file it
    names does not — the final path decides, the read is refused."""
    with _dirs("omp-fs-cwd-") as (cwd,), _real_tmp_dir() as tmp:
        link = os.path.join(tmp, "l")
        os.symlink("/etc/passwd", link)
        client, sent = _jailed_client(cwd, container=True)

        err = _read(client, sent, link)

        assert err is not None and err["code"] == -32002, sent
        assert "outside session cwd" in err["message"]


def test_symlink_from_tmp_into_omp_home_is_rejected():
    """/tmp/l -> /home/agent/.omp (omp's own config and keys): a folder link
    and a file link out of /tmp are refused for read and write, and the
    file behind them keeps its content."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-home-") as (cwd, tmp, home):
        omp_home = os.path.join(home, "agent", ".omp")
        os.makedirs(os.path.join(omp_home, "agent"))
        models = os.path.join(omp_home, "agent", "models.yml")
        with open(models, "w", encoding="utf-8") as f:
            f.write("original")
        os.symlink(omp_home, os.path.join(tmp, "l"))
        os.symlink(models, os.path.join(tmp, "models.yml"))
        client, sent = _jailed_client(cwd, container=True, default=(tmp,))

        for path in (os.path.join(tmp, "l", "agent", "models.yml"),
                     os.path.join(tmp, "models.yml")):
            for err in (_read(client, sent, path),
                        _write(client, sent, path, "hacked")):
                assert err is not None and err["code"] == -32002, (path, sent)
        err = _write(client, sent, os.path.join(tmp, "l", "new", "x.txt"))
        assert err is not None and err["code"] == -32002, sent
        assert not os.path.exists(os.path.join(omp_home, "new"))
        with open(models, encoding="utf-8") as f:
            assert f.read() == "original"


def test_tmp_does_not_open_the_mounts():
    """A link from /tmp into a mount that is no root — /workspace-ref (the
    operator's checkouts), /vault, the host's ~/.mc — resolves outside
    every root and is refused; a write through it makes nothing."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-ws-", "omp-fs-ref-",
               "omp-fs-vault-", "omp-fs-mc-") as (cwd, tmp, ws, ref, vault, mc_home):
        mounts = (ref, vault, mc_home)
        for mount in mounts:
            with open(os.path.join(mount, "secret.txt"), "w", encoding="utf-8") as f:
                f.write("original")
            os.symlink(mount, os.path.join(tmp, "to-" + os.path.basename(mount)))
        client, sent = _jailed_client(cwd, container=True,
                                      default=_container_defaults(tmp, ws))

        for mount in mounts:
            via_link = os.path.join(tmp, "to-" + os.path.basename(mount))
            for path in (os.path.join(via_link, "secret.txt"),
                         os.path.join(mount, "secret.txt")):
                for err in (_read(client, sent, path),
                            _write(client, sent, path, "hacked")):
                    assert err is not None and err["code"] == -32002, (path, sent)
            assert _write(client, sent, os.path.join(via_link, "new", "x.txt")) \
                is not None
            assert not os.path.exists(os.path.join(mount, "new")), mount
            with open(os.path.join(mount, "secret.txt"), encoding="utf-8") as f:
                assert f.read() == "original", mount


def test_symlink_from_workspace_into_omp_home_is_rejected():
    """/workspace/l -> /home/agent/.omp (omp's own config and keys, not on a
    mount): a folder link and a file link out of /workspace are refused for
    read and write, and the file behind them keeps its content."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-ws-", "omp-fs-home-") as (
            cwd, tmp, ws, home):
        omp_home = os.path.join(home, "agent", ".omp")
        os.makedirs(os.path.join(omp_home, "agent"))
        models = os.path.join(omp_home, "agent", "models.yml")
        with open(models, "w", encoding="utf-8") as f:
            f.write("original")
        os.symlink(omp_home, os.path.join(ws, "l"))
        os.symlink(models, os.path.join(ws, "models.yml"))
        client, sent = _jailed_client(cwd, container=True,
                                      default=_container_defaults(tmp, ws))

        for path in (os.path.join(ws, "l", "agent", "models.yml"),
                     os.path.join(ws, "models.yml")):
            for err in (_read(client, sent, path),
                        _write(client, sent, path, "hacked")):
                assert err is not None and err["code"] == -32002, (path, sent)
        err = _write(client, sent, os.path.join(ws, "l", "new", "x.txt"))
        assert err is not None and err["code"] == -32002, sent
        assert not os.path.exists(os.path.join(omp_home, "new"))
        with open(models, encoding="utf-8") as f:
            assert f.read() == "original"


def test_workspace_does_not_open_the_other_mounts():
    """/workspace is a mount next to others — /workspace-ref (the operator's
    checkouts), /vault, the host's ~/.mc. A link from /workspace into any of
    them resolves outside /workspace and is refused; a write through it
    makes nothing. A folder that only shares the name as a prefix
    (`/workspace-ref` next to `/workspace`) is outside too."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-ws-", "omp-fs-vault-",
               "omp-fs-mc-") as (cwd, tmp, ws, vault, mc_home):
        ref = ws + "-ref"
        try:
            for mount in (ref, vault, mc_home):
                os.makedirs(mount, exist_ok=True)
                with open(os.path.join(mount, "secret.txt"), "w", encoding="utf-8") as f:
                    f.write("original")
                os.symlink(mount, os.path.join(ws, "to-" + os.path.basename(mount)))
            client, sent = _jailed_client(cwd, container=True,
                                          default=_container_defaults(tmp, ws))

            for mount in (ref, vault, mc_home):
                via_link = os.path.join(ws, "to-" + os.path.basename(mount))
                for path in (os.path.join(via_link, "secret.txt"),
                             os.path.join(mount, "secret.txt")):
                    for err in (_read(client, sent, path),
                                _write(client, sent, path, "hacked")):
                        assert err is not None and err["code"] == -32002, (path, sent)
                assert _write(client, sent, os.path.join(via_link, "new", "x.txt")) \
                    is not None
                assert not os.path.exists(os.path.join(mount, "new")), mount
                with open(os.path.join(mount, "secret.txt"), encoding="utf-8") as f:
                    assert f.read() == "original", mount
        finally:
            shutil.rmtree(ref, ignore_errors=True)


def test_hard_link_in_tmp_is_refused():
    """/tmp lies on the container's own filesystem, next to ~/.omp (only
    /workspace and /deliverables are separate mounts). A hard link made in
    /tmp to omp's env file passes the realpath check — the link IS a path
    in /tmp — so a file served only through a scratch root must have one
    link: read and write through the hard link are refused, and the file
    keeps its content. Inside the cwd a file with two links is served as
    before (a package store may hard-link there)."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-home-") as (cwd, tmp, home):
        env_file = os.path.join(home, "omp.env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("KEY=original")
        link = os.path.join(tmp, "h")
        os.link(env_file, link)
        client, sent = _jailed_client(cwd, container=True, default=(tmp,))

        for err in (_read(client, sent, link),
                    _write(client, sent, link, "POISONED")):
            assert err is not None and err["code"] == -32002, sent
            assert "link" in err["message"], err
        with open(env_file, encoding="utf-8") as f:
            assert f.read() == "KEY=original"

        store = os.path.join(cwd, "store", "pkg.js")
        os.makedirs(os.path.dirname(store))
        with open(store, "w", encoding="utf-8") as f:
            f.write("module")
        os.makedirs(os.path.join(cwd, "node_modules"))
        os.link(store, os.path.join(cwd, "node_modules", "pkg.js"))
        assert _read(client, sent, os.path.join(cwd, "node_modules", "pkg.js")) is None
        assert sent[0]["result"] == {"content": "module"}


def _unblock_fifo(fifo: str) -> None:
    """Open the other end of a FIFO once, so a test thread stuck in open()
    gets free (the suite must not hang when the fix is missing)."""
    for flags in (os.O_WRONLY | os.O_NONBLOCK, os.O_RDONLY | os.O_NONBLOCK):
        try:
            os.close(os.open(fifo, flags))
        except OSError:
            pass


def test_fifo_is_refused_without_blocking():
    """omp's fs request is answered on the ACP reader thread. Opening a FIFO
    waits for its other end, so a read or write of one would hang the
    session with no reply. Anything that is not a regular file gets an
    error reply at once — in /tmp and in the cwd alike."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-") as (cwd, tmp):
        client, sent = _jailed_client(cwd, container=True, default=(tmp,))
        for root in (cwd, tmp):
            fifo = os.path.join(root, "f")
            os.mkfifo(fifo)
            for method, params in (("fs/read_text_file", {"path": fifo}),
                                   ("fs/write_text_file", {"path": fifo, "content": "x"})):
                sent.clear()
                worker = threading.Thread(target=_fs_request,
                                          args=(client, method, params), daemon=True)
                worker.start()
                worker.join(timeout=3)
                hung = worker.is_alive()
                while worker.is_alive():
                    _unblock_fifo(fifo)
                    worker.join(timeout=0.2)
                assert not hung, (root, method)
                err = _error(sent)
                assert err is not None and err["code"] == -32002, (root, method, sent)
                assert "not a regular file" in err["message"], err


def test_dotdot_out_of_tmp_is_rejected():
    """`..` out of /tmp is refused — on the real /tmp towards /etc/passwd,
    and on a write before any folder along the escaping path is made."""
    with _dirs("omp-fs-cwd-") as (cwd,), _real_tmp_dir() as real_tmp:
        client, sent = _jailed_client(cwd, container=True)
        escape = os.path.join(real_tmp, "..", "..", "etc", "passwd")

        err = _read(client, sent, escape)

        assert err is not None and err["code"] == -32002, sent
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-outside-") as (cwd, tmp, outside):
        client, sent = _jailed_client(cwd, container=True, default=(tmp,))
        escape = os.path.join(tmp, "a", "..", "..", os.path.basename(outside),
                              "made", "x.txt")

        err = _write(client, sent, escape)

        assert err is not None and err["code"] == -32002, sent
        assert not os.path.exists(os.path.join(outside, "made"))
        assert not os.path.exists(os.path.join(tmp, "a"))


def test_home_stays_rejected_next_to_the_scratch_root():
    """/tmp open, /home shut: with the cwd at /workspace (the chat path),
    /home/agent/z is refused for read and write and nothing is made — the
    literal path with the real roots, and a stand-in home folder with a
    stand-in /tmp."""
    with _dirs("omp-fs-ws-") as (ws,):
        client, sent = _jailed_client(ws, container=True)
        for err in (_read(client, sent, "/home/agent/z"),
                    _write(client, sent, "/home/agent/z")):
            assert err is not None and err["code"] == -32002, sent
    with _dirs("omp-fs-tmp-", "omp-fs-ws-", "omp-fs-home-") as (tmp, ws, home):
        target = os.path.join(home, "agent", "z")
        client, sent = _jailed_client(ws, container=True, default=(tmp,))

        err = _write(client, sent, target)

        assert err is not None and err["code"] == -32002, sent
        assert ws in err["message"] and tmp in err["message"]
        assert not os.path.exists(os.path.join(home, "agent"))


def test_scratch_roots_only_inside_a_container():
    """The host (Hermes builds ACPClient there too) keeps its jail: without
    the container marker and without MC_FS_SCRATCH_DIRS no scratch root is
    served — the host's /tmp is the whole machine's. In a container only
    the defaults that exist are roots, stored resolved."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-tmp2-") as (cwd, tmp, tmp2):
        missing = os.path.join(tmp2, "not-mounted")
        with _scratch_config(default=(tmp, tmp2)):
            assert acp_client.scratch_roots() == ()
        with _scratch_config(default=(tmp, missing), container=True):
            assert acp_client.scratch_roots() == (tmp,)
        client, sent = _jailed_client(cwd, default=(tmp, tmp2))

        for root in (tmp, tmp2):
            err = _write(client, sent, os.path.join(root, "x.txt"))
            assert err is not None and err["code"] == -32002, (root, sent)
            assert not os.path.exists(os.path.join(root, "x.txt"))


def test_scratch_env_overrides_the_defaults():
    """MC_FS_SCRATCH_DIRS wins over the container default: set but empty =
    no scratch root even in a container; a list = exactly those folders,
    also on the host. `/` and relative entries are never a root."""
    with _dirs("omp-fs-cwd-", "omp-fs-tmp-", "omp-fs-own-") as (cwd, tmp, own):
        with _scratch_config(env="", default=(tmp,), container=True):
            assert acp_client.scratch_roots() == ()
        configured = os.pathsep.join([own, "/", "."])
        with _scratch_config(env=configured, default=(tmp,)):
            assert acp_client.scratch_roots() == (own,)
        client, sent = _jailed_client(cwd, env="", default=(tmp,), container=True)

        err = _write(client, sent, os.path.join(tmp, "x.txt"))

        assert err is not None and err["code"] == -32002, sent
        assert not os.path.exists(os.path.join(tmp, "x.txt"))


def test_deliverables_root_still_served_next_to_the_scratch_root():
    """The #924 root keeps working with the scratch root on, and the
    refusal names every folder the model may use instead."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-", "omp-fs-tmp-",
               "omp-fs-outside-") as (cwd, deliv, tmp, outside):
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd, container=True, default=(tmp,))

        assert _write(client, sent, os.path.join(deliv, TASK_ID, "index.html")) is None
        err = _write(client, sent, os.path.join(outside, "x.txt"))

        assert err is not None and err["code"] == -32002, sent
        for root in (cwd, deliv, tmp):
            assert root in err["message"], (root, err)


def test_refusal_says_where_results_go_and_that_tmp_is_scratch():
    """A model refused at, say, ~/report.html picks a folder the message
    offers. Results belong in /deliverables/<task_id>/ (MC collects them
    there); /tmp is scratch only — MC never sees it and a recreate wipes
    it. The message says so, and keeps its first words ("path ... outside
    session cwd ...") that transcript scans match on."""
    with _dirs("omp-fs-cwd-", "omp-fs-deliv-", "omp-fs-tmp-", "omp-fs-home-") as (
            cwd, deliv, tmp, home):
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd, container=True, default=(tmp,))
        target = os.path.join(home, "report.html")

        err = _write(client, sent, target)

        assert err is not None and err["code"] == -32002, sent
        message = err["message"]
        assert message.startswith(f"path {target!r} outside session cwd {cwd!r}"), message
        assert f"results go to {os.path.join(deliv, '<task_id>') + os.sep!r}" in message
        scratch = message.index("scratch only")
        assert scratch > message.index("results go to")
        assert message.index(repr(tmp)) > scratch
        assert "wiped" in message[scratch:]


def test_refusal_never_offers_the_checkouts_as_scratch():
    """/workspace is served (operator's decision), but it is no scratch
    folder: it is the persistent checkouts mount (~/.mc/workspaces/<slug>
    on the host) with the main clone and every task's worktree in it. A
    refusal that listed it under "scratch only (MC never collects it, and
    /tmp is wiped ...)" would steer a refused model to drop files into the
    checkouts, or into another task's worktree. So a refusal names /tmp as
    scratch and does not name /workspace at all — with the real defaults on
    a stand-in layout, and with only /workspace and a folder inside it as
    scratch roots (MC_FS_SCRATCH_DIRS), where the scratch clause is left
    out."""
    with _dirs("omp-fs-deliv-", "omp-fs-tmp-", "omp-fs-ws-", "omp-fs-home-") as (
            deliv, tmp, ws, home):
        cwd = os.path.join(ws, "worktrees", "task-a")
        os.makedirs(cwd)
        nested = os.path.join(ws, "scratch")
        os.makedirs(nested)
        with _deliverables_config(env=deliv):
            client, sent = _jailed_client(cwd, container=True, checkouts=ws,
                                          default=_container_defaults(tmp, ws))
            only_ws, sent_ws = _jailed_client(cwd, container=True, checkouts=ws,
                                              env=ws + os.pathsep + nested)
        target = os.path.join(home, "report.html")
        results = f"results go to {os.path.join(deliv, '<task_id>') + os.sep!r}"

        err = _write(client, sent, target)

        assert err is not None and err["code"] == -32002, sent
        message = err["message"]
        assert message.startswith(f"path {target!r} outside session cwd {cwd!r}"), message
        assert results in message, message
        scratch = message.index("scratch only")
        assert message[scratch:].endswith(f": {tmp!r}"), message
        assert repr(ws) not in message, message

        err = _write(only_ws, sent_ws, target)

        assert err is not None and err["code"] == -32002, sent_ws
        message = err["message"]
        assert results in message, message
        assert "scratch only" not in message, message
        assert repr(ws) not in message and repr(nested) not in message, message
        # Served all the same: the hint leaves /workspace out, the jail does not.
        assert _write(client, sent, os.path.join(ws, "main", "x.py")) is None, sent
        assert _write(only_ws, sent_ws, os.path.join(ws, "main", "y.py")) is None, sent_ws


def test_scratch_defaults_match_the_container_layout():
    """The defaults are not a second truth: /workspace is the mount target
    the compose renderer gives every agent container and the cwd the omp
    entrypoint pins — and the checkouts folder a refusal never offers as
    scratch. A rename on either side trips here."""
    assert acp_client.DEFAULT_SCRATCH_DIRS == ("/tmp", "/workspace")
    assert getattr(acp_client, "CHECKOUTS_DIR", None) == "/workspace"
    assert acp_client.CONTAINER_MARKER == "/.dockerenv"
    with open(os.path.join(REPO_ROOT, "backend", "app", "services",
                           "compose_renderer.py"), encoding="utf-8") as f:
        renderer = f.read()
    with open(os.path.join(HERE, os.pardir, "entrypoint.sh"), encoding="utf-8") as f:
        entrypoint = f.read()
    assert '.mc/workspaces/{slug}:/workspace",' in renderer
    assert 'OMP_DEFAULT_CWD="${OMP_DEFAULT_CWD:-/workspace}"' in entrypoint


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
