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
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
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
    response_id: str | None = "chatcmpl-915e3d69480ffb2c",
    model: str = "Qwen/Qwen3.6-35B-A3B-FP8",
    provider: str | None = "mc-openai",
    timestamp: str = "2026-07-15T16:29:37.102Z",
    input_tokens: int = 28848,
    output_tokens: int = 135,
    cache_read: int = 0,
    cache_write: int = 0,
    role: str = "assistant",
    type_: str = "message",
    model_top_level: bool = False,
    provider_top_level: bool = False,
) -> str:
    """Builds a real-shaped omp JSONL line (ADR-045 headless harness).

    Verified in-container against mc-agent-sparky (2026-07-15, Qwen/Spark):
    everything except type/id/parentId/timestamp lives INSIDE `message` —
    model, provider, usage, responseId are all message.* fields, NOT
    top-level. ``model_top_level``/``provider_top_level`` exist only to test
    the top-level fallback path, not because real omp puts them there.
    """
    message: dict = {
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
        "api": "openai-completions",
    }
    if not model_top_level:
        message["model"] = model
    if provider is not None and not provider_top_level:
        message["provider"] = provider
    if response_id is not None:
        message["responseId"] = response_id

    line: dict = {
        "type": type_,
        "id": short_id,
        "parentId": "54c8d3f0",
        "timestamp": timestamp,
        "message": message,
    }
    if model_top_level:
        line["model"] = model
    if provider is not None and provider_top_level:
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
        assert rec["uuid"] == "sess-omp-1:chatcmpl-915e3d69480ffb2c"
        assert rec["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"
        assert rec["provider"] == "mc-openai"
        assert rec["input_tokens"] == 28848
        assert rec["output_tokens"] == 135
        assert rec["cache_read_tokens"] == 0
        assert rec["cache_write_tokens"] == 0

    def test_omp_real_sample_1to1(self):
        """The literal line captured from mc-agent-sparky's real omp-sessions
        JSONL (2026-07-15, anonymized only in tool-call argument content —
        every field relevant to parsing is untouched). Root-cause regression
        guard: an earlier fixture wrongly modeled model/provider/usage as
        top-level fields (they're nested under message), which made
        _parse_omp_line silently return None for every real omp line while
        all fixture-based tests kept passing."""
        raw_line = (
            '{"type":"message","id":"74f7a91e","parentId":"54c8d3f0",'
            '"timestamp":"2026-07-15T16:29:37.102Z","message":{"role":"assistant",'
            '"content":[{"type":"thinking","thinking":"...","thinkingSignature":"reasoning"},'
            '{"type":"toolCall","id":"chatcmpl-tool-80828a71e75b3b75","name":"bash",'
            '"arguments":{"command":"mc ack","i":"Ack task"}}],'
            '"api":"openai-completions","provider":"mc-openai",'
            '"model":"Qwen/Qwen3.6-35B-A3B-FP8",'
            '"usage":{"input":28848,"output":135,"cacheRead":0,"cacheWrite":0,'
            '"totalTokens":28983,"cost":{"input":0.00403872,"output":0.000135,'
            '"cacheRead":0,"cacheWrite":0,"total":0.00417372}},'
            '"stopReason":"toolUse","timestamp":1784132972177,'
            '"responseId":"chatcmpl-915e3d69480ffb2c","duration":4860.83,"ttft":1301.74,'
            '"contextSnapshot":{"promptTokens":28848,"nonMessageTokens":20602}}}'
        )
        rec = parse_transcript_line(
            raw_line, session_id="2026-07-15T16-29-31-091Z_019f669c-aa50"
        )
        assert rec is not None
        assert rec["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"
        assert rec["provider"] == "mc-openai"
        assert rec["input_tokens"] == 28848
        assert rec["output_tokens"] == 135
        assert rec["uuid"] == (
            "2026-07-15T16-29-31-091Z_019f669c-aa50:chatcmpl-915e3d69480ffb2c"
        )

    def test_omp_dedup_prefers_responseId_over_short_id(self):
        """message.responseId (a full chatcmpl-* id) is far less
        collision-prone than the top-level `id` (8 hex) and must win when
        present."""
        line = _make_omp_line(short_id="dupe", response_id="chatcmpl-unique-xyz")
        rec = parse_transcript_line(line, session_id="s")
        assert rec["uuid"] == "s:chatcmpl-unique-xyz"

    def test_omp_dedup_falls_back_to_short_id_without_responseId(self):
        line = _make_omp_line(short_id="dupe", response_id=None)
        rec = parse_transcript_line(line, session_id="s")
        assert rec["uuid"] == "s:dupe"

    def test_omp_dedup_key_namespaced_by_session(self):
        """The dedup key must be namespaced by session_id even when
        responseId is stable/shared, since one JSONL file == one session."""
        line = _make_omp_line(response_id="same-response-id")
        rec_a = parse_transcript_line(line, session_id="session-a")
        rec_b = parse_transcript_line(line, session_id="session-b")
        assert rec_a["uuid"] != rec_b["uuid"]
        assert rec_a["uuid"] == "session-a:same-response-id"
        assert rec_b["uuid"] == "session-b:same-response-id"

    def test_omp_line_without_session_id_uses_empty_prefix(self):
        line = _make_omp_line(response_id="abc123")
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
        del d["message"]["model"]
        assert parse_transcript_line(json.dumps(d), session_id="s") is None

    def test_omp_model_falls_back_to_top_level_if_present(self):
        """Real omp never puts model top-level, but the fallback protects
        against a future omp version moving it there without us noticing."""
        line = _make_omp_line(model_top_level=True, model="fallback-model")
        rec = parse_transcript_line(line, session_id="s")
        assert rec is not None
        assert rec["model"] == "fallback-model"

    def test_omp_provider_falls_back_to_top_level_if_present(self):
        line = _make_omp_line(provider_top_level=True, provider="fallback-provider")
        rec = parse_transcript_line(line, session_id="s")
        assert rec is not None
        assert rec["provider"] == "fallback-provider"

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
        jsonl.write_text(_make_omp_line(short_id="idA", response_id=None) + "\n")

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

    # Real omp file preamble (copied from a live sparky file, 2026-07-16):
    # line 0 is a `title` line, line 1 `model_change`, the session header with
    # the cwd sits on line 2 — its position is NOT fixed at 0.
    _OMP_PREAMBLE = (
        '{"type":"title","v":1,"title":"","updatedAt":"2026-07-16T07:08:51.469Z","pad":"  "}\n'
        '{"type":"model_change","id":"1e6aa13b","parentId":null,'
        '"timestamp":"2026-07-16T07:08:51.658Z","model":"mc-openai/Qwen/Qwen3.6-35B-A3B-FP8"}\n'
        '{"type":"session","version":3,"id":"019f69c1-b98c-7000-8977-c82d7c797c8e",'
        '"timestamp":"2026-07-16T07:08:51.469Z",'
        '"cwd":"/workspace/bench-borealis-qwen-grok-dgx-spark-qwen-3-5-6cd990"}\n'
    )

    def test_omp_session_header_cwd_fills_message_records(self, tmp_path):
        """omp-JSONL carries cwd ONLY on the first type:"session" header line.
        Root-cause regression guard (Bench #18): without the header fallback
        every omp record has cwd="" and can never be attributed to a task."""
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "2026-07-16T07-08-51-469Z_019f69c1.jsonl"
        jsonl.write_text(
            self._OMP_PREAMBLE + _make_omp_line(short_id="idA") + "\n"
        )

        records = harvest_file(str(jsonl), processed_lines=0)
        assert len(records) == 1
        assert records[0]["cwd"] == (
            "/workspace/bench-borealis-qwen-grok-dgx-spark-qwen-3-5-6cd990"
        )

    def test_omp_session_header_cwd_survives_offset_resume(self, tmp_path):
        """Offset resume skips line 0 — the header cwd must still be applied
        to records read past the offset."""
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "2026-07-16T07-08-51-469Z_019f69c1.jsonl"
        jsonl.write_text(
            self._OMP_PREAMBLE
            + _make_omp_line(short_id="idA") + "\n"
            + _make_omp_line(short_id="idB", response_id="respB") + "\n"
        )

        records = harvest_file(str(jsonl), processed_lines=4)
        assert len(records) == 1
        assert records[0]["cwd"] == (
            "/workspace/bench-borealis-qwen-grok-dgx-spark-qwen-3-5-6cd990"
        )

    def test_claude_per_line_cwd_wins_over_header(self, tmp_path):
        """Claude-format lines carry their own cwd — the header fallback must
        never overwrite a non-empty per-line cwd."""
        from app.services.token_harvester import harvest_file

        jsonl = tmp_path / "session.jsonl"
        jsonl.write_text(_make_line(uuid_="uuid-1", cwd="/home/agent") + "\n")

        records = harvest_file(str(jsonl), processed_lines=0)
        assert len(records) == 1
        assert records[0]["cwd"] == "/home/agent"


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
            _make_omp_line(short_id="omp001", response_id=None, model="Qwen/Qwen3.6-35B-A3B-FP8")
            + "\n"
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
        (omp_dir / "sess.jsonl").write_text(
            _make_omp_line(short_id="omp-evt", response_id=None) + "\n"
        )

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


# ── Bench #18 PR1: cwd translation (container → host), Grok source, ────────
# ── Hermes source ────────────────────────────────────────────────────────


class TestTranslateAgentCwd:
    """_translate_agent_cwd — inverse of dispatch._container_workspace_path.
    cli-bridge/sparky JSONL transcripts record the CONTAINER cwd
    (/workspace/...); tasks.workspace_path is the HOST path. Without this
    rewrite _resolve_task_for_rec's exact-match lookup can never hit."""

    def test_workspace_root_translated(self):
        from app.services.token_harvester import _translate_agent_cwd, _host_home

        assert _translate_agent_cwd("/workspace", "freecode") == str(
            _host_home() / ".mc" / "workspaces" / "freecode"
        )

    def test_workspace_subpath_translated(self):
        from app.services.token_harvester import _translate_agent_cwd, _host_home

        result = _translate_agent_cwd("/workspace/projects/xyz/.worktrees/task-abc", "rex")
        assert result == str(
            _host_home() / ".mc" / "workspaces" / "rex" / "projects/xyz/.worktrees/task-abc"
        )

    def test_non_workspace_cwd_passthrough(self):
        """A cwd that doesn't start with /workspace (e.g. already host-side,
        or a boss-host line) is returned unchanged."""
        from app.services.token_harvester import _translate_agent_cwd

        assert _translate_agent_cwd("/Users/testuser/some/path", "rex") == \
            "/Users/testuser/some/path"

    def test_workspacefoo_not_confused_with_workspace(self):
        """A sibling dir named /workspacefoo must NOT be treated as /workspace
        (startswith check must anchor on the path boundary)."""
        from app.services.token_harvester import _translate_agent_cwd

        assert _translate_agent_cwd("/workspacefoo/bar", "rex") == "/workspacefoo/bar"


@pytest.mark.asyncio
class TestCwdTranslationIntegration:
    """run_harvest end-to-end: a cli-bridge transcript line with a container
    cwd (/workspace/<slug>) must attribute to the task whose workspace_path
    is the corresponding host path."""

    async def test_container_cwd_attributes_to_host_task(self, tmp_path, async_db_session):
        from app.services.token_harvester import run_harvest, _host_home, _normalize_workspace_path

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)

        # Real convention (dispatch._container_workspace_path):
        # host ~/.mc/workspaces/<slug>/... <-> container /workspace/...
        host_workspace = str(_host_home() / ".mc" / "workspaces" / "rex" / "some-task-slug")

        line = _make_line(
            uuid_="cwd-xlate-001",
            cwd="/workspace/some-task-slug",
            git_branch="task/some-task-slug",
        )
        (rex_dir / "s.jsonl").write_text(line + "\n")

        task_id = uuid.uuid4()
        task_workspace_map = {
            _normalize_workspace_path(host_workspace): [{
                "task_id": task_id,
                "branch": "task/some-task-slug",
                "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "completed_at": None,
            }],
        }

        await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
            task_workspace_map=task_workspace_map,
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "cwd-xlate-001")
        )).one()
        assert event.task_id == task_id

    async def test_untranslated_cwd_would_not_match(self, tmp_path, async_db_session):
        """Regression guard: without translation, the raw /workspace/... cwd
        never matches a host-style workspace_path — task_id stays NULL. This
        documents the bug the fix closes (same fixtures, translation absent
        because the task_workspace_map key is never hit)."""
        from app.services.token_harvester import run_harvest, _normalize_workspace_path

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)

        line = _make_line(uuid_="cwd-xlate-002", cwd="/workspace/other-slug", git_branch=None)
        (rex_dir / "s.jsonl").write_text(line + "\n")

        # Map keyed by the literal (untranslated) container cwd — a host task
        # would never have this as its workspace_path in reality.
        task_workspace_map = {
            _normalize_workspace_path("/workspace/other-slug"): [{
                "task_id": uuid.uuid4(),
                "branch": "task/x",
                "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "completed_at": None,
            }],
        }

        await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
            task_workspace_map=task_workspace_map,
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "cwd-xlate-002")
        )).one()
        assert event.task_id is None


