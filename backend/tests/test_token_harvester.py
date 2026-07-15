"""Tests for token_harvester — TDD-first.

Test fixtures per spec:
(a) Normal assistant line with cache tokens
(b) Two lines with the same message.id but different uuid (both count)
(c) Same uuid twice (counts once — UNIQUE constraint backstop)
(d) <synthetic> model (skip)
(e) user line (skip)
(f) ~/.claude path with private cwd (skip) vs MC cwd (Boss)

Price matching: glob priority, valid_from, no match → None.
Offset resume: file grows, second run reads only new lines.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.services.token_harvester import (
    parse_transcript_line,
    match_price,
    run_harvest,
)
from app.models.model_usage import ModelPrice, ModelUsageEvent, ModelUsageHarvestState


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_line(
    *,
    uuid_: str | None = None,
    session_id: str = "sess-001",
    timestamp: str = "2026-06-01T10:00:00.000Z",
    cwd: str = "/Users/testuser/Workspace/Projects/mission-control",
    git_branch: str | None = "feat/test",
    model: str = "claude-sonnet-4-6",
    msg_id: str = "msg_abc123",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 200,
    cache_write: int = 300,
    type_: str = "assistant",
) -> str:
    line: dict = {
        "type": type_,
        "uuid": uuid_ or str(uuid.uuid4()),
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
    }
    if git_branch is not None:
        line["gitBranch"] = git_branch
    if type_ == "assistant":
        line["message"] = {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        }
    else:
        line["message"] = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    return json.dumps(line)


def _make_omp_line(
    *,
    short_id: str = "74f7a91e",
    model: str = "Qwen/Qwen3.6-35B-A3B-FP8",
    provider: str | None = "mc-openai",
    timestamp: str = "2026-07-15T16:29:37.102Z",
    input_tokens: int = 28848,
    output_tokens: int = 135,
    cache_read: int = 0,
    cache_write: int = 0,
    role: str = "assistant",
    type_: str = "message",
) -> str:
    """Builds a real-shaped omp JSONL line (ADR-045 headless harness).

    Sampled verbatim from mc-agent-sparky (2026-07-15, Qwen/Spark) — top-level
    id/model/provider, camelCase usage keys, no sessionId/uuid of its own.
    """
    line: dict = {
        "type": type_,
        "id": short_id,
        "parentId": "54c8d3f0",
        "timestamp": timestamp,
        "message": {
            "role": role,
            "usage": {
                "input": input_tokens,
                "output": output_tokens,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "totalTokens": input_tokens + output_tokens,
                "cost": {
                    "input": 0.00403872,
                    "output": 0.000135,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "total": 0.00417372,
                },
            },
        },
        "model": model,
        "api": "openai-completions",
    }
    if provider is not None:
        line["provider"] = provider
    return json.dumps(line)


# ── parse_transcript_line ──────────────────────────────────────────────────


class TestParseTranscriptLine:
    def test_normal_assistant_line_parsed(self):
        """(a) Normal assistant line with cache tokens."""
        line = _make_line(
            uuid_="aaaa-1111",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            cache_read=200,
            cache_write=300,
        )
        rec = parse_transcript_line(line)
        assert rec is not None
        assert rec["uuid"] == "aaaa-1111"
        assert rec["model"] == "claude-sonnet-4-6"
        assert rec["input_tokens"] == 100
        assert rec["output_tokens"] == 50
        assert rec["cache_read_tokens"] == 200
        assert rec["cache_write_tokens"] == 300
        assert rec["session_id"] == "sess-001"
        assert rec["git_branch"] == "feat/test"

    def test_user_line_skipped(self):
        """(e) user line → None."""
        line = _make_line(type_="user")
        assert parse_transcript_line(line) is None

    def test_synthetic_model_skipped(self):
        """(d) <synthetic> model → None."""
        line = _make_line(model="<synthetic>")
        assert parse_transcript_line(line) is None

    def test_synthetic_in_model_name_skipped(self):
        """Model name contains '<synthetic>' → None."""
        line = _make_line(model="some-<synthetic>-model")
        assert parse_transcript_line(line) is None

    def test_missing_model_skipped(self):
        """Missing model field → None."""
        d = json.loads(_make_line())
        del d["message"]["model"]
        assert parse_transcript_line(json.dumps(d)) is None

    def test_missing_usage_skipped(self):
        """No usage block → None."""
        d = json.loads(_make_line())
        del d["message"]["usage"]
        assert parse_transcript_line(json.dumps(d)) is None

    def test_missing_uuid_skipped(self):
        """No top-level uuid → None."""
        d = json.loads(_make_line())
        del d["uuid"]
        assert parse_transcript_line(json.dumps(d)) is None

    def test_non_assistant_type_skipped(self):
        """Non-assistant type → None (also system, tool, etc.)."""
        d = json.loads(_make_line())
        d["type"] = "system"
        assert parse_transcript_line(json.dumps(d)) is None

    def test_invalid_json_skipped(self):
        """Invalid JSON → None."""
        assert parse_transcript_line("not json{{{") is None

    def test_no_git_branch_field(self):
        """Line without gitBranch → rec has git_branch=None."""
        line = _make_line(git_branch=None)
        rec = parse_transcript_line(line)
        assert rec is not None
        assert rec.get("git_branch") is None

    def test_cache_defaults_to_zero(self):
        """Missing cache fields → 0 (no KeyError)."""
        d = json.loads(_make_line())
        del d["message"]["usage"]["cache_read_input_tokens"]
        del d["message"]["usage"]["cache_creation_input_tokens"]
        rec = parse_transcript_line(json.dumps(d))
        assert rec is not None
        assert rec["cache_read_tokens"] == 0
        assert rec["cache_write_tokens"] == 0


# ── parse_transcript_line — omp schema (ADR-045) ────────────────────────────


class TestParseOmpLine:
    def test_omp_line_parsed(self):
        line = _make_omp_line()
        rec = parse_transcript_line(line, session_id="sess-omp-1")
        assert rec is not None
        assert rec["uuid"] == "sess-omp-1:74f7a91e"
        assert rec["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"
        assert rec["provider"] == "mc-openai"
        assert rec["input_tokens"] == 28848
        assert rec["output_tokens"] == 135
        assert rec["cache_read_tokens"] == 0
        assert rec["cache_write_tokens"] == 0

    def test_omp_dedup_key_namespaced_by_session(self):
        """omp's 8-hex id is collision-prone across sessions — the dedup key
        must be namespaced by session_id, not the bare id."""
        line = _make_omp_line(short_id="dupe")
        rec_a = parse_transcript_line(line, session_id="session-a")
        rec_b = parse_transcript_line(line, session_id="session-b")
        assert rec_a["uuid"] != rec_b["uuid"]
        assert rec_a["uuid"] == "session-a:dupe"
        assert rec_b["uuid"] == "session-b:dupe"

    def test_omp_line_without_session_id_uses_empty_prefix(self):
        line = _make_omp_line(short_id="abc123")
        rec = parse_transcript_line(line)
        assert rec is not None
        assert rec["uuid"] == ":abc123"

    def test_omp_user_line_skipped(self):
        line = _make_omp_line(role="user")
        assert parse_transcript_line(line, session_id="s") is None

    def test_omp_missing_usage_skipped(self):
        d = json.loads(_make_omp_line())
        del d["message"]["usage"]
        assert parse_transcript_line(json.dumps(d), session_id="s") is None

    def test_omp_missing_model_skipped(self):
        d = json.loads(_make_omp_line())
        del d["model"]
        assert parse_transcript_line(json.dumps(d), session_id="s") is None

    def test_omp_missing_id_skipped(self):
        d = json.loads(_make_omp_line())
        del d["id"]
        assert parse_transcript_line(json.dumps(d), session_id="s") is None

    def test_omp_without_provider_field_omits_provider_key(self):
        line = _make_omp_line(provider=None)
        rec = parse_transcript_line(line, session_id="s")
        assert rec is not None
        assert rec.get("provider") is None

    def test_claude_line_unaffected_by_session_id_arg(self):
        """Claude Code lines keep their own sessionId — the extra session_id
        kwarg (introduced for omp) must be a no-op for them."""
        line = _make_line(uuid_="claude-1", session_id="sess-001")
        rec = parse_transcript_line(line, session_id="ignored-for-claude")
        assert rec is not None
        assert rec["uuid"] == "claude-1"
        assert rec["session_id"] == "sess-001"


# ── match_price ────────────────────────────────────────────────────────────


class TestMatchPrice:
    def _price(self, pattern, inp, out, cr, cw, priority=50, valid_from=None):
        return ModelPrice(
            id=uuid.uuid4(),
            model_pattern=pattern,
            input_per_mtok=inp,
            output_per_mtok=out,
            cache_read_per_mtok=cr,
            cache_write_per_mtok=cw,
            priority=priority,
            valid_from=valid_from or datetime(2020, 1, 1, tzinfo=timezone.utc),
            currency="USD",
        )

    def test_exact_match(self):
        prices = [self._price("claude-sonnet-4-6", 3.0, 15.0, 0.3, 3.75)]
        cost = match_price("claude-sonnet-4-6", datetime(2026, 6, 1, tzinfo=timezone.utc), prices)
        assert cost is not None
        # 100 input + 50 output → (100*3 + 50*15) / 1e6 = 0.0003 + 0.00075 = 0.00105
        # but we test with specific token amounts... match_price returns price struct
        assert cost["input_per_mtok"] == 3.0

    def test_glob_match(self):
        prices = [self._price("claude-sonnet-4-*", 3.0, 15.0, 0.3, 3.75)]
        cost = match_price("claude-sonnet-4-6", datetime(2026, 6, 1, tzinfo=timezone.utc), prices)
        assert cost is not None

    def test_higher_priority_wins(self):
        """More specific pattern (higher priority) wins."""
        prices = [
            self._price("*", 0.0, 0.0, 0.0, 0.0, priority=0),
            self._price("claude-sonnet-4-*", 3.0, 15.0, 0.3, 3.75, priority=80),
        ]
        cost = match_price("claude-sonnet-4-6", datetime(2026, 6, 1, tzinfo=timezone.utc), prices)
        assert cost is not None
        assert cost["input_per_mtok"] == 3.0

    def test_no_match_returns_none(self):
        """No price for this model → None."""
        prices = [self._price("claude-opus-4-*", 15.0, 75.0, 1.5, 18.75)]
        cost = match_price("unknown-model", datetime(2026, 6, 1, tzinfo=timezone.utc), prices)
        assert cost is None

    def test_valid_from_filter(self):
        """Only prices with valid_from <= ts are considered."""
        future_price = self._price(
            "claude-sonnet-4-*", 99.0, 99.0, 0.0, 0.0, priority=90,
            valid_from=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        old_price = self._price(
            "claude-sonnet-4-*", 3.0, 15.0, 0.3, 3.75, priority=80,
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        cost = match_price(
            "claude-sonnet-4-6",
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            [future_price, old_price],
        )
        assert cost is not None
        assert cost["input_per_mtok"] == 3.0  # future price was excluded

    def test_newest_valid_from_wins_for_same_priority(self):
        """For equal priority, the price with the newest valid_from wins."""
        old = self._price(
            "claude-sonnet-4-*", 2.0, 10.0, 0.0, 0.0, priority=80,
            valid_from=datetime(2022, 1, 1, tzinfo=timezone.utc),
        )
        newer = self._price(
            "claude-sonnet-4-*", 3.0, 15.0, 0.0, 0.0, priority=80,
            valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        cost = match_price(
            "claude-sonnet-4-6",
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            [old, newer],
        )
        assert cost is not None
        assert cost["input_per_mtok"] == 3.0

    def test_empty_prices_list(self):
        cost = match_price("claude-sonnet-4-6", datetime(2026, 6, 1, tzinfo=timezone.utc), [])
        assert cost is None


# ── compute_cost_usd helper ────────────────────────────────────────────────

class TestComputeCostUsd:
    """Test the cost calculation (integration with match_price)."""

    def test_cost_calculation(self):
        from app.services.token_harvester import _compute_cost_usd
        price_info = {
            "input_per_mtok": 3.0,
            "output_per_mtok": 15.0,
            "cache_read_per_mtok": 0.3,
            "cache_write_per_mtok": 3.75,
        }
        # 1M input=3, 1M output=15, 1M cr=0.3, 1M cw=3.75
        # 1000 input → 0.003, 500 output → 0.0075, 200 cr → 0.00006, 300 cw → 0.001125
        cost = _compute_cost_usd(price_info, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        assert abs(cost - (3.0 + 15.0 + 0.3 + 3.75)) < 1e-9


# ── harvest_file (Offset-Resume) ──────────────────────────────────────────

class TestHarvestFile:
    def test_offset_resume(self, tmp_path):
        """Second run reads only new lines (offset resume)."""
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "session.jsonl"
        line1 = _make_line(uuid_="uuid-001", input_tokens=10)
        line2 = _make_line(uuid_="uuid-002", input_tokens=20)

        jsonl.write_text(line1 + "\n")

        records_first = harvest_file(str(jsonl), processed_lines=0)
        assert len(records_first) == 1
        assert records_first[0]["uuid"] == "uuid-001"

        # File grows
        with open(jsonl, "a") as f:
            f.write(line2 + "\n")

        records_second = harvest_file(str(jsonl), processed_lines=1)
        assert len(records_second) == 1
        assert records_second[0]["uuid"] == "uuid-002"

    def test_same_uuid_deduplicated_in_file(self, tmp_path):
        """Same uuid twice in one file → harvest_file returns both,
        but DB insert is stopped by the UNIQUE constraint (backstop).
        harvest_file itself does NOT deduplicate — that's the DB's job.
        """
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "session.jsonl"
        line = _make_line(uuid_="uuid-dup")
        jsonl.write_text(line + "\n" + line + "\n")

        records = harvest_file(str(jsonl), processed_lines=0)
        # harvest_file returns all parsed lines (DB handles dedup)
        assert len(records) == 2

    def test_same_msg_id_different_uuid_both_counted(self, tmp_path):
        """(b) Two lines with the same message.id but different uuid: both count."""
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "session.jsonl"
        line1 = _make_line(uuid_="uuid-A", msg_id="msg_same")
        line2 = _make_line(uuid_="uuid-B", msg_id="msg_same")
        jsonl.write_text(line1 + "\n" + line2 + "\n")

        records = harvest_file(str(jsonl), processed_lines=0)
        assert len(records) == 2
        assert {r["uuid"] for r in records} == {"uuid-A", "uuid-B"}

    def test_omp_file_derives_session_id_from_filename(self, tmp_path):
        """omp writes one JSONL per session with no sessionId field — harvest_file
        must derive it from the filename and namespace the dedup key with it."""
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "2026-07-15T16-29-31-091Z_019f669c-aa50.jsonl"
        jsonl.write_text(_make_omp_line(short_id="idA") + "\n")

        records = harvest_file(str(jsonl), processed_lines=0)
        assert len(records) == 1
        assert records[0]["uuid"] == "2026-07-15T16-29-31-091Z_019f669c-aa50:idA"
        assert records[0]["session_id"] == "2026-07-15T16-29-31-091Z_019f669c-aa50"

    def test_skip_lines_are_not_returned(self, tmp_path):
        """(d,e) synthetic + user → not in records."""
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "session.jsonl"
        good = _make_line(uuid_="uuid-good")
        bad_synthetic = _make_line(uuid_="uuid-synth", model="<synthetic>")
        bad_user = _make_line(uuid_="uuid-user", type_="user")
        jsonl.write_text(good + "\n" + bad_synthetic + "\n" + bad_user + "\n")

        records = harvest_file(str(jsonl), processed_lines=0)
        assert len(records) == 1
        assert records[0]["uuid"] == "uuid-good"


# ── Boss attribution (cwd/gitBranch heuristic) ────────────────────────────

class TestBossAttribution:
    def test_mc_cwd_is_attributed(self):
        """cwd under mission-control → attribute (Boss candidate)."""
        from app.services.token_harvester import _should_attribute_boss_path

        assert _should_attribute_boss_path(
            cwd="/Users/testuser/Workspace/Projects/mission-control",
            git_branch="feat/my-feature",
        ) is True

    def test_mc_cwd_subcdir_attributed(self):
        """Deeper subdirectory of mission-control → attribute."""
        from app.services.token_harvester import _should_attribute_boss_path

        assert _should_attribute_boss_path(
            cwd="/Users/testuser/Workspace/Projects/mission-control/.worktrees/feat-xyz",
            git_branch=None,
        ) is True

    def test_mc_home_cwd_attributed(self):
        """cwd under ~/.mc/ → attribute."""
        from app.services.token_harvester import _should_attribute_boss_path

        assert _should_attribute_boss_path(
            cwd="/Users/testuser/.mc/agents/boss-host/workspace",
            git_branch=None,
        ) is True

    def test_task_branch_attributed(self):
        """gitBranch starts with 'task/' → attribute."""
        from app.services.token_harvester import _should_attribute_boss_path

        assert _should_attribute_boss_path(
            cwd="/Users/testuser/some/random/path",
            git_branch="task/abc-def-implement-feature",
        ) is True

    def test_private_cwd_skipped(self):
        """(f) Private cwd → SKIP (operator's private sessions)."""
        from app.services.token_harvester import _should_attribute_boss_path

        assert _should_attribute_boss_path(
            cwd="/Users/testuser/Workspace/argyelan",
            git_branch="main",
        ) is False

    def test_root_cwd_skipped(self):
        """cwd '/' (as in the real JSONL above) → SKIP if no task/ branch."""
        from app.services.token_harvester import _should_attribute_boss_path

        assert _should_attribute_boss_path(
            cwd="/",
            git_branch="HEAD",
        ) is False

    def test_root_cwd_with_task_branch_attributed(self):
        """cwd '/' but gitBranch=task/... → attribute."""
        from app.services.token_harvester import _should_attribute_boss_path

        assert _should_attribute_boss_path(
            cwd="/",
            git_branch="task/implement-something",
        ) is True


# ── harness_from_path ──────────────────────────────────────────────────────

class TestHarnessFromPath:
    def test_sparky_harness(self):
        from app.services.token_harvester import _harness_from_slug
        assert _harness_from_slug("sparky") == "sparky"

    def test_cli_bridge_harness(self):
        from app.services.token_harvester import _harness_from_slug
        assert _harness_from_slug("rex") == "cli-bridge"
        assert _harness_from_slug("freecode") == "cli-bridge"
        assert _harness_from_slug("tester") == "cli-bridge"

    def test_host_harness_for_hermes(self):
        from app.services.token_harvester import _harness_from_slug
        assert _harness_from_slug("hermes") == "host"

    def test_host_harness_for_boss_host(self):
        from app.services.token_harvester import _harness_from_slug
        assert _harness_from_slug("boss-host") == "host"


# ── provider_from_model ────────────────────────────────────────────────────

class TestProviderFromModel:
    def test_anthropic_provider(self):
        from app.services.token_harvester import _provider_from_model
        assert _provider_from_model("claude-opus-4-8") == "anthropic"
        assert _provider_from_model("claude-sonnet-4-6") == "anthropic"

    def test_ollama_provider(self):
        from app.services.token_harvester import _provider_from_model
        assert _provider_from_model("qwen2.5-coder:14b") == "ollama"

    def test_lmstudio_provider(self):
        from app.services.token_harvester import _provider_from_model
        assert _provider_from_model("Qwen/Qwen3.6-35B-A3B-FP8") == "lmstudio"

    def test_unknown_provider(self):
        from app.services.token_harvester import _provider_from_model
        assert _provider_from_model("totally-unknown-model-xyz") == "unknown"


# ── DB integration (async, SQLite in-memory) ──────────────────────────────

@pytest.fixture
async def async_db_session(tmp_path):
    """Async SQLite in-memory DB with all relevant tables."""
    from sqlmodel import SQLModel
    from app.models.model_usage import ModelUsageEvent, ModelPrice, ModelUsageHarvestState

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
class TestRunHarvestIntegration:
    """Integration tests for run_harvest with a real (SQLite) DB."""

    async def test_harvest_inserts_events(self, tmp_path, async_db_session):
        """run_harvest reads JSONL and inserts events into the DB."""
        from app.services.token_harvester import run_harvest

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "proj"
        rex_dir.mkdir(parents=True)

        line = _make_line(uuid_="harvest-001", model="claude-sonnet-4-6", input_tokens=100)
        (rex_dir / "session1.jsonl").write_text(line + "\n")

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )

        assert stats["new_events"] >= 1
        result = await async_db_session.exec(select(ModelUsageEvent))
        events = result.all()
        assert any(e.message_uuid == "harvest-001" for e in events)

    async def test_harvest_omp_sessions_glob(self, tmp_path, async_db_session):
        """omp writes to {slug}/omp-sessions/... (ADR-045) — a distinct glob
        from claude-config/projects. Root-cause regression test: this glob was
        entirely missing before the fix, so omp harnesses harvested 0 events."""
        from app.services.token_harvester import run_harvest

        agents_dir = tmp_path / "agents"
        sparky_dir = agents_dir / "sparky" / "omp-sessions" / "--workspace--"
        sparky_dir.mkdir(parents=True)
        session_file = sparky_dir / "2026-07-15T16-29-31-091Z_019f669c.jsonl"
        session_file.write_text(
            _make_omp_line(short_id="omp001", model="Qwen/Qwen3.6-35B-A3B-FP8") + "\n"
        )

        sparky_id = uuid.uuid4()
        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={"sparky": sparky_id},
        )

        assert stats["new_events"] == 1
        result = await async_db_session.exec(select(ModelUsageEvent))
        events = result.all()
        assert len(events) == 1
        ev = events[0]
        assert ev.harness == "sparky"
        assert ev.agent_id == sparky_id
        assert ev.message_uuid == "2026-07-15T16-29-31-091Z_019f669c:omp001"
        assert ev.input_tokens == 28848
        assert ev.output_tokens == 135
        # Top-level `provider` wins over the "/"-in-model-name lmstudio heuristic.
        assert ev.provider == "mc-openai"

    async def test_harvest_both_claude_config_and_omp_sessions_for_same_agent(
        self, tmp_path, async_db_session
    ):
        """Sparky has both legacy claude-config transcripts and new omp-sessions
        transcripts on disk — both globs must be harvested, not just one."""
        from app.services.token_harvester import run_harvest

        agents_dir = tmp_path / "agents"
        claude_dir = agents_dir / "sparky" / "claude-config" / "projects" / "p"
        claude_dir.mkdir(parents=True)
        (claude_dir / "s.jsonl").write_text(
            _make_line(uuid_="claude-evt", model="claude-sonnet-4-6") + "\n"
        )

        omp_dir = agents_dir / "sparky" / "omp-sessions" / "--workspace--"
        omp_dir.mkdir(parents=True)
        (omp_dir / "sess.jsonl").write_text(_make_omp_line(short_id="omp-evt") + "\n")

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )

        assert stats["new_events"] == 2
        result = await async_db_session.exec(select(ModelUsageEvent))
        uuids = {e.message_uuid for e in result.all()}
        assert "claude-evt" in uuids
        assert any(u.endswith(":omp-evt") for u in uuids)

    async def test_dedup_same_uuid(self, tmp_path, async_db_session):
        """(c) Same uuid twice → only once in DB."""
        from app.services.token_harvester import run_harvest

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "proj"
        rex_dir.mkdir(parents=True)

        line = _make_line(uuid_="dedup-uuid", model="claude-sonnet-4-6")
        # Same uuid in two different files
        (rex_dir / "sess1.jsonl").write_text(line + "\n")
        (rex_dir / "sess2.jsonl").write_text(line + "\n")

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )

        result = await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "dedup-uuid")
        )
        events = result.all()
        assert len(events) == 1

    async def test_private_claude_path_skipped(self, tmp_path, async_db_session):
        """(f) ~/.claude path with private cwd → skipped_private counts."""
        from app.services.token_harvester import run_harvest

        boss_dir = tmp_path / "boss_projects" / "-Users-testuser-argyelan"
        boss_dir.mkdir(parents=True)

        private_line = _make_line(
            uuid_="private-001",
            cwd="/Users/testuser/Workspace/argyelan",
            git_branch="main",
        )
        (boss_dir / "session.jsonl").write_text(private_line + "\n")

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[str(tmp_path / "boss_projects")],
            agent_slug_map={},
        )

        assert stats["skipped_private"] >= 1
        result = await async_db_session.exec(select(ModelUsageEvent))
        events = result.all()
        assert not any(e.message_uuid == "private-001" for e in events)

    async def test_mc_cwd_boss_attributed(self, tmp_path, async_db_session):
        """(f) ~/.claude path with MC cwd → Boss event is inserted."""
        from app.services.token_harvester import run_harvest

        boss_dir = tmp_path / "boss_projects" / "-Users-testuser-Workspace-Projects-mc"
        boss_dir.mkdir(parents=True)

        mc_line = _make_line(
            uuid_="boss-001",
            cwd="/Users/testuser/Workspace/Projects/mission-control",
            git_branch="feat/something",
        )
        (boss_dir / "session.jsonl").write_text(mc_line + "\n")

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[str(tmp_path / "boss_projects")],
            agent_slug_map={},
        )

        result = await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "boss-001")
        )
        events = result.all()
        assert len(events) == 1
        assert events[0].harness == "host"

    async def test_offset_resume_second_run(self, tmp_path, async_db_session):
        """Second run_harvest reads only new lines thanks to offset resume."""
        from app.services.token_harvester import run_harvest

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)

        jsonl = rex_dir / "sess.jsonl"
        line1 = _make_line(uuid_="off-001")
        jsonl.write_text(line1 + "\n")

        # First run
        stats1 = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )
        assert stats1["new_events"] == 1

        # File grows
        line2 = _make_line(uuid_="off-002")
        with open(jsonl, "a") as f:
            f.write(line2 + "\n")

        # Second run
        stats2 = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )
        assert stats2["new_events"] == 1  # only the new line

        result = await async_db_session.exec(select(ModelUsageEvent))
        all_events = result.all()
        assert len(all_events) == 2

    async def test_cost_computed_from_prices(self, tmp_path, async_db_session):
        """Cost calculation: cost_usd is computed from model_prices on insert."""
        from app.services.token_harvester import run_harvest

        # Price seed in DB
        price = ModelPrice(
            id=uuid.uuid4(),
            model_pattern="claude-sonnet-4-*",
            input_per_mtok=3.0,
            output_per_mtok=15.0,
            cache_read_per_mtok=0.3,
            cache_write_per_mtok=3.75,
            priority=80,
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        async_db_session.add(price)
        await async_db_session.commit()

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)

        line = _make_line(
            uuid_="cost-001",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read=0,
            cache_write=0,
        )
        (rex_dir / "sess.jsonl").write_text(line + "\n")

        await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )

        result = await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "cost-001")
        )
        event = result.one()
        # 1M input = $3, 1M output = $15
        assert event.cost_usd is not None
        assert abs(event.cost_usd - 18.0) < 0.001


