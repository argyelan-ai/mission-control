"""A key saved in the MC vault must reach every service that uses it.

Before this, the OpenAI key from Settings -> API keys did nothing for live
voice, Jarvis on Telegram, the morning briefing, dictation or the voice
worker, and the Discord bot token from the vault did nothing for Discord:
all of them read only ``.env``. ``effective_secret()`` is the one rule now:
**vault first, env only as the fallback** (ADR-093: what the user may change
is changeable in MC).

Every service test here has the same shape: env empty, key only in the vault
-> the service gets the vault value. Sabotage: point the read site back at
``settings.<key>`` and the test turns red.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services import secrets_helper
from app.services.secrets_helper import effective_secret, resolve_secret, upsert_secret_by_key
from tests.conftest import test_engine

# Split so secret scanners do not flag made-up test values.
VAULT_KEY = "sk-test-" + "VAULT-0123456789abcdef"
ENV_KEY = "sk-test-" + "ENV-0123456789abcdef"
VAULT_DISCORD = "discord-" + "VAULT-token"


@pytest.fixture(autouse=True)
def _own_session_goes_to_the_test_db(monkeypatch):
    """``effective_secret`` opens its own session via ``app.database``.
    Without this it would talk to whatever Postgres the default URL names."""
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.fixture
def env_empty(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "discord_bot_token", "")


async def _vault(key: str, value: str) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await upsert_secret_by_key(s, key, value)


# ── the rule itself ──────────────────────────────────────────────────────


async def test_vault_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", ENV_KEY)
    await _vault("openai_api_key", VAULT_KEY)
    assert await effective_secret("openai_api_key") == VAULT_KEY
    assert await resolve_secret("openai_api_key") == (VAULT_KEY, "vault")


async def test_env_is_the_fallback_when_vault_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", ENV_KEY)
    assert await effective_secret("openai_api_key") == ENV_KEY
    assert await resolve_secret("openai_api_key") == (ENV_KEY, "env")


async def test_nothing_anywhere_is_empty_not_an_error(env_empty):
    assert await effective_secret("openai_api_key") == ""
    assert await resolve_secret("openai_api_key") == ("", None)


async def test_vault_value_is_stripped(env_empty):
    await _vault("openai_api_key", f"  {VAULT_KEY}\n")
    assert await effective_secret("openai_api_key") == VAULT_KEY


async def test_vault_failure_degrades_to_env(monkeypatch):
    """A vault hiccup (DB down, wrong encryption key) must not turn a working
    .env install into a broken one."""
    monkeypatch.setattr(settings, "openai_api_key", ENV_KEY)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.database.async_session_maker", _boom)
    assert await effective_secret("openai_api_key") == ENV_KEY


async def test_explicit_session_is_used(session, env_empty):
    await _vault("discord_bot_token", VAULT_DISCORD)
    assert await effective_secret("discord_bot_token", session=session) == VAULT_DISCORD


async def test_unknown_key_is_a_programming_error():
    """The allowlist is the boundary: nobody reaches arbitrary Settings fields
    (crypto keys, JWT secret) through this function."""
    with pytest.raises(ValueError):
        await effective_secret("secrets_encryption_key")


def test_every_key_has_an_env_field_and_a_vault_template():
    """Each allowlisted key must exist as a Settings field (the env fallback)
    and be offered in the API-keys page (otherwise the user cannot set it)."""
    from app.routers.secrets import PROVIDER_TEMPLATES

    template_keys = {t["key"] for t in PROVIDER_TEMPLATES}
    for key in secrets_helper.EFFECTIVE_SECRET_KEYS:
        assert hasattr(settings, key), key
        assert key in template_keys, key


# ── live voice in the chat (voice_live.py, voice_live_usage.py) ──────────


async def test_voice_live_enabled_and_mints_with_vault_key(monkeypatch, env_empty):
    from app.services import voice_live

    monkeypatch.setattr(settings, "voice_live_enabled", True)
    await _vault("openai_api_key", VAULT_KEY)
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"value": "ek_short", "expires_at": 1_900_000_000,
                  "session": json.loads(request.content)["session"]},
        )

    monkeypatch.setattr(voice_live, "_transport", lambda: httpx.MockTransport(_ok))

    assert await voice_live.is_enabled() is True
    minted = await voice_live.mint_client_secret("Boss", "en")
    assert minted.client_secret == "ek_short"
    assert seen[0].headers["authorization"] == f"Bearer {VAULT_KEY}"


async def test_voice_live_refuses_to_hand_out_the_vault_key(monkeypatch, env_empty):
    """The leak guard compares against the key actually used — the vault one."""
    from app.services import voice_live

    monkeypatch.setattr(settings, "voice_live_enabled", True)
    await _vault("openai_api_key", VAULT_KEY)
    monkeypatch.setattr(
        voice_live,
        "_transport",
        lambda: httpx.MockTransport(
            lambda r: httpx.Response(200, json={"value": VAULT_KEY, "expires_at": 1})
        ),
    )
    with pytest.raises(voice_live.VoiceLiveError):
        await voice_live.mint_client_secret("Boss", "en")


async def test_voice_hangup_uses_vault_key(monkeypatch, env_empty):
    from app.services import voice_live_usage

    await _vault("openai_api_key", VAULT_KEY)
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(voice_live_usage, "_transport", lambda: httpx.MockTransport(_ok))
    assert await voice_live_usage.hangup_upstream("rtc_x1") is True
    assert seen[0].headers["authorization"] == f"Bearer {VAULT_KEY}"


async def test_voice_live_config_route_on_with_vault_key_only(auth_client, monkeypatch, env_empty):
    monkeypatch.setattr(settings, "voice_live_enabled", True)
    await _vault("openai_api_key", VAULT_KEY)
    r = await auth_client.get("/api/v1/voice/live/config")
    assert r.status_code == 200
    assert VAULT_KEY not in r.text


# ── Jarvis on Telegram (jarvis_telegram.py) ──────────────────────────────


async def test_jarvis_telegram_runs_with_vault_key(monkeypatch, env_empty, fake_redis):
    import app.redis_client
    import app.services.jarvis_telegram as jt
    from jarvis_core.brain import BrainResult

    monkeypatch.setattr(app.redis_client, "_redis", fake_redis)
    monkeypatch.setattr(settings, "jarvis_telegram_enabled", True)
    monkeypatch.setattr(settings, "jarvis_agent_token", "jarvis-tok")
    monkeypatch.setattr(settings, "telegram_chat_id", "12345")
    await _vault("openai_api_key", VAULT_KEY)
    captured: dict = {}

    class _FakeBrain:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def respond(self, user_text, history=None):
            return BrainResult(text="ok", actions=[], new_turns=[])

        async def aclose(self):
            pass

    monkeypatch.setattr(jt, "JarvisBrain", _FakeBrain)
    bot = AsyncMock()
    handler = jt.JarvisTelegramHandler(bot)

    assert await handler.is_enabled() is True
    await handler.handle_message({"chat": {"id": 12345}, "text": "status?"})
    assert captured["api_key"] == VAULT_KEY


async def test_jarvis_telegram_voice_note_uses_vault_key(monkeypatch, env_empty):
    import app.services.jarvis_telegram as jt

    await _vault("openai_api_key", VAULT_KEY)
    transcribe = AsyncMock(return_value="hallo")
    monkeypatch.setattr(jt, "transcribe_audio", transcribe)
    bot = AsyncMock()
    bot.get_file_bytes = AsyncMock(return_value=b"ogg")
    handler = jt.JarvisTelegramHandler(bot)

    text = await handler._transcribe_voice({"file_id": "f1"})
    assert text == "hallo"
    assert transcribe.await_args.kwargs["api_key"] == VAULT_KEY


# ── morning briefing (jarvis_briefing.py) ────────────────────────────────


async def test_briefing_runs_with_vault_key(monkeypatch, env_empty, fake_redis):
    from app.services import jarvis_briefing as jb

    monkeypatch.setattr(settings, "jarvis_briefing_enabled", True)
    monkeypatch.setattr(settings, "jarvis_frontier_model", "")
    monkeypatch.setattr(jb, "_JARVIS_CORE_OK", True)

    async def _get_redis():
        return fake_redis

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    monkeypatch.setattr(jb.mc_client, "vault_briefing", AsyncMock(return_value={}))
    monkeypatch.setattr(jb.mc_client, "vault_write_note", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(jb.jtools, "format_briefing_as_context", lambda b: "-")
    complete = AsyncMock(return_value="Guten Morgen.")
    monkeypatch.setattr(jb.frontier, "complete_text", complete)
    await _vault("openai_api_key", VAULT_KEY)

    res = await jb.run_briefing_once()
    assert res["ok"] is True
    assert complete.await_args.kwargs["api_key"] == VAULT_KEY


async def test_briefing_loop_starts_without_env_key(monkeypatch, env_empty):
    """The loop used to refuse to start when .env had no key — a key added in
    the app later would never have produced a briefing until a restart. The
    key is checked per run now (``run_briefing_once``), not at loop start."""
    import asyncio

    from app.services import jarvis_briefing as jb

    monkeypatch.setattr(settings, "jarvis_briefing_enabled", True)
    monkeypatch.setattr(jb, "_JARVIS_CORE_OK", True)

    slept: list[float] = []

    async def _cancel(delay):
        slept.append(delay)
        raise asyncio.CancelledError  # the loop treats this as shutdown

    monkeypatch.setattr(jb.asyncio, "sleep", _cancel)
    await jb.jarvis_briefing_loop()
    assert len(slept) == 1  # reached the daily sleep = the loop started


# ── dictation / voice notes (voice_transcription.py) ─────────────────────


async def test_dictation_transcribes_with_vault_key(monkeypatch, env_empty):
    from jarvis_core import brain

    from app.services.voice_transcription import get_voice_transcriber

    monkeypatch.setattr(settings, "stt_base_url", "")
    await _vault("openai_api_key", VAULT_KEY)
    transcribe = AsyncMock(return_value="diktat")
    monkeypatch.setattr(brain, "transcribe_audio", transcribe)

    transcriber = await get_voice_transcriber()
    assert transcriber is not None
    assert await transcriber(b"ogg") == "diktat"
    assert transcribe.await_args.kwargs["api_key"] == VAULT_KEY


async def test_dictation_without_any_key_is_off(monkeypatch, env_empty):
    from app.services.voice_transcription import get_voice_transcriber

    monkeypatch.setattr(settings, "stt_base_url", "")
    assert await get_voice_transcriber() is None


# ── Discord (discord.py) ─────────────────────────────────────────────────


async def test_discord_bot_uses_vault_token(monkeypatch, env_empty):
    from app.services import discord

    await _vault("discord_bot_token", VAULT_DISCORD)
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        def _ok(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        kwargs["transport"] = httpx.MockTransport(_ok)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(discord.httpx, "AsyncClient", _client)
    await discord.send_to_discord_channel("123", content="hi")
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == f"Bot {VAULT_DISCORD}"


# ── voice worker (separate process, fetches over the agent API) ──────────


async def _jarvis_headers(harness: str = "jarvis") -> dict[str, str]:
    import uuid

    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="J", slug=f"j-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)
        s.add(Agent(
            name=f"Jarvis-{uuid.uuid4().hex[:4]}", role="assistant", scopes=["tasks:read"],
            board_id=board.id, agent_token_hash=token_hash, agent_runtime="host",
            harness=harness,
        ))
        await s.commit()
    return {"Authorization": f"Bearer {raw}"}


async def test_voice_worker_gets_vault_key_over_the_api(client, env_empty):
    await _vault("openai_api_key", VAULT_KEY)
    r = await client.get("/api/v1/agent/voice/credentials", headers=await _jarvis_headers())
    assert r.status_code == 200
    assert r.json() == {"keys": {"OPENAI_API_KEY": VAULT_KEY}}


async def test_voice_worker_env_only_install_gets_nothing_over_the_wire(client, monkeypatch):
    """With the key only in .env the worker already has it in its own env —
    nothing is sent, the worker keeps using its env value."""
    monkeypatch.setattr(settings, "openai_api_key", ENV_KEY)
    r = await client.get("/api/v1/agent/voice/credentials", headers=await _jarvis_headers())
    assert r.status_code == 200
    assert r.json() == {"keys": {}}
    assert ENV_KEY not in r.text


async def test_voice_credentials_are_jarvis_only(client, env_empty):
    await _vault("openai_api_key", VAULT_KEY)
    r = await client.get(
        "/api/v1/agent/voice/credentials", headers=await _jarvis_headers(harness="omp")
    )
    assert r.status_code == 403
    assert VAULT_KEY not in r.text


def test_worker_applies_vault_key_then_falls_back_to_its_env():
    from jarvis_core.voice_provider import apply_vault_keys

    env = {"OPENAI_API_KEY": ENV_KEY}
    baseline = dict(env)
    apply_vault_keys({"OPENAI_API_KEY": VAULT_KEY}, env=env, baseline=baseline)
    assert env["OPENAI_API_KEY"] == VAULT_KEY
    # Key removed from the vault later: the worker's own env value comes back.
    apply_vault_keys({}, env=env, baseline=baseline)
    assert env["OPENAI_API_KEY"] == ENV_KEY
    # Backend unreachable (None): env stays what the container was started with.
    apply_vault_keys(None, env=env, baseline=baseline)
    assert env["OPENAI_API_KEY"] == ENV_KEY


def test_worker_vault_key_only_and_no_env():
    from jarvis_core.voice_provider import apply_vault_keys, resolve_voice_choice

    env: dict[str, str] = {}
    apply_vault_keys({"OPENAI_API_KEY": VAULT_KEY}, env=env, baseline={})
    # Before: "Neither OPENAI_API_KEY nor XAI_API_KEY is set" -> Jarvis silent.
    assert resolve_voice_choice(None, env=env).provider == "openai"
    apply_vault_keys({}, env=env, baseline={})
    assert "OPENAI_API_KEY" not in env


def test_worker_ignores_keys_it_does_not_know():
    from jarvis_core.voice_provider import apply_vault_keys

    env: dict[str, str] = {}
    apply_vault_keys({"PATH": "/evil", "OPENAI_API_KEY": VAULT_KEY}, env=env, baseline={})
    assert env == {"OPENAI_API_KEY": VAULT_KEY}


def test_worker_entrypoint_applies_vault_keys_before_choosing_a_provider():
    """voice_worker/main.py imports livekit (not installed in the backend test
    job), so the wiring is pinned on the source: credentials are fetched and
    applied before ``resolve_voice_choice`` decides the provider."""
    src = (Path(__file__).resolve().parents[2] / "voice_worker" / "main.py").read_text()
    body = src.split("async def entrypoint(", 1)[1].split("\ndef ", 1)[0]
    assert "mc_client.voice_credentials()" in body
    assert "apply_vault_keys(" in body
    assert body.index("apply_vault_keys(") < body.index("resolve_voice_choice(")