# ── Grok source (ADR-066 host harness) ──────────────────────────────────────


# Real line copied verbatim from ~/.grok/logs/unified.jsonl (2026-07-10,
# sid 019f4dd6-6505-7510-b05c-b6dfc47a2c2d — a real summary.json for this
# exact sid is used below too, see _GROK_REAL_SUMMARY).
_GROK_REAL_LINE = (
    '{"ts":"2026-07-10T21:02:09.251Z","src":"shell","pid":41213,"lvl":"info",'
    '"sid":"019f4dd6-6505-7510-b05c-b6dfc47a2c2d","msg":"shell.turn.inference_done",'
    '"ctx":{"loop_index":1,"model_elapsed_ms":1493,"elapsed_since_turn_start_ms":1494,'
    '"ttft_ms":767,"itl_p50_ms":0,"attempts":1,"prompt_tokens":18609,'
    '"cached_prompt_tokens":6016,"completion_tokens":35,"reasoning_tokens":27,'
    '"tokens_per_sec":48.2}}'
)

# Real summary.json for the same sid (~/.grok/sessions/<urlenc-cwd>/<sid>/summary.json).
_GROK_REAL_SUMMARY = {
    "info": {
        "id": "019f4dd6-6505-7510-b05c-b6dfc47a2c2d",
        "cwd": "/private/tmp/claude-502/-Users-op-Workspace/c254deb0-476b-4efa-8162-6576f0efbedb/scratchpad",
    },
    "session_summary": "",
    "created_at": "2026-07-10T21:02:04.137856Z",
    "updated_at": "2026-07-10T21:02:09.366124Z",
    "num_messages": 4,
    "num_chat_messages": 8,
    "current_model_id": "grok-4.5",
    "next_trace_turn": 1,
    "chat_format_version": 1,
    "request_id": "8566b412-eec9-4167-8f2c-2bd751ed97f0",
    "grok_home": "/Users/op/.grok",
    "last_active_at": "2026-07-10T21:02:09.260043Z",
    "agent_name": "grok-build-plan",
    "sandbox_profile": "off",
    "reasoning_effort": "high",
}