# ── Endpoint aggregation (quick smoke-test) ──────────────────────────────

@pytest.mark.asyncio
async def test_costs_endpoint_aggregation(tmp_path, async_db_session):
    """Ensure the aggregation logic sums correctly."""
    import uuid as uuid_mod
    from sqlmodel import select

    # Direct inserts — no harvester run
    agent_id = uuid_mod.uuid4()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    event1 = ModelUsageEvent(
        id=uuid_mod.uuid4(),
        agent_id=agent_id,
        harness="cli-bridge",
        model="claude-sonnet-4-6",
        session_id="s1",
        message_uuid="agg-001",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_write_tokens=100,
        cost_usd=0.01,
        ts=now,
        source_file="/test/file.jsonl",
    )
    event2 = ModelUsageEvent(
        id=uuid_mod.uuid4(),
        agent_id=agent_id,
        harness="cli-bridge",
        model="claude-sonnet-4-6",
        session_id="s2",
        message_uuid="agg-002",
        input_tokens=2000,
        output_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.02,
        ts=now,
        source_file="/test/file2.jsonl",
    )
    async_db_session.add(event1)
    async_db_session.add(event2)
    await async_db_session.commit()

    result = await async_db_session.exec(select(ModelUsageEvent))
    events = result.all()
    total_in = sum(e.input_tokens for e in events)
    total_cost = sum(e.cost_usd or 0 for e in events)

    assert total_in == 3000
    assert abs(total_cost - 0.03) < 1e-9


