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
import re
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
    #: (st_dev, st_ino) of ``path``, captured from the SAME lstat chain that
    #: validated it as a safe descendant — not a fresh lstat taken later.
    #: ``_open_verified`` checks the opened fd's identity against THIS
    #: pinned pair (review finding on PR #751 round 2: a fresh lstat taken
    #: right before the open is itself taken AFTER a possible swap of an
    #: INTERMEDIATE directory, so it just reports whatever file sits there
    #: now and the check passes against the wrong file).
    identity: tuple[int, int]


@dataclass(frozen=True)
class Unavailable:
    reason: str


def _safe_descendant_lstat(run_folder: Path, path: Path) -> os.stat_result | None:
    """``path`` is a regular file strictly under ``run_folder``, with NO
    symlink on any path component between them (including the file itself),
    and its resolved form still lies inside the resolved run folder. Returns
    the FINAL component's ``lstat()`` result (the caller pins its
    ``st_dev``/``st_ino`` as that file's identity) or ``None`` when any of
    that does not hold.

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
        return None
    if not rel.parts:
        return None
    cur = run_folder
    info = None
    for part in rel.parts:
        cur = cur / part
        try:
            info = cur.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
    if info is None or not stat.S_ISREG(info.st_mode):
        return None
    try:
        root_r = run_folder.resolve()
        path_r = path.resolve()
    except OSError:
        return None
    if root_r == path_r or root_r in path_r.parents:
        return info
    return None


def _is_safe_descendant(run_folder: Path, path: Path) -> bool:
    return _safe_descendant_lstat(run_folder, path) is not None


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
    # Re-validated right here (candidates were already checked once inside
    # _candidates(), via the same function) so the identity pinned into
    # Located is taken at THIS moment, as close as possible to the actual
    # read — not reused from an earlier call further back in time.
    identity_lstat = _safe_descendant_lstat(run.folder, newest)
    if identity_lstat is None:
        return Unavailable(reason=REASON_NO_TRANSCRIPT)
    # ``adapter.name``, not the raw ``harness`` string: the adapter is the
    # one source of truth for "which reader actually parsed this" (review
    # finding on PR #751) — today a head's ``spec["harness"]`` is validated
    # to exactly "omp"/"claude" by ``mc-head``'s ``load_spec``, so the two
    # already agree, but ``reader`` is a public API field (docs/specs/
    # head-launcher.md §7: "claude"|"omp"|null) and must come from the thing
    # that was actually used to read the file, not from an echo of the input.
    return Located(path=newest, adapter=adapter, reader=adapter.name, identity=(identity_lstat.st_dev, identity_lstat.st_ino))


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
    aliveness = _aliveness(run, state)
    return {
        "events": [],
        # ``live`` mirrors ``aliveness`` — head state, never a pane/process
        # probe or (as read_history's own default would do) a transcript
        # mtime, which does not exist here anyway (see read()'s comment).
        "session": {"sessionId": run.run_id, "live": aliveness == "active", "startedAt": None, "aliveness": aliveness},
        "hasMore": False,
        "subagentRuns": [],
        "source": "none",
        "reader": None,
        "reason": reason,
    }


def _open_verified(path: Path, identity: tuple[int, int]) -> Any | None:
    """Open ``path`` for reading, re-proving right NOW the exact safety
    ``locate()``'s ``_safe_descendant_lstat`` already checked — a head's
    sandbox can swap a path component for a symlink in the window between
    that check and this call (the ETag computation plus an
    ``asyncio.to_thread`` hop both sit in between). ``O_NOFOLLOW`` makes a
    swap of the FINAL component to a symlink fail outright.

    That alone is not enough: ``O_NOFOLLOW`` only ever looks at the final
    component, so a swap of an INTERMEDIATE directory (``omp-sessions``,
    a ``claude-config/projects/<dir>``) to a symlink still lets the open
    succeed, through the swapped parent, against an attacker-planted file
    of the same name (review finding on PR #751 round 2 — a fresh
    ``lstat()`` taken here, immediately before the open, does not catch
    this: that lstat runs AFTER the swap too, so it simply reports
    whatever file now sits there and agrees with itself). The defence is
    therefore identity pinned at ``locate()`` time, BEFORE any swap this
    call's caller is worried about — ``identity`` is that ``(st_dev,
    st_ino)`` pair from ``Located``, and the opened fd's own ``fstat`` is
    checked against it, never against a lstat taken now.

    Returns a text-mode file object positioned at the start, or ``None``
    when anything about that does not hold — the caller treats that
    exactly like "transcript vanished", never raises and never falls back
    to a plain ``open()``."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        post = os.fstat(fd)
        if not stat.S_ISREG(post.st_mode) or (post.st_dev, post.st_ino) != identity:
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

    fileobj = _open_verified(located.path, located.identity)
    if fileobj is None:
        return _empty(run, REASON_NO_TRANSCRIPT, state)
    result = read_history(located.path, quiet, limit=limit, before_uuid=before_uuid, fileobj=fileobj)
    aliveness = _aliveness(run, state)
    result["session"]["aliveness"] = aliveness
    # Override read_history's own ``live`` (an mtime-recency heuristic that
    # is right for an AGENT's chat, which has no other liveness signal) —
    # a head's contract is "never from pane/process, only head state"
    # (review finding on PR #751: a head that ended less than the live
    # window ago returned ``live: true`` together with ``aliveness:
    # "ended"``, contradicting that contract and able to mislead the PR 2
    # UI into treating an ended head as still live).
    result["session"]["live"] = aliveness == "active"
    result["source"] = "transcript"
    result["reader"] = located.reader
    result["reason"] = None
    return redact.mask_tree(result, redact.head_env_values(run))