# Real prompt_history.jsonl line (~/.grok/sessions/<urlenc-cwd>/prompt_history.jsonl,
# 2026-07-11) — [MC DISPATCH] task_id= regex source.
_GROK_REAL_PROMPT_HISTORY_LINE = (
    '{"timestamp":"2026-07-11T13:39:21.584940Z",'
    '"session_id":"af1f7d2c-25eb-41fd-84b3-47cc4cf4e055",'
    '"prompt":"[MC DISPATCH] task_id=14513937-c943-4c8f-93c6-b3023a79c04d '
    'board_id=7bd0be90-c45a-4a15-9037-ebb72f15ba09 '
    'attempt_id=e2b6dd29-7cbe-4ed4-ba6e-c72e69af5d54\\nTitle: Grok live smoke test"}'
)


class TestParseGrokLine:
    def test_real_sample_parsed(self):
        """1:1 real sample — token math + dedup uuid."""
        from app.services.token_harvester import parse_grok_line

        rec = parse_grok_line(_GROK_REAL_LINE)
        assert rec is not None
        assert rec["sid"] == "019f4dd6-6505-7510-b05c-b6dfc47a2c2d"
        assert rec["timestamp"] == "2026-07-10T21:02:09.251Z"
        # input = prompt_tokens - cached_prompt_tokens = 18609 - 6016
        assert rec["input_tokens"] == 12593
        assert rec["cache_read_tokens"] == 6016
        assert rec["cache_write_tokens"] == 0
        assert rec["output_tokens"] == 35  # completion_tokens (reasoning included)
        assert rec["uuid"] == "grok:019f4dd6-6505-7510-b05c-b6dfc47a2c2d:2026-07-10T21:02:09.251Z:1"

    def test_non_inference_done_lines_skipped(self):
        from app.services.token_harvester import parse_grok_line

        other = json.dumps({"ts": "x", "sid": "s1", "msg": "shell.turn.started", "ctx": {}})
        assert parse_grok_line(other) is None

    def test_invalid_json_skipped(self):
        from app.services.token_harvester import parse_grok_line

        assert parse_grok_line("not json{{{") is None

    def test_missing_ctx_skipped(self):
        from app.services.token_harvester import parse_grok_line

        line = json.dumps({"ts": "x", "sid": "s1", "msg": "shell.turn.inference_done"})
        assert parse_grok_line(line) is None

    def test_input_tokens_floor_zero(self):
        """cached_prompt_tokens > prompt_tokens (shouldn't happen, but never
        go negative)."""
        from app.services.token_harvester import parse_grok_line

        line = json.dumps({
            "ts": "2026-01-01T00:00:00.000Z", "sid": "s1",
            "msg": "shell.turn.inference_done",
            "ctx": {"loop_index": 1, "prompt_tokens": 10, "cached_prompt_tokens": 50,
                    "completion_tokens": 5},
        })
        rec = parse_grok_line(line)
        assert rec is not None
        assert rec["input_tokens"] == 0


