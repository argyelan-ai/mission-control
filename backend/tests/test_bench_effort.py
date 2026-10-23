"""Bench effort — API path (W0.6) and agent path recorded (W0.7).

- ``services.reasoning_params``: one small mapper level -> request fields.
- ``bench_studio.effort_probe``: which key the served model honours, asked
  of a fake engine (httpx MockTransport), cached in ``bench_probes``.
- ``orchestrator``: the run sends the probed key, scales max_tokens, stores
  effort_requested / effort_sent / effort_observed / finish_reason; the old
  "qwen in the name" switch is gone.
- router: ``effort`` on BenchModelSpec, 422 for levels the probe did not
  find, for unknown levels and on agent rows.
- agent path: the omp task session's ``thinking_level_change`` is read and
  stored as ``effort_observed``.

Vertical test: skipped entirely when the vertical directory is stripped.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlmodel import select

from app.models.bench import BenchChallenge, BenchEntry, BenchProbe
from app.services import reasoning_params as rp
from app.verticals.bench_studio import agent_effort, effort_probe, orchestrator


# ── reasoning_params ──────────────────────────────────────────────────────


def test_default_sends_nothing():
    assert rp.request_fields(None, None) == {}
    assert rp.request_fields(rp.REASONING_EFFORT, "default") == {}
    assert rp.wire_fields({}) is None


@pytest.mark.parametrize(
    "key,level,fields",
    [
        (rp.REASONING_EFFORT, "off", {"reasoning_effort": "none"}),
        (rp.REASONING_EFFORT, "max", {"reasoning_effort": "max"}),
        (rp.REASONING_EFFORT, "low", {"reasoning_effort": "low"}),
        (rp.ENABLE_THINKING, "off", {"chat_template_kwargs": {"enable_thinking": False}}),
        (rp.ENABLE_THINKING, "on", {"chat_template_kwargs": {"enable_thinking": True}}),
        (rp.THINKING, "off", {"chat_template_kwargs": {"thinking": False}}),
    ],
)
def test_request_fields_per_key(key, level, fields):
    assert rp.request_fields(key, level) == fields


def test_wire_fields_names_the_dotted_key():
    assert rp.wire_fields({"reasoning_effort": "max"}) == {"key": "reasoning_effort", "value": "max"}
    assert rp.wire_fields({"chat_template_kwargs": {"enable_thinking": False}}) == {
        "key": "chat_template_kwargs.enable_thinking",
        "value": False,
    }


def test_unknown_level_or_missing_key_raises():
    with pytest.raises(rp.UnsupportedEffortError):
        rp.request_fields(rp.REASONING_EFFORT, "turbo")
    with pytest.raises(rp.UnsupportedEffortError):
        rp.request_fields(None, "max")


def test_max_tokens_scaled_for_deep_effort_and_capped_by_context():
    assert rp.max_tokens_for("default", 16384) == 16384
    assert rp.max_tokens_for("off", 16384) == 16384
    assert rp.max_tokens_for("high", 16384) == 32768
    assert rp.max_tokens_for("max", 16384) == 65536
    # A known context limit caps it (prompt head-room kept) — never below base.
    assert rp.max_tokens_for("max", 16384, context_limit=40960) == 36864
    assert rp.max_tokens_for("max", 16384, context_limit=8192) == 16384


def test_reasoning_of_reads_every_engine_shape():
    assert rp.reasoning_of({"content": "42", "reasoning_content": "17+25..."}, {})["thinking"] == "on"
    assert rp.reasoning_of({"content": "42", "reasoning": "hmm"}, {})["reasoning_chars"] == 3
    assert rp.reasoning_of({"content": "<think>a b</think>42"}, {})["thinking"] == "on"
    seen = rp.reasoning_of(
        {"content": "42"}, {"completion_tokens_details": {"reasoning_tokens": 120}}
    )
    assert seen == {"thinking": "on", "reasoning_tokens": 120, "reasoning_chars": 0}
    assert rp.reasoning_of({"content": "42"}, {})["thinking"] == "off"


# ── fake engines ──────────────────────────────────────────────────────────


def _engine(honours: str | None, *, default_on: bool = True, reject_none: bool = False,
            reject_max: bool = False, calls: list | None = None):
    """An OpenAI-compatible engine that thinks by default and switches only
    on ``honours`` (None = ignores every key)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body)
        effort = body.get("reasoning_effort")
        kwargs = body.get("chat_template_kwargs") or {}
        if reject_none and effort == "none":
            return httpx.Response(400, json={"error": "reasoning_effort must be low|medium|high"})
        if reject_max and effort == "max":
            return httpx.Response(400, json={"error": "bad reasoning_effort"})
        think = default_on
        if honours == rp.REASONING_EFFORT and effort is not None:
            think = effort != "none"
        if honours == rp.ENABLE_THINKING and "enable_thinking" in kwargs:
            think = bool(kwargs["enable_thinking"])
        if honours == rp.THINKING and "thinking" in kwargs:
            think = bool(kwargs["thinking"])
        message = {"content": "42", "reasoning_content": "17 plus 25 is 42." if think else None}
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30 if think else 2},
            },
        )

    return handler


