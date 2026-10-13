"""Live voice — cost and brakes (voice PR 6, ADR-091 draft).

Pins: the cost is computed by MC from token counts with the prices in
settings (never sent by the browser); cached tokens are billed once at the
cached price; totals only grow (a repeated or stale post changes nothing);
the daily budget refuses a new call before OpenAI is asked and tells a
running call to hang up; it resets at local midnight (Europe/Zurich); the
max length refuses tools afterwards and hangs up at OpenAI when the call id
is known; the Insights summary counts today and the last 7 days.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.models.voice_live_call import VoiceLiveCall
from app.services import voice_live, voice_live_tools, voice_live_usage

FAKE_KEY = "sk-test-" + "LONGLIVED-0123456789abcdef"
FAKE_EK = "ek_shortlived_abc123"
USAGE_URL = "/api/v1/voice/live/usage"
SESSION_URL = "/api/v1/voice/live/session"


@pytest.fixture(autouse=True)
def _clean_calls():
    voice_live_tools.reset_for_tests()
    yield
    voice_live_tools.reset_for_tests()
    for task in list(voice_live_usage._DEADLINES.values()):
        task.cancel()
    voice_live_usage._DEADLINES.clear()


@pytest.fixture
def voice_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", FAKE_KEY)
    monkeypatch.setattr(settings, "voice_live_daily_budget_usd", 5.0)
    monkeypatch.setattr(settings, "voice_live_max_call_s", 1200)


@pytest.fixture
def upstream(monkeypatch):
    """Mint answers OK; every OpenAI request is recorded."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/client_secrets"):
            return httpx.Response(
                200,
                json={"value": FAKE_EK, "expires_at": 1_900_000_000, "session": json.loads(request.content)["session"]},
            )
        return httpx.Response(200, json={})

    monkeypatch.setattr(voice_live, "_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(voice_live_usage, "_transport", lambda: httpx.MockTransport(handler))
    return seen


def _usage(**kw):
    base = {f: 0 for f in voice_live_usage.USAGE_FIELDS}
    base.update(kw)
    return base


async def _open(auth_client, make_agent, name="Boss"):
    agent = await make_agent(name=name)
    r = await auth_client.post(SESSION_URL, json={"agent_id": str(agent.id), "lang": "de"})
    assert r.status_code == 200, r.text
    return agent, r.json()


async def _rows(session):
    from sqlmodel import select

    return (await session.exec(select(VoiceLiveCall))).all()


# ── prices ───────────────────────────────────────────────────────────────


def test_cost_uses_settings_prices_and_bills_cached_once(monkeypatch):
    # 1M audio in (of which 100k cached), 1M audio out, 10k text in (all cached).
    usage = _usage(
        input_audio_tokens=1_000_000,
        cached_audio_tokens=100_000,
        output_audio_tokens=1_000_000,
        input_text_tokens=10_000,
        cached_text_tokens=10_000,
    )
    # 0.9M × $32 + 0.11M × $0.40 + 1M × $64 = 28.8 + 0.044 + 64
    assert voice_live_usage.cost_usd(usage) == pytest.approx(92.844)
    # Prices come from settings, not from the code.
    monkeypatch.setattr(settings, "voice_live_price_audio_out_per_m", 0.0)
    assert voice_live_usage.cost_usd(usage) == pytest.approx(28.844)


def test_cost_never_negative_when_cached_exceeds_input():
    assert voice_live_usage.cost_usd(_usage(cached_text_tokens=500, input_text_tokens=100)) > 0


# ── local day ────────────────────────────────────────────────────────────


def test_day_starts_at_zurich_midnight_summer_and_winter():
    # 13.10. 01:30 Zurich (CEST, UTC+2) = 12.10. 23:30 UTC → day began 12.10. 22:00 UTC.
    now = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)
    assert voice_live_usage.day_start(now) == datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)
    # Winter (CET, UTC+1).
    now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    assert voice_live_usage.day_start(now) == datetime(2026, 11, 30, 23, 0, tzinfo=timezone.utc)
    assert voice_live_usage.next_reset(now) == datetime(2026, 12, 1, 23, 0, tzinfo=timezone.utc)


# ── session: row + budget ────────────────────────────────────────────────


async def test_session_writes_a_call_row_and_returns_limits(auth_client, voice_on, upstream, make_agent, session):
    agent, body = await _open(auth_client, make_agent)
    assert body["limits"] == {"idle_hangup_s": 120, "idle_warn_s": 15, "max_call_s": 1200}
    rows = await _rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.agent_id == agent.id and row.agent_name == "Boss" and row.model == "gpt-realtime-2.1"
    # The handle itself is not stored, only its hash.
    assert row.ref_hash == voice_live_usage.ref_hash(body["call_ref"])
    assert body["call_ref"] not in row.ref_hash