# ── Transcript summary for "Continue" (bauplan `heads-sichtbar` PR 4 §5) ────
#
# A `mode=continue` restart's `job.md` gets a deterministic, model-free
# recap of what the PREVIOUS run did — never the old conversation itself
# (native `--resume` is out of scope, bauplan §7; see `launcher.render_job`).
# `summarize()` is pure (events in, text out) and harness-neutral: it reads
# only the `ChatEvent` shapes `transcript_chat.read_history` already
# produces for EITHER harness, never a harness-specific field name on its
# own. `summary_for_restart()` is the one impure wrapper that ties it to a
# run on disk; it never raises — any failure to locate or read a
# transcript just means an empty summary, exactly like a head whose
# harness has no reader at all (`reason: "no_reader"`).

#: Harness-neutral file-edit tool names — Claude Code capitalises them
#: ("Edit", "Write"), omp does not ("edit", "write"); compared lower-cased.
_FILE_TOOL_NAMES = frozenset({"edit", "write", "multiedit", "notebookedit"})

#: Claude's tool `detail` carries the path directly; omp's `write` carries
#: `path`, its `edit` carries neither — see `_omp_edit_path`.
_FILE_DETAIL_KEYS = ("file_path", "path")

#: omp's `edit` tool detail has no path field at all — the path is the
#: first line of its own `input` diff text, `[<path>#<hash>]` (live-checked
#: against a real run, anhang.md section B's proto). Anchored at the start
#: so a path that happens to contain `#` or `]` later in a long diff body
#: is never mistaken for the bracket's own close.
_OMP_EDIT_PATH = re.compile(r"^\[([^\]#]+)")

MAX_SUMMARY_BYTES = 4096
MAX_SUMMARY_MESSAGES = 3
MAX_SUMMARY_MESSAGE_CHARS = 600
MAX_SUMMARY_FILES_LISTED = 12


def _tool_changed_file(ev: dict[str, Any]) -> str | None:
    name = ev.get("name")
    if not isinstance(name, str) or name.lower() not in _FILE_TOOL_NAMES:
        return None
    detail = ev.get("detail")
    if not isinstance(detail, dict):
        return None
    for key in _FILE_DETAIL_KEYS:
        value = detail.get(key)
        if isinstance(value, str) and value:
            return value
    raw = detail.get("input")
    if isinstance(raw, str):
        m = _OMP_EDIT_PATH.match(raw.strip())
        if m:
            return m.group(1)
    return None


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _quote_note(text: str) -> str:
    """Markdown-blockquote every line of ``text``, escaping a leading
    ``#`` so a note's own heading-shaped line (``### Open question``,
    ``## Previous run``) can never be mistaken for one of job.md's OWN
    section headings once pasted into the "previous run" block (round 4
    review finding). A real assistant note is multi-line markdown — the
    old code only put the bullet marker on the FIRST line (an f-string
    with an embedded ``\\n`` does not re-prefix later lines), so a later
    line that happened to read ``### Open question`` would land at column
    0 and look like a real section boundary to the next head reading
    job.md as its prompt. Blockquoting every line keeps the whole note
    visually and structurally "quoted text" no matter how many lines it
    has; escaping ``#`` defeats it even inside the blockquote, for a
    renderer or a naive ``^#+`` scan alike."""
    out: list[str] = []
    for line in text.splitlines() or [""]:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            indent = line[: len(line) - len(stripped)]
            line = f"{indent}\\{stripped}"
        out.append(f"> {line}" if line else ">")
    return "\n".join(out)


