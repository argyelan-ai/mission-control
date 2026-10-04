"""Read-only transcript access for a head run (docs/decisions/085 Nachtrag
2026-10-04 §4; docs/specs/head-launcher.md §7 ``GET /heads/{run_id}/chat/history``).

This module ONLY reads a file that is already on disk in the run's own
folder. It adds no delivery into a head's turn, no tmux probe, no SSE
tailer — see the ADR Nachtrag for the exact boundary. It reuses the
existing, already-proven chat machinery (``transcript_adapters``,
``transcript_chat.read_history``) the same way the agent chat endpoints do;
the only new piece is "which file, for a HEAD run, with this harness" —
``locate()`` below — plus the one extra masking pass every head-sourced
string needs (``redact.head_env_values``, never applicable to an agent's own
chat because an agent has no per-run secret file).

``anhang.md`` section B names the one trap this module exists to avoid:
``transcript_adapters.adapter_for`` is duck-typed on an object's ``.harness``
ATTRIBUTE and falls back to the Claude adapter for anything it does not
recognise — exactly right for an agent, silently wrong for a head, where
``spec["harness"]`` is a plain string with no such attribute. This module
therefore only ever calls ``adapter_for_harness`` (strict, string-keyed,
``None`` instead of a wrong guess), never ``adapter_for``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.heads import redact
from app.services.heads.state import ACTIVE_STATES, derive_for_run
from app.services.transcript_adapters import TranscriptAdapter, adapter_for_harness
from app.services.transcript_chat import read_history

#: A transcript past this size is not read — the head is almost certainly
#: stuck in an output loop rather than holding a conversation. Matches the
#: bauplan's "> 64 MB" rule.
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024

#: Reasons ``Unavailable`` can carry — exactly the set the router/frontend
#: contract (docs/specs/head-launcher.md §7) and the bauplan's test list
#: name; nothing here invents a fifth value.
REASON_NO_READER = "no_reader"
REASON_NOT_YET = "not_yet"
REASON_NO_TRANSCRIPT = "no_transcript"
REASON_TOO_LARGE = "too_large"


@dataclass(frozen=True)
class Located:
    path: Path
    adapter: TranscriptAdapter
    reader: str


@dataclass(frozen=True)
class Unavailable:
    reason: str


def _is_safe_descendant(run_folder: Path, path: Path) -> bool:
    """``path`` is a regular file strictly under ``run_folder``, with NO
    symlink on any path component between them (including the file itself),
    and its resolved form still lies inside the resolved run folder.

    Same defence as ``routers/agent_chat.get_subagent_history`` (its
    docstring explains why a symlink on an INTERMEDIATE directory, not just
    the final file, must be checked on the unresolved path before
    ``resolve()`` ever runs) — a head's sandbox is writable by the head
    itself, so ``omp-sessions`` or a ``claude-config/projects/<dir>`` entry
    could in principle be replaced with a symlink pointing anywhere the
    backend container can read.
    """
    try:
        rel = path.relative_to(run_folder)
    except ValueError:
        return False
    if not rel.parts:
        return False
    cur = run_folder
    info = None
    for part in rel.parts:
        cur = cur / part
        try:
            info = cur.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
    if info is None or not stat.S_ISREG(info.st_mode):
        return False
    try:
        root_r = run_folder.resolve()
        path_r = path.resolve()
    except OSError:
        return False
    return root_r == path_r or root_r in path_r.parents


def _candidates(run_folder: Path, glob_pattern: str) -> list[Path]:
    try:
        found = list(run_folder.glob(glob_pattern))
    except OSError:
        return []
    return [p for p in found if _is_safe_descendant(run_folder, p)]


def locate(run: Any) -> Located | Unavailable:
    """Which file (if any) holds this run's main transcript, and which
    adapter reads it. Never raises; every failure becomes a typed reason."""
    harness = run.spec.get("harness") if isinstance(run.spec, dict) else None
    adapter = adapter_for_harness(harness)
    if adapter is None or not adapter.head_transcript_glob:
        return Unavailable(reason=REASON_NO_READER)

    candidates = _candidates(run.folder, adapter.head_transcript_glob)
    if not candidates:
        state = derive_for_run(run, time.time())["state"]
        reason = REASON_NOT_YET if state in ACTIVE_STATES else REASON_NO_TRANSCRIPT
        return Unavailable(reason=reason)

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    newest = max(candidates, key=mtime)
    try:
        size = newest.stat().st_size
    except OSError:
        return Unavailable(reason=REASON_NO_TRANSCRIPT)
    if size > MAX_TRANSCRIPT_BYTES:
        return Unavailable(reason=REASON_TOO_LARGE)
    # ``adapter.name``, not the raw ``harness`` string: the adapter is the
    # one source of truth for "which reader actually parsed this" (review
    # finding on PR #751) — today a head's ``spec["harness"]`` is validated
    # to exactly "omp"/"claude" by ``mc-head``'s ``load_spec``, so the two
    # already agree, but ``reader`` is a public API field (docs/specs/
    # head-launcher.md §7: "claude"|"omp"|null) and must come from the thing
    # that was actually used to read the file, not from an echo of the input.
    return Located(path=newest, adapter=adapter, reader=adapter.name)


def etag(located: Located | Unavailable, limit: int, before_uuid: str | None, state: str | None = None) -> str | None:
    """A strong ETag over the file identity Claude/omp cannot fake by
    touching mtime alone (``st_ino`` survives an editor's atomic-replace
    only on the SAME filesystem, which every head transcript is — it is
    written in place by the harness, never replaced), plus the page
    parameters (a different ``limit``/``before_uuid`` is a different page
    and must not collide on the cache key) and the derived head ``state``.

    ``state`` matters because the response body carries more than the file's
    bytes: ``session.aliveness`` is derived from ``.wrapper/status.json``,
    not from the transcript file. Without it in the key, the NORMAL end of a
    head (the transcript's last write lands before the wrapper writes
    ``exited``) produces no new ETag: a client polling with
    ``If-None-Match`` gets 304 forever and never learns the run ended —
    review finding on PR #751. The caller computes the state once
    (``derive_for_run``) and passes the SAME value to both this function and
    ``read()`` so the two can never disagree about which state the response
    reflects. ``None`` for an unavailable transcript — there is nothing
    stable to key on, and the body is tiny anyway."""
    if isinstance(located, Unavailable):
        return None
    try:
        st = located.path.stat()
    except OSError:
        return None
    raw = f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{limit}:{before_uuid or ''}:{state or ''}"
    return '"' + hashlib.sha256(raw.encode()).hexdigest()[:32] + '"'


def _aliveness(run: Any, state: str | None = None) -> str:
    """Head aliveness comes ONLY from the derived head state (files MC
    already trusts for every other surface) — never from a pane probe or a
    process check. ``starting``/``running`` is the one meaning of "this
    could still grow"; everything else (needs_you, passed, failed, stopped)
    is ended, even if the transcript's own mtime looks recent. ``state``, if
    given, is the CALLER's already-computed ``derive_for_run(...)["state"]``
    (same value used for the ETag above) — reused rather than derived again
    with a fresh ``now``, so the two can never read a different moment."""
    if state is None:
        state = derive_for_run(run, time.time())["state"]
    return "active" if state in ACTIVE_STATES else "ended"


def _empty(run: Any, reason: str, state: str | None = None) -> dict[str, Any]:
    return {
        "events": [],
        "session": {"sessionId": run.run_id, "live": False, "startedAt": None, "aliveness": _aliveness(run, state)},
        "hasMore": False,
        "subagentRuns": [],
        "source": "none",
        "reader": None,
        "reason": reason,
    }


def _open_verified(path: Path) -> Any | None:
    """Open ``path`` for reading, re-proving right NOW the exact safety
    ``locate()``'s ``_is_safe_descendant`` already checked — a head's
    sandbox can swap the final component for a symlink in the window
    between that check and this call (the ETag computation plus an
    ``asyncio.to_thread`` hop both sit in between). ``O_NOFOLLOW`` makes a
    swap-to-symlink fail outright; the ``st_ino``/``st_dev`` comparison
    against a fresh ``lstat`` additionally catches a swap to a DIFFERENT
    regular file (e.g. a hard link planted next to the original) that an
    ``O_NOFOLLOW``-only open would not notice. Returns a text-mode file
    object positioned at the start, or ``None`` when anything about that
    does not hold — the caller treats that exactly like "transcript
    vanished", never raises and never falls back to a plain ``open()``."""
    try:
        pre = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(pre.st_mode):
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        post = os.fstat(fd)
        if not stat.S_ISREG(post.st_mode) or (post.st_ino, post.st_dev) != (pre.st_ino, pre.st_dev):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return os.fdopen(fd, "r", encoding="utf-8", errors="replace")


