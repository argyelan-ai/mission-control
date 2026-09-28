"""Insights heatmap — tokens and list-price cost per calendar day.

Days are counted in the operator's zone (Europe/Zurich in these tests), not in
UTC, and share the query and source rules of the weekly baseline, so a day in
the heatmap and a week in the table can never disagree.
"""
import uuid
from datetime import date, datetime, timezone

import pytest

from app.models.model_usage import ModelUsageEvent
from app.services.usage_baseline import MAX_DAYS, compute_daily_usage, compute_weekly_baseline

ZRH = "Europe/Zurich"
# Wednesday 2026-10-28, after the switch to winter time (Sunday 2026-10-25).
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _row(ts, *, harness="cli-bridge", locality=None, inp=100, out=10, cr=1000, cw=50, cost=1.0):
    return ModelUsageEvent(
        id=uuid.uuid4(), harness=harness, agent_id=None, model="claude-opus-5", session_id="s",
        message_uuid=f"d-{uuid.uuid4()}", input_tokens=inp, output_tokens=out,
        cache_read_tokens=cr, cache_write_tokens=cw, cost_usd=cost, ts=ts,
        source_file="/x.jsonl", locality=locality,
        head_run_id=str(uuid.uuid4()) if harness.startswith("head-") else None,
    )


def _days(result):
    return {d["date"]: d for d in result["days"]}


async def test_days_follow_the_zone_across_the_dst_switch(session):
    session.add_all([
        # Sat 24.10. 23:30 summer time (UTC+2)
        _row(datetime(2026, 10, 24, 21, 30, tzinfo=timezone.utc)),
        # Sun 25.10. 00:30 summer time — UTC still says the 24th
        _row(datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc)),
        # Sun 25.10. 23:30 winter time (UTC+1) — last hour of the 25-hour day
        _row(datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc)),
        # Mon 26.10. 00:30 winter time — UTC still says the 25th
        _row(datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)),
    ])
    await session.commit()

    local = _days(await compute_daily_usage(session, days=7, now=NOW, tz=ZRH))
    assert (local["2026-10-24"]["events"], local["2026-10-25"]["events"], local["2026-10-26"]["events"]) == (1, 2, 1)

    utc = _days(await compute_daily_usage(session, days=7, now=NOW, tz="UTC"))
    assert (utc["2026-10-24"]["events"], utc["2026-10-25"]["events"], utc["2026-10-26"]["events"]) == (2, 2, 0)


async def test_days_follow_the_zone_across_the_spring_switch(session):
    # Sun 29.03.2026: 02:00 winter time jumps to 03:00 summer time.
    session.add_all([
        _row(datetime(2026, 3, 28, 23, 30, tzinfo=timezone.utc)),  # Sun 29.03. 00:30 (UTC+1)
        _row(datetime(2026, 3, 29, 21, 30, tzinfo=timezone.utc)),  # Sun 29.03. 23:30 (UTC+2)
        _row(datetime(2026, 3, 29, 22, 30, tzinfo=timezone.utc)),  # Mon 30.03. 00:30 (UTC+2)
    ])
    await session.commit()

    now = datetime(2026, 3, 31, 12, tzinfo=timezone.utc)
    days = _days(await compute_daily_usage(session, days=4, now=now, tz=ZRH))
    assert (days["2026-03-28"]["events"], days["2026-03-29"]["events"], days["2026-03-30"]["events"]) == (0, 2, 1)


async def test_every_day_is_listed_oldest_first_with_today_last(session):
    result = await compute_daily_usage(session, days=3, now=NOW, tz=ZRH)
    assert [d["date"] for d in result["days"]] == ["2026-10-26", "2026-10-27", "2026-10-28"]
    assert result["start"] == "2026-10-26"
    assert result["tz"] == ZRH
    assert all(d["events"] == 0 and d["top_source"] is None for d in result["days"])


async def test_window_starts_at_local_midnight(session):
    session.add_all([
        _row(datetime(2026, 10, 25, 22, 59, tzinfo=timezone.utc)),  # Sun 25.10. 23:59 local: before
        _row(datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc)),   # Mon 26.10. 00:00 local: inside
    ])
    await session.commit()

    result = await compute_daily_usage(session, days=3, now=NOW, tz=ZRH)
    assert sum(d["events"] for d in result["days"]) == 1


