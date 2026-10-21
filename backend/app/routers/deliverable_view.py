"""Multi-file HTML deliverables, served as a real page inside a sandbox.

The task detail shows an HTML deliverable as the rendered page in an
``<iframe sandbox="allow-scripts">`` (frontend ``HtmlPreview``). A single-file
page goes in as ``srcdoc``. A page that links its own files (``style.css``,
``logo.png``, ``app.js``, ``about.html``) cannot work that way — a ``srcdoc``
frame has no folder to resolve them against, and an iframe cannot send the
operator's Bearer header to the file endpoint. So:

1. ``POST …/deliverables/{id}/view-url`` (operator auth, like the file
   download) mints a signed URL for the deliverable's FOLDER:
   ``/api/v1/deliverable-view/<token>/<page>.html``.
2. ``GET /api/v1/deliverable-view/<token>/<path>`` serves files from that
   folder only. The token is in the PATH on purpose: relative links in the
   page resolve to siblings under the same token.

Security boundary — the page is untrusted, model-written content, and the
token is readable by it (it is in the page's own URL):

* **The token opens one folder, read-only, for at most 10 minutes.** It is
  HMAC-signed with a key derived from the JWT secret (domain-separated, and
  not a JWT: no session decoder anywhere accepts it), bound to the operator
  (their ``token_version`` — logging out kills it, within
  ``GRANT_CACHE_SECONDS``), the task, the deliverable and the folder. GET
  only.
* **The folder is the task's own:** the deliverable's stored path must lie in
  the task's deliverable area (``deliverable_paths.accepted_path_prefixes``),
  the task's folder on disk must be a real folder inside a deliverables
  mount (no symlink to another task or out of the mount), and the served
  folder must stay inside it (realpath). A request passes
  ``fs_service.safe_join`` (ADR-040's one containment guard) and is then
  **opened once**: from ``/`` down to the file, every step is an
  ``openat`` with ``O_NOFOLLOW`` on the folder opened before it, and the
  bytes come from that one open handle (``_open_in_folder``). So a symlink
  anywhere on the way is refused — also one swapped in between the check
  and the read (time-of-check to time-of-use) — and the folder view serves
  no symlinks at all. Only regular files are served (no FIFO, no device).
  ``..`` and hidden names (``.env``, ``.git``) are refused by name. A
  folder that changes under a cached check is a 403, never a 500.
  **Known limit — hard links are not detected.** A hard link is not a
  link one could see; it IS the file. So one inside the folder that names
  a file elsewhere on the same filesystem is served. Accepted: only
  someone who writes into this folder can make one, the target must sit on
  the same filesystem (from inside a container: on the same mount), and on
  Linux with ``fs.protected_hardlinks`` (the usual default) they must own
  the target or be allowed to read and write it — so that writer could
  have copied those bytes into the deliverable anyway. Refusing
  ``st_nlink > 1`` would also refuse every ordinary file that merely has a
  second name.
* **Every answer carries the sandbox as a header**, so the page runs in an
  opaque origin even when the URL is opened as its own tab: CSP
  ``sandbox allow-scripts`` (no ``allow-same-origin``), ``nosniff``,
  ``no-referrer``. Errors (403, 404, 405, 416, 429, a path without a
  token) get the strict set: ``sandbox; default-src 'none'``, nosniff,
  no-referrer, ``no-store`` (``_ViewRoute``). Two answers on this path come
  from middleware ahead of the router and carry only Caddy's headers: a
  CORS preflight (``OPTIONS`` with ``Access-Control-Request-Method``) and
  slowapi's per-IP 429 — both fixed text, no input echoed. Its sources are
  the token's folder URL plus ``https:``
  (CDN libraries and fonts, as in the srcdoc preview) — not ``'self'``:
  ``'self'`` would be all of MC (and WebKit does not match ``'self'`` inside
  a sandboxed page at all, measured 2026-10-21). Over plain http that keeps
  the page away from every other MC URL. Over https it does NOT: ``https:``
  covers MC's own origin. What keeps the page from acting as the operator
  there is the auth layer — the session cookie is no credential for a
  cross-site subrequest (``auth._cookie_allowed``), and the page cannot
  send a header. ``frame-ancestors 'self'``: only MC may frame it (it also
  wins over the ``X-Frame-Options: DENY`` a TLS proxy may add).
* **CORS:** ``Access-Control-Allow-Origin: *``. Module scripts,
  ``<link crossorigin>`` and fonts of an opaque-origin page are CORS
  requests with ``Origin: null`` and would be dropped otherwise. The token
  in the path is the capability; no credential is involved.
* **A page that floods runs out of its own requests:** at most
  ``VIEW_MAX_REQUESTS`` per token (then 429), and the DB/folder checks are
  reused for ``GRANT_CACHE_SECONDS`` instead of three DB reads per file.
  Answers may be cached privately while the token lives (the URL is unique
  per mint). MC's per-IP limit counts per path, so the flood never reaches
  the UI's routes.
* **No cookies:** this endpoint never reads one and never sets one.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import stat
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Message, Receive, Scope, Send

from app.auth import require_user
from app.config import settings
from app.database import get_session, release_session
from app.log_redaction import redact_secrets
from app.services import fs_service
from app.services.deliverable_paths import accepted_path_prefixes
from app.services.fs_roots import FsRoot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["deliverable-view"])

VIEW_PATH = "/api/v1/deliverable-view"

# How long a minted view URL works. The frame loads the page and its files
# right away; a fresh URL is minted every time a preview opens.
VIEW_TTL_SECONDS = 600

# How many requests one view URL may make. A landing page with its CSS,
# images, scripts and a handful of sub-pages needs tens; a page in a loop
# (`new Image().src = 'a.png?' + n`) stops here (429) instead of keeping MC
# busy. Reopening the preview mints a new URL with a new budget.
VIEW_MAX_REQUESTS = 600

# How long a checked token's folder is reused before the user/deliverable/
# folder checks hit the DB again. Logging out or moving the deliverable
# takes effect within this window.
GRANT_CACHE_SECONDS = 10

# Per-token state, in process (the backend runs one worker). Only correctly
# signed, unexpired tokens get an entry, and entries go with their token.
_budgets: dict[str, list[int]] = {}  # token -> [requests left, token expiry]
_grants: dict[str, tuple["_Folder", float]] = {}  # token -> (folder, checked until)

# The backend's deliverable mounts (container paths, see docker-compose.yml
# and fs_service.resolve_deliverable). A folder is only served from strictly
# inside one of them.
ZONE_ROOTS: tuple[str, ...] = ("/deliverables", "/shared-deliverables", "/shared-mcp")

_HTML_SUFFIXES = frozenset({".html", ".htm"})

# ── The token ──────────────────────────────────────────────────────────────
#
# "dv1.<claims>.<signature>", both parts base64url without padding. The key
# is HMAC(jwt_secret_key, label): the same secret MC already has, but a
# signature made with it can never pass as a JWT signature (and the claims
# are not a JWT header/payload either).

_TOKEN_PREFIX = "dv1"
_KEY_LABEL = b"mc/deliverable-view/v1"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,1024}$")


def _signing_key() -> bytes:
    return hmac.new(settings.jwt_secret_key.encode(), _KEY_LABEL, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())


def _folder_tag(folder: str) -> str:
    """Binds a token to a folder without putting the path into the URL."""
    return _b64(hashlib.sha256(folder.encode()).digest()[:16])


@dataclass(frozen=True)
class ViewClaims:
    user_id: uuid.UUID
    token_version: int
    task_id: uuid.UUID
    deliverable_id: uuid.UUID
    folder_tag: str
    expires: int


def mint_view_token(
    *,
    user_id: uuid.UUID,
    token_version: int,
    task_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    folder: str,
    ttl: int = VIEW_TTL_SECONDS,
) -> str:
    claims = {
        "u": str(user_id),
        "tv": int(token_version),
        "t": str(task_id),
        "d": str(deliverable_id),
        "f": _folder_tag(folder),
        "exp": int(time.time()) + int(ttl),
        # Every mint is a new URL (and so a new request budget), even twice
        # in the same second.
        "n": _b64(os.urandom(6)),
    }
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{_TOKEN_PREFIX}.{body}.{_sign(body)}"


def read_view_token(token: str) -> ViewClaims | None:
    """The token's claims, or ``None`` when it is malformed, not signed by
    MC, or expired."""
    if not _TOKEN_RE.match(token or ""):
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        return None
    _, body, signature = parts
    if not hmac.compare_digest(_sign(body), signature):
        return None
    try:
        claims = json.loads(_unb64(body))
        expires = int(claims["exp"])
        result = ViewClaims(
            user_id=uuid.UUID(claims["u"]),
            token_version=int(claims["tv"]),
            task_id=uuid.UUID(claims["t"]),
            deliverable_id=uuid.UUID(claims["d"]),
            folder_tag=str(claims["f"]),
            expires=expires,
        )
    except (ValueError, KeyError, TypeError):
        return None
    if time.time() >= expires:
        return None
    return result


# ── The folder ─────────────────────────────────────────────────────────────


class _NoFolderView(Exception):
    """This deliverable has no folder that may be served."""


@dataclass(frozen=True)
class _Folder:
    logical: str  # the stored-path form, what the token is bound to
    real: Path  # realpath on disk, strictly inside a deliverables mount
    entry: str | None  # the page itself, for a single-file deliverable


async def _view_folder(deliverable, session: AsyncSession) -> _Folder:
    """The folder a view URL of ``deliverable`` may serve from.

    A folder deliverable serves its own root; a file deliverable serves the
    folder it sits in — but only inside the task's deliverable area, never
    a legacy/back-end path whose parent could be any host folder.
    """
    stored = deliverable.path or ""
    if not stored or stored.startswith(("http://", "https://")) or "\x00" in stored:
        raise _NoFolderView("no local path")
    if ".." in stored.split("/"):
        raise _NoFolderView("path with ..")

    resolved = await fs_service.resolve_deliverable(deliverable, session, target="container")
    if not resolved:
        raise _NoFolderView("unresolvable")
    normalized = os.path.normpath(stored)
    if os.path.isdir(resolved):
        logical, on_disk, entry = normalized, resolved, None
    elif os.path.isfile(resolved):
        logical, on_disk = os.path.dirname(normalized), os.path.dirname(resolved)
        entry = os.path.basename(resolved)
    else:
        raise _NoFolderView("not on disk")

    prefixes = accepted_path_prefixes(deliverable.task_id)
    task_prefix = next((p for p in prefixes if (logical + "/").startswith(p)), None)
    if task_prefix is None:
        raise _NoFolderView("outside the task's deliverable area")

    zone = next((z for z in ZONE_ROOTS if resolved.startswith(z.rstrip("/") + "/")), None)
    if zone is None:
        raise _NoFolderView("outside the deliverable mounts")
    zone = zone.rstrip("/")
    zone_real = Path(zone).resolve()

    # The task's own folder, through the same resolver (slug layout
    # included), taken as its NAME under the real mount — never resolved: a
    # task folder that is a symlink (to another task's folder, or out of the
    # mount) would otherwise vouch for wherever it points. The served
    # folder's realpath must lie inside that name.
    task_root = await fs_service.resolve_deliverable(
        SimpleNamespace(path=task_prefix.rstrip("/"), agent_id=getattr(deliverable, "agent_id", None)),
        session,
        target="container",
    )
    if not task_root or not task_root.startswith(zone + "/"):
        raise _NoFolderView("task folder outside the deliverable mount")
    task_dir = zone_real / os.path.relpath(os.path.normpath(task_root), zone)
    real = Path(on_disk).resolve()
    if not real.is_relative_to(task_dir):
        raise _NoFolderView("folder points out of the task's folder")
    return _Folder(logical=logical, real=real, entry=entry)


def _folder_root(folder: _Folder) -> FsRoot:
    return FsRoot(
        key="deliverable-view",
        label="",
        icon="",
        subpath="",
        native_open=False,
        sensitive=False,
        container_override=str(folder.real),
    )


def _clean_relpath(path: str) -> str:
    """The request path inside the folder, or 403/404. ``safe_join`` is the
    containment guard; this only refuses what never belongs in a page URL."""
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        raise HTTPException(status_code=403, detail="Path not allowed")
    parts = path.split("/")
    if any(p in ("..", ".") for p in parts):
        raise HTTPException(status_code=403, detail="Path not allowed")
    if any(p.startswith(".") for p in parts if p):
        raise HTTPException(status_code=404, detail="File not found")
    return path


_NOT_ALLOWED = "Path not allowed"
_NOT_FOUND = "File not found"

# Every step of the walk: a directory, never followed if it is a symlink
# (Linux answers ENOTDIR, macOS ELOOP). O_PATH where it exists (Linux, the
# container): it needs only search permission on a folder, like a lookup by
# name, not read permission.
_DIR_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
# The file: never followed; O_NONBLOCK so a FIFO cannot hang the open (it
# is refused right after, by fstat).
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _check_contained(folder: _Folder, rel: str) -> None:
    """ADR-040's containment guard: the resolved path stays in the folder
    (403). Only a check — nothing is read through what it returns."""
    try:
        fs_service.safe_join(_folder_root(folder), rel)
    except fs_service.FsAccessError:
        raise HTTPException(status_code=403, detail=_NOT_ALLOWED) from None
    except (OSError, RuntimeError, ValueError):  # a symlink loop, a vanished folder
        raise HTTPException(status_code=403, detail=_NOT_ALLOWED) from None


def _refusal(dir_fd: int, name: str, exc: OSError) -> HTTPException:
    """403 for a symlink (or anything unexpected), 404 for a name that is not
    there or not what the path needs. Only picks the status code — nothing
    is read through this lookup."""
    if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
        try:
            if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                return HTTPException(status_code=403, detail=_NOT_ALLOWED)
        except OSError:
            pass
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return HTTPException(status_code=404, detail=_NOT_FOUND)
    return HTTPException(status_code=403, detail=_NOT_ALLOWED)


def _open_in_folder(folder: _Folder, rel: str) -> tuple[BinaryIO, int]:
    """Open ``folder.real/rel`` ONCE for reading and return it with its size.

    The walk starts at ``/`` and opens every folder on the way relative to
    the one before (``dir_fd``) with ``O_NOFOLLOW``, the file too. A symlink
    anywhere — in the folder, or swapped into the path after a check — makes
    the open fail instead of being followed, so what is opened is exactly
    the file at that path, and the caller serves the bytes from this handle
    (no second lookup by name). 403 for a symlink, 404 for a missing name
    or anything but a regular file. ``rel`` is already clean
    (``_clean_relpath``: no ``..``, ``.``, hidden or empty parts)."""
    steps = [*folder.real.parts[1:], *rel.split("/")[:-1]]
    name = rel.split("/")[-1]
    fd = os.open("/", _DIR_FLAGS)
    try:
        for step in steps:
            try:
                nxt = os.open(step, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise _refusal(fd, step, exc) from None
            os.close(fd)
            fd = nxt
        try:
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=fd)
        except OSError as exc:
            raise _refusal(fd, name, exc) from None
    finally:
        os.close(fd)
    try:
        st = os.fstat(file_fd)
        if not stat.S_ISREG(st.st_mode):
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return os.fdopen(file_fd, "rb", buffering=0), st.st_size
    except BaseException:
        os.close(file_fd)
        raise


async def _open_file(folder: _Folder, rel: str) -> tuple[BinaryIO, int]:
    """``_check_contained`` + ``_open_in_folder`` off the event loop. Every
    filesystem surprise is a refusal, never a 500."""

    def work() -> tuple[BinaryIO, int]:
        _check_contained(folder, rel)
        try:
            return _open_in_folder(folder, rel)
        except HTTPException:
            raise
        except (OSError, ValueError):
            raise HTTPException(status_code=403, detail=_NOT_ALLOWED) from None

    return await anyio.to_thread.run_sync(work)


# ── Per-token budget and check cache ───────────────────────────────────────


def _forget_expired(now: float) -> None:
    for token in [t for t, (_, exp) in _budgets.items() if exp <= now]:
        _budgets.pop(token, None)
    for token in [t for t, (_, until) in _grants.items() if until <= now]:
        _grants.pop(token, None)


def _take_request(token: str, claims: ViewClaims) -> bool:
    """One request off this token's budget; False when it is used up."""
    now = time.time()
    if len(_budgets) > 256:
        _forget_expired(now)
    entry = _budgets.setdefault(token, [VIEW_MAX_REQUESTS, claims.expires])
    if entry[0] <= 0:
        return False
    entry[0] -= 1
    return True


