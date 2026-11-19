"""Multi-file HTML deliverables, served as a page in a sandbox
(routers/deliverable_view.py).

An HTML deliverable that links its own CSS, images or scripts cannot render
from a ``srcdoc`` (the frame has no folder to resolve them against). The
operator mints a short-lived, signed view URL for the deliverable's folder;
the sandboxed frame loads the page and its files from it.

What these tests pin (security review focus):
  - the URL only ever reads inside that one folder: ``..``, encoded ``..``,
    symlinks (out of the folder, inside it, or swapped in between the check
    and the read), hidden files and a task folder that points out of the
    deliverables area are all refused; the bytes come from the one file
    that was opened;
  - the signature is checked: expired, tampered, foreign-task and
    wrong-folder tokens get 403, and so does a token of a user who logged
    out since;
  - every answer carries the sandbox: files get CSP ``sandbox allow-scripts``
    (no ``allow-same-origin``) with sources scoped to the token's folder,
    nosniff, no referrer and a private cache; errors (403/404/405/416/429,
    no token) get ``sandbox; default-src 'none'``, nosniff, no referrer and
    no-store; no cookie is read or set;
  - a folder that changes under a cached check is a 403, never a 500;
  - the view token is never a session credential.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from urllib.parse import quote

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine  # noqa: F401 — shared SQLite engine

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")  # auth_client's admin


# ── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture
def zone(tmp_path, monkeypatch):
    """A stand-in for the backend's /deliverables mount.

    The real resolver (fs_service.resolve_deliverable, slug injection
    included) still runs; only its container prefix ``/deliverables/`` is
    moved under tmp, and the view router's list of deliverable areas points
    there too."""
    from app.routers import deliverable_view
    from app.services import fs_service

    root = tmp_path / "zone"
    root.mkdir()
    real_resolve = fs_service.resolve_deliverable

    async def resolve_in_tmp(deliverable, session, *, target="container"):
        path = await real_resolve(deliverable, session, target=target)
        if path and path.startswith("/deliverables/"):
            return str(root) + path[len("/deliverables"):]
        return path

    monkeypatch.setattr(fs_service, "resolve_deliverable", resolve_in_tmp)
    monkeypatch.setattr(deliverable_view, "ZONE_ROOTS", (str(root),))
    return root


async def _seed(*, path: str, task_id: uuid.UUID | None = None, agent=None):
    """Board + task + deliverable (optionally owned by an agent)."""
    from app.models.board import Board
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task

    board_id = uuid.uuid4()
    task_id = task_id or uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{board_id.hex[:8]}"))
        s.add(Task(id=task_id, board_id=board_id, title="T"))
        if agent is not None:
            s.add(agent)
        s.add(TaskDeliverable(
            id=deliverable_id,
            task_id=task_id,
            agent_id=agent.id if agent is not None else None,
            deliverable_type="file",
            title="page",
            path=path.format(task=task_id),
        ))
        await s.commit()
    return board_id, task_id, deliverable_id


def _write_site(folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="style.css"><img src="logo.png">'
    )
    (folder / "style.css").write_text("body{color:green}")
    (folder / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / "app.js").write_text("document.title='ok'")
    (folder / "data.unknownext").write_bytes(b"\x00\x01")
    (folder / ".env").write_text("SECRET=1")


async def _mint(client: AsyncClient, board_id, task_id, deliverable_id, subpath=None):
    return await client.post(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}/view-url",
        json={"subpath": subpath},
    )


async def _site(auth_client, zone, *, agent=None):
    """A file deliverable /deliverables/<task>/index.html with siblings, and
    its minted view URL. Returns (url, folder_url, ids)."""
    board_id, task_id, deliverable_id = await _seed(
        path="/deliverables/{task}/index.html", agent=agent
    )
    slug_dir = zone / agent.name if agent is not None else zone
    _write_site(slug_dir / str(task_id))
    resp = await _mint(auth_client, board_id, task_id, deliverable_id)
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    return url, url.rsplit("/", 1)[0] + "/", (board_id, task_id, deliverable_id)


def _token_of(url: str) -> str:
    return url.split("/api/v1/deliverable-view/", 1)[1].split("/", 1)[0]


async def _anon_get(client: AsyncClient, url: str, headers: dict | None = None, **kw):
    """GET without the operator's Authorization header — an iframe cannot
    send one, so the view URL must work on its own."""
    # httpx merges per-request headers INTO the client's ones, so the
    # client's Authorization is taken off for this one request.
    saved = client.headers.pop("authorization", None)
    try:
        return await client.get(url, headers=headers, **kw)
    finally:
        if saved is not None:
            client.headers["Authorization"] = saved


# ── minting ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_returns_a_short_lived_url_for_the_page(auth_client, zone):
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/index.html")
    _write_site(zone / str(task_id))

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"].startswith("/api/v1/deliverable-view/")
    assert body["url"].endswith("/index.html")
    assert 0 < body["expires_in"] <= 600


@pytest.mark.asyncio
async def test_mint_needs_a_logged_in_operator(client, zone):
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/index.html")
    _write_site(zone / str(task_id))

    resp = await _mint(client, board_id, task_id, deliverable_id)

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mint_refuses_a_deliverable_of_another_task(auth_client, zone):
    board_id, task_id, _ = await _seed(path="/deliverables/{task}/index.html")
    _, other_task, other_deliverable = await _seed(path="/deliverables/{task}/index.html")
    _write_site(zone / str(other_task))

    resp = await _mint(auth_client, board_id, task_id, other_deliverable)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mint_refuses_a_path_outside_the_task_area(auth_client, zone, tmp_path):
    """A legacy/back-end path (not under /deliverables/<task>/) never gets a
    folder view: its parent folder could be anything on the host."""
    loose = tmp_path / "loose"
    _write_site(loose)
    board_id, task_id, deliverable_id = await _seed(path=str(loose / "index.html"))

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mint_refuses_a_path_in_another_tasks_folder(auth_client, zone):
    """Back-end writers skip the write-time path check: a deliverable of this
    task that points into ANOTHER task's folder gets no folder view."""
    other_task = uuid.uuid4()
    _write_site(zone / str(other_task))
    board_id, task_id, deliverable_id = await _seed(path=f"/deliverables/{other_task}/index.html")

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mint_refuses_a_task_folder_that_points_out_of_the_area(auth_client, zone, tmp_path):
    """The task folder itself is a symlink to somewhere else on the host —
    serving 'its' folder would serve that place."""
    outside = tmp_path / "outside"
    _write_site(outside)
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/index.html")
    os.symlink(outside, zone / str(task_id))

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mint_only_for_html_pages(auth_client, zone):
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/style.css")
    _write_site(zone / str(task_id))

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_mint_for_a_page_inside_a_folder_deliverable(auth_client, zone):
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/site")
    root = zone / str(task_id) / "site"
    _write_site(root / "pages")
    (root / "assets").mkdir()
    (root / "assets" / "shared.css").write_text("h1{color:red}")

    resp = await _mint(auth_client, board_id, task_id, deliverable_id, subpath="pages/index.html")
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.endswith("/pages/index.html")

    # The folder is the deliverable's root: ../assets from pages/ is inside it.
    folder = url[: -len("pages/index.html")]
    shared = await _anon_get(auth_client, folder + "assets/shared.css")
    assert shared.status_code == 200
    assert shared.content == b"h1{color:red}"


