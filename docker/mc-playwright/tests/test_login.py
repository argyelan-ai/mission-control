"""Pure-function tests for the login/auth helpers in media.py
(`LoginSpec`, `build_storage_state`, `MC_AUTH_STORAGE_KEY`).

Incident 2026-09-13: `mc verify` against the MC UI itself (http://caddy/,
/tasks, /agents) returned three byte-identical screenshots of the sign-in
mask — the tester almost signed off a `visual_proof` card on the strength
of a login page. Investigation found the login-injection machinery
(auth_token → localStorage, credential_id → form-login) already fully
built and, once actually exercised end to end against the live stack,
working correctly — but with ZERO automated test coverage for the part
that actually drives the browser. `test_visual_verify_endpoint.py`
explicitly says "the real Playwright call is mocked — the mc-playwright
container is covered by a separate live test", but that separate live test
never existed. This file is the fast half of closing that gap; the actual
browser-driven form-login is covered by test_login_live.py (opt-in, needs
a running stack).

Run (same pattern as test_media.py — no playwright needed, media.py is
deliberately playwright-free):
    cd backend && python -m pytest ../docker/mc-playwright/tests/test_login.py -v
"""
from media import (
    MC_AUTH_STORAGE_KEY,
    LoginSpec,
    build_storage_state,
)


# ── LoginSpec defaults match the REAL MC login form ─────────────────────────
#
# frontend-v2/src/app/login/page.tsx (read, not assumed):
#   <input ref={emailRef} id="email" type="email" ... />
#   <input id="password" type={showPassword ? "text" : "password"} ... />
#   <button type="submit" ...>
#
# The password field's `type` toggles to "text" when the user clicks the
# show/hide-password eye icon — LoginSpec's default password_selector must
# still match via `type="password"`, which is the field's INITIAL type on
# page load (Playwright fills before ever touching that button).


def test_login_spec_defaults_match_mc_login_form():
    """Regression pin: if LoginSpec's defaults or the real login form's
    input attributes drift apart, mc-playwright's default form-login goes
    silently blind again (this is the exact incident class — a login that
    LOOKS wired but never actually clicks anything, or clicks the wrong
    thing)."""
    spec = LoginSpec(url="http://caddy/login", username="x", password="y")

    # id="email" type="email" — the login page's real markup.
    assert 'input[type="email"]' in spec.username_selector
    # id="password" type="password" (initial state before the eye toggle).
    assert spec.password_selector == 'input[type="password"]'
    # <button type="submit"> — the only submit button on the login form.
    assert spec.submit_selector == 'button[type="submit"]'


def test_login_spec_requires_url_username_password():
    """The three fields with no default must be required — a LoginSpec
    silently defaulting one of these would send an empty string into a
    real login form and produce another unnoticed sign-in-mask
    screenshot."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LoginSpec(username="x", password="y")  # missing url
    with pytest.raises(ValidationError):
        LoginSpec(url="http://caddy/login", password="y")  # missing username
    with pytest.raises(ValidationError):
        LoginSpec(url="http://caddy/login", username="x")  # missing password


# ── build_storage_state ──────────────────────────────────────────────────────


def test_build_storage_state_no_token_returns_none():
    assert build_storage_state("http://caddy/agents", None) is None
    assert build_storage_state("http://caddy/agents", "") is None


def test_build_storage_state_valid_token_sets_localstorage_on_target_origin():
    state = build_storage_state("http://caddy/agents", "jwt-abc-123")
    assert state is not None
    assert state["cookies"] == []
    assert len(state["origins"]) == 1
    origin = state["origins"][0]
    # Origin-scoped to the TARGET url's origin, not the login page's —
    # localStorage is per-origin, and http://caddy/agents IS the origin the
    # frontend's own JS will read AUTH_TOKEN_KEY from after navigate.
    assert origin["origin"] == "http://caddy"
    assert origin["localStorage"] == [
        {"name": MC_AUTH_STORAGE_KEY, "value": "jwt-abc-123"}
    ]


def test_build_storage_state_uses_the_real_frontend_storage_key():
    """MC_AUTH_STORAGE_KEY must be the literal key frontend-v2/src/lib/api.ts
    reads (`AUTH_TOKEN_KEY = "mc_auth_token"`) — a drift here means the
    injected token sits in localStorage under a key the app never looks at,
    and the agent gets the sign-in mask with no error at all (auth_token
    injection has no loud-failure path the way form-login does)."""
    assert MC_AUTH_STORAGE_KEY == "mc_auth_token"


def test_build_storage_state_invalid_url_returns_none_not_raise():
    """A malformed target_url (no scheme/netloc) must degrade to `None`
    (caller falls back to an unauthenticated context) — never raise and
    never silently build a state with an empty/wrong origin."""
    assert build_storage_state("not-a-url", "jwt-abc-123") is None
    assert build_storage_state("", "jwt-abc-123") is None


def test_build_storage_state_origin_excludes_path_and_query():
    """localStorage is origin-scoped — a state built with the full URL
    (path/query included) as the "origin" would silently never apply on
    navigate, since Playwright's storage_state only matches on scheme+host
    [+port]."""
    state = build_storage_state("http://caddy/agents?x=1#frag", "tok")
    assert state["origins"][0]["origin"] == "http://caddy"


if __name__ == "__main__":
    import sys

    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