def _checked_folder(token: str) -> _Folder | None:
    hit = _grants.get(token)
    if hit is None:
        return None
    folder, until = hit
    if time.time() >= until:
        _grants.pop(token, None)
        return None
    return folder


# ── Response headers ───────────────────────────────────────────────────────

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")


def _request_hosts(request: Request) -> list[str]:
    """The host(s) the browser used for this URL: ``Host``, and the original
    host when a dev proxy (Next rewrites) forwards it. Only well-formed
    values — they end up in a response header."""
    hosts: list[str] = []
    for raw in (request.headers.get("host"), request.headers.get("x-forwarded-host")):
        value = (raw or "").split(",")[0].strip()
        if value and _HOST_RE.match(value) and value not in hosts:
            hosts.append(value)
    return hosts


def view_csp(request: Request, token: str) -> str:
    """The page's CSP: the sandbox, plus sources limited to this token's
    folder (and https:/data:/blob: like the srcdoc preview, for CDN libraries
    and fonts). No fetch/XHR, no forms, framed only by MC."""
    folder = " ".join(
        f"{scheme}://{host}{VIEW_PATH}/{token}/"
        for host in _request_hosts(request)
        for scheme in ("http", "https")
    )
    return "; ".join([
        "sandbox allow-scripts",
        "default-src 'none'",
        f"script-src {folder} 'unsafe-inline' 'unsafe-eval' https: data: blob:",
        f"style-src {folder} 'unsafe-inline' https: data:",
        f"img-src {folder} https: data: blob:",
        f"font-src {folder} https: data:",
        f"media-src {folder} https: data: blob:",
        f"frame-src {folder} https:",
        "connect-src 'none'",
        "form-action 'none'",
        f"base-uri {folder}".rstrip(),
        "frame-ancestors 'self'",
    ])


