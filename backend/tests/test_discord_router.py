"""Phase 29-01 Task 2 (+ Phase 30-02 update): backend/app/routers/discord.py.

Tests the new standalone Discord router (D-04 / ADR-039). Mirrors the
externally observable behavior of the legacy `routers/agents.py` Discord
endpoints but with:

- new prefix `/api/v1/discord/*` (gateway-independent)
- direct calls to `services/discord.py` helpers (no GatewayClient indirection)
- Phase 30: guild_id + category_id come from the `discord_config` DB row
  (was `settings.discord_*` env vars during Phase 29 stop-gap)
- JWT auth via `Depends(require_user)` (no agent-scoped variant)
"""

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ---------------------------------------------------------------------------
# Phase 30 — discord_config seed helper
# ---------------------------------------------------------------------------


async def _seed_discord_config(
    *,
    guild_id: str | None = "1234567890",
    category_id: str | None = None,
    bot_configured: bool = True,
):
    """Seed the single discord_config row. Idempotent: replaces any existing
    row by deleting first."""
    from app.models.discord_config import DiscordConfig
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        existing = (await s.exec(select(DiscordConfig))).all()
        for row in existing:
            await s.delete(row)
        await s.commit()
        cfg = DiscordConfig(
            guild_id=guild_id,
            category_id=category_id,
            bot_configured=bot_configured,
        )
        s.add(cfg)
        await s.commit()
        await s.refresh(cfg)
        return cfg


async def _clear_discord_config():
    """Remove all discord_config rows (test "bot not configured" path)."""
    from app.models.discord_config import DiscordConfig
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        existing = (await s.exec(select(DiscordConfig))).all()
        for row in existing:
            await s.delete(row)
        await s.commit()


# ---------------------------------------------------------------------------
# 1. Module import & router shape
# ---------------------------------------------------------------------------


def test_discord_router_module_imports():
    """The new module imports cleanly with no openclaw imports."""
    import app.routers.discord as discord_router

    assert hasattr(discord_router, "router"), "module must expose `router`"
    assert discord_router.router.prefix == "/api/v1/discord", (
        f"prefix must be /api/v1/discord, got {discord_router.router.prefix}"
    )


def test_discord_router_has_required_routes():
    """Router exposes the 4 Phase 29-01 endpoints."""
    import app.routers.discord as discord_router

    paths_and_methods = {}
    for route in discord_router.router.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        paths_and_methods.setdefault(path, set()).update(methods)

    assert "/api/v1/discord/channels" in paths_and_methods, (
        f"missing GET /channels — found: {list(paths_and_methods)}"
    )
    assert "GET" in paths_and_methods["/api/v1/discord/channels"]

    per_agent_path = "/api/v1/discord/agents/{agent_id}/channel"
    assert per_agent_path in paths_and_methods, (
        f"missing /agents/{{agent_id}}/channel — found: {list(paths_and_methods)}"
    )
    methods = paths_and_methods[per_agent_path]
    assert "POST" in methods, methods
    assert "PATCH" in methods, methods
    assert "DELETE" in methods, methods


def test_discord_router_does_not_import_gateway_code():
    """D-13 invariant: no gateway/openclaw imports allowed in new router.

    Checks live module attributes (what's actually imported into the
    namespace), not raw source. Docstrings that mention these symbols
    in prose are fine.
    """
    import app.routers.discord as discord_router

    forbidden = (
        "rpc",
        "openclaw_rpc",
        "gateway_sync",
        "gateway_client",
        "GatewayClient",
        "Gateway",
    )
    for symbol in forbidden:
        assert not hasattr(discord_router, symbol), (
            f"forbidden symbol {symbol!r} imported into routers/discord.py"
        )


