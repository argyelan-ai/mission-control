"""Insights — box totals next to MC's own local tokens.

A box's own counters (runtime_token_hours) see every request, MC's
transcripts only the ones MC sent. Per day and week Insights now shows the
box total, what MC attributed to local models, and the difference as
"external" (direct API use from other machines) — never negative: when MC
counted more than the box saw, the difference is clamped to 0 and flagged.
"""
import uuid
from datetime import datetime, timezone

from app.models.model_usage import ModelUsageEvent
from app.models.runtime_token import RuntimeTokenCounter, RuntimeTokenHour
from app.services.usage_baseline import compute_daily_usage, compute_weekly_baseline

HOST = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # Wednesday
SINCE = datetime(2026, 10, 12, 8, 15, tzinfo=timezone.utc)  # Monday morning


def _since(session, at=SINCE):
    session.add(RuntimeTokenCounter(host_id=HOST, engine="tensorfold", first_seen_at=at, updated_at=at))


def _box(hour, prompt, cached, generated, engine="tensorfold", host=HOST):
    return RuntimeTokenHour(host_id=host, engine=engine, hour=hour, prompt_tokens=prompt,
                            cached_tokens=cached, generated_tokens=generated)


def _mc(ts, *, locality="local", inp=300, cr=600, cw=100, out=200):
    return ModelUsageEvent(
        id=uuid.uuid4(), harness="cli-bridge", agent_id=None, model="local-model", session_id="s",
        message_uuid=f"b-{uuid.uuid4()}", input_tokens=inp, output_tokens=out, cache_read_tokens=cr,
        cache_write_tokens=cw, cost_usd=0.0, ts=ts, source_file="/x.jsonl", locality=locality,
    )


def _days(result):
    return {d["date"]: d for d in result["days"]}


async def test_day_shows_box_total_via_mc_and_external(session):
    _since(session)
    session.add_all([
        _box(datetime(2026, 10, 13, 9, tzinfo=timezone.utc), 6000, 5400, 300),
        _box(datetime(2026, 10, 13, 10, tzinfo=timezone.utc), 4000, 3600, 200),
        _mc(datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)),
        # Cloud usage is not on the box and must not be subtracted.
        _mc(datetime(2026, 10, 13, 9, 40, tzinfo=timezone.utc), locality="cloud", inp=99_999, out=99_999),
    ])
    await session.commit()

    day = _days(await compute_daily_usage(session, days=7, now=NOW, tz="UTC"))["2026-10-13"]
    assert day["box"] == {"prompt_tokens": 10_000, "cached_tokens": 9000, "generated_tokens": 500,
                          "fresh_tokens": 1500}
    # MC local: prompt = input + cache read + cache write; fresh = prompt - cached + generated.
    assert day["via_mc_local"] == {"prompt_tokens": 1000, "cached_tokens": 600, "generated_tokens": 200,
                                   "fresh_tokens": 600}
    assert day["external"] == {"prompt_tokens": 9000, "cached_tokens": 8400, "generated_tokens": 300,
                               "fresh_tokens": 900, "clamped": False}
    # Existing fields untouched.
    assert day["events"] == 2 and day["generated_tokens"] == 300 + 200 + 99_999 * 2


async def test_external_never_goes_negative_and_says_so(session):
    _since(session)
    session.add_all([
        _box(datetime(2026, 10, 13, 9, tzinfo=timezone.utc), 500, 100, 50),
        _mc(datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)),  # prompt 1000, cached 600, gen 200
    ])
    await session.commit()

    ext = _days(await compute_daily_usage(session, days=7, now=NOW, tz="UTC"))["2026-10-13"]["external"]
    assert ext == {"prompt_tokens": 0, "cached_tokens": 0, "generated_tokens": 0, "fresh_tokens": 0,
                   "clamped": True}


async def test_days_before_counting_started_have_no_box_numbers(session):
    _since(session)
    session.add(_mc(datetime(2026, 10, 11, 9, tzinfo=timezone.utc)))
    await session.commit()

    result = await compute_daily_usage(session, days=7, now=NOW, tz="UTC")
    assert result["box_tracking_since"] == SINCE.isoformat()
    days = _days(result)
    assert days["2026-10-11"]["box"] is None
    assert days["2026-10-11"]["external"] is None
    assert days["2026-10-11"]["via_mc_local"] is None
    # From the first day on, an idle box is an honest zero, not a gap.
    assert days["2026-10-14"]["box"] == {"prompt_tokens": 0, "cached_tokens": 0, "generated_tokens": 0,
                                         "fresh_tokens": 0}


async def test_mc_usage_before_the_counting_hour_is_not_subtracted(session):
    _since(session)
    session.add_all([
        _box(datetime(2026, 10, 12, 9, tzinfo=timezone.utc), 1000, 0, 100),
        _mc(datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)),  # before the box was first seen
    ])
    await session.commit()

    day = _days(await compute_daily_usage(session, days=7, now=NOW, tz="UTC"))["2026-10-12"]
    assert day["via_mc_local"]["fresh_tokens"] == 0
    assert day["external"]["fresh_tokens"] == 1100


async def test_without_any_box_everything_box_related_is_none(session):
    session.add(_mc(datetime(2026, 10, 13, 9, tzinfo=timezone.utc)))
    await session.commit()
    result = await compute_daily_usage(session, days=3, now=NOW, tz="UTC")
    assert result["box_tracking_since"] is None
    assert all(d["box"] is None and d["external"] is None for d in result["days"])


async def test_boxes_add_up_and_weeks_agree_with_days(session):
    _since(session)
    other = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
    session.add_all([
        _box(datetime(2026, 10, 12, 9, tzinfo=timezone.utc), 1000, 800, 100),
        _box(datetime(2026, 10, 13, 9, tzinfo=timezone.utc), 2000, 1500, 200, host=other, engine="vllm"),
        _mc(datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)),
    ])
    await session.commit()

    weeks = (await compute_weekly_baseline(session, weeks=2, now=NOW, tz="UTC"))["weeks"]
    previous, current = weeks
    assert previous["totals"]["box"] is None  # the week before counting began
    assert current["totals"]["box"] == {"prompt_tokens": 3000, "cached_tokens": 2300,
                                        "generated_tokens": 300, "fresh_tokens": 1000}
    assert current["totals"]["external"]["fresh_tokens"] == 1000 - 600
    weekly_result = await compute_weekly_baseline(session, weeks=2, now=NOW, tz="UTC")
    assert weekly_result["box_tracking_since"] == SINCE.isoformat()


async def test_by_day_api_carries_the_box_fields(auth_client, session):
    _since(session)
    session.add(_box(datetime(2026, 10, 13, 9, tzinfo=timezone.utc), 1000, 0, 100))
    await session.commit()
    resp = await auth_client.get("/api/v1/intelligence/costs/by-day?days=7&tz=UTC")
    assert resp.status_code == 200
    body = resp.json()
    assert "box_tracking_since" in body
    assert {"box", "via_mc_local", "external"} <= set(body["days"][-1])
    resp = await auth_client.get("/api/v1/intelligence/costs/by-week?weeks=2&tz=UTC")
    assert {"box", "via_mc_local", "external"} <= set(resp.json()["weeks"][-1]["totals"])