class TestGrokSessionIndex:
    def test_real_summary_json_indexed(self, tmp_path):
        from app.services.token_harvester import _build_grok_session_index

        sess_dir = tmp_path / "sessions" / "%2Fsome%2Fcwd" / "019f4dd6-6505-7510-b05c-b6dfc47a2c2d"
        sess_dir.mkdir(parents=True)
        (sess_dir / "summary.json").write_text(json.dumps(_GROK_REAL_SUMMARY))

        index = _build_grok_session_index(str(tmp_path / "sessions"))
        entry = index["019f4dd6-6505-7510-b05c-b6dfc47a2c2d"]
        assert entry["model"] == "grok-4.5"
        assert entry["cwd"] == (
            "/private/tmp/claude-502/-Users-op-Workspace/"
            "c254deb0-476b-4efa-8162-6576f0efbedb/scratchpad"
        )

    def test_missing_sessions_base_returns_empty(self, tmp_path):
        from app.services.token_harvester import _build_grok_session_index

        assert _build_grok_session_index(str(tmp_path / "nonexistent")) == {}


class TestGrokTaskIndex:
    def test_real_prompt_history_task_id_extracted(self, tmp_path):
        from app.services.token_harvester import _build_grok_task_index

        cwd_dir = tmp_path / "sessions" / "%2FUsers%2Fop%2F.mc%2Fworkspaces%2Fgrok"
        cwd_dir.mkdir(parents=True)
        (cwd_dir / "prompt_history.jsonl").write_text(_GROK_REAL_PROMPT_HISTORY_LINE + "\n")

        index = _build_grok_task_index(str(tmp_path / "sessions"))
        assert index["af1f7d2c-25eb-41fd-84b3-47cc4cf4e055"] == uuid.UUID(
            "14513937-c943-4c8f-93c6-b3023a79c04d"
        )

    def test_no_task_id_prompt_not_indexed(self, tmp_path):
        from app.services.token_harvester import _build_grok_task_index

        cwd_dir = tmp_path / "sessions" / "%2Fsome%2Fcwd"
        cwd_dir.mkdir(parents=True)
        line = json.dumps({"session_id": "s-no-task", "prompt": "just chatting, no dispatch"})
        (cwd_dir / "prompt_history.jsonl").write_text(line + "\n")

        index = _build_grok_task_index(str(tmp_path / "sessions"))
        assert "s-no-task" not in index


