"""Boss attribution of host Claude Code transcripts (bug found 22.10.2026).

The host's Claude Code project root (``<home>/.claude/projects``) is shared
by the lead agent (Boss) and every interactive session the operator runs
on the same machine. The harvester used to book every line with an
MC-looking cwd or a ``task/`` branch on the Boss — so the operator's own
sessions made Boss look ~150x more expensive than it is.

Rule now: a transcript belongs to the Boss only if it lives in the Boss's
own project directory, i.e. the Claude Code encoding of the Boss's working
directory (the stable host-agent checkout, ``MC_CHECKOUT_PATH``). Every
other MC-looking transcript is the operator's own usage (agent_id NULL,
harness ``operator-session``); private ones stay skipped.

All paths here live under tmp_path — no real home directory.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from app.models.model_usage import ModelUsageEvent
from app.services.token_harvester import (
    OPERATOR_SESSION_HARNESS,
    encode_cwd,
    lead_agent_ids,
    lead_checkout_path,
    lead_project_dir_name,
    reattribute_operator_sessions,
    run_harvest,
)


def _line(uuid_: str, cwd: str, *, git_branch: str | None = "main",
          ts: str = "2026-10-21T21:56:24.000Z", model: str = "claude-opus-5") -> str:
    d = {
        "type": "assistant",
        "uuid": uuid_,
        "sessionId": "sess",
        "timestamp": ts,
        "cwd": cwd,
        "message": {
            "id": f"msg-{uuid_}",
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 50,
            },
        },
    }
    if git_branch is not None:
        d["gitBranch"] = git_branch
    return json.dumps(d)


def _write(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME_HOST", str(h))
    from app.config import settings

    monkeypatch.setattr(settings, "mc_checkout_path", "", raising=False)
    return h


# ── where the Boss works ───────────────────────────────────────────────────


def test_lead_checkout_defaults_to_stable_host_agent_checkout(home):
    # Same default as docker/shared/ensure-checkout.sh (MC_CHECKOUT_PATH).
    assert lead_checkout_path() == str(home / ".mc" / "checkouts" / "mission-control")
    assert lead_project_dir_name() == encode_cwd(str(home / ".mc" / "checkouts" / "mission-control"))


def test_lead_checkout_follows_configured_checkout(home, tmp_path, monkeypatch):
    from app.config import settings

    custom = tmp_path / "elsewhere" / "mc-stable"
    monkeypatch.setattr(settings, "mc_checkout_path", str(custom) + "/")
    assert lead_checkout_path() == str(custom)
    assert lead_project_dir_name() == encode_cwd(str(custom))


def test_configured_checkout_expands_tilde_against_the_host_home(home, monkeypatch):
    """`~` in MC_CHECKOUT_PATH is the HOST home (HOME_HOST), never the
    container's /home/mcuser — and `..`/trailing slashes are normalised, or the
    encoded project dir name would not match Claude Code's."""
    from app.config import settings

    monkeypatch.setattr(settings, "mc_checkout_path", "~/stable/../stable/mc/")
    assert lead_checkout_path() == str(home / "stable" / "mc")
    assert lead_project_dir_name() == encode_cwd(str(home / "stable" / "mc"))


def test_lead_agent_ids_is_the_one_rule_for_harvest_and_reattribution():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert lead_agent_ids({"coder": a, "boss": b, "boss-old": c}) == [b, c]
    assert lead_agent_ids({"coder": a}) == []


def test_transcript_chat_and_baseline_use_the_same_lead_dir(home, tmp_path, monkeypatch):
    """One source of truth: the Sessions chat and the usage baseline must look
    at the same Boss directory as the harvester."""
    from types import SimpleNamespace

    from app.config import settings
    from app.services import transcript_chat, usage_baseline

    custom = tmp_path / "elsewhere" / "mc-stable"
    monkeypatch.setattr(settings, "mc_checkout_path", str(custom))
    boss = SimpleNamespace(slug="boss", agent_runtime="host")
    assert transcript_chat.resolve_transcript_dir(boss) == (
        home / ".claude" / "projects" / encode_cwd(str(custom))
    )
    assert usage_baseline._lead_checkout_dir() == encode_cwd(str(custom))


