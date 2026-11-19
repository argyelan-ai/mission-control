"""The HttpOnly session cookie is not a credential for cross-site
subrequests (auth.py, ``_authenticate_user``).

Why: an HTML deliverable is shown as the rendered page in an
``<iframe sandbox="allow-scripts">``. The page is untrusted, model-written
content and runs in an opaque origin. Its CSP allows ``https:`` sources (CDN
libraries and fonts), and when MC itself is served over https that includes
MC's own origin. WebKit (Safari, every iPhone browser) sends MC's
SameSite=Lax ``mc_sse_token`` cookie on such a sandboxed page's image, script
and frame requests (measured in Playwright WebKit, 2026-10-21; Chromium does
not). ``require_user`` falls back to that cookie on every route, so the page
could call any authenticated GET route as the operator.

The browser marks those requests ``Sec-Fetch-Site: cross-site`` (the
initiator is an opaque origin). MC's own UI is always same-origin. So a
cross-site request may not use the cookie, except a top-level navigation
(a link from another app opening an MC URL), which the sandbox cannot start:
no ``allow-top-navigation``, no ``allow-popups``. A request without
Fetch-Metadata headers (older browsers, server-side callers) is unchanged.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

ROUTE = "/api/v1/auth/me"  # any require_user GET route


def _fetch_meta(site: str | None, mode: str | None = None, dest: str | None = None) -> dict:
    headers = {}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    if mode is not None:
        headers["Sec-Fetch-Mode"] = mode
    if dest is not None:
        headers["Sec-Fetch-Dest"] = dest
    return headers


@pytest.fixture
async def cookie_client(auth_client: AsyncClient):
    """The operator's browser with only the session cookie — what an
    <img>/<script>/<iframe> request carries (it cannot send a header)."""
    token = auth_client.headers.pop("Authorization").split(" ", 1)[1]
    auth_client.cookies.set("mc_sse_token", token)
    try:
        yield auth_client, token
    finally:
        auth_client.cookies.clear()
        auth_client.headers["Authorization"] = f"Bearer {token}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,dest",
    [
        ("no-cors", "image"),     # <img src="https://mc/api/v1/...">
        ("no-cors", "script"),    # <script src=...>
        ("no-cors", "style"),     # <link rel=stylesheet href=...>
        ("cors", "script"),       # <script type=module src=...>
        ("navigate", "iframe"),   # <iframe src=...> or the frame navigating itself
        ("cors", "empty"),        # fetch() (blocked by the page CSP, still refused here)
        (None, None),             # cross-site without mode/dest
    ],
)
async def test_cookie_is_not_a_credential_for_a_cross_site_subrequest(cookie_client, mode, dest):
    client, _ = cookie_client

    resp = await client.get(ROUTE, headers=_fetch_meta("cross-site", mode, dest))

    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site,mode,dest",
    [
        ("same-origin", "cors", "empty"),       # MC's own UI
        ("same-origin", "no-cors", "image"),    # MC's own <img src="/api/...">
        ("same-site", "cors", "empty"),         # another port of the same host
        ("none", "navigate", "document"),       # typed URL / bookmark
        ("cross-site", "navigate", "document"), # a link from another app (Lax)
        (None, None, None),                     # no Fetch-Metadata at all
    ],
)
async def test_cookie_still_works_where_it_did(cookie_client, site, mode, dest):
    client, _ = cookie_client

    resp = await client.get(ROUTE, headers=_fetch_meta(site, mode, dest))

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_fetch_metadata_values_are_case_insensitive(cookie_client):
    client, _ = cookie_client

    resp = await client.get(ROUTE, headers=_fetch_meta("Cross-Site", "No-Cors", "Image"))

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_header_is_unaffected(cookie_client):
    """Only the ambient cookie is refused. A caller that sends the header
    holds the token itself (a sandboxed page never does)."""
    client, token = cookie_client

    resp = await client.get(
        ROUTE,
        headers={"Authorization": f"Bearer {token}", **_fetch_meta("cross-site", "cors", "empty")},
    )

    assert resp.status_code == 200