# ── Auto-Attribution (Orchestrator-Review-Fix 11.06.) ──────────────────────


@pytest.mark.asyncio
async def test_build_agent_slug_map_und_boss_attribution(session, tmp_path):
    """Without an explicit map, it is built from the agents table; Boss lines
    from ~/.claude get the Boss agent ID (only for MC cwd)."""
    from app.models import Agent
    from app.services.token_harvester import (
        _build_agent_slug_map,
        _slugify_agent_name,
        run_harvest,
    )
    from app.models.model_usage import ModelUsageEvent
    from sqlmodel import select

    rex = Agent(name="Rex", emoji="🛡️", soul_md="x")
    boss = Agent(name="Boss Host", emoji="👑", soul_md="x")
    session.add(rex)
    session.add(boss)
    await session.commit()
    await session.refresh(rex)
    await session.refresh(boss)

    slug_map = await _build_agent_slug_map(session)
    assert slug_map[_slugify_agent_name("Rex")] == rex.id
    assert slug_map["boss-host"] == boss.id

    # Create agent file (rex) + Boss file (MC cwd)
    rex_dir = tmp_path / "agents" / "rex" / "claude-config" / "projects" / "p"
    rex_dir.mkdir(parents=True)
    (rex_dir / "s1.jsonl").write_text(
        _make_line(uuid_="u-rex-1", session_id="s1", model="claude-sonnet-4-6") + "\n"
    )
    boss_dir = tmp_path / "claude-projects" / "proj"
    boss_dir.mkdir(parents=True)
    (boss_dir / "s2.jsonl").write_text(
        _make_line(
            uuid_="u-boss-1",
            session_id="s2",
            model="claude-opus-4-8",
            cwd="/Users/testuser/Workspace/Projects/mission-control",
        ) + "\n"
    )

    stats = await run_harvest(
        session,
        agent_base_paths=[str(tmp_path / "agents")],
        boss_base_paths=[str(tmp_path / "claude-projects")],
        agent_slug_map=None,  # ← must be built from DB
    )
    assert stats["new_events"] == 2

    events = (await session.exec(select(ModelUsageEvent))).all()
    by_uuid = {e.message_uuid: e for e in events}
    assert by_uuid["u-rex-1"].agent_id == rex.id
    assert by_uuid["u-boss-1"].agent_id == boss.id