_REAL_CLIENT = httpx.AsyncClient


def _route(monkeypatch, handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(effort_probe.httpx, "AsyncClient", factory)


# ── run_probe ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_finds_reasoning_effort_with_max(monkeypatch):
    calls: list = []
    _route(monkeypatch, _engine(rp.REASONING_EFFORT, calls=calls))
    result = await effort_probe.run_probe("http://engine/v1", "deepseek-x")
    assert result["status"] == "ok"
    assert result["thinking_key"] == rp.REASONING_EFFORT
    assert result["effort_param"] == rp.REASONING_EFFORT
    assert result["default_observed"] == "on"
    assert result["levels"] == ["off", "low", "medium", "high", "max"]
    # baseline, the off switch, the max check — no more
    assert len(calls) == 3
    assert all(c["max_tokens"] == effort_probe.PROBE_MAX_TOKENS for c in calls)


@pytest.mark.asyncio
async def test_probe_drops_max_when_engine_rejects_it(monkeypatch):
    _route(monkeypatch, _engine(rp.REASONING_EFFORT, reject_max=True))
    result = await effort_probe.run_probe("http://engine/v1", "m")
    assert result["levels"] == ["off", "low", "medium", "high"]


@pytest.mark.asyncio
async def test_probe_finds_enable_thinking_switch_only(monkeypatch):
    _route(monkeypatch, _engine(rp.ENABLE_THINKING))
    result = await effort_probe.run_probe("http://engine/v1", "qwen-x")
    assert result["status"] == "ok"
    assert result["thinking_key"] == rp.ENABLE_THINKING
    assert result["effort_param"] is None
    assert result["levels"] == ["off"]
    tried = [a.get("key") for a in result["raw"]["attempts"]]
    assert tried == [None, rp.REASONING_EFFORT, rp.ENABLE_THINKING]


@pytest.mark.asyncio
async def test_probe_finds_thinking_key_after_a_rejected_one(monkeypatch):
    _route(monkeypatch, _engine(rp.THINKING, reject_none=True))
    result = await effort_probe.run_probe("http://engine/v1", "dspark-x")
    assert result["thinking_key"] == rp.THINKING
    assert result["raw"]["attempts"][1]["http"] == 400


@pytest.mark.asyncio
async def test_probe_default_off_engine_offers_on(monkeypatch):
    _route(monkeypatch, _engine(rp.ENABLE_THINKING, default_on=False))
    result = await effort_probe.run_probe("http://engine/v1", "m")
    assert result["default_observed"] == "off"
    assert result["levels"] == ["on"]


@pytest.mark.asyncio
async def test_probe_reports_no_effect_when_every_key_is_ignored(monkeypatch):
    """Sabotage acceptance: a key that does nothing is found out — no level
    besides the default is offered."""
    _route(monkeypatch, _engine(None))
    result = await effort_probe.run_probe("http://engine/v1", "m")
    assert result["status"] == "no_effect"
    assert result["thinking_key"] is None
    assert result["levels"] == []


@pytest.mark.asyncio
async def test_probe_dead_engine_is_failed_not_raised(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    _route(monkeypatch, handler)
    result = await effort_probe.run_probe("http://engine/v1", "m")
    assert result["status"] == "failed"
    assert result["default_observed"] == "unknown"


# ── get_or_probe cache ────────────────────────────────────────────────────


@pytest.fixture
def spark_runtime(monkeypatch):
    rt = SimpleNamespace(
        id=uuid.uuid4(),
        serving_since=datetime(2026, 10, 21, 20, 12, tzinfo=timezone.utc),
        max_context_len=None,
    )
    monkeypatch.setattr(effort_probe, "spark_runtime", AsyncMock(return_value=rt))
    return rt


@pytest.mark.asyncio
async def test_probe_is_cached_per_serving_since(session, monkeypatch, spark_runtime):
    calls: list = []
    _route(monkeypatch, _engine(rp.REASONING_EFFORT, calls=calls))
    first = await effort_probe.get_or_probe(session, "deepseek-x")
    second = await effort_probe.get_or_probe(session, "deepseek-x")
    assert first.id == second.id
    assert len(calls) == 3  # probed once

    # Engine restarted: new serving_since -> a fresh probe, a second row.
    spark_runtime.serving_since = spark_runtime.serving_since + timedelta(hours=2)
    third = await effort_probe.get_or_probe(session, "deepseek-x")
    assert third.id != first.id
    assert len(calls) == 6
    rows = (await session.exec(select(BenchProbe))).all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_failed_probe_is_retried(session, monkeypatch, spark_runtime):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    _route(monkeypatch, down)
    row = await effort_probe.get_or_probe(session, "m")
    assert row.status == "failed"
    _route(monkeypatch, _engine(rp.REASONING_EFFORT))
    row = await effort_probe.get_or_probe(session, "m")
    assert row.status == "ok"


def test_level_fields_refuses_unprobed_levels():
    row = BenchProbe(served_model_id="m", status="ok", thinking_key=rp.ENABLE_THINKING, levels=["off"])
    assert effort_probe.level_fields(row, "off") == {"chat_template_kwargs": {"enable_thinking": False}}
    assert effort_probe.level_fields(row, "default") == {}
    with pytest.raises(rp.UnsupportedEffortError):
        effort_probe.level_fields(row, "max")


# ── _spark_generate: the payload carries the probed key, no name hack ─────


def _capture_generate(monkeypatch, model: str, response: dict):
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return response

    class _Client:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            captured["json"] = json
            return _Resp()

    class _Spark:
        llm_url = "http://spark:8000/v1"

        def __init__(self, timeout=None):
            self.timeout = timeout

        async def _resolve_llm_model(self):
            return model

    monkeypatch.setattr("app.services.spark_client.SparkClient", _Spark)
    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", _Client)
    return captured


_HTML_RESPONSE = {
    "model": "Qwen/Qwen3.6-35B-A3B-FP8",
    "choices": [
        {
            "message": {"content": "<html></html>", "reasoning_content": "plan the page"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 900,
        "completion_tokens_details": {"reasoning_tokens": 700},
    },
}


@pytest.mark.asyncio
async def test_spark_generate_no_longer_switches_qwen_thinking_off(monkeypatch):
    captured = _capture_generate(monkeypatch, "Qwen/Qwen3.6-35B-A3B-FP8", _HTML_RESPONSE)
    content, metrics = await orchestrator._spark_generate("p", None)
    assert content == "<html></html>"
    assert "chat_template_kwargs" not in captured["json"]
    assert "reasoning_effort" not in captured["json"]
    assert metrics["effort_sent"] is None
    # The server default is printed as what it was: thinking on.
    assert metrics["effort_observed"]["thinking"] == "on"
    assert metrics["effort_observed"]["reasoning_tokens"] == 700
    assert metrics["finish_reason"] == "stop"
    assert metrics["truncated"] is False
    assert metrics["max_tokens"] == orchestrator.SPARK_MAX_TOKENS
    assert metrics["temperature"] == 0.7


@pytest.mark.asyncio
async def test_spark_generate_sends_effort_fields_and_scaled_budget(monkeypatch):
    captured = _capture_generate(monkeypatch, "deepseek-x", _HTML_RESPONSE)
    _, metrics = await orchestrator._spark_generate(
        "p", "deepseek-x", effort_fields={"reasoning_effort": "max"}, max_tokens=65536
    )
    assert captured["json"]["reasoning_effort"] == "max"
    assert captured["json"]["max_tokens"] == 65536
    assert captured["timeout"] == orchestrator.SPARK_TIMEOUT_S * 4
    assert metrics["effort_sent"] == {"key": "reasoning_effort", "value": "max"}
    assert metrics["max_tokens"] == 65536


# ── generate_spark_entry: requested effort -> request + stored record ─────


async def _spark_entry(session, effort: str):
    ch = BenchChallenge(title="T", prompt_text="make a page")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entry = BenchEntry(
        challenge_id=ch.id, model_label="DeepSeek", source_kind="spark",
        spark_model="deepseek-x", metrics={"effort_requested": effort},
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return ch, entry


def _probe_row(levels, key=rp.REASONING_EFFORT, status="ok"):
    return BenchProbe(
        served_model_id="deepseek-x", status=status, thinking_key=key,
        effort_param=key if key == rp.REASONING_EFFORT else None, levels=levels,
        default_observed="on",
    )


@pytest.mark.asyncio
async def test_generate_spark_entry_sends_probed_key_and_records_it(session, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off", "max"])))
    monkeypatch.setattr(effort_probe, "spark_runtime", AsyncMock(return_value=None))
    gen = AsyncMock(return_value=("<html>x</html>", {
        "duration_ms": 10, "effort_sent": {"key": "reasoning_effort", "value": "max"},
        "effort_observed": {"thinking": "on", "reasoning_tokens": 5000, "reasoning_chars": 9},
        "finish_reason": "stop", "max_tokens": 65536,
    }))
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, entry = await _spark_entry(session, "max")

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "generated"
    gen.assert_awaited_once_with(
        "make a page", "deepseek-x", effort_fields={"reasoning_effort": "max"}, max_tokens=65536
    )
    assert entry.metrics["effort_requested"] == "max"
    assert entry.metrics["thinking_key"] == "reasoning_effort"
    assert entry.metrics["effort_sent"] == {"key": "reasoning_effort", "value": "max"}
    assert entry.metrics["effort_observed"]["reasoning_tokens"] == 5000


@pytest.mark.asyncio
async def test_generate_spark_entry_default_does_not_probe(session, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    probe = AsyncMock()
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    gen = AsyncMock(return_value=("<html>x</html>", {"duration_ms": 10}))
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, entry = await _spark_entry(session, "default")

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)

    probe.assert_not_awaited()
    gen.assert_awaited_once_with(
        "make a page", "deepseek-x", effort_fields={}, max_tokens=orchestrator.SPARK_MAX_TOKENS
    )


@pytest.mark.asyncio
async def test_truncated_run_keeps_its_record_and_says_why(session, tmp_path, monkeypatch):
    """Thinking ate the whole budget: no HTML, but finish_reason and the
    effort record stay on the entry and the error names max_tokens."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    gen = AsyncMock(return_value=("", {
        "duration_ms": 10, "finish_reason": "length", "truncated": True, "max_tokens": 16384,
        "effort_observed": {"thinking": "on", "reasoning_tokens": 16384, "reasoning_chars": 1},
    }))
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, entry = await _spark_entry(session, "default")

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "failed"
    assert "max_tokens (16384)" in entry.error
    assert entry.metrics["finish_reason"] == "length"
    assert entry.metrics["effort_observed"]["reasoning_tokens"] == 16384


@pytest.mark.asyncio
async def test_level_gone_after_restart_fails_the_entry(session, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off"], key=rp.ENABLE_THINKING)))
    gen = AsyncMock()
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, entry = await _spark_entry(session, "max")

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    assert entry.status == "failed"
    assert "'max' is not available" in entry.error
    gen.assert_not_awaited()


# ── router ────────────────────────────────────────────────────────────────


@pytest.fixture
def _no_background(monkeypatch):
    monkeypatch.setattr(orchestrator, "start_challenge", AsyncMock())


def _body(*, effort=None, source="spark"):
    spec = {"label": "DeepSeek", "source_kind": source}
    if source == "spark":
        spec["spark_model"] = "deepseek-x"
    else:
        spec["agent_id"] = str(uuid.uuid4())
    if effort is not None:
        spec["effort"] = effort
    return {"title": "Effort", "prompt_text": "one page", "models": [spec]}


@pytest.mark.asyncio
async def test_create_with_probed_effort_stores_it(auth_client, session, monkeypatch, _no_background):
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off", "low", "medium", "high", "max"])))
    resp = await auth_client.post("/api/v1/bench/challenges", json=_body(effort="max"))
    assert resp.status_code == 201, resp.text
    entry = resp.json()["entries"][0]
    assert entry["metrics"] == {"effort_requested": "max"}


@pytest.mark.asyncio
async def test_create_without_effort_stores_default_and_never_probes(auth_client, monkeypatch, _no_background):
    probe = AsyncMock()
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    resp = await auth_client.post("/api/v1/bench/challenges", json=_body())
    assert resp.status_code == 201, resp.text
    assert resp.json()["entries"][0]["metrics"] == {"effort_requested": "default"}
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_unknown_level_422(auth_client, _no_background):
    resp = await auth_client.post("/api/v1/bench/challenges", json=_body(effort="turbo"))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_unprobed_level_422(auth_client, monkeypatch, _no_background):
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row(["off"], key=rp.ENABLE_THINKING)))
    resp = await auth_client.post("/api/v1/bench/challenges", json=_body(effort="max"))
    assert resp.status_code == 422
    assert "not available" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_create_effort_on_no_effect_probe_422(auth_client, monkeypatch, _no_background):
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=_probe_row([], key=None, status="no_effect")))
    resp = await auth_client.post("/api/v1/bench/challenges", json=_body(effort="off"))
    assert resp.status_code == 422
    assert "no_effect" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_create_agent_row_with_effort_422(auth_client, _no_background):
    resp = await auth_client.post("/api/v1/bench/challenges", json=_body(effort="high", source="agent"))
    assert resp.status_code == 422
    assert "agent path" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_effort_levels_endpoint(auth_client, monkeypatch):
    """GET reads the stored probe only (round-1 P3: it used to probe)."""
    row = _probe_row(["off", "low", "medium", "high", "max"])
    row.probed_at = datetime(2026, 10, 21, 22, 14, tzinfo=timezone.utc)
    stored = AsyncMock(return_value=row)
    probe = AsyncMock()
    monkeypatch.setattr(effort_probe, "stored_probe", stored)
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    resp = await auth_client.get("/api/v1/bench/effort-levels?model=deepseek-x")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["levels"] == ["off", "low", "medium", "high", "max"]
    assert data["default_observed"] == "on"
    assert "raw" not in data
    stored.assert_awaited_once()
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_effort_levels_endpoint_unreachable_box(auth_client, monkeypatch):
    monkeypatch.setattr(
        orchestrator, "spark_models_status",
        AsyncMock(return_value={"reachable": False, "models": [], "active": None}),
    )
    resp = await auth_client.get("/api/v1/bench/effort-levels")
    assert resp.status_code == 200
    assert resp.json()["reachable"] is False
    assert resp.json()["levels"] == []


# ── agent path: effort recorded from the omp task session (W0.7) ─────────


def _write_task_session(root, task_id: str, *, level="high", configured=None, extra_levels=()):
    """The two files one ACP task attempt leaves (shape of omp 18 / the omp
    bridge): MC's display copy in ``.mc/`` carries the taskId header; omp's
    own file carries the thinking level line."""
    sid = str(uuid.uuid4())
    folder = root / "--workspace--"
    (folder / ".mc").mkdir(parents=True, exist_ok=True)
    bridge = folder / ".mc" / f"2026-10-21T20-21-42_{sid}.jsonl"
    bridge.write_text(
        json.dumps({"type": "session", "version": 3, "id": sid, "timestamp": "2026-10-21T20:21:42.818Z",
                    "cwd": "", "bridge": "acp", "taskId": task_id}) + "\n"
        + json.dumps({"type": "message", "id": "acp00001", "message": {"role": "user", "content": [
            {"type": "text", "text": "One-shot benchmark task (Benchmark Studio)."}]}}) + "\n"
    )
    lines = [
        {"type": "session", "version": 3, "id": sid, "timestamp": "2026-10-21T20:21:42.765Z", "cwd": "/workspace"},
        {"type": "model_change", "id": "3cc24a0f", "model": "mc-openai/some-model"},
        {"type": "thinking_level_change", "id": "7fb41b90", "thinkingLevel": level, "configured": configured},
    ]
    for lv in extra_levels:
        lines.append({"type": "thinking_level_change", "id": uuid.uuid4().hex[:8], "thinkingLevel": lv, "configured": lv})
    own = folder / f"2026-10-21T20-21-42-765Z_{sid}.jsonl"
    own.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return bridge, own


def _omp_agent(slug="worker"):
    return SimpleNamespace(slug=slug, agent_runtime="cli-bridge", harness="omp")


@pytest.fixture
def omp_home(tmp_path, monkeypatch):
    from app.services import omp_chat

    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    root = tmp_path / ".mc" / "agents" / "worker" / "omp-sessions"
    root.mkdir(parents=True)
    return root


def test_reads_session_default_from_omp_file(omp_home):
    task_id = str(uuid.uuid4())
    _write_task_session(omp_home, task_id)
    seen = agent_effort.omp_task_effort(_omp_agent(), task_id)
    assert seen == {"source": "omp_session", "level": "high", "configured": None,
                    "changed": False, "attempts": 1}


def test_reads_a_changed_level(omp_home):
    task_id = str(uuid.uuid4())
    _write_task_session(omp_home, task_id, extra_levels=("low",))
    seen = agent_effort.omp_task_effort(_omp_agent(), task_id)
    assert seen["level"] == "low"
    assert seen["changed"] is True


def test_foreign_task_session_is_not_read(omp_home):
    _write_task_session(omp_home, str(uuid.uuid4()), level="xhigh")
    seen = agent_effort.omp_task_effort(_omp_agent(), str(uuid.uuid4()))
    assert seen["source"] == "unknown"


def test_no_transcript_folder_is_unknown(tmp_path):
    seen = agent_effort.omp_task_effort(SimpleNamespace(slug=None), str(uuid.uuid4()))
    assert seen == {"source": "unknown", "reason": "no omp transcript folder for this agent"}


@pytest.mark.asyncio
async def test_on_task_done_records_agent_effort(
    session, tmp_path, monkeypatch, omp_home, make_board, make_task, make_agent
):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(name="Worker", slug="worker", harness="omp", board_id=board.id)
    task = await make_task(board.id, title="[Bench] x", status="done")
    _write_task_session(omp_home, str(task.id))
    ch = BenchChallenge(title="T", prompt_text="p")
    session.add(ch)
    await session.commit()
    entry = BenchEntry(challenge_id=ch.id, model_label="Worker", source_kind="agent",
                       agent_id=agent.id, task_id=task.id, status="generating")
    session.add(entry)
    await session.commit()

    await orchestrator.on_task_done(session, task)
    await session.refresh(entry)

    assert entry.metrics["effort_requested"] == "default"
    assert entry.metrics["effort_observed"]["source"] == "omp_session"
    assert entry.metrics["effort_observed"]["level"] == "high"
    assert entry.metrics["effort_observed"]["configured"] is None


@pytest.mark.asyncio
async def test_non_omp_agent_effort_is_unknown_with_reason(session, make_agent):
    agent = await make_agent(name="Claude", slug="claude-x", harness="claude")
    entry = SimpleNamespace(agent_id=agent.id)
    task = SimpleNamespace(id=uuid.uuid4(), created_at=None)
    seen = await agent_effort.observed_agent_effort(session, entry, task)
    assert seen["source"] == "unknown"
    assert "claude" in seen["reason"]


# ── review follow-ups (#946) ──────────────────────────────────────────────


def test_reasoning_of_counts_text_before_a_lone_think_close():
    """No reasoning parser + a template that opens <think> in the PROMPT:
    the answer is ``…</think>42`` — that is thinking, not "off"."""
    seen = rp.reasoning_of({"content": "17 plus 25 is 42.\n</think>\n\n42"}, {})
    assert seen["thinking"] == "on"
    assert seen["reasoning_chars"] == len("17 plus 25 is 42.")
    # An empty think block closed in the answer is still "off".
    assert rp.reasoning_of({"content": "\n</think>\n\n42"}, {})["thinking"] == "off"
    # A full block keeps working; text after </think> is not counted.
    assert rp.reasoning_of({"content": "<think>ab</think>42"}, {})["reasoning_chars"] == 2


@pytest.mark.asyncio
async def test_probe_sees_default_thinking_without_a_reasoning_parser(monkeypatch):
    """Engine whose default answer is ``…</think>42`` and that honours
    enable_thinking: the probe must report default_observed "on" and find
    the switch — not "server does not think"."""

    def handler(request):
        body = json.loads(request.content)
        off = (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False
        content = "42" if off else "Add them: 42.\n</think>\n\n42"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {},
        })

    _route(monkeypatch, handler)
    result = await effort_probe.run_probe("http://engine/v1", "qwen-x")
    assert result["default_observed"] == "on"
    assert result["status"] == "ok"
    assert result["thinking_key"] == rp.ENABLE_THINKING
    assert result["levels"] == ["off"]


@pytest.mark.asyncio
async def test_default_on_a_thinking_server_gets_the_on_budget_without_probing(
    session, tmp_path, monkeypatch, spark_runtime
):
    """'default' on a model that thinks by default behaves like 'on' — it
    gets the same max_tokens (x2), read from the STORED probe; no request."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    row = _probe_row(["off"], key=rp.ENABLE_THINKING)
    row.runtime_id = spark_runtime.id
    row.serving_since = spark_runtime.serving_since
    session.add(row)
    await session.commit()
    probe = AsyncMock()
    monkeypatch.setattr(effort_probe, "get_or_probe", probe)
    gen = AsyncMock(return_value=("<html>x</html>", {"duration_ms": 10}))
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, entry = await _spark_entry(session, "default")

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)
    await session.refresh(entry)

    probe.assert_not_awaited()
    gen.assert_awaited_once_with(
        "make a page", "deepseek-x", effort_fields={}, max_tokens=orchestrator.SPARK_MAX_TOKENS * 2
    )
    assert entry.metrics["effort_requested"] == "default"
    assert entry.metrics["default_observed"] == "on"


@pytest.mark.asyncio
async def test_default_keeps_the_base_budget_when_the_server_does_not_think(
    session, tmp_path, monkeypatch, spark_runtime
):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    row = _probe_row(["on"], key=rp.ENABLE_THINKING)
    row.default_observed = "off"
    row.runtime_id = spark_runtime.id
    row.serving_since = spark_runtime.serving_since
    session.add(row)
    await session.commit()
    gen = AsyncMock(return_value=("<html>x</html>", {"duration_ms": 10}))
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, entry = await _spark_entry(session, "default")

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)

    assert gen.await_args.kwargs["max_tokens"] == orchestrator.SPARK_MAX_TOKENS


@pytest.mark.asyncio
async def test_default_ignores_a_probe_of_an_older_engine_run(
    session, tmp_path, monkeypatch, spark_runtime
):
    """The stored probe belongs to the engine run it was made on: after a
    restart (new serving_since) it no longer scales 'default'."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    row = _probe_row(["off"], key=rp.ENABLE_THINKING)
    row.runtime_id = spark_runtime.id
    row.serving_since = spark_runtime.serving_since - timedelta(hours=3)
    session.add(row)
    await session.commit()
    gen = AsyncMock(return_value=("<html>x</html>", {"duration_ms": 10}))
    monkeypatch.setattr(orchestrator, "_spark_generate", gen)
    ch, entry = await _spark_entry(session, "default")

    await orchestrator.generate_spark_entry(session, entry, ch.prompt_text)

    assert gen.await_args.kwargs["max_tokens"] == orchestrator.SPARK_MAX_TOKENS


@pytest.mark.asyncio
async def test_spark_generate_logs_the_effort_sent(monkeypatch, caplog):
    _capture_generate(monkeypatch, "deepseek-x", _HTML_RESPONSE)
    with caplog.at_level("INFO", logger="mc.bench_studio"):
        await orchestrator._spark_generate(
            "p", None, effort_fields={"reasoning_effort": "max"}, max_tokens=65536
        )
    line = next(r.getMessage() for r in caplog.records if "bench spark request" in r.getMessage())
    assert "effort_sent={'key': 'reasoning_effort', 'value': 'max'}" in line
    assert "max_tokens=65536" in line


@pytest.mark.asyncio
async def test_effort_levels_endpoint_box_offline_for_a_named_model(auth_client, monkeypatch):
    """Named model, box down: the probe's first request got no HTTP answer
    -> reachable false ("Box offline"), not "Check failed"."""
    row = BenchProbe(
        served_model_id="deepseek-x", status="failed", levels=[], default_observed="unknown",
        raw={"attempts": [{"key": None, "value": None, "http": None, "error": "ConnectError"}]},
    )
    monkeypatch.setattr(effort_probe, "get_or_probe", AsyncMock(return_value=row))
    monkeypatch.setattr(effort_probe, "spark_busy_reason", AsyncMock(return_value=None))
    check = {"model": "deepseek-x"}
    resp = await auth_client.post("/api/v1/bench/effort-levels/check", json=check)
    assert resp.json()["reachable"] is False
    assert resp.json()["status"] == "failed"

    # The box answered, just not with 200 -> reached, the check failed.
    row.raw = {"attempts": [{"key": None, "value": None, "http": 500, "error": "boom"}]}
    resp = await auth_client.post("/api/v1/bench/effort-levels/check", json=check)
    assert resp.json()["reachable"] is True

    # A stored failed probe: the read-only GET says the same (no new probe).
    monkeypatch.setattr(effort_probe, "stored_probe", AsyncMock(return_value=row))
    resp = await auth_client.get("/api/v1/bench/effort-levels?model=deepseek-x")
    assert resp.json()["status"] == "failed"
    assert resp.json()["reachable"] is True


def test_level_fields_no_effect_is_not_offered_failed_is_check_failed():
    """Round-8 review MUST-1: only a failed probe raises the probe-failed
    error; a ``no_effect`` probe raises the plain not-offered one."""
    with pytest.raises(rp.UnsupportedEffortError) as no_eff:
        effort_probe.level_fields(_probe_row([], key=None, status="no_effect"), "max")
    assert not isinstance(no_eff.value, effort_probe.EffortProbeFailedError)
    with pytest.raises(effort_probe.EffortProbeFailedError):
        effort_probe.level_fields(_probe_row([], key=None, status="failed"), "max")