async def test_budget_reached_refuses_new_call_before_openai(auth_client, voice_on, upstream, make_agent, session):
    session.add(VoiceLiveCall(ref_hash="a" * 64, agent_name="Boss", cost_usd=5.01))
    await session.commit()
    agent = await make_agent(name="Boss")
    r = await auth_client.post(SESSION_URL, json={"agent_id": str(agent.id)})
    assert r.status_code == 402
    assert r.json() == {"detail": {"reason": "budget_reached"}}
    assert upstream == []  # nothing asked of OpenAI


async def test_budget_just_below_still_allows_a_call(auth_client, voice_on, upstream, make_agent, session):
    session.add(VoiceLiveCall(ref_hash="b" * 64, agent_name="Boss", cost_usd=4.99))
    await session.commit()
    agent = await make_agent(name="Boss")
    r = await auth_client.post(SESSION_URL, json={"agent_id": str(agent.id)})
    assert r.status_code == 200


async def test_yesterdays_cost_does_not_count(auth_client, voice_on, upstream, make_agent, session):
    yesterday = voice_live_usage.day_start() - timedelta(minutes=1)
    session.add(VoiceLiveCall(ref_hash="c" * 64, agent_name="Boss", cost_usd=50.0, started_at=yesterday))
    await session.commit()
    agent = await make_agent(name="Boss")
    r = await auth_client.post(SESSION_URL, json={"agent_id": str(agent.id)})
    assert r.status_code == 200