@pytest.mark.asyncio
@pytest.mark.parametrize("subpath", ["../../index.html", "/etc/passwd", "pages/../../x.html"])
async def test_mint_refuses_a_subpath_out_of_the_folder(auth_client, zone, subpath):
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/site")
    _write_site(zone / str(task_id) / "site" / "pages")

    resp = await _mint(auth_client, board_id, task_id, deliverable_id, subpath=subpath)

    assert resp.status_code == 400


# ── serving ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_page_and_its_files_are_served_with_the_right_types(auth_client, zone):
    url, folder, _ = await _site(auth_client, zone)

    expected = {
        "index.html": "text/html",
        "style.css": "text/css",
        "logo.png": "image/png",
        "app.js": "javascript",
        "data.unknownext": "application/octet-stream",
    }
    for name, ctype in expected.items():
        resp = await _anon_get(auth_client, folder + name)
        assert resp.status_code == 200, (name, resp.text)
        assert ctype in resp.headers["content-type"], (name, resp.headers["content-type"])


@pytest.mark.asyncio
async def test_every_answer_carries_the_sandbox_headers(auth_client, zone):
    url, folder, _ = await _site(auth_client, zone)
    token = _token_of(url)

    for name in ("index.html", "style.css", "logo.png"):
        resp = await _anon_get(auth_client, folder + name)
        csp = resp.headers["content-security-policy"]
        directives = [d.strip() for d in csp.split(";")]
        # The sandbox: scripts run, but in an opaque origin — no MC storage,
        # cookies or DOM, no top navigation, no popups, no forms.
        assert "sandbox allow-scripts" in directives, csp
        assert "allow-same-origin" not in csp
        assert "allow-top-navigation" not in csp
        assert "allow-popups" not in csp
        assert "allow-forms" not in csp
        assert "connect-src 'none'" in directives
        assert "form-action 'none'" in directives
        assert "frame-ancestors 'self'" in directives
        # Files come from THIS folder only, never from the rest of MC.
        # ('self' would be the whole MC origin — and WebKit does not match
        # 'self' inside a sandboxed page at all.)
        assert "'self'" not in csp.replace("frame-ancestors 'self'", "")
        assert f"http://test/api/v1/deliverable-view/{token}/" in csp
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert "set-cookie" not in resp.headers


@pytest.mark.asyncio
async def test_answers_are_cached_privately_for_the_tokens_lifetime_at_most(auth_client, zone):
    """The URL is unique per mint, so the operator's browser may keep a file
    while the token lives (a page that asks for the same image again gets it
    from the cache, not from MC). Never a shared cache."""
    from app.routers import deliverable_view

    url, folder, _ = await _site(auth_client, zone)

    resp = await _anon_get(auth_client, folder + "style.css")

    cache = [p.strip() for p in resp.headers["cache-control"].split(",")]
    assert "private" in cache, cache
    assert "public" not in cache
    max_age = next(int(p.split("=", 1)[1]) for p in cache if p.startswith("max-age="))
    assert 0 < max_age <= deliverable_view.VIEW_TTL_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["app.js", "style.css", "font.woff2", "index.html"])