# ── harvest ────────────────────────────────────────────────────────────────


async def _harvest(session, home: Path, boss_id) -> dict:
    return await run_harvest(
        session,
        agent_base_paths=[],
        boss_base_paths=[str(home / ".claude" / "projects")],
        agent_slug_map={"boss": boss_id},
        task_workspace_map={},
        grok_log_path=str(home / "none.jsonl"),
        hermes_state_db_path=str(home / "none.db"),
        heads_root=str(home / "no-heads"),
    )


async def _by_uuid(session) -> dict[str, ModelUsageEvent]:
    return {e.message_uuid: e for e in (await session.exec(select(ModelUsageEvent))).all()}


async def test_only_the_boss_session_dir_is_booked_on_boss(session, home):
    boss_id = uuid.uuid4()
    projects = home / ".claude" / "projects"
    checkout = lead_checkout_path()
    lead_dir = projects / encode_cwd(checkout)
    operator_repo = str(home / "Workspace" / "Projects" / "mission-control")

    # Boss's own session + one of its subagents
    _write(lead_dir / "boss-sess.jsonl", _line("boss-1", checkout))
    _write(lead_dir / "boss-sess" / "subagents" / "agent-a.jsonl", _line("boss-sub-1", checkout))
    # operator's interactive session in a parent folder whose subagent cd'd into the MC repo
    _write(projects / encode_cwd(str(home / "Workspace")) / "op.jsonl",
           _line("op-1", operator_repo, git_branch="fix/x"))
    # operator session started inside an MC worktree, on a task/ branch
    _write(projects / encode_cwd(operator_repo + "/.claude/worktrees/w") / "op2.jsonl",
           _line("op-2", operator_repo + "/.claude/worktrees/w", git_branch="task/foo"))
    # operator session that even cd'd into the Boss checkout — the DIR decides, not the cwd
    _write(projects / encode_cwd(str(home / "Workspace")) / "op3.jsonl", _line("op-3", checkout))
    # a dir whose name only starts with the Boss dir name is NOT the Boss dir
    _write(projects / (encode_cwd(checkout) + "-wt") / "op4.jsonl", _line("op-4", checkout + "-wt"))
    # private, non-MC session → never harvested
    _write(projects / encode_cwd(str(home / "private")) / "p.jsonl",
           _line("priv-1", str(home / "private"), git_branch="main"))

    stats = await _harvest(session, home, boss_id)
    rows = await _by_uuid(session)

    assert rows["boss-1"].agent_id == boss_id and rows["boss-1"].harness == "host"
    assert rows["boss-sub-1"].agent_id == boss_id and rows["boss-sub-1"].harness == "host"
    for op in ("op-1", "op-2", "op-3", "op-4"):
        assert rows[op].agent_id is None, op
        assert rows[op].harness == OPERATOR_SESSION_HARNESS, op
    assert "priv-1" not in rows
    assert stats["skipped_private"] == 1
    assert stats["operator_session_events"] == 4


async def test_missing_lead_dir_warns_once(session, home, caplog):
    """A different MC_CHECKOUT_PATH for backend and host agent would silently
    move every Boss line into the operator bucket — say so in the log, once."""
    import logging

    from app.services import token_harvester

    token_harvester._warned_missing_lead_dirs.clear()
    projects = home / ".claude" / "projects"
    _write(projects / encode_cwd(str(home / "Workspace")) / "op.jsonl",
           _line("op-w", str(home / "Workspace" / "Projects" / "mission-control"), git_branch="fix/x"))

    with caplog.at_level(logging.WARNING, logger="mc.token_harvester"):
        await _harvest(session, home, uuid.uuid4())
        await _harvest(session, home, uuid.uuid4())
    hits = [r for r in caplog.records if "lead session dir" in r.getMessage()]
    assert len(hits) == 1
    assert "MC_CHECKOUT_PATH" in hits[0].getMessage()

    # present lead dir → no warning
    token_harvester._warned_missing_lead_dirs.clear()
    caplog.clear()
    _write(projects / encode_cwd(lead_checkout_path()) / "b.jsonl", _line("boss-w", lead_checkout_path()))
    with caplog.at_level(logging.WARNING, logger="mc.token_harvester"):
        await _harvest(session, home, uuid.uuid4())
    assert not [r for r in caplog.records if "lead session dir" in r.getMessage()]