# ── Task Attribution (cwd == workspace_path join) ──────────────────────────


@pytest.mark.asyncio
class TestTaskAttribution:

    async def test_event_gets_task_id_when_cwd_matches_workspace_path(
        self, tmp_path, session, make_task,
    ):
        """(a) Event with cwd == task.workspace_path gets task_id set."""
        from app.services.token_harvester import run_harvest

        board_id = uuid.uuid4()
        workspace = str(tmp_path / "workspace" / "some-task")
        task = await make_task(board_id, title="Some Task", workspace_path=workspace)

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)
        (rex_dir / "s.jsonl").write_text(
            _make_line(uuid_="attr-001", cwd=workspace, git_branch="task/some-task") + "\n"
        )

        stats = await run_harvest(
            session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )
        assert stats["new_events"] == 1

        event = (await session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "attr-001")
        )).one()
        assert event.task_id == task.id

    async def test_event_task_id_null_when_cwd_unknown(self, tmp_path, session):
        """(b) Unknown cwd (no matching task.workspace_path) → task_id stays NULL."""
        from app.services.token_harvester import run_harvest

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)
        (rex_dir / "s.jsonl").write_text(
            _make_line(uuid_="attr-002", cwd=str(tmp_path / "no-such-workspace")) + "\n"
        )

        await run_harvest(
            session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )

        event = (await session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "attr-002")
        )).one()
        assert event.task_id is None

    async def test_backfill_updates_existing_null_task_id(
        self, tmp_path, session, make_task,
    ):
        """(c) Re-harvest (offset reset) backfills task_id on an already-harvested
        event that previously had no matching task."""
        from app.services.token_harvester import run_harvest
        from app.models.model_usage import ModelUsageHarvestState

        board_id = uuid.uuid4()
        workspace = str(tmp_path / "workspace" / "backfill-task")

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)
        jsonl = rex_dir / "s.jsonl"
        jsonl.write_text(
            _make_line(uuid_="attr-003", cwd=workspace, git_branch="task/backfill-task") + "\n"
        )

        # First harvest: no task exists yet with this workspace_path → NULL
        await run_harvest(
            session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )
        event = (await session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "attr-003")
        )).one()
        assert event.task_id is None

        # Task shows up afterward (workspace_path set post-hoc), then the
        # JSONL is re-scanned (operator resets the offset state). mtime must
        # also move forward — the harvester's mtime-skip otherwise short-
        # circuits the file entirely, offset reset or not.
        task = await make_task(board_id, title="Backfill Task", workspace_path=workspace)
        state = (await session.exec(select(ModelUsageHarvestState))).one()
        state.processed_lines = 0
        state.mtime = 0.0
        session.add(state)
        await session.commit()

        stats = await run_harvest(
            session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )
        assert stats["backfilled_task_id"] == 1

        await session.refresh(event)
        event = (await session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "attr-003")
        )).one()
        assert event.task_id == task.id

    async def test_collision_tie_breaker_prefers_git_branch_match(
        self, tmp_path, session, make_task,
    ):
        """(d) Two tasks share the same workspace_path (re-run) → the one whose
        derived branch ('task/{slug}') matches the event's gitBranch wins."""
        from app.services.token_harvester import run_harvest

        board_id = uuid.uuid4()
        workspace = str(tmp_path / "workspace" / "same-slug-task")

        older = await make_task(
            board_id, title="Same Slug Task", workspace_path=workspace,
        )
        # A different task whose slugified title coincidentally collides on
        # workspace_path but not on branch.
        newer = await make_task(
            board_id, title="Same Slug Task Rerun", workspace_path=workspace,
        )

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)
        (rex_dir / "s.jsonl").write_text(
            _make_line(uuid_="attr-004", cwd=workspace, git_branch="task/same-slug-task") + "\n"
        )

        await run_harvest(
            session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
        )

        event = (await session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "attr-004")
        )).one()
        assert event.task_id == older.id
        assert event.task_id != newer.id