def _view_headers(request: Request, token: str, claims: ViewClaims) -> dict[str, str]:
    max_age = max(1, min(VIEW_TTL_SECONDS, claims.expires - int(time.time())))
    return {
        "Content-Security-Policy": view_csp(request, token),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        # The URL is unique per mint: the operator's browser may keep a file
        # while the token lives, never a shared cache.
        "Cache-Control": f"private, max-age={max_age}",
        # Opaque-origin CORS requests (module scripts, crossorigin CSS,
        # fonts) carry Origin: null. The path token is the capability.
        "Access-Control-Allow-Origin": "*",
    }


# What every answer that is not a file of the page carries: an error page
# never runs a script, never sends a Referer, is never stored (a 404 can be a
# 200 a second later, a 403 must not outlive its reason). frame-ancestors as
# on the files, so an error inside the preview frame shows instead of being
# blocked by a TLS proxy's X-Frame-Options.
ERROR_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _guard_start(message: Message) -> None:
    """Make an ``http.response.start`` safe to show: a file answer (200/206
    with its folder CSP) keeps its headers, everything else gets
    ``ERROR_HEADERS`` — also a 2xx that somehow lacks a CSP (fail closed)."""
    headers = MutableHeaders(scope=message)
    if message["status"] in (200, 206) and "content-security-policy" in headers:
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "no-referrer")
        return
    for key, value in ERROR_HEADERS.items():
        headers[key] = value
    # An error answer is no CORS resource of the page, and repeats no URL.
    for key in ("access-control-allow-origin", "location"):
        if key in headers:
            del headers[key]


