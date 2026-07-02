"""Soft-delete engine for the Files page — move-to-trash, NEVER ``rm``.

Locked policy (see SPEC): a "delete" moves the target into
``~/.mc/.trash/<YYYYMMDD-HHMMSS>/<root_key>/<canonical_rel>/`` — a reversible,
same-device rename within ``~/.mc``. ``.trash`` is NOT a registered FsRoot, so
it is never listed, served, or walked by the indexer.

The SOFT-DELETE primitives (:func:`validate_source`, :func:`trash_one`,
:func:`_dest`, :func:`canonical_rel`) contain NO hard-delete primitive on
purpose: no ``os.remove``, ``os.unlink``, ``Path.unlink``, ``shutil.rmtree`` or
``os.rmdir`` appears in them (``test_module_has_no_hard_delete`` enforces it
over the soft-delete locus). ``shutil.move`` stays an atomic rename because
every deletable root is host-backed & same-fs as ``~/.mc`` — guaranteed by the
import-time invariants in ``fs_roots``.

The INVERSE operations (list / restore / purge) live below. ``restore_one``
also never hard-deletes — it ``shutil.move``s the bytes back. ``purge_one`` and
``_cleanup_empty_parents`` are the SINGLE audited hard-delete locus (see the
``=== AUDITED HARD-DELETE LOCUS ===`` fence near the bottom): every destructive
primitive there is guarded by an ``is_relative_to(trash_root())`` containment
assertion on the line immediately above it, and a behavioural test
(``test_purge_one_escape_refused``) proves an escaping id leaves outside files
untouched. ``.trash`` is not writable by sandboxed agents, which bounds TOCTOU.

Two phases the caller MUST keep separate (both for delete AND for restore/purge
batches): validate ALL targets up front, then mutate — a containment violation
on one item never half-applies a batch.

LIMITATION (v1, documented in :func:`restore_one`): restoring a file whose
``TaskDeliverable`` row was cascade-deleted by ``files.delete`` brings the FILE
back but NOT the DB row — the re-index ``_upsert`` creates a fresh file_index
row with ``deliverable_id=None``. Acceptable for v1.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from app.services import fs_service
from app.services.fs_roots import (
    DELETABLE_KEYS,
    FsRoot,
    RootBlocked,
    RootNotFound,
    get_deletable_root,
    mc_home,
)


def trash_root() -> Path:
    """The soft-delete destination root (``~/.mc/.trash``)."""
    return mc_home() / ".trash"


def timestamp() -> str:
    """A second-resolution batch stamp (``YYYYMMDD-HHMMSS``)."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def canonical_rel(root: FsRoot, src: Path) -> str:
    """Root-relative resolved rel path.

    The ONE key used for BOTH the trash destination AND the file_index cascade
    match — so a non-canonical request (``a/./b.txt``) can never trash bytes
    while leaving the matching index row orphaned.
    """
    base = root.container_path.resolve()
    return str(src.resolve().relative_to(base))


def validate_source(root: FsRoot, subpath: str) -> tuple[Path, str]:
    """PHASE-1 dry check — raises :class:`fs_service.FsAccessError` /
    :class:`fs_service.FsNotFound`, moves nothing.

    Returns ``(resolved_src, canonical_rel)`` on success.
    """
    src = fs_service.safe_join(root, subpath)  # source containment (symlink-safe)
    base = root.container_path.resolve()
    if src.resolve() == base:
        # CRITICAL: '' / '/' / '.' would resolve to the root itself → trash the
        # WHOLE root. Refuse.
        raise fs_service.FsAccessError("refusing to trash an entire root")
    # safe_join() RESOLVES symlinks, so ``src`` is never itself a link. Re-derive
    # the un-resolved join to detect a symlinked leaf: don't silently move a link
    # and cascade as if the target deliverable were deleted.
    raw = base / (subpath or "").lstrip("/")
    if raw.is_symlink():
        raise fs_service.FsAccessError("symlink_not_deletable")
    if not src.exists():
        raise fs_service.FsNotFound(subpath)
    return src, canonical_rel(root, src)


def _dest(root_key: str, rel: str, *, ts: str) -> Path:
    base = trash_root().resolve()
    target = base / ts / root_key / rel  # rel is canonical → contains no '..'
    # Assertion (NOT the real guard — the real guard is source safe_join + the
    # canonical rel having no '..'). Belt-and-suspenders.
    if not target.resolve().is_relative_to(base):
        raise fs_service.FsAccessError("trash dest escapes .trash")
    return target