async def test_cors_requests_of_the_sandboxed_page_are_allowed(auth_client, zone, name):
    """The page runs in an opaque origin, so a module script, a
    ``<link crossorigin>`` stylesheet and an @font-face font are CORS
    requests with ``Origin: null``. Without Access-Control-Allow-Origin the
    browser drops them (measured: Chromium and WebKit). The token in the
    path is the capability; no credentials are ever involved."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    (zone / str(task_id) / "font.woff2").write_bytes(b"wOF2")

    resp = await _anon_get(
        auth_client, folder + name, headers={"Origin": "null", "Sec-Fetch-Mode": "cors"}
    )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


# ── a page that floods ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_page_has_a_request_budget_per_token(auth_client, zone, monkeypatch):
    """A page in a loop (``new Image().src = 'a.png?' + n``) runs out of ITS
    requests, refused with 429 — a fresh preview (new mint) works again."""
    from app.routers import deliverable_view

    monkeypatch.setattr(deliverable_view, "VIEW_MAX_REQUESTS", 5)
    url, folder, (board_id, task_id, deliverable_id) = await _site(auth_client, zone)

    statuses = [(await _anon_get(auth_client, folder + f"logo.png?{n}")).status_code for n in range(8)]

    assert statuses[:5] == [200] * 5, statuses
    assert statuses[5:] == [429] * 3, statuses
    fresh = (await _mint(auth_client, board_id, task_id, deliverable_id)).json()["url"]
    assert (await _anon_get(auth_client, fresh)).status_code == 200


@pytest.mark.asyncio
async def test_a_bad_token_does_not_use_up_a_budget(auth_client, zone, monkeypatch):
    """Only a correctly signed token is counted — garbage cannot fill memory
    or block anyone."""
    from app.routers import deliverable_view

    monkeypatch.setattr(deliverable_view, "VIEW_MAX_REQUESTS", 2)
    url, folder, _ = await _site(auth_client, zone)
    for _ in range(5):
        assert (await _anon_get(auth_client, "/api/v1/deliverable-view/garbage/x.png")).status_code == 403

    assert (await _anon_get(auth_client, folder + "logo.png")).status_code == 200
    assert "garbage" not in deliverable_view._budgets


@pytest.mark.asyncio
async def test_repeated_requests_reuse_the_checked_folder(auth_client, zone, monkeypatch):
    """The DB and folder checks run once per token and short window, not per
    file: 1500 asset requests from one page were 4500 DB reads."""
    from app.routers import deliverable_view

    url, folder, _ = await _site(auth_client, zone)
    calls = []
    real = deliverable_view._view_folder

    async def counting(deliverable, session):
        calls.append(1)
        return await real(deliverable, session)

    monkeypatch.setattr(deliverable_view, "_view_folder", counting)

    for name in ("index.html", "style.css", "logo.png", "app.js", "style.css"):
        assert (await _anon_get(auth_client, folder + name)).status_code == 200
    assert len(calls) == 1, len(calls)

    # After the short window the checks run again (logout, moved folder).
    later = time.time() + deliverable_view.GRANT_CACHE_SECONDS + 1
    monkeypatch.setattr(deliverable_view.time, "time", lambda: later)
    assert (await _anon_get(auth_client, folder + "logo.png")).status_code == 200
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_logout_ends_the_view_within_the_check_window(auth_client, zone, session, monkeypatch):
    from app.models.user import User
    from app.routers import deliverable_view

    url, folder, _ = await _site(auth_client, zone)
    assert (await _anon_get(auth_client, folder + "logo.png")).status_code == 200
    user = await session.get(User, USER_ID)
    user.token_version += 1
    session.add(user)
    await session.commit()

    later = time.time() + deliverable_view.GRANT_CACHE_SECONDS + 1
    monkeypatch.setattr(deliverable_view.time, "time", lambda: later)

    assert (await _anon_get(auth_client, folder + "logo.png")).status_code == 403


@pytest.mark.asyncio
async def test_flooding_the_view_does_not_rate_limit_the_rest_of_mc(auth_client, zone):
    """MC's per-IP limit is counted per path (slowapi key_style "url"), so a
    page that hammers its own files never puts MC's UI routes over budget."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    import app.main as main_module

    url, folder, _ = await _site(auth_client, zone)
    original = main_module.app.state.limiter
    main_module.app.state.limiter = Limiter(key_func=get_remote_address, default_limits=["3/minute"])
    try:
        flood = [(await _anon_get(auth_client, folder + f"logo.png?{n}")).status_code for n in range(6)]
        ui = await auth_client.get("/api/v1/auth/me")
    finally:
        main_module.app.state.limiter = original

    assert 429 in flood, flood  # the limiter is live in this test
    assert ui.status_code == 200, ui.status_code


@pytest.mark.asyncio
async def test_csp_follows_the_host_the_browser_used(auth_client, zone):
    """Behind a dev proxy (Next rewrites) the browser's host arrives as
    X-Forwarded-Host. Only a well-formed host gets into the header — a value
    that tries to add CSP directives is dropped."""
    url, folder, _ = await _site(auth_client, zone)
    token = _token_of(url)

    proxied = await _anon_get(auth_client, folder + "index.html", headers={"X-Forwarded-Host": "localhost:3001"})
    assert f"http://localhost:3001/api/v1/deliverable-view/{token}/" in proxied.headers["content-security-policy"]

    injected = await _anon_get(
        auth_client, folder + "index.html", headers={"X-Forwarded-Host": "evil.example; script-src *"}
    )
    csp = injected.headers["content-security-policy"]
    assert "evil.example" not in csp
    assert "script-src *" not in csp


@pytest.mark.asyncio
async def test_folder_of_an_agent_with_a_slug(auth_client, zone):
    """cli-bridge agents write /deliverables/<task>/…, the files live under
    <slug>/<task>/ — the real resolver's mapping is what gets served."""
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name="pagebot", role="dev", agent_runtime="cli-bridge")
    url, folder, _ = await _site(auth_client, zone, agent=agent)

    resp = await _anon_get(auth_client, folder + "style.css")

    assert resp.status_code == 200
    assert resp.content == b"body{color:green}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "escape",
    [
        "%2e%2e/%2e%2e/secret.txt",
        "..%2F..%2Fsecret.txt",
        "%2e%2e%2f%2e%2e%2fsecret.txt",
        "sub/%2e%2e/%2e%2e/%2e%2e/secret.txt",
        "..%5C..%5Csecret.txt",
        "%2Fetc%2Fpasswd",
    ],
)
async def test_dot_dot_out_of_the_folder_is_refused(auth_client, zone, escape):
    (zone / "secret.txt").write_text("other task")
    (zone.parent / "secret.txt").write_text("host file")
    url, folder, _ = await _site(auth_client, zone)
    (zone.parent / "x").mkdir(exist_ok=True)

    resp = await _anon_get(auth_client, folder + escape)

    assert resp.status_code in (403, 404), resp.status_code
    assert b"other task" not in resp.content
    assert b"host file" not in resp.content