class _ViewRoute(APIRoute):
    """A route whose every answer passes ``_guard_start`` — the endpoint's
    files and HTTPExceptions, the 405 Starlette raises for a wrong method
    (outside the endpoint), and an unexpected error (logged, answered with
    500 and the strict headers instead of the bare error page)."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                _guard_start(message)
            await send(message)

        try:
            await super().handle(scope, receive, guarded_send)
        except StarletteHTTPException as exc:
            if started:
                raise
            await JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )(scope, receive, guarded_send)
        except Exception as exc:
            if started:
                raise
            # The traceback, redacted here: the path (and so an error
            # message) carries the token, and the log filter only sees the
            # message, never exc_info.
            trace = "".join(traceback.format_exception(exc))
            logger.error("deliverable view failed\n%s", redact_secrets(trace))
            await JSONResponse({"detail": "Internal Server Error"}, status_code=500)(
                scope, receive, guarded_send
            )


# At most 18 digits per number: more than any file size (10^18 bytes), and
# int() never sees a number it refuses (Python stops at 4300 digits with a
# ValueError, which would be a 500). A longer number does not match, so the
# range is ignored like any other malformed one.
_RANGE_RE = re.compile(r"bytes=(\d{0,18})-(\d{0,18})")
_CHUNK = 64 * 1024


def _byte_range(request: Request, size: int) -> tuple[int, int] | None:
    """The one byte range to serve, as (first, last), or ``None`` for the
    whole file. A malformed range, another unit, several ranges, a number
    longer than 18 digits or an ``If-Range`` (this route sends no validator)
    are ignored — RFC 9110 allows that, and media players only ever ask for
    one range. 416 when the range lies past the end."""
    spec = (request.headers.get("range") or "").replace(" ", "")
    if not spec or "if-range" in request.headers:
        return None
    m = _RANGE_RE.fullmatch(spec)
    if not m or m.group(1) == m.group(2) == "":
        return None
    unsatisfiable = HTTPException(
        status_code=416, detail="Range not satisfiable", headers={"Content-Range": f"bytes */{size}"}
    )
    if m.group(1) == "":  # suffix: the last n bytes
        n = int(m.group(2))
        if n == 0 or size == 0:
            raise unsatisfiable
        return max(0, size - n), size - 1
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) else size - 1
    if m.group(2) and last < first:
        return None  # invalid: ignored
    if first >= size:
        raise unsatisfiable
    return first, min(last, size - 1)


async def _read_from(handle: BinaryIO, offset: int, length: int) -> AsyncIterator[bytes]:
    """The bytes of the OPEN file (``os.pread`` on its descriptor — never a
    second open by name); the handle is closed when the answer ends or the
    client goes away."""
    try:
        fd = handle.fileno()
        while length > 0:
            chunk = await anyio.to_thread.run_sync(os.pread, fd, min(_CHUNK, length), offset)
            if not chunk:
                break
            offset += len(chunk)
            length -= len(chunk)
            yield chunk
    finally:
        handle.close()


def _file_response(
    request: Request, handle: BinaryIO, size: int, media_type: str, headers: dict[str, str]
) -> StreamingResponse:
    try:
        span = _byte_range(request, size)
    except HTTPException:
        handle.close()
        raise
    headers = {**headers, "Accept-Ranges": "bytes"}
    if span is None:
        first, length, status = 0, size, 200
    else:
        first, last = span
        length, status = last - first + 1, 206
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _read_from(handle, first, length), status_code=status, media_type=media_type, headers=headers
    )


# The page's own URLs: every answer passes ``_guard_start``. Joined into
# ``router`` at the end of the module (the route class carries over).
view_router = APIRouter(route_class=_ViewRoute)


# ── Endpoints ──────────────────────────────────────────────────────────────


class ViewUrlBody(BaseModel):
    # A page inside a folder deliverable (relative to its root). Not used for
    # a single-file deliverable.
    subpath: str | None = None


@router.post("/boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}/view-url")
async def mint_deliverable_view_url(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    body: ViewUrlBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """A short-lived URL that shows an HTML deliverable with the files of its
    folder (CSS, images, scripts, other pages) — for the sandboxed preview
    frame. Same auth as the deliverable download; the URL itself opens only
    that folder, read-only, for ``expires_in`` seconds."""
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")
    deliverable = await session.get(TaskDeliverable, deliverable_id)
    if not deliverable or deliverable.task_id != task_id:
        raise HTTPException(status_code=404, detail="Deliverable not found")

    try:
        folder = await _view_folder(deliverable, session)
    except (_NoFolderView, OSError, RuntimeError, ValueError) as exc:
        logger.info("deliverable view refused for %s: %s", deliverable_id, exc)
        raise HTTPException(status_code=404, detail="No page view for this deliverable") from None

    if folder.entry is not None:
        if body.subpath:
            raise HTTPException(status_code=400, detail="A single-file deliverable has no subpath")
        page = folder.entry
    else:
        if not body.subpath:
            raise HTTPException(status_code=400, detail="Which page of the folder? (subpath)")
        try:
            page = _clean_relpath(body.subpath)
        except HTTPException:
            raise HTTPException(status_code=400, detail="Invalid subpath") from None
    if Path(page).suffix.lower() not in _HTML_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only HTML pages have a page view")
    # The page must be what the view will serve: opened the same way (no
    # symlink on the way, a regular file), then closed again.
    try:
        handle, _ = await _open_file(folder, _clean_relpath(page))
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Page not found") from None
        if folder.entry is not None:  # the deliverable's own file is a symlink
            raise HTTPException(status_code=404, detail="No page view for this deliverable") from None
        raise HTTPException(status_code=400, detail="Invalid subpath") from None
    handle.close()

    token = mint_view_token(
        user_id=current_user.id,
        token_version=getattr(current_user, "token_version", 0) or 0,
        task_id=task_id,
        deliverable_id=deliverable_id,
        folder=folder.logical,
    )
    return {
        "url": f"{VIEW_PATH}/{token}/{quote(page, safe='/')}",
        "expires_in": VIEW_TTL_SECONDS,
    }


@view_router.get("/deliverable-view/{token}/{file_path:path}")
async def serve_deliverable_view(
    token: str,
    file_path: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """One file of a deliverable folder, for the sandboxed page view.

    Auth is the signed token in the path — never a cookie, never a header
    (an iframe can send neither). See the module docstring."""
    from app.models.deliverable import TaskDeliverable
    from app.models.user import User

    denied = HTTPException(status_code=403, detail="View link invalid or expired")
    claims = read_view_token(token)
    if claims is None:
        raise denied
    if not _take_request(token, claims):
        raise HTTPException(status_code=429, detail="This page asked for too many files")

    folder = _checked_folder(token)
    if folder is None:
        user = await session.get(User, claims.user_id)
        if not user or not user.is_active or user.token_version != claims.token_version:
            raise denied
        deliverable = await session.get(TaskDeliverable, claims.deliverable_id)
        if not deliverable or deliverable.task_id != claims.task_id:
            raise denied
        try:
            folder = await _view_folder(deliverable, session)
        except (_NoFolderView, OSError, RuntimeError, ValueError):
            raise denied from None
        if not hmac.compare_digest(_folder_tag(folder.logical), claims.folder_tag):
            raise denied
        _grants[token] = (folder, time.time() + GRANT_CACHE_SECONDS)
        # All DB work is done; a large file must not pin a pool connection.
        await release_session(session, route=VIEW_PATH)

    rel = _clean_relpath(file_path)
    handle, size = await _open_file(folder, rel)
    media_type = mimetypes.guess_type(rel.rsplit("/", 1)[-1])[0] or "application/octet-stream"
    return _file_response(request, handle, size, media_type, _view_headers(request, token, claims))


# Anything else under the view prefix is answered here, with the strict
# headers, instead of by the app's bare 404/405 or a slash redirect that
# would repeat the URL: another method on a file URL (405, GET only), and a
# path without a token or without a file part (404). Registered after the
# file route, so a GET of a file never gets here.
_FALLBACK_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


async def _view_not_found(request: Request):
    rest = request.path_params.get("rest", "")
    if request.method != "GET" and "/" in rest:
        raise HTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": "GET"})
    raise HTTPException(status_code=404, detail="Not Found")


view_router.add_api_route(
    "/deliverable-view", _view_not_found, methods=_FALLBACK_METHODS, include_in_schema=False
)
view_router.add_api_route(
    "/deliverable-view/{rest:path}", _view_not_found, methods=_FALLBACK_METHODS, include_in_schema=False
)
router.include_router(view_router)