def trash_one(root: FsRoot, src: Path, rel: str, *, ts: str) -> Path:
    """PHASE-2 move only. Caller has already validated.

    Same-fs atomic rename within ``~/.mc``. NEVER unlink/rm. Uniquifies the
    destination if something is already trashed there (reversibility).
    """
    dest = _dest(root.key, rel, ts=ts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Symlink-safe parent: refuse to write THROUGH a symlinked trash component.
    base = trash_root().resolve()
    for p in dest.parents:
        try:
            inside = p.resolve().is_relative_to(base)
        except OSError:
            inside = False
        if inside and p.is_symlink():
            raise fs_service.FsAccessError("symlink in trash path")
    if dest.exists():
        # CRITICAL: never overwrite an already-trashed file (breaks reversibility).
        dest = dest.with_name(f"{dest.name}-{uuid.uuid4().hex[:8]}")
    shutil.move(str(src), str(dest))  # rename within ~/.mc (same device)
    return dest


# === INVERSE: list / restore / purge of ~/.mc/.trash =======================
#
# trash_id is the path RELATIVE to ~/.mc/.trash, i.e. "<ts>/<root_key>/<rel>".
# It is the documented inverse of _dest() above (base/ts/root_key/rel).

_TS_RE = re.compile(r"^\d{8}-\d{6}$")
_ROOT_KEY_RE = re.compile(r"^[a-z0-9-]+$")


def parse_trash_id(trash_id: str) -> tuple[str, str, str]:
    """Split a .trash-relative id into ``(ts, root_key, rel)`` — inverse of _dest.

    Validates structure strictly (the source/dest containment guards do the
    filesystem-level work; this is the syntactic gate):
      - reject NUL / absolute / empty;
      - require >= 3 ``/``-segments;
      - segment[0] = ts must match ``YYYYMMDD-HHMMSS`` AND parse via strptime;
      - segment[1] = root_key must match ``^[a-z0-9-]+$``;
      - segments[2:] joined = rel, must be non-empty and contain no ``.``/``..``
        or empty segment (no smuggled traversal through a "valid" structure).

    Raises :class:`fs_service.FsAccessError` on any violation.
    """
    if not trash_id or "\0" in trash_id:
        raise fs_service.FsAccessError("empty or NUL trash_id")
    if trash_id.startswith("/"):
        raise fs_service.FsAccessError("absolute trash_id")
    parts = [p for p in trash_id.strip("/").split("/")]
    if len(parts) < 3:
        raise fs_service.FsAccessError("malformed trash_id (need ts/root/rel)")
    ts, root_key, rel_parts = parts[0], parts[1], parts[2:]
    if not _TS_RE.match(ts):
        raise fs_service.FsAccessError(f"bad trash timestamp: {ts!r}")
    try:
        datetime.strptime(ts, "%Y%m%d-%H%M%S")
    except ValueError as e:
        raise fs_service.FsAccessError(f"bad trash timestamp: {ts!r}") from e
    if not _ROOT_KEY_RE.match(root_key):
        raise fs_service.FsAccessError(f"bad root_key: {root_key!r}")
    if not rel_parts or any(seg in ("", ".", "..") for seg in rel_parts):
        raise fs_service.FsAccessError("empty or traversal rel segment")
    return ts, root_key, "/".join(rel_parts)


def deleted_at_iso(ts: str) -> str:
    """ISO-8601 of the ``<ts>`` segment (``20260618-120000`` → ``2026-06-18T12:00:00``)."""
    return datetime.strptime(ts, "%Y%m%d-%H%M%S").isoformat()


def _resolve_in_trash(trash_id: str) -> Path:
    """The .trash containment guard — the source/purge equivalent of safe_join.

    SECURITY (mirrors trash_one's symlink-parent loop): the symlink-component
    check runs on the RAW, un-resolved join (each component's ``is_symlink()``)
    BEFORE any ``.resolve()`` — resolving first would silently collapse the very
    symlinks we must refuse. Sequence:
      1. reject NUL / absolute / ``..`` via :func:`parse_trash_id`-style checks;
      2. raw = base / trash_id; walk base→raw, refuse any symlinked component;
      3. target = raw.resolve(); assert ``is_relative_to(base)`` (belt & braces).

    Returns the RESOLVED Path (callers — incl. purge — act ONLY on this object,
    never re-joining the raw string).
    """
    if not trash_id or "\0" in trash_id:
        raise fs_service.FsAccessError("empty or NUL trash_id")
    if trash_id.startswith("/"):
        raise fs_service.FsAccessError("absolute trash_id")
    base = trash_root().resolve()
    rel = trash_id.strip("/")
    raw = base / rel
    # Symlink-component check on the UN-resolved path (O_NOFOLLOW semantics):
    # walk every component from base down to raw, refuse any that is a symlink.
    try:
        rel_to_base = raw.relative_to(base)
    except ValueError:
        # a lexical ".." pushed it above base before any resolution
        raise fs_service.FsAccessError("trash_id escapes .trash")
    cur = base
    for seg in rel_to_base.parts:
        if seg in ("..", "."):
            raise fs_service.FsAccessError("traversal segment in trash_id")
        cur = cur / seg
        if cur.is_symlink():  # raw lstat — does NOT follow
            raise fs_service.FsAccessError("symlink component in trash path")
    target = raw.resolve()
    if not target.is_relative_to(base):
        raise fs_service.FsAccessError("trash_id escapes .trash")
    return target


def list_trash(*, cap: int = 5000) -> list[dict]:
    """Bounded listing of leaf FILES under ``~/.mc/.trash``.

    Returns at most ``cap`` entries (huge-trash guard, mirrors
    ``file_indexer`` ``max_entries``; a truncated list is acceptable for v1 —
    purge-all still works on whatever the user can see). Each entry::

        {trash_id, original_root, original_subpath, name, size, mtime, deleted_at}

    Entries with a MALFORMED structure (fewer than 3 segments, bad ts/root) are
    SKIPPED, never surfaced. Entries whose ``root_key`` is not in
    ``DELETABLE_KEYS`` are also skipped — the UI must never offer a restore that
    will be rejected, and a planted ``.trash/<ts>/secrets/...`` blob is never
    advertised as restorable. Returns ``[]`` when ``.trash`` does not exist.
    """
    base = trash_root()
    if not base.exists() or not base.is_dir():
        return []
    base_res = base.resolve()
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(base):
        for fn in filenames:
            full = Path(dirpath) / fn
            try:
                rel_to_trash = str(full.relative_to(base))
            except ValueError:
                continue
            try:
                ts, root_key, rel = parse_trash_id(rel_to_trash)
            except fs_service.FsAccessError:
                continue  # stray top-level file / malformed structure
            if root_key not in DELETABLE_KEYS:
                continue  # never surface a non-restorable (e.g. planted) blob
            # Skip symlinks defensively (never advertise a link as a real file).
            if full.is_symlink():
                continue
            try:
                if not full.resolve().is_relative_to(base_res):
                    continue
                st = full.stat()
            except OSError:
                continue
            out.append(
                {
                    "trash_id": rel_to_trash,
                    "original_root": root_key,
                    "original_subpath": rel,
                    "name": full.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "deleted_at": deleted_at_iso(ts),
                }
            )
            if len(out) >= cap:
                return out
    return out


def restore_one(trash_id: str) -> tuple[str, str]:
    """Move a trashed file back to its original (deletable) root.

    Returns ``(root_key, dest_subpath)`` — ``dest_subpath`` may be uniquified.

    SECURITY phases:
      1. SOURCE containment (symlink-safe): :func:`_resolve_in_trash`.
      2. structural parse → ``(ts, root_key, rel)``.
      3. DESTINATION root: :func:`fs_roots.get_deletable_root` — NOT
         ``get_browsable_root``. A file may only be restored to a root it was
         legally deletable FROM (the writable set), which also re-asserts the
         host-backed/same-fs invariant (no cross-volume copy+unlink data loss).
         ``RootNotFound``/``RootBlocked`` propagate so the router skips.
      4. DESTINATION containment: ``fs_service.safe_join`` (../symlink/absolute).
      5. refuse restore THROUGH a symlinked destination component (raw-join walk,
         like ``validate_source``), then re-verify the landing dir is real &
         contained immediately before the move (O_NOFOLLOW on the parent).
      6. NEVER overwrite: uniquify the destination if it already exists.
      7. ``shutil.move`` (same-fs rename within ~/.mc).

    LIMITATION (v1): if this file's ``TaskDeliverable`` was cascade-deleted by
    ``files.delete``, the bytes come back but the DB row does NOT — the caller's
    re-index ``_upsert`` creates a fresh file_index row with
    ``deliverable_id=None``. Documented & acceptable.
    """
    src = _resolve_in_trash(trash_id)  # source containment, symlink-safe
    if not src.exists() or not src.is_file():
        raise fs_service.FsNotFound(trash_id)
    _ts, root_key, rel = parse_trash_id(trash_id)
    root = get_deletable_root(root_key)  # RootNotFound/RootBlocked → router skip
    dest = fs_service.safe_join(root, rel)  # destination containment guard
    base = root.container_path.resolve()

    # Refuse restore THROUGH a symlinked destination component (un-resolved walk,
    # mirrors validate_source: never write through a link that escapes the root).
    raw = base / rel
    cur = base
    for seg in raw.relative_to(base).parts:
        cur = cur / seg
        if cur.is_symlink():
            raise fs_service.FsAccessError("symlink in restore destination")

    if dest.exists():
        # CRITICAL: never overwrite on restore (mirror trash_one uniquify).
        dest = dest.with_name(f"{dest.name}-{uuid.uuid4().hex[:8]}")

    # Create missing parents, refusing a symlinked level at each step.
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Re-verify the landing dir AT MOVE TIME: real dir, not a symlink, still
    # contained. O_NOFOLLOW on the parent so the kernel refuses link traversal.
    if parent.is_symlink():
        raise fs_service.FsAccessError("restore parent is a symlink")
    try:
        dir_fd = os.open(str(parent), os.O_NOFOLLOW | os.O_DIRECTORY | os.O_RDONLY)
    except OSError as e:
        raise fs_service.FsAccessError("restore parent not a real dir") from e
    try:
        if not Path(os.path.realpath(str(parent))).is_relative_to(base):
            raise fs_service.FsAccessError("restore parent escapes root")
    finally:
        os.close(dir_fd)

    shutil.move(str(src), str(dest))  # rename within ~/.mc (same device)

    # Final landing verification (the move could in theory land off-root via a
    # race): confirm dest is still inside the root, else move it back & refuse.
    landed = dest.resolve()
    if not landed.is_relative_to(base):
        shutil.move(str(dest), str(src))
        raise fs_service.FsAccessError("restored file landed outside root")
    return root_key, str(dest.relative_to(base))


# === AUDITED HARD-DELETE LOCUS =============================================
# The ONLY place in this module where unlink/rmtree/rmdir may appear. Every
# destructive call is preceded by an is_relative_to(trash_root()) containment
# assertion on the line immediately above it. test_module_has_no_hard_delete
# proves no such primitive leaks into the soft-delete locus above; a separate
# behavioural test proves an escaping id leaves outside files untouched.


def _cleanup_empty_parents(target: Path) -> None:
    """Remove now-empty ``<ts>/<root>`` dirs after a purge — STRICTLY inside .trash.

    Capped to exactly the 2 known levels (``target.parent`` =<root>, then <ts>),
    never an open-ended while-loop. Each rmdir is guarded by ``is_relative_to``
    AND ``!= stop`` so it can never climb to / delete ``.trash`` itself.
    """
    stop = trash_root().resolve()
    for parent in (target.parent, target.parent.parent):
        try:
            p = parent.resolve()
        except OSError:
            return
        if p == stop or not p.is_relative_to(stop):
            return  # reached/escaped the .trash sentinel — STOP
        try:
            if any(p.iterdir()):
                return  # not empty — and a non-empty sibling above stays
        except OSError:
            return
        # GUARD: p.is_relative_to(stop) and p != stop verified above.
        p.rmdir()  # noqa: hard-delete — confined to .trash by the guard above


def purge_one(trash_id: str) -> None:
    """The ONE intentional hard-delete — STRICTLY confined to ``~/.mc/.trash``.

    Acts ONLY on the resolved ``Path`` returned by :func:`_resolve_in_trash`
    (containment + symlink-refusal); never re-joins the raw string. Re-asserts
    ``is_relative_to(trash_root())`` on the line immediately above the
    destructive call (belt-and-suspenders, enforced by the invariant test).
    """
    target = _resolve_in_trash(trash_id)  # STRICT .trash containment + symlink-safe
    stop = trash_root().resolve()
    if target.is_dir() and not target.is_symlink():
        # GUARD: target.is_relative_to(stop) — proven by _resolve_in_trash above.
        assert target.is_relative_to(stop), "purge target escaped .trash"
        shutil.rmtree(target)  # noqa: hard-delete — confined to .trash by the assert above
    else:
        # GUARD: target.is_relative_to(stop) — proven by _resolve_in_trash above.
        assert target.is_relative_to(stop), "purge target escaped .trash"
        os.unlink(target)  # noqa: hard-delete — confined to .trash by the assert above (O_NOFOLLOW on final)
    _cleanup_empty_parents(target)