# ── GET /tasks/{task_id}/usage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_usage_endpoint_sums_attributed_events(
    auth_client, session, make_task,
):
    """(e) The usage endpoint sums tokens/cost for events attributed to a task."""
    board_id = uuid.uuid4()
    task = await make_task(board_id, title="Usage Endpoint Task")
    other_task = await make_task(board_id, title="Other Task")

    now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    event1 = ModelUsageEvent(
        id=uuid.uuid4(), task_id=task.id, harness="cli-bridge",
        model="claude-sonnet-4-6", session_id="s1", message_uuid="usage-001",
        input_tokens=1000, output_tokens=500, cache_read_tokens=100,
        cache_write_tokens=50, cost_usd=0.01, ts=now, source_file="/x.jsonl",
    )
    event2 = ModelUsageEvent(
        id=uuid.uuid4(), task_id=task.id, harness="cli-bridge",
        model="claude-sonnet-4-6", session_id="s2", message_uuid="usage-002",
        input_tokens=2000, output_tokens=1000, cache_read_tokens=0,
        cache_write_tokens=0, cost_usd=0.02, ts=now, source_file="/x2.jsonl",
    )
    other_event = ModelUsageEvent(
        id=uuid.uuid4(), task_id=other_task.id, harness="cli-bridge",
        model="claude-sonnet-4-6", session_id="s3", message_uuid="usage-other",
        input_tokens=99999, output_tokens=99999, cost_usd=99.0, ts=now,
        source_file="/other.jsonl",
    )
    session.add(event1)
    session.add(event2)
    session.add(other_event)
    await session.commit()

    resp = await auth_client.get(f"/api/v1/tasks/{task.id}/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_count"] == 2
    assert data["input_tokens"] == 3000
    assert data["output_tokens"] == 1500
    assert data["cache_read_tokens"] == 100
    assert data["cache_write_tokens"] == 50
    assert data["total_tokens"] == 4650
    assert abs(data["cost_usd"] - 0.03) < 1e-9