def read(
    run: Any,
    located: Located | Unavailable,
    limit: int = 400,
    before_uuid: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """One page of this run's transcript, in the exact shape an agent's
    chat history has (``events``/``session``/``hasMore``/``subagentRuns``),
    plus ``source``/``reader``/``reason`` for a frontend/empty state. Always
    masked before it is returned — see module docstring. ``state`` is the
    caller's ``derive_for_run(...)["state"]`` (see ``etag()``'s docstring);
    omitting it just derives it again here with a fresh ``now``."""
    if isinstance(located, Unavailable):
        return _empty(run, located.reason, state)

    # Read exactly the one file: no usage-context side effects on other
    # runs (``stamp_usage`` is the token harvester's job, not this
    # viewer's), and no subagent directory scan (a head's conversation has
    # no subagent concept MC shows — ``_no_subagent_runs`` is each
    # adapter's own default already, this just makes it explicit and
    # removes any chance of touching another path on disk for this call).
    # Same pattern as ``routers.agent_chat.get_subagent_history``.
    quiet = dataclasses.replace(located.adapter, stamp_usage=lambda ev, p: None, subagent_runs=lambda p: [])

    fileobj = _open_verified(located.path)
    if fileobj is None:
        return _empty(run, REASON_NO_TRANSCRIPT, state)
    result = read_history(located.path, quiet, limit=limit, before_uuid=before_uuid, fileobj=fileobj)
    result["session"]["aliveness"] = _aliveness(run, state)
    result["source"] = "transcript"
    result["reader"] = located.reader
    result["reason"] = None
    return redact.mask_tree(result, redact.head_env_values(run))