async def test_budget_zero_means_no_budget(auth_client, voice_on, upstream, make_agent, session, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_daily_budget_usd", 0.0)
    session.add(VoiceLiveCall(ref_hash="d" * 64, agent_name="Boss", cost_usd=500.0))
    await session.commit()
    agent = await make_agent(name="Boss")
    r = await auth_client.post(SESSION_URL, json={"agent_id": str(agent.id)})
    assert r.status_code == 200


async def test_config_reports_budget_and_limits(auth_client, voice_on, session):
    session.add(VoiceLiveCall(ref_hash="e" * 64, agent_name="Boss", cost_usd=1.25))
    await session.commit()
    r = await auth_client.get("/api/v1/voice/live/config")
    assert r.status_code == 200
    body = r.json()
    assert body["limits"] == {"idle_hangup_s": 120, "idle_warn_s": 15, "max_call_s": 1200}
    assert body["budget"]["limit_usd"] == 5.0
    assert body["budget"]["spent_usd"] == pytest.approx(1.25)
    assert body["budget"]["reached"] is False
    assert body["budget"]["resets_at"]


# ── usage reports ────────────────────────────────────────────────────────


async def test_usage_post_computes_cost_server_side(auth_client, voice_on, upstream, make_agent, session):
    agent, body = await _open(auth_client, make_agent)
    r = await auth_client.post(
        USAGE_URL,
        json={
            "call_ref": body["call_ref"],
            "agent_id": str(agent.id),
            "usage": _usage(input_audio_tokens=10_000, output_audio_tokens=5_000),
            # A browser-sent price or cost is refused, not trusted.
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["call_cost_usd"] == pytest.approx(10_000 * 32e-6 + 5_000 * 64e-6)
    assert out["today_usd"] == pytest.approx(out["call_cost_usd"])
    assert out["hang_up"] is None
    row = (await _rows(session))[0]
    await session.refresh(row)
    assert row.input_audio_tokens == 10_000 and row.output_audio_tokens == 5_000


async def test_usage_refuses_a_cost_field(auth_client, voice_on, upstream, make_agent):
    agent, body = await _open(auth_client, make_agent)
    r = await auth_client.post(
        USAGE_URL,
        json={"call_ref": body["call_ref"], "agent_id": str(agent.id), "usage": _usage(), "cost_usd": 0},
    )
    assert r.status_code == 422


async def test_usage_totals_only_grow(auth_client, voice_on, upstream, make_agent, session):
    agent, body = await _open(auth_client, make_agent)
    ref = {"call_ref": body["call_ref"], "agent_id": str(agent.id)}
    await auth_client.post(USAGE_URL, json={**ref, "usage": _usage(output_audio_tokens=9_000)})
    # A stale, smaller post arrives late.
    r = await auth_client.post(USAGE_URL, json={**ref, "usage": _usage(output_audio_tokens=1_000)})
    assert r.json()["call_cost_usd"] == pytest.approx(9_000 * 64e-6)


async def test_usage_end_closes_the_row(auth_client, voice_on, upstream, make_agent, session):
    agent, body = await _open(auth_client, make_agent)
    r = await auth_client.post(
        USAGE_URL,
        json={
            "call_ref": body["call_ref"],
            "agent_id": str(agent.id),
            "usage": _usage(output_audio_tokens=100),
            "ended": True,
            "end_reason": "idle",
        },
    )
    assert r.status_code == 200
    row = (await _rows(session))[0]
    await session.refresh(row)
    assert row.ended_at is not None and row.end_reason == "idle"


async def test_usage_unknown_call_or_wrong_agent_refused(auth_client, voice_on, upstream, make_agent):
    agent, body = await _open(auth_client, make_agent)
    other = await make_agent(name="Other", slug="other-one")
    r = await auth_client.post(USAGE_URL, json={"call_ref": "nope", "agent_id": str(agent.id), "usage": _usage()})
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "call_unknown"
    r = await auth_client.post(USAGE_URL, json={"call_ref": body["call_ref"], "agent_id": str(other.id), "usage": _usage()})
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "wrong_agent"


async def test_usage_tells_a_running_call_to_hang_up_on_budget(auth_client, voice_on, upstream, make_agent, session):
    agent, body = await _open(auth_client, make_agent)
    ref = {"call_ref": body["call_ref"], "agent_id": str(agent.id)}
    # 80k output audio tokens = $5.12 > $5.00 budget.
    r = await auth_client.post(USAGE_URL, json={**ref, "usage": _usage(output_audio_tokens=80_000)})
    assert r.json()["hang_up"] == "budget_reached"
    # The next call is refused.
    r = await auth_client.post(SESSION_URL, json={"agent_id": str(agent.id)})
    assert r.status_code == 402


async def test_usage_budget_hangs_up_at_openai_when_call_id_known(auth_client, voice_on, upstream, make_agent):
    agent, body = await _open(auth_client, make_agent)
    ref = {"call_ref": body["call_ref"], "agent_id": str(agent.id), "openai_call_id": "rtc_abc123"}
    r = await auth_client.post(USAGE_URL, json={**ref, "usage": _usage(output_audio_tokens=80_000)})
    assert r.json()["hang_up"] == "budget_reached"
    hangups = [q for q in upstream if q.url.path.endswith("/hangup")]
    assert [str(q.url) for q in hangups] == ["https://api.openai.com/v1/realtime/calls/rtc_abc123/hangup"]
    assert hangups[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"


async def test_usage_rejects_a_call_id_that_could_shape_the_url(auth_client, voice_on, upstream, make_agent):
    agent, body = await _open(auth_client, make_agent)
    r = await auth_client.post(
        USAGE_URL,
        json={
            "call_ref": body["call_ref"],
            "agent_id": str(agent.id),
            "usage": _usage(),
            "openai_call_id": "rtc_x/../../v1/files",
        },
    )
    assert r.status_code == 422


async def test_usage_past_max_length_says_hang_up(auth_client, voice_on, upstream, make_agent, session):
    agent, body = await _open(auth_client, make_agent)
    row = (await _rows(session))[0]
    row.started_at = datetime.now(timezone.utc) - timedelta(seconds=1201)
    session.add(row)
    await session.commit()
    r = await auth_client.post(
        USAGE_URL, json={"call_ref": body["call_ref"], "agent_id": str(agent.id), "usage": _usage()}
    )
    assert r.json()["hang_up"] == "max_length"


async def test_usage_after_backend_restart_still_books(auth_client, voice_on, upstream, make_agent, session):
    """The in-memory call list is gone after a restart; the cost still lands."""
    agent, body = await _open(auth_client, make_agent)
    voice_live_tools.reset_for_tests()
    r = await auth_client.post(
        USAGE_URL,
        json={"call_ref": body["call_ref"], "agent_id": str(agent.id), "usage": _usage(output_audio_tokens=100), "ended": True},
    )
    assert r.status_code == 200


# ── max length: tools + server timer ─────────────────────────────────────


async def test_tools_refused_after_max_length(auth_client, voice_on, upstream, make_agent, monkeypatch):
    agent, body = await _open(auth_client, make_agent)
    monkeypatch.setattr(settings, "voice_live_max_call_s", 60)
    # The TTL is fixed at open; re-open with the short max to test the setting.
    ref = voice_live_tools.open_call(agent.id)
    real_now = voice_live_tools._now
    monkeypatch.setattr(voice_live_tools, "_now", lambda: real_now() + 60 + voice_live_usage.MAX_LENGTH_GRACE_S + 1)
    r = await auth_client.post(
        "/api/v1/voice/live/tool",
        json={"call_ref": ref, "agent_id": str(agent.id), "name": "get_agent_status", "arguments": {}},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "call_expired"


async def test_server_timer_hangs_up_at_max_length(voice_on, upstream, session):
    row = VoiceLiveCall(
        ref_hash="f" * 64,
        agent_name="Boss",
        openai_call_id="rtc_live1",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=1300),
    )
    session.add(row)
    await session.commit()
    assert await voice_live_usage.enforce_deadline(session, row.id) is True
    assert [q.url.path for q in upstream] == ["/v1/realtime/calls/rtc_live1/hangup"]
    await session.refresh(row)
    assert row.ended_at is not None and row.end_reason == "server_max_length"


async def test_server_timer_leaves_a_short_or_ended_call_alone(voice_on, upstream, session):
    young = VoiceLiveCall(ref_hash="g" * 64, agent_name="Boss", openai_call_id="rtc_young")
    ended = VoiceLiveCall(
        ref_hash="h" * 64,
        agent_name="Boss",
        openai_call_id="rtc_ended",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=1300),
        ended_at=datetime.now(timezone.utc),
    )
    session.add(young)
    session.add(ended)
    await session.commit()
    assert await voice_live_usage.enforce_deadline(session, young.id) is False
    assert await voice_live_usage.enforce_deadline(session, ended.id) is False
    assert upstream == []


async def test_hangup_never_logs_the_key(voice_on, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(
        voice_live_usage,
        "_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(500, json={"error": {"message": FAKE_KEY}})),
    )
    with caplog.at_level(logging.DEBUG):
        await voice_live_usage.hangup_upstream("rtc_x1")
    assert FAKE_KEY not in caplog.text


# ── Insights summary ─────────────────────────────────────────────────────


async def test_summary_today_and_seven_days(auth_client, voice_on, session):
    now = datetime.now(timezone.utc)
    today0 = voice_live_usage.day_start(now)
    session.add(
        VoiceLiveCall(
            ref_hash="1" * 64, agent_name="Boss", cost_usd=0.30,
            started_at=today0 + timedelta(minutes=1), last_seen_at=today0 + timedelta(minutes=4),
        )
    )
    session.add(
        VoiceLiveCall(
            ref_hash="2" * 64, agent_name="Boss", cost_usd=0.70,
            started_at=today0 - timedelta(days=3), last_seen_at=today0 - timedelta(days=3) + timedelta(minutes=6),
        )
    )
    session.add(  # 8 days ago: outside the 7-day window
        VoiceLiveCall(
            ref_hash="3" * 64, agent_name="Boss", cost_usd=9.0,
            started_at=today0 - timedelta(days=8), last_seen_at=today0 - timedelta(days=8) + timedelta(minutes=6),
        )
    )
    session.add(  # minted but never connected: not a call
        VoiceLiveCall(ref_hash="4" * 64, agent_name="Boss", started_at=today0 + timedelta(minutes=2),
                      last_seen_at=today0 + timedelta(minutes=2))
    )
    await session.commit()
    r = await auth_client.get("/api/v1/voice/live/usage/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["today"] == {"calls": 1, "seconds": 180, "cost_usd": pytest.approx(0.30)}
    assert body["week"] == {"calls": 2, "seconds": 540, "cost_usd": pytest.approx(1.00)}
    assert body["budget_usd"] == 5.0
    assert body["enabled"] is True
    assert body["per_minute_usd"] == pytest.approx(1.00 / 9, abs=1e-4)


async def test_summary_works_with_the_switch_off(auth_client, monkeypatch):
    """History outlives the switch: Insights still shows what was spent."""
    monkeypatch.setattr(settings, "voice_live_enabled", False)
    r = await auth_client.get("/api/v1/voice/live/usage/summary")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


async def test_agent_delete_keeps_cost_history(auth_client, voice_on, upstream, make_agent, session):
    from unittest.mock import patch

    from app.utils import utcnow

    agent = await make_agent(name="Gone", slug="gone-one", agent_runtime="cli-bridge", archived_at=utcnow())
    r = await auth_client.post(SESSION_URL, json={"agent_id": str(agent.id)})
    assert r.status_code == 200
    with patch("app.services.docker_agent_sync.remove_docker_agent_container", return_value={"ok": "true"}):
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 204, resp.text
    rows = await _rows(session)
    assert len(rows) == 1
    await session.refresh(rows[0])
    assert rows[0].agent_id is None and rows[0].agent_name == "Gone"