def summarize(events: list[dict[str, Any]], *, last_step: str | None = None, env_values: tuple[str, ...] = ()) -> str:
    """A short, deterministic recap of ``events`` (the same ``ChatEvent``
    list ``read()``/the chat history endpoint return) — NO model call, ever:
    a tool-use count, the basenames of files an ``Edit``/``Write`` tool
    touched (first-touch order, deduplicated), the last reported step, and
    the last few assistant messages verbatim (clipped). ``""`` when there is
    nothing to say (a fresh run, or a harness with no reader) — the caller
    (``launcher.render_job``) treats that exactly like no previous run at
    all, never as an error.

    Always masked (``redact.mask_text`` with the caller's ``env_values``)
    and capped at ``MAX_SUMMARY_BYTES`` regardless of what the caller
    passes in — this function's own contract, proven by a sabotage test
    that plants a secret directly in ``events`` and calls it with no
    upstream masking at all, not just a property of ``read()``'s pipeline."""
    tool_count = 0
    seen_files: set[str] = set()
    files: list[str] = []
    assistant_texts: list[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        if kind == "tool":
            tool_count += 1
            path = _tool_changed_file(ev)
            if path:
                # Masked before it is even kept (round 4 review finding: a
                # tool's own path is caller-controlled text exactly like an
                # assistant message, so the same "mask before you cut/keep
                # it" rule applies here too).
                base = redact.mask_text(_basename(path), env_values)
                if base not in seen_files:
                    seen_files.add(base)
                    files.append(base)
        elif kind == "message" and ev.get("role") == "assistant":
            text = (ev.get("text") or "").strip()
            if text:
                # Masked on the FULL text, BEFORE the per-message clip
                # below ever shortens it (round 4 review finding: the old
                # order — clip to 600 chars first, mask the assembled
                # result after — let a secret straddling that cut leave a
                # partial, no-longer-matching prefix behind; live probe: a
                # 32-char env value at offset 581 leaked its first 18
                # chars). Masking while the value is still whole means the
                # match is made, and replaced, before any clip can split it.
                assistant_texts.append(redact.mask_text(text, env_values))

    last_step = redact.mask_text((last_step or "").strip(), env_values) or None
    if not tool_count and not files and not assistant_texts and not last_step:
        return ""

    lines: list[str] = []
    if last_step:
        lines.append(f"- Last step: {last_step}")
    if tool_count:
        lines.append(f"- Tools used: {tool_count}")
    if files:
        shown = files[:MAX_SUMMARY_FILES_LISTED]
        more = len(files) - len(shown)
        listed = ", ".join(shown) + (f", +{more} more" if more > 0 else "")
        lines.append(f"- Files touched: {listed}")

    recent = assistant_texts[-MAX_SUMMARY_MESSAGES:]
    if recent:
        if lines:
            lines.append("")
        lines.append("Last assistant notes:")
        for text in recent:
            clipped = text if len(text) <= MAX_SUMMARY_MESSAGE_CHARS else text[: MAX_SUMMARY_MESSAGE_CHARS - 1] + "…"
            # Blockquoted, every line — see `_quote_note`'s own docstring
            # (round 4 review finding: a real note is multi-line markdown
            # and can contain a line shaped exactly like one of job.md's
            # OWN section headings).
            lines.append(_quote_note(clipped))

    # A second, whole-text pass: idempotent on what the per-field masking
    # above already redacted, and still this function's own standing
    # contract regardless of what the caller passes in (unchanged from
    # before this fix) — e.g. a secret that somehow reached `last_step`'s
    # non-string neighbours or a future field this function grows.
    text = redact.mask_text("\n".join(lines).strip() + "\n", env_values)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        text = encoded[:MAX_SUMMARY_BYTES].decode("utf-8", errors="ignore").rstrip() + "\n…\n"
    return text


def summary_for_restart(run: Any) -> str:
    """Best-effort ``summarize()`` for THIS run, for a ``mode=continue``
    restart (``routers.heads.restart_head``). Never raises: a transcript
    that cannot be located or read (no reader, nothing written yet, too
    large) just means no events reach ``summarize()`` — NOT the same as an
    empty result. The run's last reported step (``run.step``) is passed
    through regardless, and almost every real run has one by the time a
    "continue" restart is even possible, so the result still carries a
    "Last step: …" line (round 4 review finding: an earlier version of
    this docstring, and the PR text, claimed "no transcript" alone means
    "nothing to add" — live check against three runs with no transcript,
    including 1b386683, found a non-empty 51-byte recap in every one).
    ``""`` only when there is truly nothing at all to show — no events AND
    no step."""
    try:
        state = derive_for_run(run, time.time())["state"]
        result = read(run, locate(run), limit=1000, state=state)
    except Exception:  # noqa: BLE001 — a restart must never fail because of this
        return ""
    events = result.get("events")
    if not isinstance(events, list):
        return ""
    return summarize(events, last_step=run.step, env_values=redact.head_env_values(run))
