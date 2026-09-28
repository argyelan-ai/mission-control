"""E0 token baseline — tokens and list-price cost per ISO week × source.

Source buckets come only from columns the harvester already writes
(harness, agent_id, source_file, locality); anything else is "unattributed".
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models.host import Host
from app.models.model_usage import ModelUsageEvent
from app.models.runtime import Runtime
from app.services.transcript_chat import encode_cwd
from app.services.usage_baseline import compute_weekly_baseline

# Thursday of ISO week 2026-W39 (Monday 2026-09-21).
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
W39 = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
W38 = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
HOME = "/home/op"
LEAD_DIR = encode_cwd(f"{HOME}/.mc/checkouts/mission-control")


@pytest.fixture(autouse=True)
def _host_home(monkeypatch):
    monkeypatch.setenv("HOME_HOST", HOME)


def _row(harness="host", *, ts=W39, agent_id=None, model="claude-opus-5", source_file="/x.jsonl",
         locality=None, inp=100, out=10, cr=1000, cw=50, cost=1.0):
    return ModelUsageEvent(
        id=uuid.uuid4(), harness=harness, agent_id=agent_id, model=model, session_id="s",
        message_uuid=f"b-{uuid.uuid4()}", input_tokens=inp, output_tokens=out,
        cache_read_tokens=cr, cache_write_tokens=cw, cost_usd=cost, ts=ts,
        source_file=source_file, locality=locality,
        head_run_id=str(uuid.uuid4()) if harness.startswith("head-") else None,
    )


def _week(result, key):
    return next(w for w in result["weeks"] if w["week"] == key)


def _sources(week):
    return {s["source"]: s for s in week["sources"]}


async def test_sources_come_from_existing_columns(session, make_agent):
    boss = await make_agent("Boss", agent_runtime="host")
    coder = await make_agent("Coder")
    session.add_all([
        # interactive operator session: host CLI project dir that is not the lead checkout
        _row(agent_id=boss.id, source_file=f"{HOME}/.claude/projects/-Users-op-Workspace/a.jsonl", cost=2.0),
        # lead: the host CLI project dir of the lead checkout ...
        _row(agent_id=boss.id, source_file=f"{HOME}/.claude/projects/{LEAD_DIR}/b.jsonl", cost=3.0),
        # ... and the lead's own config dir
        _row(agent_id=boss.id, source_file=f"{HOME}/.mc/agents/boss/claude-config/projects/-home-agent/c.jsonl"),
        # a lead-checkout worktree dir is an operator session, not the lead
        _row(agent_id=boss.id, source_file=f"{HOME}/.claude/projects/{LEAD_DIR}-wt/d.jsonl"),
        _row("cli-bridge", agent_id=coder.id, source_file=f"{HOME}/.mc/agents/coder/claude-config/projects/p/e.jsonl"),
        _row("head-omp", model="GLM-5.3-Flash-EXL3", locality="local"),
        _row("head-claude", locality="cloud"),
        _row("head-claude", locality=None),
        _row("cli-bridge", agent_id=None),
    ])
    await session.commit()

    result = await compute_weekly_baseline(session, weeks=1, now=NOW)
    src = _sources(_week(result, "2026-W39"))

    assert src["operator"]["events"] == 2
    assert src["operator"]["cost_usd"] == pytest.approx(3.0)
    assert src["lead"]["events"] == 2
    assert src["lead"]["cost_usd"] == pytest.approx(4.0)
    assert src["agents:cli-bridge"]["events"] == 1
    assert src["heads:local"]["events"] == 1
    assert src["heads:cloud"]["events"] == 1
    assert src["heads:unknown"]["events"] == 1
    assert src["unattributed"]["events"] == 1
    assert sum(s["events"] for s in src.values()) == 9


async def test_weeks_split_on_iso_monday_and_sum_tokens(session):
    session.add_all([
        _row(ts=W38, inp=1, out=2, cr=3, cw=4, cost=0.5),
        _row(ts=W39, inp=10, out=20, cr=30, cw=40, cost=1.5),
        _row(ts=W39, inp=10, out=20, cr=30, cw=40, cost=None),
        _row(ts=datetime(2026, 9, 1, tzinfo=timezone.utc)),  # before the window
    ])
    await session.commit()

    result = await compute_weekly_baseline(session, weeks=2, now=NOW)

    assert [w["week"] for w in result["weeks"]] == ["2026-W38", "2026-W39"]
    w38, w39 = result["weeks"]
    assert w38["week_start"] == "2026-09-14" and not w38["partial"]
    assert w39["week_start"] == "2026-09-21" and w39["partial"]
    assert w38["totals"]["total_tokens"] == 10
    t = w39["totals"]
    assert (t["input_tokens"], t["output_tokens"], t["cache_read_tokens"], t["cache_write_tokens"]) == (20, 40, 60, 80)
    assert t["total_tokens"] == 200
    assert t["events"] == 2
    assert t["cost_usd"] == pytest.approx(1.5)
    assert t["unpriced_events"] == 1


async def test_empty_weeks_are_listed_with_zeros(session):
    result = await compute_weekly_baseline(session, weeks=3, now=NOW)
    assert [w["week"] for w in result["weeks"]] == ["2026-W37", "2026-W38", "2026-W39"]
    assert all(w["totals"]["events"] == 0 and w["totals"]["local_share"] is None for w in result["weeks"])


async def test_local_share_uses_row_locality_then_local_runtime_models(session):
    box = Host(id=uuid.uuid4(), slug="box", display_name="Box", kind="ssh")
    session.add(box)
    session.add_all([
        Runtime(slug="glm-box", display_name="g", runtime_type="vllm_docker", endpoint="http://x",
                model_identifier="GLM-5.3-Flash-EXL3", host_id=box.id),
        # served locally AND by a cloud runtime -> ambiguous -> counted as cloud
        Runtime(slug="ds-box", display_name="d", runtime_type="ssh_process", endpoint="http://x",
                model_identifier="deepseek-v4-flash", host_id=box.id),
        Runtime(slug="ds-cloud", display_name="c", runtime_type="cloud", endpoint="http://x",
                model_identifier="deepseek-v4-flash"),
    ])
    session.add_all([
        _row("hermes", model="glm-5.3-flash-exl3", inp=100, out=100, cr=0, cw=0),  # case-insensitive match
        _row("cli-bridge", model="deepseek-v4-flash", inp=100, out=100, cr=0, cw=0),
        _row("host", model="claude-opus-5", inp=100, out=100, cr=0, cw=0),
        _row("head-omp", model="whatever", locality="local", inp=100, out=100, cr=0, cw=0),
    ])
    await session.commit()

    result = await compute_weekly_baseline(session, weeks=1, now=NOW)
    t = _week(result, "2026-W39")["totals"]

    assert t["local_tokens"] == 400
    assert t["local_output_tokens"] == 200
    assert t["local_share"] == pytest.approx(0.5)
    assert t["local_output_share"] == pytest.approx(0.5)


async def test_weeks_parameter_is_bounded(session):
    assert len((await compute_weekly_baseline(session, weeks=0, now=NOW))["weeks"]) == 1
    assert len((await compute_weekly_baseline(session, weeks=500, now=NOW))["weeks"]) == 26


async def test_endpoint_returns_week_table(auth_client, session):
    session.add(_row(ts=datetime.now(timezone.utc), cost=0.25))
    await session.commit()

    resp = await auth_client.get("/api/v1/intelligence/costs/by-week?weeks=2")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["weeks"]) == 2
    assert body["weeks"][-1]["totals"]["events"] == 1
    assert body["weeks"][-1]["totals"]["cost_usd"] == pytest.approx(0.25)


async def test_endpoint_requires_login(client):
    resp = await client.get("/api/v1/intelligence/costs/by-week")
    assert resp.status_code in (401, 403)


async def test_harvested_before_freezes_a_snapshot(session):
    early, late = _row(cost=1.0), _row(cost=2.0)
    early.harvested_at = datetime(2026, 9, 23, tzinfo=timezone.utc)
    late.harvested_at = datetime(2026, 9, 24, 11, tzinfo=timezone.utc)
    session.add_all([early, late])
    await session.commit()

    cut = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    result = await compute_weekly_baseline(session, weeks=1, now=NOW, harvested_before=cut)

    assert result["weeks"][0]["totals"]["events"] == 1
    assert result["weeks"][0]["totals"]["cost_usd"] == pytest.approx(1.0)
