"""Tests for the Slack connection test (services/slack_client + routers/slack).

Covers:
- happy path: auth.test ok -> workspace + bot name
- invalid bot token: Slack's own error code reaches the operator
- missing app-level token: reported as its OWN defect, not as a bot-token error
- swapped tokens: spoken-language message, no network call at all
- POST /api/v1/slack/test-connection requires admin (403 for a viewer)
- no token value ever appears in a response or a log record

All Slack HTTP traffic is stubbed — these tests never touch the network.
Only obvious dummy tokens are used.
"""

import logging
import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import slack_client
from app.services.slack_client import test_connection
from tests.conftest import test_engine

BOT_TOKEN = "xoxb-TEST-0000-not-a-real-token"
APP_TOKEN = "xapp-TEST-0000-not-a-real-token"


async def _set_secret(key: str, value: str) -> None:
    from app.services.secrets_helper import upsert_secret_by_key

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await upsert_secret_by_key(s, key, value, provider="slack")


def _stub_auth_test(monkeypatch, payload: dict, *, calls: list | None = None):
    """Replaces httpx.AsyncClient.post with a canned Slack response."""

    async def fake_post(self, url, **kwargs):  # noqa: ANN001
        if calls is not None:
            calls.append((url, kwargs))
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _stub_slack_api(monkeypatch, payload: dict):
    """Endpoint-level stub.

    The service-level stub above patches httpx.AsyncClient.post globally, which
    would also hijack the test client's own requests. Endpoint tests therefore
    replace the module's HTTP seam instead.
    """

    async def fake_call(bot_token: str) -> dict:
        return payload

    monkeypatch.setattr(slack_client, "_call_auth_test", fake_call)


@pytest.mark.asyncio
async def test_connection_ok_returns_workspace_and_bot(monkeypatch, session):
    await _set_secret("slack_bot_token", BOT_TOKEN)
    await _set_secret("slack_app_token", APP_TOKEN)
    _stub_auth_test(monkeypatch, {"ok": True, "team": "Acme HQ", "user": "mission-control"})

    result = await test_connection(session)

    assert result.connected is True
    assert result.team == "Acme HQ"
    assert result.bot_user == "mission-control"
    assert result.socket_mode_ready is True
    assert result.error is None
    assert result.app_token_error is None


@pytest.mark.asyncio
async def test_invalid_bot_token_surfaces_slack_error(monkeypatch, session):
    await _set_secret("slack_bot_token", BOT_TOKEN)
    await _set_secret("slack_app_token", APP_TOKEN)
    _stub_auth_test(monkeypatch, {"ok": False, "error": "invalid_auth"})

    result = await test_connection(session)

    assert result.connected is False
    assert result.error is not None
    assert "invalid_auth" in result.error
    # The app token is fine — it must not be blamed for the bot token's failure.
    assert result.app_token_error is None
    assert result.socket_mode_ready is True


@pytest.mark.asyncio
async def test_unknown_slack_error_is_passed_through(monkeypatch, session):
    await _set_secret("slack_bot_token", BOT_TOKEN)
    _stub_auth_test(monkeypatch, {"ok": False, "error": "some_new_slack_code"})

    result = await test_connection(session)

    assert result.connected is False
    assert "some_new_slack_code" in (result.error or "")


@pytest.mark.asyncio
async def test_missing_app_token_is_its_own_defect(monkeypatch, session):
    await _set_secret("slack_bot_token", BOT_TOKEN)
    _stub_auth_test(monkeypatch, {"ok": True, "team": "Acme HQ", "user": "mission-control"})

    result = await test_connection(session)

    # Bot side is healthy...
    assert result.connected is True
    assert result.error is None
    # ...but Socket Mode is not ready, reported separately and by name.
    assert result.socket_mode_ready is False
    assert result.app_token_set is False
    assert result.app_token_error is not None
    assert "connections:write" in result.app_token_error


@pytest.mark.asyncio
async def test_swapped_tokens_get_a_spoken_message_without_calling_slack(monkeypatch, session):
    calls: list = []
    await _set_secret("slack_bot_token", APP_TOKEN)  # xapp- in the bot field
    await _set_secret("slack_app_token", BOT_TOKEN)  # xoxb- in the app field
    _stub_auth_test(monkeypatch, {"ok": False, "error": "invalid_auth"}, calls=calls)

    result = await test_connection(session)

    assert result.connected is False
    assert "app-level token" in (result.error or "")
    assert "xoxb-" in (result.error or "")
    assert "bot token" in (result.app_token_error or "")
    # Never reported as Slack's generic invalid_auth.
    assert "invalid_auth" not in (result.error or "")
    assert calls == []  # the format check short-circuits before the network call