async def test_boss_dir_lines_are_boss_even_without_mc_markers(session, home, tmp_path, monkeypatch):
    """Inside the Boss dir there is no heuristic: a configured checkout that
    does not look like 'mission-control' still belongs to the Boss."""
    from app.config import settings

    checkout = str(tmp_path / "stable" / "repo")
    monkeypatch.setattr(settings, "mc_checkout_path", checkout)
    boss_id = uuid.uuid4()
    lead_dir = home / ".claude" / "projects" / encode_cwd(checkout)
    _write(lead_dir / "s.jsonl", _line("boss-x", checkout, git_branch="main"))

    await _harvest(session, home, boss_id)
    rows = await _by_uuid(session)
    assert rows["boss-x"].agent_id == boss_id


async def test_cost_view_shows_operator_sessions_apart_from_boss(auth_client, session, make_agent):
    boss = await make_agent("Boss", agent_runtime="host")
    now = datetime.now(timezone.utc)

    def row(harness, cost, agent_id=None):
        return ModelUsageEvent(
            id=uuid.uuid4(), harness=harness, agent_id=agent_id, model="m", session_id="s",
            message_uuid=f"c-{uuid.uuid4()}", input_tokens=1, output_tokens=1,
            cost_usd=cost, ts=now, source_file="/f.jsonl",
        )

    session.add_all([
        row("host", 1.0, boss.id),
        row(OPERATOR_SESSION_HARNESS, 7.0),
        row(OPERATOR_SESSION_HARNESS, 3.0),
        row("host", 0.5),  # boss line without an agent row
    ])
    await session.commit()

    resp = await auth_client.get("/api/v1/intelligence/costs?days=30&include_sessions=true")
    assert resp.status_code == 200
    by_name = {r["agent_name"]: r for r in resp.json()["agents"]}
    assert by_name["Boss"]["cost_usd"] == pytest.approx(1.0)
    assert by_name["Operator sessions"]["cost_usd"] == pytest.approx(10.0)
    assert by_name["Operator sessions"]["event_count"] == 2
    assert by_name["Unattributed"]["cost_usd"] == pytest.approx(0.5)
    assert by_name["Operator sessions"]["agent_id"] not in ("", by_name["Unattributed"]["agent_id"])
    assert "Operator sessions" in {s["agent_name"] for s in resp.json()["sessions"]}


async def test_usage_baseline_counts_operator_session_rows_as_operator(session, home):
    from app.services.usage_baseline import compute_weekly_baseline

    ts = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    session.add(ModelUsageEvent(
        id=uuid.uuid4(), harness=OPERATOR_SESSION_HARNESS, agent_id=None, model="m",
        session_id="s", message_uuid=f"b-{uuid.uuid4()}", input_tokens=1, output_tokens=1,
        cost_usd=2.0, ts=ts, source_file=str(home / ".claude" / "projects" / "-x" / "a.jsonl"),
    ))
    await session.commit()

    result = await compute_weekly_baseline(
        session, weeks=1, now=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    )
    week = next(w for w in result["weeks"] if w["week"] == "2026-W39")
    sources = {s["source"]: s for s in week["sources"]}
    assert sources["operator"]["events"] == 1
    assert "unattributed" not in sources


# ── one-off re-attribution of existing rows ───────────────────────────────


