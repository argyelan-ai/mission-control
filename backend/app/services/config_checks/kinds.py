"""The registry entries: every configuration check MC can run.

One :func:`register` call per kind. The old per-page test buttons
(Telegram, Slack, HuggingFace, embeddings, GitHub, host probe, runtime model
probe) call :func:`store.run_check` with their kind, so this file holds the
one procedure per check and the buttons only shape their old answer.

API keys are one kind (``secret``) with one :class:`SecretTester` per secret
key — a new provider key is a new ``SECRET_TESTERS`` entry, nothing else.
Keys that belong to a richer check (channel bots, GitHub, embeddings) are
tested by that kind and left out of ``secret``.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services.config_checks.registry import CheckKind, Outcome, Subject, register

_TIMEOUT = 10.0


# ── shared HTTP helper ────────────────────────────────────────────────────


async def _http_status(method: str, url: str, headers: dict, body: dict | None = None) -> int:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)
        return resp.status_code


def _status_outcome(code: int, provider: str) -> Outcome:
    if 200 <= code < 300:
        return Outcome(True, f"{provider} accepted the key (HTTP {code}).")
    if code in (401, 403):
        return Outcome(False, f"{provider} rejected the key (HTTP {code}) — expired, revoked or mistyped?")
    if code == 429:
        return Outcome(False, f"{provider} is rate limiting (HTTP 429) — try again later.")
    return Outcome(False, f"{provider} answered HTTP {code}.")


# ── kind: secret (API keys) ───────────────────────────────────────────────


@dataclass(frozen=True)
class SecretTester:
    cost: str
    run: Callable[[str], Awaitable[Outcome]]


async def hf_whoami(token: str) -> tuple[int, dict]:
    """HuggingFace ``whoami-v2`` — the HF twin of Telegram's ``getMe``."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            "https://huggingface.co/api/whoami-v2",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            return resp.status_code, resp.json()
        except Exception:  # noqa: BLE001 — HF served HTML/an empty body
            return resp.status_code, {}


async def _test_hf(token: str) -> Outcome:
    detail: dict = {"connected": False, "username": None, "error": None}
    try:
        code, data = await hf_whoami(token)
        if code == 200:
            detail["connected"] = True
            detail["username"] = data.get("name") or data.get("fullname")
        elif code == 401:
            detail["error"] = "Token abgelehnt (401) — abgelaufen oder widerrufen?"
        else:
            detail["error"] = f"HuggingFace antwortete mit HTTP {code}"
    except Exception as e:  # noqa: BLE001 — reportable state
        detail["error"] = f"{type(e).__name__}: {e}"
    if detail["connected"]:
        return Outcome(True, f"Signed in as {detail['username'] or 'a HuggingFace account'}.", detail)
    return Outcome(False, detail["error"] or "HuggingFace check failed.", detail)


