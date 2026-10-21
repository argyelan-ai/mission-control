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
  folder must stay inside it (realpath). Files are resolved through
  ``fs_service.safe_join`` (ADR-040's one containment guard): ``..`` and
  symlinks out of the folder are refused, and hidden files (``.env``,
  ``.git``) are never served — also not through a harmless-looking
  symlink to one.
* **Every answer carries the sandbox as a header**, so the page runs in an
  opaque origin even when the URL is opened as its own tab: CSP
  ``sandbox allow-scripts`` (no ``allow-same-origin``), ``nosniff``,
  ``no-referrer``. Its sources are the token's folder URL plus ``https:``
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
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.config import settings
from app.database import get_session, release_session
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


def _resolve_file(folder: _Folder, rel: str) -> Path:
    """``rel`` inside the folder, symlinks followed — 403 when it leaves the
    folder, 404 when what it resolves to is hidden. A harmless name can be a
    symlink to ``.env`` or into ``.git``: the file on disk is what counts."""
    try:
        target = fs_service.safe_join(_folder_root(folder), rel)
    except fs_service.FsAccessError:
        raise HTTPException(status_code=403, detail="Path not allowed") from None
    if any(p.startswith(".") for p in target.relative_to(folder.real).parts):
        raise HTTPException(status_code=404, detail="File not found")
    return target


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
    except _NoFolderView as exc:
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
    try:
        target = _resolve_file(folder, page)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Invalid subpath") from None
    if target.suffix.lower() not in _HTML_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only HTML pages have a page view")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Page not found")

    token = mint_view_token(
        user_id=current_user.id,
        token_version=getattr(current_user, "token_version", 0) or 0,
        task_id=task_id,
        deliverable_id=deliverable_id,
        folder=folder.logical,
    )
    rel = target.relative_to(folder.real).as_posix()
    return {
        "url": f"{VIEW_PATH}/{token}/{quote(rel, safe='/')}",
        "expires_in": VIEW_TTL_SECONDS,
    }


@router.get("/deliverable-view/{token}/{file_path:path}")
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
        except _NoFolderView:
            raise denied from None
        if not hmac.compare_digest(_folder_tag(folder.logical), claims.folder_tag):
            raise denied
        _grants[token] = (folder, time.time() + GRANT_CACHE_SECONDS)
        # All DB work is done; a large file must not pin a pool connection.
        await release_session(session, route=VIEW_PATH)

    target = _resolve_file(folder, _clean_relpath(file_path))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, headers=_view_headers(request, token, claims))