@pytest.mark.asyncio
class TestGrokHarvestIntegration:
    """run_harvest end-to-end for the Grok source: unified.jsonl + sessions/
    (summary.json + prompt_history.jsonl) → ModelUsageEvent."""

    def _write_grok_fixtures(self, tmp_path, *, with_task_id: bool = True):
        grok_log = tmp_path / "unified.jsonl"
        grok_log.write_text(_GROK_REAL_LINE + "\n")

        sessions_base = tmp_path / "sessions"
        cwd_dir_name = "%2FUsers%2Fop%2F.mc%2Fworkspaces%2Fgrok"
        sess_dir = sessions_base / cwd_dir_name / "019f4dd6-6505-7510-b05c-b6dfc47a2c2d"
        sess_dir.mkdir(parents=True)
        summary = dict(_GROK_REAL_SUMMARY)
        summary["info"] = dict(summary["info"])
        summary["info"]["cwd"] = "/Users/op/.mc/workspaces/grok"
        (sess_dir / "summary.json").write_text(json.dumps(summary))

        if with_task_id:
            history_line = json.dumps({
                "session_id": "019f4dd6-6505-7510-b05c-b6dfc47a2c2d",
                "prompt": "[MC DISPATCH] task_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa board_id=x",
            })
            (sessions_base / cwd_dir_name / "prompt_history.jsonl").write_text(history_line + "\n")

        return grok_log, sessions_base

    async def test_grok_event_inserted_with_task_id_from_prompt_history(
        self, tmp_path, async_db_session
    ):
        from app.services.token_harvester import run_harvest

        grok_log, sessions_base = self._write_grok_fixtures(tmp_path)
        grok_agent_id = uuid.uuid4()

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={"grok": grok_agent_id},
            grok_log_path=str(grok_log),
            grok_sessions_path=str(sessions_base),
            hermes_state_db_path=str(tmp_path / "nonexistent_state.db"),
        )
        assert stats["new_events"] == 1
        assert stats["grok_skipped_no_summary"] == 0

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.harness == "grok")
        )).one()
        assert event.model == "grok-4.5"
        assert event.provider == "xai"
        assert event.agent_id == grok_agent_id
        assert event.input_tokens == 12593
        assert event.output_tokens == 35
        assert event.cache_read_tokens == 6016
        assert event.task_id == uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def test_grok_falls_back_to_cwd_workspace_map_without_prompt_history_task_id(
        self, tmp_path, async_db_session
    ):
        from app.services.token_harvester import run_harvest, _normalize_workspace_path

        grok_log, sessions_base = self._write_grok_fixtures(tmp_path, with_task_id=False)
        fallback_task_id = uuid.uuid4()
        task_workspace_map = {
            _normalize_workspace_path("/Users/op/.mc/workspaces/grok"): [{
                "task_id": fallback_task_id,
                "branch": "task/x",
                "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "completed_at": None,
            }],
        }

        await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            task_workspace_map=task_workspace_map,
            grok_log_path=str(grok_log),
            grok_sessions_path=str(sessions_base),
            hermes_state_db_path=str(tmp_path / "nonexistent_state.db"),
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.harness == "grok")
        )).one()
        assert event.task_id == fallback_task_id

    async def test_grok_event_skipped_without_summary_match(self, tmp_path, async_db_session):
        """No summary.json for the sid → skipped (counted), never guessed."""
        from app.services.token_harvester import run_harvest

        grok_log = tmp_path / "unified.jsonl"
        grok_log.write_text(_GROK_REAL_LINE + "\n")
        sessions_base = tmp_path / "sessions"
        sessions_base.mkdir()

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(grok_log),
            grok_sessions_path=str(sessions_base),
            hermes_state_db_path=str(tmp_path / "nonexistent_state.db"),
        )
        assert stats["new_events"] == 0
        assert stats["grok_skipped_no_summary"] == 1

        result = await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.harness == "grok")
        )
        assert result.all() == []

    async def test_grok_price_applied(self, tmp_path, async_db_session):
        """A grok-4.5* price row (as already exists in prod) computes cost_usd."""
        from app.services.token_harvester import run_harvest

        price = ModelPrice(
            id=uuid.uuid4(), model_pattern="grok-4.5*",
            input_per_mtok=2.0, output_per_mtok=6.0,
            cache_read_per_mtok=0.2, cache_write_per_mtok=0.0,
            priority=80, valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        async_db_session.add(price)
        await async_db_session.commit()

        grok_log, sessions_base = self._write_grok_fixtures(tmp_path)

        await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(grok_log),
            grok_sessions_path=str(sessions_base),
            hermes_state_db_path=str(tmp_path / "nonexistent_state.db"),
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.harness == "grok")
        )).one()
        assert event.cost_usd is not None
        assert event.cost_usd > 0

    async def test_grok_idempotent_second_run_zero_new(self, tmp_path, async_db_session):
        from app.services.token_harvester import run_harvest

        grok_log, sessions_base = self._write_grok_fixtures(tmp_path)
        kwargs = dict(
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(grok_log),
            grok_sessions_path=str(sessions_base),
            hermes_state_db_path=str(tmp_path / "nonexistent_state.db"),
        )

        stats1 = await run_harvest(async_db_session, **kwargs)
        assert stats1["new_events"] == 1

        stats2 = await run_harvest(async_db_session, **kwargs)
        assert stats2["new_events"] == 0

        result = await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.harness == "grok")
        )
        assert len(result.all()) == 1

    async def test_truncated_file_resets_offset_instead_of_hanging(
        self, tmp_path, async_db_session
    ):
        """Review fix (21.07.): a stored offset past the file's actual line
        count (log rotation / Grok Build CLI reset) must reset to 0 instead
        of silently reading nothing forever."""
        from app.services.token_harvester import run_harvest
        from app.models.model_usage import ModelUsageHarvestState

        grok_log, sessions_base = self._write_grok_fixtures(tmp_path)  # 1 real line

        # Seed a stale harvest_state claiming 99 lines were already
        # processed — now far past the file's single actual line. mtime is
        # deliberately wrong too, so the mtime-unchanged guard doesn't short
        # -circuit before the truncation check ever runs.
        stale_state = ModelUsageHarvestState(
            file_path=str(grok_log), mtime=0.0, processed_lines=99,
        )
        async_db_session.add(stale_state)
        await async_db_session.commit()

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(grok_log),
            grok_sessions_path=str(sessions_base),
            hermes_state_db_path=str(tmp_path / "nonexistent_state.db"),
        )
        # Without the guard, processed_lines=99 > 1 line would make
        # harvest_grok_file skip everything — the reset makes this 1, not 0.
        assert stats["new_events"] == 1


# ── Hermes source ────────────────────────────────────────────────────────


