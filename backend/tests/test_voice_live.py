"""Live voice in the chat — client-secret minting (voice PR 1, ADR-091 draft).

Pins: switch off / no key → 404; admin only; the long-lived OpenAI key never
appears in a response body or a log line (also on upstream errors that echo
it); quota → ``no_credits``; the session config is server-owned (model,
semantic VAD with barge-in, transcription, persona) and the persona is a
snapshot (a wording change must be a visible diff).
"""
from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest

from app.config import settings
from app.services import voice_live

# Split so secret scanners do not flag the made-up test key.
FAKE_KEY = "sk-test-" + "LONGLIVED-0123456789abcdef"
FAKE_EK = "ek_shortlived_abc123"


@pytest.fixture
def voice_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", FAKE_KEY)


def _upstream(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(voice_live, "_transport", lambda: httpx.MockTransport(_wrapped))
    return seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "value": FAKE_EK,
            "expires_at": 1_900_000_000,
            "session": json.loads(request.content)["session"],
        },
    )


async def _agent(make_agent, name="Boss"):
    return await make_agent(name=name)


# ── switch and auth ──────────────────────────────────────────────────────


async def test_config_404_when_switch_off(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", False)
    monkeypatch.setattr(settings, "openai_api_key", FAKE_KEY)
    r = await auth_client.get("/api/v1/voice/live/config")
    assert r.status_code == 404


async def test_config_404_when_key_missing(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    r = await auth_client.get("/api/v1/voice/live/config")
    assert r.status_code == 404


async def test_config_on(auth_client, voice_on):
    r = await auth_client.get("/api/v1/voice/live/config")
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "model": "gpt-realtime-2.1", "voice": "marin"}
    assert FAKE_KEY not in r.text


async def test_session_404_when_switch_off_and_no_upstream_call(auth_client, make_agent, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", False)
    monkeypatch.setattr(settings, "openai_api_key", FAKE_KEY)
    seen = _upstream(monkeypatch, _ok)
    agent = await _agent(make_agent)
    r = await auth_client.post("/api/v1/voice/live/session", json={"agent_id": str(agent.id)})
    assert r.status_code == 404
    assert seen == []


async def test_session_requires_login(client, voice_on, make_agent):
    agent = await _agent(make_agent)
    r = await client.post("/api/v1/voice/live/session", json={"agent_id": str(agent.id)})
    assert r.status_code in (401, 403)


@pytest.mark.parametrize("role", ["operator", "viewer"])
async def test_session_admin_only(client, voice_on, make_agent, monkeypatch, role):
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.auth import create_access_token
    from app.models.user import User
    from tests.conftest import test_engine

    seen = _upstream(monkeypatch, _ok)
    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"{role}@mc.local", name=role, role=role, is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), role)}"
    agent = await _agent(make_agent)
    r = await client.post("/api/v1/voice/live/session", json={"agent_id": str(agent.id)})
    assert r.status_code == 403
    assert seen == []


async def test_session_unknown_agent_404(auth_client, voice_on, monkeypatch):
    seen = _upstream(monkeypatch, _ok)
    r = await auth_client.post("/api/v1/voice/live/session", json={"agent_id": str(uuid.uuid4())})
    assert r.status_code == 404
    assert seen == []


async def test_session_rejects_unknown_lang(auth_client, voice_on, make_agent, monkeypatch):
    _upstream(monkeypatch, _ok)
    agent = await _agent(make_agent)
    r = await auth_client.post(
        "/api/v1/voice/live/session", json={"agent_id": str(agent.id), "lang": "fr"}
    )
    assert r.status_code == 422


# ── minting ──────────────────────────────────────────────────────────────