@pytest.mark.asyncio
async def test_symlink_out_of_the_folder_is_refused(auth_client, zone, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host file")
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    os.symlink(secret, zone / str(task_id) / "link.txt")

    resp = await _anon_get(auth_client, folder + "link.txt")

    assert resp.status_code == 403
    assert b"host file" not in resp.content


@pytest.mark.asyncio
async def test_hidden_files_are_not_served(auth_client, zone):
    url, folder, _ = await _site(auth_client, zone)

    resp = await _anon_get(auth_client, folder + ".env")

    assert resp.status_code == 404
    assert b"SECRET" not in resp.content


@pytest.mark.asyncio
@pytest.mark.parametrize("alias,target", [("env.txt", ".env"), ("cfg", ".git/config"), ("repo", ".git")])
async def test_hidden_files_behind_a_symlink_alias_are_not_served(auth_client, zone, alias, target):
    """The name in the URL is harmless (``env.txt``), the file it resolves to
    is hidden. The folder view serves no symlink at all, so the alias is
    refused before anything is read."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    (site / ".git").mkdir()
    (site / ".git" / "config").write_text("[remote] url=SECRET")
    os.symlink(site / target, site / alias)

    request = alias if alias != "repo" else "repo/config"
    resp = await _anon_get(auth_client, folder + request)

    assert resp.status_code == 403, resp.status_code
    assert b"SECRET" not in resp.content


@pytest.mark.asyncio
async def test_a_task_folder_symlinked_to_another_tasks_folder_is_refused(auth_client, zone):
    """Task A's folder is a symlink to task B's folder in the same mount: A's
    view URL must not serve B's files (one agent presenting another agent's
    output as its own deliverable)."""
    _, other_task, _ = await _seed(path="/deliverables/{task}/index.html")
    _write_site(zone / str(other_task))
    (zone / str(other_task) / "secret.txt").write_text("OTHER TASK DATA")
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/index.html")
    os.symlink(zone / str(other_task), zone / str(task_id))

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)
    assert resp.status_code == 404, resp.text

    # A correctly signed token for that folder does not open it either.
    from app.routers import deliverable_view

    forged = deliverable_view.mint_view_token(
        user_id=USER_ID, token_version=0, task_id=task_id,
        deliverable_id=deliverable_id, folder=f"/deliverables/{task_id}",
    )
    served = await _anon_get(auth_client, f"/api/v1/deliverable-view/{forged}/secret.txt")
    assert served.status_code == 403
    assert b"OTHER TASK" not in served.content


@pytest.mark.asyncio
async def test_a_folder_deliverable_symlinked_to_another_tasks_folder_is_refused(auth_client, zone):
    """Same, one level down: /deliverables/<A>/site → <B>/."""
    _, other_task, _ = await _seed(path="/deliverables/{task}/index.html")
    _write_site(zone / str(other_task))
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/site")
    (zone / str(task_id)).mkdir()
    os.symlink(zone / str(other_task), zone / str(task_id) / "site")

    resp = await _mint(auth_client, board_id, task_id, deliverable_id, subpath="index.html")

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_a_slug_agents_task_folder_symlinked_to_another_task_is_refused(auth_client, zone):
    """cli-bridge layout (<slug>/<task>): the task folder under the slug is a
    symlink to another task's folder."""
    from app.models.agent import Agent

    _, other_task, _ = await _seed(path="/deliverables/{task}/index.html")
    _write_site(zone / str(other_task))
    agent = Agent(id=uuid.uuid4(), name="pagebot", role="dev", agent_runtime="cli-bridge")
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/index.html", agent=agent)
    (zone / "pagebot").mkdir()
    os.symlink(zone / str(other_task), zone / "pagebot" / str(task_id))

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_missing_file_is_404(auth_client, zone):
    url, folder, _ = await _site(auth_client, zone)

    resp = await _anon_get(auth_client, folder + "nope.css")

    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_only_get(auth_client, zone, method):
    url, folder, _ = await _site(auth_client, zone)

    resp = await auth_client.request(method, folder + "index.html")

    assert resp.status_code == 405


# ── the signature ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_token_is_403(auth_client, zone, monkeypatch):
    from app.routers import deliverable_view

    url, folder, _ = await _site(auth_client, zone)
    later = time.time() + deliverable_view.VIEW_TTL_SECONDS + 1
    monkeypatch.setattr(deliverable_view.time, "time", lambda: later)

    resp = await _anon_get(auth_client, folder + "index.html")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_tampered_or_garbage_token_is_403(auth_client, zone):
    url, folder, _ = await _site(auth_client, zone)
    token = _token_of(url)
    prefix, body, sig = token.split(".")
    flipped = sig[:-2] + ("A" if sig[-2] != "A" else "B") + sig[-1]

    for bad in (
        f"{prefix}.{body}.{flipped}",          # signature changed
        f"{prefix}.{body[:-4]}AAAA.{sig}",      # claims changed
        f"{prefix}.{body}",                      # signature cut off
        "garbage",
        "x" * 2000,
    ):
        resp = await _anon_get(auth_client, f"/api/v1/deliverable-view/{bad}/index.html")
        assert resp.status_code == 403, bad


