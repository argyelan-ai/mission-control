"""Boot step: make sure the MC home tree (``~/.mc``) exists and is writable.

Why this exists (measured on a fresh Linux box whose login user had uid 1001):
the backend container ran as uid 1000, ``~/.mc`` is a bind mount owned by the
host user, and the first write — ``mkdir ~/.mc/vault`` inside migration 0112 —
crashed with ``PermissionError``. Backend unhealthy, frontend and caddy never
started, and nothing said why. On macOS the problem is invisible because
Docker Desktop maps every container uid onto the host user.

The fix has two halves:

* the container entrypoint (``docker-entrypoint.sh``) runs the process as the
  host user's uid/gid when ``PUID``/``PGID`` are set (``setup.sh`` writes them
  on Linux), so the bind mount is writable by construction;
* this module is the boot step that creates the core directories (instead of
  a migration) and, when it cannot, says in ONE log line and in ``/health``
  what is wrong and what to do — a degraded but reachable stack instead of an
  unhealthy restart loop without a reason.

Never fatal: a problem here is logged and reported, the process keeps booting.
Paths in the user-facing hint are shown as ``~/.mc/...`` so the unauthenticated
``/health`` answer does not disclose the host's home path.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("mc.startup")

# Directories the core reads or writes right after boot. Created here (as the
# process user) so neither a migration nor Docker's bind-mount auto-creation
# (which creates missing sources as root on Linux) has to.
# (relative path under ~/.mc, mode for a NEW directory)
CORE_DIRS: tuple[tuple[str, int], ...] = (
    ("vault", 0o755),
    ("vault/attachments/files", 0o755),
    ("vault/attachments/images", 0o755),
    ("vault/attachments/audio", 0o755),
    ("agents", 0o755),
    ("workspaces", 0o755),
    ("deliverables", 0o755),
    ("attachments", 0o755),
    ("mcp-screenshots", 0o755),
    ("wake-requests", 0o755),
    ("secrets", 0o700),
    ("ssh", 0o700),
)

DOCS_HINT = "docs/handbook/operations/platforms.md#host-user-id-linux-nas"
# /health re-checks a recorded problem at most this often, so the warning
# clears by itself once the operator fixed ownership (no restart needed).
RECHECK_SECONDS = 30.0


@dataclass
class Problem:
    path: str  # display path, "~/.mc/<sub>"
    reason: str  # "missing" | "not_writable" | "not_a_directory" | "create_failed"
    owner_uid: int | None = None
    owner_gid: int | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "reason": self.reason,
            "owner_uid": self.owner_uid,
            "owner_gid": self.owner_gid,
            "error": self.error,
        }


@dataclass
class MCHomeStatus:
    ok: bool
    uid: int
    gid: int
    problems: list[Problem] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    hint: str = ""
    checked_at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "process_uid": self.uid,
            "process_gid": self.gid,
            "problems": [p.as_dict() for p in self.problems],
            "hint": self.hint,
            "docs": DOCS_HINT,
        }


_last_status: MCHomeStatus | None = None
_last_root: Path | None = None


def _display(rel: str) -> str:
    return "~/.mc" if not rel else f"~/.mc/{rel}"


def _owner(path: Path) -> tuple[int | None, int | None]:
    try:
        st = path.stat()
        return st.st_uid, st.st_gid
    except OSError:
        return None, None


def _writable(path: Path) -> bool:
    # access(2) honours uid/gid/mode AND a read-only mount (EROFS).
    return os.access(path, os.W_OK | os.X_OK)


def _probe_write(path: Path) -> str | None:
    """Really create + delete a file. Returns the error text, or None if fine."""
    probe = path / f".mc-write-probe-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc.strerror or exc}"


def _build_hint(problems: list[Problem], uid: int, gid: int) -> str:
    if not problems:
        return ""
    root_problem = next((p for p in problems if p.path == "~/.mc"), None)
    if root_problem and root_problem.reason == "missing":
        return (
            "~/.mc is not mounted into the container. Check that HOME_HOST in the "
            "backend environment is the host's $HOME and that the ${HOME}/.mc "
            "volume line is present in docker-compose.yml."
        )
    owners = {
        (p.owner_uid, p.owner_gid)
        for p in problems
        if p.owner_uid is not None and p.owner_uid not in (0, uid)
    }
    if owners:
        owner_uid, owner_gid = sorted(owners, key=lambda o: (o[0] or 0))[0]
        return (
            f"~/.mc belongs to uid {owner_uid} but the backend runs as uid {uid}. "
            f"Set PUID={owner_uid} and PGID={owner_gid} in .env (setup.sh does this "
            f"on Linux: PUID=$(id -u), PGID=$(id -g)), then run `docker compose up -d`."
        )
    paths = ", ".join(sorted({p.path for p in problems}))
    return (
        f"The backend (uid {uid}, gid {gid}) cannot write {paths}. Usually Docker "
        f"created it as root before the first start. Fix it on the host with "
        f"`sudo chown -R $(id -u):$(id -g) ~/.mc` and make sure PUID/PGID in .env "
        f"match `id -u`/`id -g`, then run `docker compose up -d`."
    )


def check_mc_home(root: Path | None = None, *, create: bool = True) -> MCHomeStatus:
    """Create (``create=True``) and verify the core directories under ``root``.

    ``root`` defaults to :func:`app.services.fs_roots.mc_home`. With
    ``create=False`` nothing is written — only ``stat``/``access`` calls
    (missing directories are then reported as ``missing``).
    """
    global _last_status, _last_root
    if root is None:
        from app.services.fs_roots import mc_home

        root = mc_home()
    uid, gid = os.getuid(), os.getgid()
    problems: list[Problem] = []
    created: list[str] = []

    if not root.exists():
        # The bind mount is missing entirely when the parent (HOME_HOST) is
        # absent too; otherwise try to create ~/.mc ourselves.
        if create and root.parent.is_dir():
            try:
                root.mkdir(mode=0o755)
                created.append(_display(""))
            except OSError as exc:
                ou, og = _owner(root.parent)
                problems.append(Problem(_display(""), "create_failed", ou, og, str(exc)))
        else:
            problems.append(Problem(_display(""), "missing"))

    if root.is_dir():
        err = None
        if not _writable(root):
            err = "no write permission"
        elif create:
            err = _probe_write(root)
        if err:
            ou, og = _owner(root)
            problems.append(Problem(_display(""), "not_writable", ou, og, err))
        for rel, mode in CORE_DIRS:
            path = root / rel
            if path.is_dir():
                if not _writable(path):
                    ou, og = _owner(path)
                    problems.append(Problem(_display(rel), "not_writable", ou, og))
                continue
            if path.exists():
                ou, og = _owner(path)
                problems.append(Problem(_display(rel), "not_a_directory", ou, og))
                continue
            if not create:
                problems.append(Problem(_display(rel), "missing"))
                continue
            try:
                path.mkdir(mode=mode, parents=True, exist_ok=True)
                created.append(_display(rel))
            except OSError as exc:
                # Report the owner of the nearest existing parent — that is
                # the directory the process was not allowed to write into.
                parent = path.parent
                while not parent.exists() and parent != root:
                    parent = parent.parent
                ou, og = _owner(parent)
                problems.append(
                    Problem(
                        _display(rel),
                        "create_failed",
                        ou,
                        og,
                        f"{type(exc).__name__}: {exc.strerror or exc}",
                    )
                )
    elif root.exists():
        ou, og = _owner(root)
        problems.append(Problem(_display(""), "not_a_directory", ou, og))

    status = MCHomeStatus(
        ok=not problems,
        uid=uid,
        gid=gid,
        problems=problems,
        created=created,
        hint=_build_hint(problems, uid, gid),
    )
    _last_status = status
    _last_root = root
    return status


def ensure_mc_home(root: Path | None = None) -> MCHomeStatus:
    """The boot step: create + verify, then log the result once."""
    status = check_mc_home(root, create=True)
    if status.created:
        logger.info("MC home: created %s", ", ".join(status.created))
    if status.ok:
        logger.info("MC home writable (process uid %d, gid %d)", status.uid, status.gid)
    else:
        for p in status.problems:
            logger.warning(
                "MC home problem: %s %s (owner uid=%s gid=%s)%s",
                p.path,
                p.reason,
                p.owner_uid,
                p.owner_gid,
                f" — {p.error}" if p.error else "",
            )
        # The one line an operator needs. ERROR so it stands out in
        # `docker compose logs backend`.
        logger.error("MC HOME NOT WRITABLE — %s (see %s)", status.hint, DOCS_HINT)
    return status


def health_detail() -> dict | None:
    """``None`` when fine or never checked; otherwise the detail for /health.

    A recorded problem is re-checked at most every :data:`RECHECK_SECONDS`
    (same create-and-verify as the boot step, so directories that could not be
    created before appear now), and the report clears once the operator fixed
    it — no restart needed. Nothing is touched while the status is fine.
    """
    status = _last_status
    if status is None or status.ok:
        return None
    if time.monotonic() - status.checked_at >= RECHECK_SECONDS:
        status = check_mc_home(_last_root, create=True)
        if status.ok:
            logger.info("MC home is writable again — problem resolved")
            return None
    return status.as_dict()


def reset_for_tests() -> None:
    global _last_status, _last_root
    _last_status = None
    _last_root = None


__all__ = [
    "CORE_DIRS",
    "MCHomeStatus",
    "Problem",
    "check_mc_home",
    "ensure_mc_home",
    "health_detail",
    "reset_for_tests",
]


def _main() -> int:
    """``python -m app.services.mc_home_preflight`` — the same check by hand.

    Prints the result as JSON; exit code 0 = writable, 1 = problem. Handy as
    ``docker compose exec -u mcuser backend python -m app.services.mc_home_preflight``
    and used by scripts/test-host-uid.sh.
    """
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    status = ensure_mc_home()
    print(json.dumps(status.as_dict()))
    return 0 if status.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