def _make_hermes_db(path: Path) -> None:
    """Builds a sqlite DB with the REAL Hermes schema (introspected from
    ~/.hermes/state.db via `sqlite3 -readonly ... .schema`, columns kept
    verbatim — only the FTS triggers/tables are dropped since they're
    irrelevant to the harvester and not worth reproducing in a fixture)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0, "handoff_state" TEXT, "handoff_platform" TEXT,
            "handoff_error" TEXT, "cwd" TEXT, "rewind_count" INTEGER NOT NULL DEFAULT 0,
            "session_key" TEXT, "chat_id" TEXT, "chat_type" TEXT, "thread_id" TEXT,
            "display_name" TEXT, "origin_json" TEXT, "expiry_finalized" INTEGER DEFAULT 0,
            "git_branch" TEXT, "git_repo_root" TEXT, "compression_failure_cooldown_until" REAL,
            "compression_failure_error" TEXT, "archived" INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            codex_message_items TEXT
            , "platform_message_id" TEXT, "observed" INTEGER DEFAULT 0,
            "active" INTEGER NOT NULL DEFAULT 1, "compacted" INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


@pytest.mark.asyncio
class TestHermesHarvestIntegration:
    async def test_finished_session_inserted_with_task_id_from_first_user_message(
        self, tmp_path, async_db_session
    ):
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)

        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cwd, git_branch) "
            "VALUES (?, 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 1000, 200, 50, 0, '/some/cwd', 'task/x')",
            ("hermes-sess-001", now - 3600, now),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
            ("hermes-sess-001",
             "[MC DISPATCH] task_id=dcc67a52-e8f2-4354-b928-f844074c99ba board_id=x\nTitle: Test",
             now - 3600),
        )
        conn.commit()
        conn.close()

        hermes_agent_id = uuid.uuid4()
        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={"hermes": hermes_agent_id},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )
        assert stats["new_events"] == 1
        assert stats["hermes_sessions_scanned"] == 1

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "hermes:hermes-sess-001")
        )).one()
        assert event.harness == "hermes"
        assert event.agent_id == hermes_agent_id
        assert event.input_tokens == 1000
        assert event.output_tokens == 200
        assert event.cache_read_tokens == 50
        assert event.task_id == uuid.UUID("dcc67a52-e8f2-4354-b928-f844074c99ba")

    async def test_unfinished_session_skipped(self, tmp_path, async_db_session):
        """ended_at IS NULL → not scanned at all."""
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at) "
            "VALUES ('unfinished-1', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, NULL)",
            (now,),
        )
        conn.commit()
        conn.close()

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )
        assert stats["new_events"] == 0
        assert stats["hermes_sessions_scanned"] == 0

    async def test_fallback_to_cwd_git_branch_workspace_map(self, tmp_path, async_db_session):
        """No task_id in the first user message → fall back to cwd/git_branch
        workspace map resolution (same _resolve_task_id cascade as JSONL sources)."""
        from app.services.token_harvester import run_harvest, _normalize_workspace_path

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, cwd, git_branch) "
            "VALUES ('sess-fallback', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, '/x/workspace', 'task/fallback-slug')",
            (now - 100, now),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES "
            "('sess-fallback', 'user', 'no dispatch marker here', ?)",
            (now - 100,),
        )
        conn.commit()
        conn.close()

        fallback_task_id = uuid.uuid4()
        task_workspace_map = {
            _normalize_workspace_path("/x/workspace"): [{
                "task_id": fallback_task_id,
                "branch": "task/fallback-slug",
                "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "completed_at": None,
            }],
        }

        await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            task_workspace_map=task_workspace_map,
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "hermes:sess-fallback")
        )).one()
        assert event.task_id == fallback_task_id

    async def test_old_session_before_cutoff_excluded(self, tmp_path, async_db_session):
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at) "
            "VALUES ('too-old', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?)",
            (old_ts, old_ts + 60),
        )
        conn.commit()
        conn.close()

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )
        assert stats["new_events"] == 0
        assert stats["hermes_sessions_scanned"] == 0

    async def test_idempotent_second_run_zero_new(self, tmp_path, async_db_session):
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('idem-sess', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 500)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        kwargs = dict(
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )
        stats1 = await run_harvest(async_db_session, **kwargs)
        assert stats1["new_events"] == 1

        stats2 = await run_harvest(async_db_session, **kwargs)
        assert stats2["new_events"] == 0

        result = await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.harness == "hermes")
        )
        assert len(result.all()) == 1

    async def test_wal_and_shm_never_touched_copy_only_semantics(self, tmp_path, async_db_session):
        """Review fix (21.07.): -wal/-shm are no longer mounted or copied at
        all (Docker creates a DIRECTORY on the host for a missing bind
        source, and Hermes deletes -wal/-shm itself on clean shutdown — a
        backend recreate at the wrong moment would corrupt Hermes' own DB).
        The harvester now reads ONLY the checkpointed state.db, whether or
        not -wal/-shm happen to exist next to it on disk."""
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        # Even if -wal/-shm DO exist on disk (as they normally would next to
        # a live Hermes DB), the harvester must never touch them.
        (tmp_path / "state.db-wal").write_bytes(b"not a real wal file")
        (tmp_path / "state.db-shm").write_bytes(b"not a real shm file")

        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('wal-less-sess', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 42)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )
        assert stats["new_events"] == 1

    async def test_hermes_price_applied(self, tmp_path, async_db_session):
        from app.services.token_harvester import run_harvest

        price = ModelPrice(
            id=uuid.uuid4(), model_pattern="Qwen/Qwen3.6-27B-FP8",
            input_per_mtok=0.0, output_per_mtok=0.0,
            cache_read_per_mtok=0.0, cache_write_per_mtok=0.0,
            priority=90, valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        async_db_session.add(price)
        await async_db_session.commit()

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens, output_tokens) "
            "VALUES ('priced-sess', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 1000, 1000)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "hermes:priced-sess")
        )).one()
        assert event.cost_usd == 0.0  # matched (local model, $0), not None (unmatched)

    async def test_reasoning_tokens_included_in_output_tokens(self, tmp_path, async_db_session):
        """Billing convention (review fix, 21.07.): reasoning_tokens is a
        distinct sqlite column here (unlike Grok, where it's already folded
        into completion_tokens) but is still billed as output."""
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, "
            "input_tokens, output_tokens, reasoning_tokens) "
            "VALUES ('reason-sess', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 100, 50, 20)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "hermes:reason-sess")
        )).one()
        assert event.output_tokens == 70  # 50 + 20, not 50

    async def test_billing_provider_column_preferred_over_heuristic(self, tmp_path, async_db_session):
        """sessions.billing_provider is Hermes' own authoritative label —
        must win over the model-name heuristic when non-null."""
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, billing_provider) "
            # A model that the heuristic would call "lmstudio" (has a "/") —
            # billing_provider should still win.
            "VALUES ('provider-sess', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 'openrouter')",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "hermes:provider-sess")
        )).one()
        assert event.provider == "openrouter"

    async def test_billing_provider_null_falls_back_to_heuristic(self, tmp_path, async_db_session):
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at) "
            "VALUES ('no-provider-sess', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )

        event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "hermes:no-provider-sess")
        )).one()
        assert event.provider == "lmstudio"  # heuristic: "/" in model name

    async def test_cutoff_filters_on_ended_at_not_started_at(self, tmp_path, async_db_session):
        """Review fix (21.07.): a session started 60 days ago but ended
        recently must be INCLUDED (started_at-based filtering would have
        wrongly excluded it); a session started recently but... the cutoff
        is always about when the session actually finished."""
        from app.services.token_harvester import run_harvest

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        old_start = now - 60 * 86400  # 60 days ago — outside a started_at-based window
        recent_end = now - 10
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('long-running-sess', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 10)",
            (old_start, recent_end),
        )
        conn.commit()
        conn.close()

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(tmp_path / "nonexistent_unified.jsonl"),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )
        assert stats["hermes_sessions_scanned"] == 1
        assert stats["new_events"] == 1


class TestHermesMtimeSkipAndThrottle:
    """Review fix (21.07.): the Hermes source copies+reads the whole
    state.db on every read — an unconditional per-tick copy would churn
    hundreds of MB/day. mtime-unchanged skips entirely; even a changed
    mtime is throttled to at most one re-read per throttle window."""

    async def test_mtime_unchanged_skips_scan_entirely(self, tmp_path, async_db_session):
        from app.services.token_harvester import _harvest_hermes
        from app.models.model_usage import ModelUsageHarvestState

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('mtime-sess', 'cli', 'm', ?, ?, 10)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        current_mtime = os.path.getmtime(db_path)
        # Pre-seed state as if this exact file version was already harvested.
        state_map = {
            str(db_path): ModelUsageHarvestState(
                file_path=str(db_path), mtime=current_mtime, processed_lines=0,
            ),
        }
        stats = {"new_events": 0, "hermes_sessions_scanned": 0}

        await _harvest_hermes(async_db_session, str(db_path), None, [], stats, state_map)

        assert stats["hermes_sessions_scanned"] == 0  # never even opened the copy

    async def test_changed_mtime_within_throttle_window_still_skips(
        self, tmp_path, async_db_session
    ):
        from app.services.token_harvester import _harvest_hermes
        from app.models.model_usage import ModelUsageHarvestState

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('throttled-sess', 'cli', 'm', ?, ?, 10)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        current_mtime = os.path.getmtime(db_path)
        # Stored mtime deliberately differs (simulating Hermes having written
        # again since the last successful harvest) — but processed_lines
        # (repurposed as the last-run unix ts) says "just now".
        state_map = {
            str(db_path): ModelUsageHarvestState(
                file_path=str(db_path), mtime=current_mtime - 1, processed_lines=int(now),
            ),
        }
        stats = {"new_events": 0, "hermes_sessions_scanned": 0}

        await _harvest_hermes(
            async_db_session, str(db_path), None, [], stats, state_map,
            throttle_seconds=900,
        )

        assert stats["hermes_sessions_scanned"] == 0  # throttled despite mtime differing

    async def test_throttle_window_elapsed_allows_scan(self, tmp_path, async_db_session):
        from app.services.token_harvester import _harvest_hermes
        from app.models.model_usage import ModelUsageHarvestState

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('unthrottled-sess', 'cli', 'm', ?, ?, 10)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        current_mtime = os.path.getmtime(db_path)
        state_map = {
            str(db_path): ModelUsageHarvestState(
                file_path=str(db_path), mtime=current_mtime - 1, processed_lines=int(now) - 1000,
            ),
        }
        stats = {"new_events": 0, "hermes_sessions_scanned": 0}

        await _harvest_hermes(
            async_db_session, str(db_path), None, [], stats, state_map,
            throttle_seconds=900,
        )

        assert stats["hermes_sessions_scanned"] == 1  # 1000s > 900s throttle window


class TestHermesImmutableCopyOpen:
    """Review fix (21.07.): open the COPY with sqlite3 URI ?immutable=1 —
    direct unit tests of the sync reader, no event loop needed."""

    def test_reads_finished_sessions_from_a_healthy_copy(self, tmp_path):
        from app.services.token_harvester import _read_hermes_sessions_sync

        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('immut-sess', 'cli', 'm', ?, ?, 7)",
            (now - 5, now),
        )
        conn.commit()
        conn.close()

        rows = _read_hermes_sessions_sync(str(db_path), now - 86400)
        assert rows is not None
        assert len(rows) == 1
        assert rows[0]["id"] == "immut-sess"
        assert rows[0]["input_tokens"] == 7

    def test_corrupt_copy_returns_none_not_raises(self, tmp_path):
        """Torn copy ('database disk image is malformed') → None, not an
        exception — the caller (run_harvest's per-source try/except) is a
        second line of defense, but this is the expected/anticipated path,
        handled locally and quietly."""
        from app.services.token_harvester import _read_hermes_sessions_sync

        bad_db = tmp_path / "state.db"
        bad_db.write_text("this is not a valid sqlite file at all")

        rows = _read_hermes_sessions_sync(str(bad_db), 0.0)
        assert rows is None

    def test_tmp_dir_cleaned_up_even_on_corrupt_copy(self, tmp_path):
        """No leaked temp dirs regardless of whether the copy is readable."""
        import glob

        from app.services.token_harvester import _read_hermes_sessions_sync

        bad_db = tmp_path / "state.db"
        bad_db.write_text("not sqlite")

        before = set(glob.glob(tempfile.gettempdir() + "/mc_hermes_harvest_*"))
        _read_hermes_sessions_sync(str(bad_db), 0.0)
        after = set(glob.glob(tempfile.gettempdir() + "/mc_hermes_harvest_*"))
        assert after - before == set()


class TestCopyHermesDbTmpDirLeak:
    """Review fix (21.07.): a failed copy must not leak its temp dir."""

    def test_failed_copy_cleans_up_tmp_dir(self, tmp_path):
        import glob

        from app.services.token_harvester import _copy_hermes_db

        missing_src = tmp_path / "does-not-exist.db"
        before = set(glob.glob(tempfile.gettempdir() + "/mc_hermes_harvest_*"))
        with pytest.raises(OSError):
            _copy_hermes_db(str(missing_src))
        after = set(glob.glob(tempfile.gettempdir() + "/mc_hermes_harvest_*"))
        assert after - before == set()


@pytest.mark.asyncio
class TestPerSourceIsolation:
    """Review fix (21.07.): claude/omp, grok, and hermes each run in their
    own try/except + commit their own work immediately — one source raising
    must not discard the other sources' already-processed events."""

    async def test_grok_source_failure_does_not_lose_other_sources_events(
        self, tmp_path, async_db_session, monkeypatch
    ):
        import app.services.token_harvester as th
        from app.services.token_harvester import run_harvest

        # Claude/omp fixture
        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)
        (rex_dir / "s.jsonl").write_text(
            _make_line(uuid_="isolation-claude-001") + "\n"
        )

        # Hermes fixture
        db_path = tmp_path / "state.db"
        _make_hermes_db(db_path)
        now = datetime.now(timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens) "
            "VALUES ('isolation-hermes-001', 'cli', 'Qwen/Qwen3.6-27B-FP8', ?, ?, 10)",
            (now - 10, now),
        )
        conn.commit()
        conn.close()

        # Force the grok source to blow up mid-run.
        async def _boom(*args, **kwargs):
            raise RuntimeError("simulated grok source failure")

        monkeypatch.setattr(th, "_process_grok_file", _boom)

        grok_log = tmp_path / "unified.jsonl"
        grok_log.write_text("")  # must exist so run_harvest even tries the source

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(grok_log),
            grok_sessions_path=str(tmp_path / "nonexistent_sessions"),
            hermes_state_db_path=str(db_path),
        )

        assert stats["source_errors"] == 1

        claude_event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "isolation-claude-001")
        )).one()
        assert claude_event is not None

        hermes_event = (await async_db_session.exec(
            select(ModelUsageEvent).where(
                ModelUsageEvent.message_uuid == "hermes:isolation-hermes-001"
            )
        )).one()
        assert hermes_event is not None

    async def test_claude_omp_source_failure_does_not_block_grok_and_hermes(
        self, tmp_path, async_db_session, monkeypatch
    ):
        """The failing source doesn't even have to be first — a failure in
        the FIRST guarded block must not prevent the LATER ones from running
        and committing at all."""
        import app.services.token_harvester as th
        from app.services.token_harvester import run_harvest

        async def _boom(*args, **kwargs):
            raise RuntimeError("simulated claude/omp source failure")

        monkeypatch.setattr(th, "_process_jsonl_file", _boom)

        agents_dir = tmp_path / "agents"
        rex_dir = agents_dir / "rex" / "claude-config" / "projects" / "p"
        rex_dir.mkdir(parents=True)
        (rex_dir / "s.jsonl").write_text(_make_line(uuid_="wont-be-inserted") + "\n")

        grok_log = tmp_path / "unified.jsonl"
        grok_log.write_text(_GROK_REAL_LINE + "\n")
        sessions_base = tmp_path / "sessions"
        cwd_dir = sessions_base / "%2FUsers%2Fop%2F.mc%2Fworkspaces%2Fgrok" / \
            "019f4dd6-6505-7510-b05c-b6dfc47a2c2d"
        cwd_dir.mkdir(parents=True)
        (cwd_dir / "summary.json").write_text(json.dumps(_GROK_REAL_SUMMARY))

        stats = await run_harvest(
            async_db_session,
            agent_base_paths=[str(agents_dir)],
            boss_base_paths=[],
            agent_slug_map={},
            grok_log_path=str(grok_log),
            grok_sessions_path=str(sessions_base),
            hermes_state_db_path=str(tmp_path / "nonexistent_state.db"),
        )

        assert stats["source_errors"] == 1
        grok_event = (await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.harness == "grok")
        )).one()
        assert grok_event is not None
        result = await async_db_session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.message_uuid == "wont-be-inserted")
        )
        assert result.all() == []  # the failing source's event never made it in