async def test_session_mints_client_secret_with_server_owned_config(
    auth_client, voice_on, make_agent, monkeypatch
):
    seen = _upstream(monkeypatch, _ok)
    agent = await _agent(make_agent, name="Boss")
    r = await auth_client.post(
        "/api/v1/voice/live/session", json={"agent_id": str(agent.id), "lang": "de"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.pop("call_ref"), str)
    assert body == {
        "client_secret": FAKE_EK,
        "expires_at": 1_900_000_000,
        "model": "gpt-realtime-2.1",
        "voice": "marin",
        "calls_url": "https://api.openai.com/v1/realtime/calls",
        "agent_name": "Boss",
    }

    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == "https://api.openai.com/v1/realtime/client_secrets"
    assert req.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    sent = json.loads(req.content)
    assert sent["expires_after"] == {"anchor": "created_at", "seconds": 60}
    s = sent["session"]
    assert s["type"] == "realtime"
    assert s["model"] == "gpt-realtime-2.1"
    assert s["audio"]["output"]["voice"] == "marin"
    assert s["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "create_response": True,
        "interrupt_response": True,
    }
    assert s["audio"]["input"]["transcription"]["model"]
    assert "language" not in s["audio"]["input"]["transcription"]
    # PR 2: two hands, both run by MC — never a stop/keys tool (PR 4).
    assert [t["name"] for t in s["tools"]] == ["send_to_agent", "get_agent_status"]
    assert s["tool_choice"] == "auto"
    assert "Boss" in s["instructions"]
    assert "German" in s["instructions"].split("# Language and length")[1].splitlines()[1]


@pytest.mark.parametrize(
    "status,payload,reason,expected_status",
    [
        (429, {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}, "no_credits", 402),
        (429, {"error": {"type": "requests", "code": "rate_limit_exceeded"}}, "rate_limited", 429),
        (401, {"error": {"code": "invalid_api_key"}}, "upstream_auth", 502),
        (500, {"error": {"code": "server_error"}}, "upstream_error", 502),
    ],
)
async def test_upstream_errors_map_to_reason_without_key(
    auth_client, voice_on, make_agent, monkeypatch, caplog, status, payload, reason, expected_status
):
    # Upstream echoes the key in its message (OpenAI does this for a wrong
    # key: "Incorrect API key provided: sk-…") — it must not travel on.
    payload["error"]["message"] = f"Incorrect API key provided: {FAKE_KEY}"
    _upstream(monkeypatch, lambda _r: httpx.Response(status, json=payload))
    agent = await _agent(make_agent)
    with caplog.at_level(logging.DEBUG):
        r = await auth_client.post("/api/v1/voice/live/session", json={"agent_id": str(agent.id)})
    assert r.status_code == expected_status
    assert r.json() == {"detail": {"reason": reason}}
    assert FAKE_KEY not in r.text
    assert FAKE_KEY not in caplog.text


async def test_network_error_no_key_in_logs(auth_client, voice_on, make_agent, monkeypatch, caplog):
    def _boom(request):
        raise httpx.ConnectError(f"connect failed for {request.headers['Authorization']}")

    _upstream(monkeypatch, _boom)
    agent = await _agent(make_agent)
    with caplog.at_level(logging.DEBUG):
        r = await auth_client.post("/api/v1/voice/live/session", json={"agent_id": str(agent.id)})
    assert r.status_code == 502
    assert r.json() == {"detail": {"reason": "upstream_unreachable"}}
    assert FAKE_KEY not in r.text
    assert FAKE_KEY not in caplog.text


async def test_never_hands_out_the_long_lived_key(auth_client, voice_on, make_agent, monkeypatch, caplog):
    """Sabotage guard: an upstream (or a bug) that answers with the real key
    as 'value' must not reach the browser."""
    _upstream(monkeypatch, lambda _r: httpx.Response(200, json={"value": FAKE_KEY, "expires_at": 1}))
    agent = await _agent(make_agent)
    with caplog.at_level(logging.DEBUG):
        r = await auth_client.post("/api/v1/voice/live/session", json={"agent_id": str(agent.id)})
    assert r.status_code == 502
    assert FAKE_KEY not in r.text
    assert FAKE_KEY not in caplog.text


async def test_success_logs_no_secret(auth_client, voice_on, make_agent, monkeypatch, caplog):
    _upstream(monkeypatch, _ok)
    agent = await _agent(make_agent)
    with caplog.at_level(logging.DEBUG):
        r = await auth_client.post("/api/v1/voice/live/session", json={"agent_id": str(agent.id)})
    assert r.status_code == 200
    assert FAKE_KEY not in caplog.text
    assert FAKE_EK not in caplog.text


# ── persona ──────────────────────────────────────────────────────────────


def test_persona_v2_snapshot():
    text = voice_live.build_instructions("Boss", "en")
    assert voice_live.PERSONA_VERSION == "v2"
    # Order recommended by OpenAI's realtime prompting guide (bauplan B2).
    heads = [line for line in text.splitlines() if line.startswith("# ")]
    assert heads == [
        "# Personality",
        "# Language and length",
        "# Backchannel",
        "# Interruptions",
        "# Delegation",
        "# Status",
        "# Progress updates",
        "# Hard rules",
    ]
    for rule in (
        "Notiert, geb ich weiter.",
        "Got it, passing it on.",
        "never a cancel",
        "Do NOT delegate only when",
        "Default: pass it on.",
        "Never say something is done, finished or successful unless Boss reported it",
        "Approvals are never given by voice",
        "bitte im Chat antippen",
        "Stopping the agent by voice is not available yet",
        "Stop button",
        "One or two short sentences",
        # voice PR 3: progress items + the no-success guard
        "[MC progress",
        "agent_final=true",
        "[MC correction",
        "Never interrupt the operator",
    ):
        assert rule in text, rule
    assert "not built yet" not in text  # PR 1 wording is gone
    assert "Start in English" in text
    assert "Start in German" in voice_live.build_instructions("Boss", "de")


def test_persona_clamps_agent_name():
    text = voice_live.build_instructions("x" * 500, "en")
    assert "x" * 81 not in text



def test_voice_delegates_by_default_not_general_questions():
    """Live test 13.10.2026: "Antworte bitte nur mit dem Wort Kirsche" was
    answered by the voice itself instead of reaching the agent. The operator's
    rule: the chat mic talks to THIS agent (like Hermes) — the voice answers
    itself only for status, greetings/line checks and approvals."""
    from app.services import voice_live
    src = __import__("inspect").getsource(voice_live)
    assert "general question you can answer" not in src
    assert "Default: pass it on." in src