# ---------------------------------------------------------------------------
# 2. Auth gates (require_user on every endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_channels_requires_auth(client):
    """GET /api/v1/discord/channels without JWT → 401."""
    resp = await client.get("/api/v1/discord/channels")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_create_channel_requires_auth(client):
    """POST /api/v1/discord/agents/{id}/channel without JWT → 401."""
    fake_id = uuid.uuid4()
    resp = await client.post(
        f"/api/v1/discord/agents/{fake_id}/channel",
        json={"name": "test-channel"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_rename_channel_requires_auth(client):
    fake_id = uuid.uuid4()
    resp = await client.patch(
        f"/api/v1/discord/agents/{fake_id}/channel",
        json={"new_name": "renamed"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_delete_channel_requires_auth(client):
    fake_id = uuid.uuid4()
    resp = await client.delete(f"/api/v1/discord/agents/{fake_id}/channel")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 3. Bot-not-configured guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_channels_bot_not_configured(auth_client):
    """If discord_bot_token (env) is empty OR no discord_config row → 400."""
    # conftest sets neither token nor seeds a config row.
    await _clear_discord_config()
    resp = await auth_client.get("/api/v1/discord/channels")
    assert resp.status_code == 400, resp.text
    assert "not configured" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_channel_bot_not_configured(auth_client, make_board, make_agent):
    await _clear_discord_config()
    board = await make_board()
    agent = await make_agent(board_id=board.id)
    resp = await auth_client.post(
        f"/api/v1/discord/agents/{agent.id}/channel",
        json={"name": "test-channel"},
    )
    assert resp.status_code == 400, resp.text
    assert "not configured" in resp.text.lower()


# ---------------------------------------------------------------------------
# 4. Channel-create flow (happy path + 404 + 409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_channel_agent_not_found(auth_client):
    """404 when agent UUID does not exist (with bot configured)."""
    fake_id = uuid.uuid4()
    await _seed_discord_config(guild_id="1234567890", category_id=None)
    with patch("app.routers.discord.settings") as mock_settings:
        mock_settings.discord_bot_token = "fake-token"
        resp = await auth_client.post(
            f"/api/v1/discord/agents/{fake_id}/channel",
            json={"name": "test-channel"},
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_create_channel_already_bound_returns_409(
    auth_client, make_board, make_agent
):
    """If agent.discord_channel_id is already set → 409."""
    board = await make_board()
    agent = await make_agent(
        board_id=board.id,
        discord_channel_id="999999999999",
        discord_channel_name="existing",
    )
    await _seed_discord_config(guild_id="1234567890", category_id=None)
    with patch("app.routers.discord.settings") as mock_settings:
        mock_settings.discord_bot_token = "fake-token"
        resp = await auth_client.post(
            f"/api/v1/discord/agents/{agent.id}/channel",
            json={"name": "duplicate"},
        )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_create_channel_happy_path(auth_client, make_board, make_agent):
    """Happy path: agent has no channel → create, bind, emit event, 201."""
    board = await make_board()
    agent = await make_agent(board_id=board.id, discord_channel_id=None)

    fake_channel = {
        "id": "555555555555",
        "name": "agent-channel",
        "context": "Discord channel",
        "bound_agent_id": None,
    }

    await _seed_discord_config(
        guild_id="1234567890", category_id="category-id-789"
    )
    with patch("app.routers.discord.settings") as mock_settings, patch(
        "app.routers.discord.create_guild_text_channel",
        new=AsyncMock(return_value=fake_channel),
    ) as mock_create:
        mock_settings.discord_bot_token = "fake-token"

        resp = await auth_client.post(
            f"/api/v1/discord/agents/{agent.id}/channel",
            json={"name": "agent-channel", "context": "for this agent"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["channel_id"] == "555555555555"
    assert body["name"] == "agent-channel"

    mock_create.assert_awaited_once()
    # category_id should be plumbed from discord_config default (Phase 30)
    kwargs = mock_create.call_args.kwargs
    assert kwargs["category_id"] == "category-id-789"
    assert kwargs["name"] == "agent-channel"
    assert kwargs["context"] == "for this agent"


@pytest.mark.asyncio
async def test_create_channel_overrides_category(auth_client, make_board, make_agent):
    """Per-request category_id overrides the discord_config default."""
    board = await make_board()
    agent = await make_agent(board_id=board.id, discord_channel_id=None)

    fake_channel = {"id": "111", "name": "c", "context": "x", "bound_agent_id": None}

    await _seed_discord_config(
        guild_id="1234567890", category_id="default-cat"
    )
    with patch("app.routers.discord.settings") as mock_settings, patch(
        "app.routers.discord.create_guild_text_channel",
        new=AsyncMock(return_value=fake_channel),
    ) as mock_create:
        mock_settings.discord_bot_token = "fake-token"

        resp = await auth_client.post(
            f"/api/v1/discord/agents/{agent.id}/channel",
            json={"name": "c", "category_id": "override-cat"},
        )

    assert resp.status_code == 201, resp.text
    mock_create.assert_awaited_once()
    assert mock_create.call_args.kwargs["category_id"] == "override-cat"


@pytest.mark.asyncio
async def test_create_channel_discord_failure_returns_502(
    auth_client, make_board, make_agent
):
    """If create_guild_text_channel raises httpx.HTTPError → 502 + no DB write."""
    board = await make_board()
    agent = await make_agent(board_id=board.id, discord_channel_id=None)

    await _seed_discord_config(guild_id="1234567890", category_id=None)
    with patch("app.routers.discord.settings") as mock_settings, patch(
        "app.routers.discord.create_guild_text_channel",
        new=AsyncMock(side_effect=httpx.HTTPError("Discord down")),
    ):
        mock_settings.discord_bot_token = "fake-token"

        resp = await auth_client.post(
            f"/api/v1/discord/agents/{agent.id}/channel",
            json={"name": "doomed"},
        )

    assert resp.status_code == 502, resp.text

    # Agent must not have any partial state (no orphan DB write)
    from app.models.agent import Agent
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Agent, agent.id)
        assert refreshed.discord_channel_id is None


# ---------------------------------------------------------------------------
# 5. Rename flow (PATCH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_channel_no_binding_returns_404(
    auth_client, make_board, make_agent
):
    board = await make_board()
    agent = await make_agent(board_id=board.id, discord_channel_id=None)
    resp = await auth_client.patch(
        f"/api/v1/discord/agents/{agent.id}/channel",
        json={"new_name": "renamed"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_rename_channel_happy_path(auth_client, make_board, make_agent):
    """PATCH bound channel → calls Discord API + persists new name."""
    board = await make_board()
    agent = await make_agent(
        board_id=board.id,
        discord_channel_id="555555555555",
        discord_channel_name="old-name",
    )

    # Fake httpx response object
    fake_resp = httpx.Response(
        200,
        json={"id": "555555555555", "name": "new-name"},
        request=httpx.Request(
            "PATCH", "https://discord.com/api/v10/channels/555555555555"
        ),
    )

    with patch("app.routers.discord.settings") as mock_settings, patch(
        "app.routers.discord.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_settings.discord_bot_token = "fake-token"

        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_client.patch = AsyncMock(return_value=fake_resp)

        resp = await auth_client.patch(
            f"/api/v1/discord/agents/{agent.id}/channel",
            json={"new_name": "new-name"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["old_name"] == "old-name"
    assert body["new_name"] == "new-name"

    # Verify the Discord PATCH was called with the right body + auth header
    mock_client.patch.assert_awaited_once()
    call = mock_client.patch.call_args
    assert "channels/555555555555" in call.args[0]
    assert call.kwargs["json"] == {"name": "new-name"}
    assert call.kwargs["headers"]["Authorization"] == "Bot fake-token"


@pytest.mark.asyncio
async def test_rename_channel_discord_failure_returns_502(
    auth_client, make_board, make_agent
):
    """Discord API failure → 502 + DB unchanged."""
    board = await make_board()
    agent = await make_agent(
        board_id=board.id,
        discord_channel_id="555555555555",
        discord_channel_name="old-name",
    )

    with patch("app.routers.discord.settings") as mock_settings, patch(
        "app.routers.discord.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_settings.discord_bot_token = "fake-token"

        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_client.patch = AsyncMock(side_effect=httpx.HTTPError("network"))

        resp = await auth_client.patch(
            f"/api/v1/discord/agents/{agent.id}/channel",
            json={"new_name": "new-name"},
        )

    assert resp.status_code == 502, resp.text

    # DB unchanged
    from app.models.agent import Agent
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Agent, agent.id)
        assert refreshed.discord_channel_name == "old-name"


# ---------------------------------------------------------------------------
# 6. Delete (unbind) flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_channel_no_binding_returns_404(
    auth_client, make_board, make_agent
):
    board = await make_board()
    agent = await make_agent(board_id=board.id, discord_channel_id=None)
    resp = await auth_client.delete(f"/api/v1/discord/agents/{agent.id}/channel")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_delete_channel_clears_fields(auth_client, make_board, make_agent):
    """DELETE clears discord_channel_id + discord_channel_name (no API call)."""
    board = await make_board()
    agent = await make_agent(
        board_id=board.id,
        discord_channel_id="555555555555",
        discord_channel_name="will-be-unbound",
    )

    resp = await auth_client.delete(f"/api/v1/discord/agents/{agent.id}/channel")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"unbound": True}

    from app.models.agent import Agent
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Agent, agent.id)
        assert refreshed.discord_channel_id is None
        assert refreshed.discord_channel_name is None


# ---------------------------------------------------------------------------
# 7. List-channels happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_channels_happy_path(auth_client):
    """GET /channels returns the helper output verbatim."""
    fake_channels = [
        {"id": "1", "name": "general", "context": "Discord channel"},
        {"id": "2", "name": "ops", "context": "Discord channel"},
    ]
    await _seed_discord_config(guild_id="1234567890", category_id=None)
    with patch("app.routers.discord.settings") as mock_settings, patch(
        "app.routers.discord.list_guild_channels",
        new=AsyncMock(return_value=fake_channels),
    ) as mock_list:
        mock_settings.discord_bot_token = "fake-token"

        resp = await auth_client.get("/api/v1/discord/channels")

    assert resp.status_code == 200, resp.text
    assert resp.json() == fake_channels
    mock_list.assert_awaited_once_with("1234567890")


# ---------------------------------------------------------------------------
# 8. Phase 30 — admin GET/PATCH /config endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_config_empty_returns_defaults(auth_client):
    """GET /config with no DB row returns "not configured" defaults."""
    await _clear_discord_config()
    resp = await auth_client.get("/api/v1/discord/config")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "guild_id": None,
        "category_id": None,
        "bot_configured": False,
    }


@pytest.mark.asyncio
async def test_get_config_populated_returns_row(auth_client):
    """GET /config after a row exists returns the row JSON."""
    await _seed_discord_config(
        guild_id="111", category_id="222", bot_configured=True
    )
    resp = await auth_client.get("/api/v1/discord/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["guild_id"] == "111"
    assert body["category_id"] == "222"
    assert body["bot_configured"] is True


@pytest.mark.asyncio
async def test_patch_config_creates_row_when_missing(auth_client):
    """PATCH /config with guild_id=X creates the row if none exists."""
    await _clear_discord_config()
    resp = await auth_client.patch(
        "/api/v1/discord/config",
        json={"guild_id": "999", "bot_configured": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["guild_id"] == "999"
    assert body["category_id"] is None
    assert body["bot_configured"] is True

    # Verify a row exists in DB.
    from app.models.discord_config import DiscordConfig
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(DiscordConfig))).all()
        assert len(rows) == 1
        assert rows[0].guild_id == "999"


@pytest.mark.asyncio
async def test_patch_config_updates_existing_row(auth_client):
    """PATCH /config updates only the fields in payload, leaves others alone."""
    await _seed_discord_config(
        guild_id="orig-guild",
        category_id="orig-cat",
        bot_configured=False,
    )
    resp = await auth_client.patch(
        "/api/v1/discord/config",
        json={"guild_id": "new-guild"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["guild_id"] == "new-guild"
    assert body["category_id"] == "orig-cat"  # untouched
    assert body["bot_configured"] is False  # untouched


@pytest.mark.asyncio
async def test_patch_config_requires_auth(client):
    """PATCH /config without JWT → 401."""
    resp = await client.patch(
        "/api/v1/discord/config",
        json={"guild_id": "x"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_get_config_requires_auth(client):
    """GET /config without JWT → 401."""
    resp = await client.get("/api/v1/discord/config")
    assert resp.status_code == 401, resp.text