@pytest.mark.asyncio
async def test_token_for_another_task_is_403(auth_client, zone):
    """Correctly signed, but its task is not the deliverable's task."""
    from app.routers import deliverable_view

    _, _, (_, task_id, deliverable_id) = await _site(auth_client, zone)
    forged = deliverable_view.mint_view_token(
        user_id=USER_ID,
        token_version=0,
        task_id=uuid.uuid4(),
        deliverable_id=deliverable_id,
        folder=f"/deliverables/{task_id}",
    )

    resp = await _anon_get(auth_client, f"/api/v1/deliverable-view/{forged}/index.html")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_token_for_another_folder_is_403(auth_client, zone):
    """Correctly signed for this deliverable, but bound to another folder
    (e.g. the deliverable was moved after the URL was minted)."""
    from app.routers import deliverable_view

    _, _, (_, task_id, deliverable_id) = await _site(auth_client, zone)
    forged = deliverable_view.mint_view_token(
        user_id=USER_ID,
        token_version=0,
        task_id=task_id,
        deliverable_id=deliverable_id,
        folder=f"/deliverables/{task_id}/elsewhere",
    )

    resp = await _anon_get(auth_client, f"/api/v1/deliverable-view/{forged}/index.html")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_token_dies_when_the_operator_logs_out(auth_client, zone, session):
    from app.models.user import User

    url, folder, _ = await _site(auth_client, zone)
    user = await session.get(User, USER_ID)
    user.token_version += 1
    session.add(user)
    await session.commit()

    resp = await _anon_get(auth_client, folder + "index.html")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_session_cookie_is_not_a_credential_here(auth_client, zone):
    """The view endpoint never reads cookies: a valid login cookie with a
    bad token still gets 403."""
    from app.auth import create_access_token

    await _site(auth_client, zone)
    jwt = create_access_token(str(USER_ID), "admin")

    resp = await _anon_get(
        auth_client,
        "/api/v1/deliverable-view/garbage/index.html",
        cookies={"mc_sse_token": jwt},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_view_token_is_never_a_session_token(auth_client, zone):
    """The token sits in the page's own URL, so the page can read it. It must
    open nothing but its folder: not a REST route, not a JWT."""
    from jose import JWTError, jwt as jose_jwt

    from app.config import settings

    url, folder, _ = await _site(auth_client, zone)
    token = _token_of(url)

    with pytest.raises(JWTError):
        jose_jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
    resp = await auth_client.get(
        "/api/v1/boards", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_urls_with_spaces_and_unicode_round_trip(auth_client, zone):
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/my site/Über uns.html")
    folder = zone / str(task_id) / "my site"
    folder.mkdir(parents=True)
    (folder / "Über uns.html").write_text("<p>hallo</p>")

    resp = await _mint(auth_client, board_id, task_id, deliverable_id)
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.endswith("/" + quote("Über uns.html"))

    page = await _anon_get(auth_client, url)
    assert page.status_code == 200
    # The page itself, behind MC's preview guard (no doctype: it goes first).
    assert page.content.endswith("<p>hallo</p>".encode())
    assert page.content.startswith(b"<script>")


# ── hardening (security review 2026-10-21) ─────────────────────────────────
#
# The page is untrusted and can read its own URL, token included, so this
# route is reachable by whatever the page (or anyone it leaks the URL to)
# asks for. Four things pinned here:
#   1. a file is opened ONCE, with no symlink anywhere on the way, and served
#      from that open handle — a symlink swapped in between the check and the
#      read is refused (TOCTOU), and the folder view serves no symlinks;
#   2. every answer of the route carries the protective headers, errors too;
#   3. a folder that changes under a cached check is a 403, never a 500;
#   4. (test_log_redaction.py) the token never lands in a log line.

ERROR_CSP = "sandbox; default-src 'none'; frame-ancestors 'self'"


def _assert_error_headers(resp):
    """The strict set every non-file answer carries: a sandbox with nothing
    allowed (an error page never runs a script), nosniff, no referrer, never
    stored."""
    csp = resp.headers.get("content-security-policy", "")
    directives = [d.strip() for d in csp.split(";")]
    assert "sandbox" in directives, (resp.status_code, csp)
    assert "default-src 'none'" in directives, (resp.status_code, csp)
    assert "allow-scripts" not in csp, (resp.status_code, csp)
    assert resp.headers.get("x-content-type-options") == "nosniff", resp.status_code
    assert resp.headers.get("referrer-policy") == "no-referrer", resp.status_code
    assert resp.headers.get("cache-control") == "no-store", resp.status_code
    assert "set-cookie" not in resp.headers


def _assert_file_headers(resp, token):
    csp = resp.headers["content-security-policy"]
    assert "sandbox allow-scripts" in [d.strip() for d in csp.split(";")], csp
    assert f"/api/v1/deliverable-view/{token}/" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"].startswith("private, max-age=")


@pytest.mark.asyncio
async def test_a_file_swapped_for_a_symlink_after_the_check_is_not_followed(auth_client, zone, tmp_path, monkeypatch):
    """TOCTOU: the containment check passes on a real file, then the file is
    replaced by a symlink to a host file before it is read. The route must
    not read by name a second time — it answers 403 and serves nothing of the
    host file."""
    from app.services import fs_service

    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST FILE CONTENT")
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    real_check = fs_service.safe_join
    swapped = []

    def check_then_swap(root, subpath):
        result = real_check(root, subpath)
        if subpath == "style.css" and not swapped:
            (site / "style.css").unlink()
            os.symlink(secret, site / "style.css")
            swapped.append(1)
        return result

    monkeypatch.setattr(fs_service, "safe_join", check_then_swap)

    resp = await _anon_get(auth_client, folder + "style.css")

    assert swapped, "the check ran"
    assert resp.status_code == 403, resp.status_code
    assert b"HOST FILE" not in resp.content
    _assert_error_headers(resp)


@pytest.mark.asyncio
async def test_a_folder_swapped_for_a_symlink_after_the_check_is_not_followed(auth_client, zone, tmp_path, monkeypatch):
    """Same race one level up: ``assets/`` is a real folder at check time and
    a symlink to a host folder at read time."""
    from app.services import fs_service

    outside = tmp_path / "host-dir"
    outside.mkdir()
    (outside / "x.css").write_text("HOST DIR CONTENT")
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    (site / "assets").mkdir()
    (site / "assets" / "x.css").write_text("own file")
    real_check = fs_service.safe_join

    def check_then_swap(root, subpath):
        result = real_check(root, subpath)
        if subpath == "assets/x.css" and (site / "assets").is_dir() and not (site / "assets").is_symlink():
            (site / "assets").rename(site / "assets-old")
            os.symlink(outside, site / "assets")
        return result

    monkeypatch.setattr(fs_service, "safe_join", check_then_swap)

    resp = await _anon_get(auth_client, folder + "assets/x.css")

    assert resp.status_code == 403, resp.status_code
    assert b"HOST DIR" not in resp.content


@pytest.mark.asyncio
@pytest.mark.parametrize("make,request_path", [
    (lambda site: os.symlink(site / "style.css", site / "alias.css"), "alias.css"),
    (lambda site: os.symlink("style.css", site / "rel-alias.css"), "rel-alias.css"),
    (lambda site: os.symlink(site / "img", site / "pics"), "pics/a.png"),
])
async def test_symlinks_inside_the_folder_are_refused_too(auth_client, zone, make, request_path):
    """A deliverable site needs no symlinks, and a symlink is the one thing a
    page's folder could use to reach elsewhere: the folder view refuses every
    one, also a harmless-looking alias to a sibling."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    (site / "img").mkdir()
    (site / "img" / "a.png").write_bytes(b"\x89PNG")
    make(site)

    resp = await _anon_get(auth_client, folder + request_path)

    assert resp.status_code == 403, resp.status_code
    _assert_error_headers(resp)
    # The real files themselves are still served.
    assert (await _anon_get(auth_client, folder + "style.css")).status_code == 200
    assert (await _anon_get(auth_client, folder + "img/a.png")).status_code == 200


@pytest.mark.asyncio
async def test_a_symlink_loop_is_a_403_not_a_crash(auth_client, zone):
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    os.symlink("loop.css", site / "loop.css")

    resp = await _anon_get(auth_client, folder + "loop.css")

    assert resp.status_code == 403, resp.status_code


@pytest.mark.asyncio
async def test_a_fifo_or_folder_is_not_served(auth_client, zone):
    """Only regular files: a folder is a 404, and a FIFO (which would block a
    reader until someone writes to it) is refused without waiting."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    (site / "sub").mkdir()
    os.mkfifo(site / "pipe.css")

    assert (await _anon_get(auth_client, folder + "sub")).status_code == 404
    resp = await _anon_get(auth_client, folder + "pipe.css")
    assert resp.status_code == 404, resp.status_code
    _assert_error_headers(resp)


@pytest.mark.asyncio
async def test_the_task_folder_replaced_by_a_symlink_under_a_cached_check_is_403(auth_client, zone, tmp_path):
    """The first request checks the folder and caches the check. Then the
    task folder is moved away and a symlink to a host folder (with the same
    file names) takes its place. The next request inside the cache window
    must refuse with 403 — not crash with a 500, and not serve the host
    folder."""
    outside = tmp_path / "host-site"
    outside.mkdir()
    (outside / "style.css").write_text("HOST SITE CONTENT")
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    assert (await _anon_get(auth_client, folder + "style.css")).status_code == 200

    site.rename(tmp_path / "moved-away")
    os.symlink(outside, site)

    resp = await _anon_get(auth_client, folder + "style.css")

    assert resp.status_code == 403, resp.status_code
    assert b"HOST SITE" not in resp.content
    _assert_error_headers(resp)


@pytest.mark.asyncio
async def test_unexpected_os_errors_on_the_way_to_the_file_are_403(auth_client, zone, monkeypatch):
    """Whatever the filesystem throws (a folder vanishing mid-walk, EIO on a
    network mount): a refusal with the strict headers, never a 500."""
    import errno

    from app.routers import deliverable_view

    url, folder, _ = await _site(auth_client, zone)
    real_open = deliverable_view.os.open

    def failing_open(path, flags, *args, **kwargs):
        if path == "style.css":
            raise OSError(errno.EIO, "I/O error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(deliverable_view.os, "open", failing_open)

    resp = await _anon_get(auth_client, folder + "style.css")

    assert resp.status_code == 403, resp.status_code
    _assert_error_headers(resp)


@pytest.mark.asyncio
async def test_error_answers_carry_the_protective_headers(auth_client, zone, monkeypatch):
    """Not only the files: every answer under /api/v1/deliverable-view/ —
    bad token, ``..``, missing, hidden, over budget, wrong method, no token
    at all — carries a sandbox with nothing allowed, nosniff, no referrer and
    no-store. A future error page that echoes input could otherwise run as a
    page of MC's origin when opened as its own tab."""
    from app.routers import deliverable_view

    url, folder, _ = await _site(auth_client, zone)

    cases = [
        ("GET", "/api/v1/deliverable-view/garbage/x.png", 403),
        ("GET", folder + "%2e%2e/secret.txt", 403),
        ("GET", folder + "nope.css", 404),
        ("GET", folder + ".env", 404),
        ("GET", folder, 403),  # empty file path
        ("HEAD", folder + "index.html", 405),
        ("OPTIONS", folder + "index.html", 405),
        ("POST", folder + "index.html", 405),
        ("DELETE", folder + "index.html", 405),
        ("GET", "/api/v1/deliverable-view/", 404),
        ("GET", "/api/v1/deliverable-view", 404),
        ("GET", "/api/v1/deliverable-view/" + _token_of(url), 404),
        ("POST", "/api/v1/deliverable-view/x", 404),
    ]
    for method, path, status in cases:
        saved = auth_client.headers.pop("authorization", None)
        try:
            resp = await auth_client.request(method, path)
        finally:
            if saved is not None:
                auth_client.headers["Authorization"] = saved
        assert resp.status_code == status, (method, path, resp.status_code, resp.text)
        _assert_error_headers(resp)
        assert "location" not in resp.headers, (method, path)  # no redirect that repeats the token

    # Over budget (429).
    monkeypatch.setattr(deliverable_view, "VIEW_MAX_REQUESTS", 1)
    url2, folder2, _ = await _site(auth_client, zone)
    assert (await _anon_get(auth_client, folder2 + "logo.png")).status_code == 200
    over = await _anon_get(auth_client, folder2 + "logo.png")
    assert over.status_code == 429
    _assert_error_headers(over)


@pytest.mark.asyncio
async def test_a_byte_range_is_served_from_the_open_file_with_the_headers(auth_client, zone):
    """Media (``<video>``, ``<audio>``) asks for byte ranges — Safari plays
    nothing without them. Each part comes with the page's headers."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    token = _token_of(url)
    (zone / str(task_id) / "clip.mp4").write_bytes(bytes(range(10)))

    full = await _anon_get(auth_client, folder + "clip.mp4")
    assert full.status_code == 200
    assert full.content == bytes(range(10))
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == "10"
    _assert_file_headers(full, token)

    for spec, body, content_range in [
        ("bytes=0-3", bytes(range(4)), "bytes 0-3/10"),
        ("bytes=7-", bytes([7, 8, 9]), "bytes 7-9/10"),
        ("bytes=-2", bytes([8, 9]), "bytes 8-9/10"),
        ("bytes=8-99", bytes([8, 9]), "bytes 8-9/10"),
    ]:
        part = await _anon_get(auth_client, folder + "clip.mp4", headers={"Range": spec})
        assert part.status_code == 206, (spec, part.status_code)
        assert part.content == body, spec
        assert part.headers["content-range"] == content_range, spec
        assert part.headers["content-length"] == str(len(body)), spec
        _assert_file_headers(part, token)


@pytest.mark.asyncio
async def test_an_unsatisfiable_range_is_416_with_the_headers(auth_client, zone):
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    (zone / str(task_id) / "clip.mp4").write_bytes(bytes(range(10)))

    for spec in ("bytes=999-", "bytes=10-", "bytes=-0"):
        resp = await _anon_get(auth_client, folder + "clip.mp4", headers={"Range": spec})
        assert resp.status_code == 416, (spec, resp.status_code)
        assert resp.headers["content-range"] == "bytes */10", spec
        assert bytes(range(10)) not in resp.content
        _assert_error_headers(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec",
    [
        "bytes=abc",
        "bytes=5-2",
        "items=0-1",
        "bytes=0-1,4-5",
        "bytes=-",
        # Numbers no file size needs. Python's int() refuses more than 4300
        # digits (ValueError), which used to end in a 500 and an ERROR
        # traceback per request.
        pytest.param("bytes=" + "1" * 5000 + "-", id="huge-first"),
        pytest.param("bytes=0-" + "9" * 5000, id="huge-last"),
        pytest.param("bytes=-" + "9" * 5000, id="huge-suffix"),
    ],
)
async def test_a_range_the_route_does_not_serve_gets_the_whole_file(auth_client, zone, spec, caplog):
    """A malformed range, another unit, several ranges or a number longer
    than any file size are ignored (RFC 9110 allows it): the whole file, 200,
    with the page's headers — never a 400 without them, never a 500."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    token = _token_of(url)
    (zone / str(task_id) / "clip.mp4").write_bytes(bytes(range(10)))

    with caplog.at_level(logging.ERROR, logger="app.routers.deliverable_view"):
        resp = await _anon_get(auth_client, folder + "clip.mp4", headers={"Range": spec})

    assert resp.status_code == 200, (spec[:40], resp.status_code)
    assert resp.content == bytes(range(10))
    _assert_file_headers(resp, token)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_if_range_gets_the_whole_file(auth_client, zone):
    """The route sends no validator (no ETag, no Last-Modified), so a client
    that asks "range only if unchanged" gets the whole current file."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    (zone / str(task_id) / "clip.mp4").write_bytes(bytes(range(10)))

    resp = await _anon_get(
        auth_client, folder + "clip.mp4", headers={"Range": "bytes=0-1", "If-Range": '"abc"'}
    )

    assert resp.status_code == 200
    assert resp.content == bytes(range(10))


@pytest.mark.asyncio
async def test_mint_refuses_a_page_that_is_a_symlink(auth_client, zone):
    """The page itself must be servable: a symlinked page gets no view URL
    (the view would refuse it anyway)."""
    board_id, task_id, deliverable_id = await _seed(path="/deliverables/{task}/site")
    site = zone / str(task_id) / "site"
    _write_site(site)
    os.symlink(site / "index.html", site / "home.html")

    resp = await _mint(auth_client, board_id, task_id, deliverable_id, subpath="home.html")
    assert resp.status_code == 400, resp.text
    ok = await _mint(auth_client, board_id, task_id, deliverable_id, subpath="index.html")
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_the_bytes_come_from_the_opened_file_not_a_second_lookup(auth_client, zone, tmp_path, monkeypatch):
    """The other half of the race: the file is opened (no symlink), and right
    after that the name is swapped for a symlink to a host file. What is sent
    must be the bytes of the file that was opened — the route never looks
    the name up a second time to read it."""
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST FILE CONTENT")
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    real_open = os.open
    swapped = []

    def open_then_swap(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == "style.css" and flags & os.O_NOFOLLOW and not swapped:
            (site / "style.css").rename(site / "style-old.css")
            os.symlink(secret, site / "style.css")
            swapped.append(1)
        return fd

    monkeypatch.setattr(os, "open", open_then_swap)

    resp = await _anon_get(auth_client, folder + "style.css")

    assert swapped, "the file was opened with O_NOFOLLOW"
    assert resp.status_code == 200
    assert resp.content == b"body{color:green}"
    assert b"HOST FILE" not in resp.content


@pytest.mark.asyncio
async def test_an_unexpected_error_is_a_500_with_the_strict_headers_and_no_token_in_the_log(
    auth_client, zone, monkeypatch, caplog
):
    """A bug after the checks (here: building the headers fails) still answers
    with the strict headers, and the logged traceback carries no token."""
    import logging

    from app.routers import deliverable_view

    url, folder, _ = await _site(auth_client, zone)
    token = _token_of(url)

    def broken(request, tok, claims):
        raise RuntimeError(f"boom for {request.url.path}")  # the path holds the token

    monkeypatch.setattr(deliverable_view, "_view_headers", broken)

    with caplog.at_level(logging.ERROR, logger="app.routers.deliverable_view"):
        resp = await _anon_get(auth_client, folder + "style.css")

    assert resp.status_code == 500
    _assert_error_headers(resp)
    # caplog.text is what a handler writes: message AND any exc_info
    # traceback (which no log filter ever sees).
    assert "boom for" in caplog.text, caplog.text  # the traceback is logged ...
    assert token not in caplog.text  # ... without the token


# ── storage stand-ins for the page (round-1 user test t4 M1, 2026-10-23) ───
#
# The page lives in an opaque origin (sandbox without allow-same-origin), so
# reading localStorage / sessionStorage / document.cookie throws a
# SecurityError. A game that reads its high score on the first line of its
# script then never binds its START button: "Run page" showed a dead page.
# Every HTML page this route serves starts with MC's preview guard (the same
# script the srcdoc preview puts in its guard head) — after a leading
# doctype, so the page keeps standards mode.


def _guard_tag() -> bytes:
    from app.routers import deliverable_view

    return ("<script>" + deliverable_view.PREVIEW_GUARD_SCRIPT + "</script>").encode()


def test_the_guard_script_defines_storage_stand_ins_and_reports_errors():
    from app.routers import deliverable_view

    script = deliverable_view.PREVIEW_GUARD_SCRIPT
    assert "localStorage" in script and "sessionStorage" in script
    assert '"cookie"' in script
    assert 'mcPreview: "page-error"' in script
    # Only thrown errors count. A rejected promise is no broken page: a start
    # sound before the first tap or a fetch (connect-src 'none') reject in
    # pages that run fine (review of #953).
    assert "unhandledrejection" not in script
    # It sits inside a <script> element: it must never close it early.
    assert "</script" not in script.lower()


@pytest.mark.asyncio
async def test_an_html_page_starts_with_the_guard_after_its_doctype(auth_client, zone):
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    token = _token_of(url)
    page = (zone / str(task_id) / "index.html").read_bytes()

    resp = await _anon_get(auth_client, folder + "index.html")

    assert resp.status_code == 200
    doctype = b"<!doctype html>"
    assert resp.content == doctype + _guard_tag() + page[len(doctype):]
    assert resp.headers["content-length"] == str(len(resp.content))
    assert "text/html" in resp.headers["content-type"]
    _assert_file_headers(resp, token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page", "at"),
    [
        (b"<p>no doctype</p>", 0),
        (b"\xef\xbb\xbf<!DOCTYPE html><p>x", len(b"\xef\xbb\xbf<!DOCTYPE html>")),
        (b"  \n<!-- made by an agent -->\n<!DocType html>\n<p>x", len(b"  \n<!-- made by an agent -->\n<!DocType html>")),
        (b'<?xml version="1.0"?><!doctype html><p>x', len(b'<?xml version="1.0"?><!doctype html>')),
        (b"<!--><!doctype html><p>x", len(b"<!--><!doctype html>")),
        (b"<!-- never closed <!doctype html>", 0),
        (b"<script>run()</script><!doctype html>", 0),
        (b"<!doctype html", 0),
    ],
)
async def test_where_the_guard_goes(auth_client, zone, page, at):
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    (zone / str(task_id) / "page.html").write_bytes(page)

    resp = await _anon_get(auth_client, folder + "page.html")

    assert resp.status_code == 200
    assert resp.content == page[:at] + _guard_tag() + page[at:]


@pytest.mark.asyncio
async def test_the_guard_scan_is_linear_on_a_hostile_prologue(auth_client, zone):
    import time as _time

    from app.routers import deliverable_view

    evil = b"<!---->" * 20_000 + b" " * 200_000 + b"<!-- x" * 2_000 + b"y"
    t0 = _time.perf_counter()
    assert deliverable_view.guard_insert_at(evil) == 0
    assert _time.perf_counter() - t0 < 0.5


@pytest.mark.asyncio
async def test_other_files_and_ranges_of_a_page_are_left_alone(auth_client, zone):
    """CSS, scripts and images come back byte for byte; a page asked for in
    parts gets the whole guarded page (the guard shifts every byte)."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)

    css = await _anon_get(auth_client, folder + "style.css")
    assert css.content == b"body{color:green}"
    js = await _anon_get(auth_client, folder + "app.js")
    assert js.content == b"document.title='ok'"

    ranged = await _anon_get(auth_client, folder + "index.html", headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 200
    assert _guard_tag() in ranged.content


@pytest.mark.asyncio
async def test_a_very_large_page_is_served_unchanged(auth_client, zone, monkeypatch):
    from app.routers import deliverable_view

    monkeypatch.setattr(deliverable_view, "HTML_GUARD_MAX_BYTES", 10)
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    page = (zone / str(task_id) / "index.html").read_bytes()

    resp = await _anon_get(auth_client, folder + "index.html")

    assert resp.status_code == 200
    assert resp.content == page
