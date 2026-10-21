"""Multi-file HTML deliverables, served as a page in a sandbox
(routers/deliverable_view.py).

An HTML deliverable that links its own CSS, images or scripts cannot render
from a ``srcdoc`` (the frame has no folder to resolve them against). The
operator mints a short-lived, signed view URL for the deliverable's folder;
the sandboxed frame loads the page and its files from it.

What these tests pin (security review focus):
  - the URL only ever reads inside that one folder: ``..``, encoded ``..``,
    symlinks out of the folder, hidden files and a task folder that points
    out of the deliverables area are all refused;
  - the signature is checked: expired, tampered, foreign-task and
    wrong-folder tokens get 403, and so does a token of a user who logged
    out since;
  - every answer carries the sandbox: CSP ``sandbox allow-scripts`` (no
    ``allow-same-origin``), sources scoped to the token's folder, nosniff,
    no referrer, no caching, and no cookie is read or set;
  - the view token is never a session credential.
"""
from __future__ import annotations

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
    is hidden. What counts is the file on disk, not the requested name."""
    url, folder, (_, task_id, _) = await _site(auth_client, zone)
    site = zone / str(task_id)
    (site / ".git").mkdir()
    (site / ".git" / "config").write_text("[remote] url=SECRET")
    os.symlink(site / target, site / alias)

    request = alias if alias != "repo" else "repo/config"
    resp = await _anon_get(auth_client, folder + request)

    assert resp.status_code == 404, resp.status_code
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
    assert page.content == "<p>hallo</p>".encode()