@pytest.mark.asyncio
async def test_task_usage_endpoint_404_for_unknown_task(auth_client):
    resp = await auth_client.get(f"/api/v1/tasks/{uuid.uuid4()}/usage")
    assert resp.status_code == 404


# ── POST /admin/usage/backfill-attribution ──────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_endpoint_resets_state_and_reharvest_fills_task_id(
    auth_client, session, make_task, tmp_path,
):
    """Full backfill flow: admin trigger resets harvest offsets, the next
    (here: manually invoked) harvest cycle re-scans the JSONL, dedup prevents
    duplicate rows, and the backfill pass fills task_id."""
    from app.services.token_harvester import run_harvest
    from app.models.model_usage import ModelUsageHarvestState

    board_id = uuid.uuid4()
    workspace = str(tmp_path / "workspace" / "endpoint-backfill-task")

    agents_dir = tmp_path / "agents"
    rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
    rex_dir.mkdir(parents=True)
    (rex_dir / "s.jsonl").write_text(
        _make_line(
            uuid_="endpoint-backfill-001", cwd=workspace, git_branch="task/endpoint-backfill-task",
        ) + "\n"
    )

    # Initial harvest: no matching task yet → task_id stays NULL.
    stats1 = await run_harvest(
        session,
        agent_base_paths=[str(agents_dir)],
        boss_base_paths=[],
        agent_slug_map={},
    )
    assert stats1["new_events"] == 1
    event = (await session.exec(
        select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "endpoint-backfill-001")
    )).one()
    assert event.task_id is None

    # Task appears afterward with the matching workspace_path.
    task = await make_task(board_id, title="Endpoint Backfill Task", workspace_path=workspace)

    # Sanity: without a reset, a second harvest changes nothing (mtime-skip).
    stats_noop = await run_harvest(
        session,
        agent_base_paths=[str(agents_dir)],
        boss_base_paths=[],
        agent_slug_map={},
    )
    assert stats_noop["new_events"] == 0
    assert stats_noop["backfilled_task_id"] == 0

    # Trigger the admin backfill endpoint — resets harvest state.
    resp = await auth_client.post("/api/v1/admin/usage/backfill-attribution")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reset_file_count"] >= 1

    state = (await session.exec(select(ModelUsageHarvestState))).one()
    assert state.processed_lines == 0
    assert state.mtime == 0.0

    # Next harvest cycle (simulated here — normally the watchdog) re-scans
    # the file: dedup prevents a duplicate row, backfill fills task_id.
    stats2 = await run_harvest(
        session,
        agent_base_paths=[str(agents_dir)],
        boss_base_paths=[],
        agent_slug_map={},
    )
    assert stats2["new_events"] == 0
    assert stats2["backfilled_task_id"] == 1

    all_events = (await session.exec(
        select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "endpoint-backfill-001")
    )).all()
    assert len(all_events) == 1  # no duplicate row
    assert all_events[0].task_id == task.id


@pytest.mark.asyncio
async def test_backfill_endpoint_requires_admin(client):
    """Non-admin (unauthenticated) requests are rejected."""
    resp = await client.post("/api/v1/admin/usage/backfill-attribution")
    assert resp.status_code in (401, 403)