async def _seed_existing(session, home, make_agent):
    boss = await make_agent("Boss", agent_runtime="host")
    coder = await make_agent("Coder")
    projects = home / ".claude" / "projects"
    lead_dir = encode_cwd(lead_checkout_path())

    def row(uid, source, *, agent_id=boss.id, harness="host", cost=1.0,
            ts=datetime(2026, 10, 5, tzinfo=timezone.utc)):
        return ModelUsageEvent(
            id=uuid.uuid4(), harness=harness, agent_id=agent_id, model="claude-opus-5",
            session_id="s", message_uuid=uid, input_tokens=1, output_tokens=1,
            cost_usd=cost, ts=ts, source_file=str(source),
        )

    session.add_all([
        row("own", projects / lead_dir / "a.jsonl", cost=0.5),
        row("own-sub", projects / lead_dir / "a" / "subagents" / "x.jsonl", cost=0.25),
        row("legacy", home / ".mc" / "agents" / "boss" / "claude-config" / "projects" / "p" / "c.jsonl"),
        row("op-a", projects / "-ws" / "b.jsonl", cost=10.0),
        row("op-b", projects / (lead_dir + "-wt") / "d.jsonl", cost=20.0),
        row("op-old", projects / "-ws" / "e.jsonl", cost=5.0,
            ts=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        row("coder", home / ".mc" / "agents" / "coder" / "claude-config" / "projects" / "p" / "f.jsonl",
            agent_id=coder.id, harness="cli-bridge"),
    ])
    await session.commit()
    return boss, coder


async def test_reattribution_dry_run_counts_and_writes_nothing(session, home, make_agent):
    boss, _ = await _seed_existing(session, home, make_agent)

    report = await reattribute_operator_sessions(session)

    assert report["applied"] is False
    b = report["buckets"]
    assert b["boss_session_dir"] == {"rows": 2, "cost_usd": pytest.approx(0.75)}
    assert b["boss_other_sources"] == {"rows": 1, "cost_usd": pytest.approx(1.0)}
    assert b["operator_session"] == {"rows": 3, "cost_usd": pytest.approx(35.0)}
    assert report["updated"] == 0

    rows = await _by_uuid(session)
    assert rows["op-a"].agent_id == boss.id and rows["op-a"].harness == "host"


async def test_reattribution_apply_moves_only_operator_rows_and_is_idempotent(session, home, make_agent):
    boss, coder = await _seed_existing(session, home, make_agent)

    report = await reattribute_operator_sessions(session, apply=True)
    assert report["applied"] is True
    assert report["updated"] == 3

    session.expire_all()
    rows = await _by_uuid(session)
    for uid in ("op-a", "op-b", "op-old"):
        assert rows[uid].agent_id is None and rows[uid].harness == OPERATOR_SESSION_HARNESS
    for uid in ("own", "own-sub", "legacy"):
        assert rows[uid].agent_id == boss.id and rows[uid].harness == "host"
    assert rows["coder"].agent_id == coder.id

    again = await reattribute_operator_sessions(session, apply=True)
    assert again["updated"] == 0
    assert again["buckets"]["operator_session"]["rows"] == 0
    assert again["buckets"]["already_operator_session"]["rows"] == 3


async def test_reattribution_since_limits_the_window(session, home, make_agent):
    await _seed_existing(session, home, make_agent)

    report = await reattribute_operator_sessions(
        session, apply=True, since=datetime(2026, 10, 1, tzinfo=timezone.utc)
    )
    assert report["updated"] == 2

    session.expire_all()
    rows = await _by_uuid(session)
    assert rows["op-old"].harness == "host"  # before the window: untouched


def test_reattribution_script_is_dry_run_unless_apply(monkeypatch, capsys):
    import scripts.reattribute_operator_sessions as script

    calls = []

    async def fake_run(apply, since):
        calls.append((apply, since))
        return {"buckets": {"operator_session": {"rows": 3, "cost_usd": 35.0}}, "updated": 0}

    monkeypatch.setattr(script, "_run", fake_run)
    monkeypatch.setattr("sys.argv", ["x"])
    script.main()
    monkeypatch.setattr("sys.argv", ["x", "--apply", "--since", "2026-10-01"])
    script.main()

    assert calls[0] == (False, None)
    assert calls[1] == (True, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert "dry run: would move 3 rows" in capsys.readouterr().out