async def _test_claude_token(token: str) -> Outcome:
    """Decision E8a: a real one-token request, so green means "Claude answers",
    not just "the token looks right". Spends a little plan quota — the cost
    hint makes the UI run it only on save and on request."""
    code = await _http_status(
        "POST",
        "https://api.anthropic.com/v1/messages",
        {
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        },
        {
            "model": settings.model_aliases.get("haiku") or settings.model_aliases.get("default"),
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    return _status_outcome(code, "Claude")


async def _test_openai(key: str) -> Outcome:
    code = await _http_status(
        "GET", "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}
    )
    return _status_outcome(code, "OpenAI")


async def _test_anthropic_api(key: str) -> Outcome:
    code = await _http_status(
        "GET",
        "https://api.anthropic.com/v1/models",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    return _status_outcome(code, "Anthropic")


async def _test_openrouter(key: str) -> Outcome:
    code = await _http_status(
        "GET", "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}
    )
    return _status_outcome(code, "OpenRouter")


async def _test_google(key: str) -> Outcome:
    code = await _http_status(
        "GET",
        "https://generativelanguage.googleapis.com/v1beta/models",
        {"x-goog-api-key": key},
    )
    return _status_outcome(code, "Google AI")


async def _test_discord(token: str) -> Outcome:
    code = await _http_status(
        "GET", "https://discord.com/api/v10/users/@me", {"Authorization": f"Bot {token}"}
    )
    return _status_outcome(code, "Discord")


#: One entry per API key MC can test. A key without an entry is listed as
#: "cannot be tested" — never as green.
SECRET_TESTERS: dict[str, SecretTester] = {
    "claude_code_oauth_token": SecretTester("quota", _test_claude_token),
    "anthropic_api_key": SecretTester("free", _test_anthropic_api),
    "openai_api_key": SecretTester("free", _test_openai),
    "openrouter_api_key": SecretTester("free", _test_openrouter),
    "google_api_key": SecretTester("free", _test_google),
    "hf_token": SecretTester("free", _test_hf),
    "discord_bot_token": SecretTester("free", _test_discord),
}

_UNDECRYPTABLE = "undecryptable"


def _owned_by_other_kinds() -> set[str]:
    """Secret keys a richer check owns — not listed again under ``secret``."""
    from app.services.ai_provider_config import (
        EMBEDDINGS_API_KEY_SECRET_KEY,
        EMBEDDINGS_CLOUD_API_KEY_SECRET_KEY,
    )
    from app.services.channel_config import TELEGRAM_TOKEN_SECRET_KEYS
    from app.services.github_config import OWNER_SECRET_KEY, TOKEN_SECRET_KEY
    from app.services.slack_client import APP_TOKEN_KEY, BOT_TOKEN_KEY

    return {
        BOT_TOKEN_KEY,
        APP_TOKEN_KEY,
        OWNER_SECRET_KEY,
        TOKEN_SECRET_KEY,
        EMBEDDINGS_API_KEY_SECRET_KEY,
        EMBEDDINGS_CLOUD_API_KEY_SECRET_KEY,
        *TELEGRAM_TOKEN_SECRET_KEYS,
    }


async def _secret_subjects(session: AsyncSession) -> list[Subject]:
    from app.models.secret import Secret
    from app.services.encryption import safe_decrypt

    owned = _owned_by_other_kinds()
    subjects = []
    for row in (await session.exec(select(Secret).order_by(Secret.key))).all():
        if row.key in owned:
            continue
        value = safe_decrypt(row.encrypted_value)
        tester = SECRET_TESTERS.get(row.key)
        subjects.append(
            Subject(
                id=row.key,
                label=row.label or row.key,
                configured=True,
                # An undecryptable value still gets a stable hash, so the
                # result clears once the right key/value is back.
                material=value if value is not None else (_UNDECRYPTABLE, row.encrypted_value),
                cost=tester.cost if tester else None,
                testable=tester is not None,
                secret_key=row.key,
            )
        )
    return subjects


async def _run_secret(session: AsyncSession, subject: Subject) -> Outcome:
    if isinstance(subject.material, tuple) and subject.material[:1] == (_UNDECRYPTABLE,):
        return Outcome(
            False,
            "Cannot decrypt the stored value — was it saved with another "
            "SECRETS_ENCRYPTION_KEY? Restore that .env or enter the value again.",
        )
    return await SECRET_TESTERS[subject.id].run(subject.material)


register(CheckKind("secret", "API keys", "free", _secret_subjects, _run_secret))


# ── kind: telegram (command + reports bot) ────────────────────────────────


async def telegram_get_me(token: str) -> dict:
    """Telegram ``getMe`` — the Telegram twin of Slack's ``auth.test``."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        return resp.json()


_TELEGRAM_BOTS = {
    # subject id → (token setting, chat id setting, label)
    "command_bot": ("telegram_bot_token", "telegram_chat_id", "Telegram command bot"),
    "reports_bot": ("telegram_reports_bot_token", "telegram_reports_chat_id", "Telegram reports bot"),
}


async def _telegram_subjects(session: AsyncSession) -> list[Subject]:
    return [
        Subject(
            id=sid,
            label=label,
            configured=bool(getattr(settings, token_attr)),
            material=getattr(settings, token_attr) or None,
            secret_key=token_attr,
        )
        for sid, (token_attr, _chat, label) in _TELEGRAM_BOTS.items()
    ]


def telegram_unset_entry(subject_id: str) -> dict:
    """The old button's answer for a bot without a token (nothing to call)."""
    token_attr, chat_attr, _ = _TELEGRAM_BOTS[subject_id]
    return {
        "token_set": bool(getattr(settings, token_attr)),
        "chat_id_set": bool(getattr(settings, chat_attr)),
        "connected": False,
        "bot_username": None,
        "error": None,
    }


async def _run_telegram(session: AsyncSession, subject: Subject) -> Outcome:
    entry = telegram_unset_entry(subject.id)
    try:
        data = await telegram_get_me(subject.material)
        if data.get("ok"):
            entry["connected"] = True
            entry["bot_username"] = data.get("result", {}).get("username")
        else:
            entry["error"] = str(data.get("description", "Telegram API error"))
    except Exception as e:  # noqa: BLE001 — reportable state, not a 500
        entry["error"] = f"{type(e).__name__}: {e}"
    if entry["connected"]:
        return Outcome(True, f"Bot @{entry['bot_username']} answers.", entry)
    return Outcome(False, entry["error"] or "Telegram check failed.", entry)


register(CheckKind("telegram", "Telegram bots", "free", _telegram_subjects, _run_telegram))


# ── kind: slack ───────────────────────────────────────────────────────────


async def _slack_subjects(session: AsyncSession) -> list[Subject]:
    from app.services.secrets_helper import get_secret_plaintext_by_key
    from app.services.slack_client import APP_TOKEN_KEY, BOT_TOKEN_KEY

    bot = (await get_secret_plaintext_by_key(session, BOT_TOKEN_KEY) or "").strip()
    app_token = (await get_secret_plaintext_by_key(session, APP_TOKEN_KEY) or "").strip()
    return [
        Subject(
            id="workspace",
            label="Slack workspace",
            configured=bool(bot),
            material=(bot, app_token) if bot else None,
            secret_key=BOT_TOKEN_KEY,
        )
    ]


async def _run_slack(session: AsyncSession, subject: Subject) -> Outcome:
    from app.services import slack_client

    result = await slack_client.test_connection(session)
    detail = result.to_dict()
    if result.connected:
        msg = f"Connected to {result.team or 'the workspace'} as {result.bot_user or 'the bot'}."
        if not result.socket_mode_ready:
            msg += " Socket Mode is not ready (app token)."
        return Outcome(True, msg, detail)
    return Outcome(False, result.error or "Slack check failed.", detail)


register(CheckKind("slack", "Slack", "free", _slack_subjects, _run_slack))


# ── kind: github ──────────────────────────────────────────────────────────


async def _github_subjects(session: AsyncSession) -> list[Subject]:
    from app.services.github_config import TOKEN_SECRET_KEY, resolve_github_config

    cfg = await resolve_github_config(session, fresh=True)
    return [
        Subject(
            id="connection",
            label="GitHub",
            configured=cfg.configured,
            material=(cfg.owner, cfg.token) if cfg.configured else None,
            secret_key=TOKEN_SECRET_KEY,
        )
    ]


async def _run_github(session: AsyncSession, subject: Subject) -> Outcome:
    """Live ``gh api`` probe: login, owner reachability, rate limit."""
    from app.services.git_service import GitService
    from app.services.github_config import OWNER_PATTERN

    owner = subject.material[0]
    detail: dict = {
        "connected": False,
        "login": None,
        "owner_type": None,
        "rate_limit_remaining": None,
        "rate_limit_total": None,
        "error": None,
    }
    git = GitService()
    try:
        user_out = await asyncio.wait_for(git._run_cmd("gh", "api", "user"), timeout=15)
        detail["login"] = json.loads(user_out).get("login")
        if re.match(OWNER_PATTERN, owner):
            owner_out = await asyncio.wait_for(git._run_cmd("gh", "api", f"users/{owner}"), timeout=15)
            detail["owner_type"] = json.loads(owner_out).get("type")
        else:
            raise RuntimeError(f"Ungültiger GitHub-Owner: {owner!r}")
        rate_out = await asyncio.wait_for(git._run_cmd("gh", "api", "rate_limit"), timeout=15)
        core = json.loads(rate_out).get("resources", {}).get("core", {})
        detail["rate_limit_remaining"] = core.get("remaining")
        detail["rate_limit_total"] = core.get("limit")
        detail["connected"] = True
    except asyncio.TimeoutError:
        detail["error"] = "GitHub nicht erreichbar (Timeout nach 15s)."
    except (RuntimeError, ValueError) as e:
        detail["error"] = str(e)[:300]
    if detail["connected"]:
        return Outcome(True, f"Signed in as {detail['login']}.", detail)
    return Outcome(False, detail["error"] or "GitHub check failed.", detail)


register(CheckKind("github", "GitHub", "rate_limited", _github_subjects, _run_github))


# ── kind: embeddings (the active provider) ────────────────────────────────


async def _embeddings_subjects(session: AsyncSession) -> list[Subject]:
    from app.services import ai_provider_config
    from app.services.embedding_provider import active_embedding_provider

    provider = active_embedding_provider()
    # The bearer key is part of what gets tested: without it a replaced key
    # would keep the old green (ADR-093 §2). It also puts the key into
    # ``scrub()``. Each arm reads its own key (see embedding_provider.headers).
    get_key = (
        ai_provider_config.get_embeddings_cloud_api_key
        if provider.key == "cloud"
        else ai_provider_config.get_embeddings_api_key
    )
    return [
        Subject(
            id=provider.key,
            label=provider.label,
            configured=True,
            material=(provider.url(), provider.model(), await get_key() or None),
            ref=provider,
        )
    ]


async def _run_embeddings(session: AsyncSession, subject: Subject) -> Outcome:
    """Embed one short string and check the vector size: a provider that
    answers with the wrong size would poison the existing collections."""
    from app.services.embedding_provider import EMBED_DIM

    provider = subject.ref
    detail: dict = {
        "provider": provider.key,
        "label": provider.label,
        "url": provider.url(),
        "model": provider.model(),
        "connected": False,
        "dimension": None,
        "expected_dimension": EMBED_DIM,
        "error": None,
    }
    try:
        vec = await provider.embed("ping")
        detail["connected"] = True
        detail["dimension"] = len(vec)
        if len(vec) != EMBED_DIM:
            detail["error"] = (
                f"Antwort hat {len(vec)} Dimensionen statt {EMBED_DIM} — "
                f"passt nicht zu den bestehenden Vektoren."
            )
    except Exception as e:  # noqa: BLE001 — reportable state, not a 500
        detail["error"] = f"{type(e).__name__}: {e}"
    if detail["connected"] and not detail["error"]:
        return Outcome(True, f"Answers with {detail['dimension']} dimensions.", detail)
    return Outcome(False, detail["error"] or "Embeddings check failed.", detail)


register(CheckKind("embeddings", "Embeddings", "free", _embeddings_subjects, _run_embeddings))


# ── kind: host (SSH-reachable boxes) ──────────────────────────────────────


async def _host_subjects(session: AsyncSession) -> list[Subject]:
    from app.models.host import Host
    from app.services.host_resolver import ssh_capable

    return [
        Subject(
            id=str(h.id),
            label=h.display_name or h.slug,
            configured=True,
            # Known limit: the key FILE's contents are not fingerprinted. Swapping
            # the key behind the same path keeps the old result; re-run the probe.
            material=(h.ssh_host, h.ssh_user, h.ssh_key_path),
            ref=h,
        )
        for h in (await session.exec(select(Host).order_by(Host.slug))).all()
        if ssh_capable(h)
    ]


async def _run_host(session: AsyncSession, subject: Subject) -> Outcome:
    from app.services import host_probe
    from app.services.host_resolver import resolved_host_from_row

    result = await host_probe.probe_host(resolved_host_from_row(subject.ref))
    if result.get("reachable"):
        return Outcome(True, "Reachable over SSH.", result)
    return Outcome(False, result.get("reason") or "Not reachable over SSH.", result)


register(CheckKind("host", "Boxes (SSH)", "free", _host_subjects, _run_host))


# ── kind: runtime (OpenAI-compatible model endpoints) ─────────────────────


async def _runtime_subjects(session: AsyncSession) -> list[Subject]:
    from app.models.runtime import Runtime
    from app.services.agent_runtime_switch import _PROBEABLE_RUNTIME_TYPES

    return [
        Subject(
            id=str(rt.id),
            label=rt.display_name or rt.slug,
            configured=bool(rt.endpoint),
            material=rt.endpoint or None,
            ref=rt,
        )
        for rt in (await session.exec(select(Runtime).order_by(Runtime.slug))).all()
        if rt.runtime_type in _PROBEABLE_RUNTIME_TYPES
    ]


async def _run_runtime(session: AsyncSession, subject: Subject) -> Outcome:
    from app.services import agent_runtime_switch

    model_id = await agent_runtime_switch.probe_runtime_model(subject.ref)
    detail = {"model_id": model_id}
    if model_id:
        return Outcome(True, f"Serves {model_id}.", detail)
    return Outcome(False, "No chat model answered at the endpoint.", detail)


register(CheckKind("runtime", "Model runtimes", "free", _runtime_subjects, _run_runtime))