@pytest.mark.asyncio
async def test_bot_token_with_foreign_prefix_is_named(monkeypatch, session):
    calls: list = []
    await _set_secret("slack_bot_token", "xoxp-TEST-user-token")
    _stub_auth_test(monkeypatch, {"ok": True}, calls=calls)

    result = await test_connection(session)

    assert result.connected is False
    assert "xoxb-" in (result.error or "")
    assert calls == []


@pytest.mark.asyncio
async def test_no_tokens_at_all_reports_both_gaps(session):
    result = await test_connection(session)

    assert result.connected is False
    assert result.bot_token_set is False
    assert result.app_token_set is False
    assert "xoxb-" in (result.error or "")
    assert result.app_token_error is not None


@pytest.mark.asyncio
async def test_transport_error_is_reported_not_raised(monkeypatch, session):
    await _set_secret("slack_bot_token", BOT_TOKEN)
    await _set_secret("slack_app_token", APP_TOKEN)

    async def boom(self, url, **kwargs):  # noqa: ANN001
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)

    result = await test_connection(session)

    assert result.connected is False
    assert "Slack" in (result.error or "")


# ── Endpoint ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoint_returns_result_for_admin(monkeypatch, auth_client: AsyncClient):
    await _set_secret("slack_bot_token", BOT_TOKEN)
    await _set_secret("slack_app_token", APP_TOKEN)
    _stub_slack_api(monkeypatch, {"ok": True, "team": "Acme HQ", "user": "mission-control"})

    resp = await auth_client.post("/api/v1/slack/test-connection")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is True
    assert body["team"] == "Acme HQ"
    assert body["bot_user"] == "mission-control"
    assert body["socket_mode_ready"] is True


@pytest.mark.asyncio
async def test_endpoint_forbidden_for_non_admin(client: AsyncClient):
    from app.auth import create_access_token
    from app.models.user import User

    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email="viewer@mc.local", name="Viewer", role="viewer", is_active=True))
        await s.commit()

    token = create_access_token(str(user_id), "viewer")
    resp = await client.post(
        "/api/v1/slack/test-connection", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 403, resp.text


# ── Security invariant ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tokens_never_appear_in_response_or_logs(monkeypatch, auth_client: AsyncClient, caplog):
    """The assurance that matters: neither the API response nor any log line
    may carry a token — not on success, not on failure, not when swapped."""
    scenarios = [
        # (bot secret, app secret, slack payload)
        (BOT_TOKEN, APP_TOKEN, {"ok": True, "team": "Acme HQ", "user": "mission-control"}),
        (BOT_TOKEN, APP_TOKEN, {"ok": False, "error": "invalid_auth"}),
        (APP_TOKEN, BOT_TOKEN, {"ok": False, "error": "invalid_auth"}),
        (BOT_TOKEN, None, {"ok": True, "team": "Acme HQ", "user": "mission-control"}),
    ]

    for bot, app_tok, payload in scenarios:
        await _set_secret("slack_bot_token", bot)
        await _set_secret("slack_app_token", app_tok or "")
        _stub_slack_api(monkeypatch, payload)

        with caplog.at_level(logging.DEBUG, logger="mc.slack"):
            caplog.clear()
            resp = await auth_client.post("/api/v1/slack/test-connection")

        assert resp.status_code == 200, resp.text
        raw = resp.text
        logged = "\n".join(r.getMessage() for r in caplog.records)

        for secret in (BOT_TOKEN, APP_TOKEN):
            assert secret not in raw, f"token leaked into response: {payload}"
            assert secret not in logged, f"token leaked into logs: {payload}"
            # Not even the distinctive tail of the token.
            assert secret.split("-", 2)[-1] not in raw
            assert secret.split("-", 2)[-1] not in logged


def test_service_never_formats_a_token_into_a_log_call():
    """Static guard: the module must not pass a token variable to the logger.

    Cheap, but it catches the regression a future edit would introduce
    (`log.info("token=%s", bot_token)`) before it ever runs.
    """
    import inspect

    source = inspect.getsource(slack_client)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("log."):
            assert "token" not in stripped or "_set" in stripped or "ready" in stripped, (
                f"suspicious log line may carry a token: {stripped}"
            )


# ── Secret catalog ──────────────────────────────────────────────────────────


def test_slack_tokens_are_fixed_catalog_fields():
    """A newcomer must FIND the two fields, not invent their key names."""
    from app.routers.secrets import PROVIDER_TEMPLATES

    by_key = {t["key"]: t for t in PROVIDER_TEMPLATES}

    bot = by_key["slack_bot_token"]
    assert bot["provider"] == "slack"
    assert bot["placeholder"] == "xoxb-..."
    assert "OAuth & Permissions" in bot["description"]

    app_tok = by_key["slack_app_token"]
    assert app_tok["provider"] == "slack"
    assert app_tok["placeholder"] == "xapp-..."
    assert "Socket Mode" in app_tok["description"]
    assert "connections:write" in app_tok["description"]