async def test_generated_tokens_leave_cache_reads_out(session):
    ts = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
    session.add_all([
        _row(ts, inp=100, out=10, cr=5000, cw=50, cost=2.0),
        _row(ts, harness="head-omp", locality="local", inp=40, out=60, cr=900, cw=0, cost=None),
    ])
    await session.commit()

    day = _days(await compute_daily_usage(session, days=2, now=NOW, tz=ZRH))["2026-10-27"]
    assert day["generated_tokens"] == 210  # input + output, no cache
    assert day["local_generated_tokens"] == 100
    assert day["total_tokens"] == 6160
    assert day["cost_usd"] == pytest.approx(2.0)
    assert day["unpriced_events"] == 1
    assert day["local_output_share"] == pytest.approx(60 / 70)
    assert day["top_source"] == "unattributed"  # 110 generated vs. the head's 100


async def test_days_parameter_is_bounded(session):
    assert len((await compute_daily_usage(session, days=0, now=NOW, tz=ZRH))["days"]) == 1
    assert len((await compute_daily_usage(session, days=5000, now=NOW, tz=ZRH))["days"]) == MAX_DAYS


async def test_unknown_zone_is_rejected(session):
    with pytest.raises(ValueError):
        await compute_daily_usage(session, days=1, now=NOW, tz="Mars/Olympus")


async def test_weeks_can_follow_the_zone_too(session):
    # Mon 26.10. 00:30 local = Sun 25.10. 23:30 UTC (ISO week 43 in UTC, 44 locally)
    session.add(_row(datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)))
    await session.commit()

    local = await compute_weekly_baseline(session, weeks=2, now=NOW, tz=ZRH)
    utc = await compute_weekly_baseline(session, weeks=2, now=NOW)
    assert [w["totals"]["events"] for w in local["weeks"]] == [0, 1]
    assert [w["totals"]["events"] for w in utc["weeks"]] == [1, 0]
    assert local["weeks"][-1]["totals"]["generated_tokens"] == 110


async def test_heatmap_day_and_week_agree(session):
    for d in range(19, 29):
        session.add(_row(datetime(2026, 10, d, 9, tzinfo=timezone.utc), inp=d, out=1, cost=0.1 * d))
    await session.commit()

    days = await compute_daily_usage(session, days=10, now=NOW, tz=ZRH)
    weeks = await compute_weekly_baseline(session, weeks=2, now=NOW, tz=ZRH)
    this_week = [d for d in days["days"] if date.fromisoformat(d["date"]) >= date(2026, 10, 26)]
    assert sum(d["generated_tokens"] for d in this_week) == weeks["weeks"][-1]["totals"]["generated_tokens"]
    assert sum(d["cost_usd"] for d in this_week) == pytest.approx(weeks["weeks"][-1]["totals"]["cost_usd"])


async def test_endpoint_returns_days_in_the_requested_zone(auth_client, session):
    session.add(_row(datetime.now(timezone.utc), cost=0.25))
    await session.commit()

    resp = await auth_client.get(f"/api/v1/intelligence/costs/by-day?days=2&tz={ZRH}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tz"] == ZRH
    assert len(body["days"]) == 2
    assert body["days"][-1]["events"] == 1


async def test_endpoint_defaults_to_the_configured_zone(auth_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "usage_timezone", ZRH)
    resp = await auth_client.get("/api/v1/intelligence/costs/by-day?days=1")
    assert resp.status_code == 200
    assert resp.json()["tz"] == ZRH


async def test_endpoint_rejects_an_unknown_zone(auth_client):
    resp = await auth_client.get("/api/v1/intelligence/costs/by-day?tz=Mars/Olympus")
    assert resp.status_code == 422


async def test_by_week_endpoint_accepts_a_zone(auth_client):
    resp = await auth_client.get(f"/api/v1/intelligence/costs/by-week?weeks=1&tz={ZRH}")
    assert resp.status_code == 200
    assert resp.json()["tz"] == ZRH


async def test_endpoint_requires_login(client):
    resp = await client.get("/api/v1/intelligence/costs/by-day")
    assert resp.status_code in (401, 403)


@pytest.mark.postgres
async def test_postgres_lane_counts_days_in_the_zone(session):
    # date_trunc on the real schema — the SQLite lane only sees strftime.
    await test_days_follow_the_zone_across_the_dst_switch(session)
