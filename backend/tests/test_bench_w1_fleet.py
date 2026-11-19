"""Shared W1 test fleet (concept 30 W1.2 acceptance data) + finished-test
builders. Imported by the other test_bench_w1_* modules; the two checks at
the bottom keep the fleet itself honest."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.models.agent import Agent  # noqa: E402
from app.models.board import Board  # noqa: E402
from app.models.host import Host  # noqa: E402
from app.models.model_usage import ModelPrice  # noqa: E402
from app.models.runtime import Runtime  # noqa: E402
from app.models.runtime_host import RuntimeHost  # noqa: E402
from app.verticals.bench_studio import w1_plan  # noqa: E402

DS = "DeepSeek-V4.1-Flash-1017"  # model-catalog: allow (test data, never a request)
GLM = "GLM-5.3-Flash"  # model-catalog: allow (test data)
CLOUD_DS = "deepseek-v4.1-flash"  # model-catalog: allow (test data)
PAID = "example-frontier-1"
SPARK_EP = "http://192.0.2.10:8000/v1"
WOL_EP = "http://192.0.2.20:8000/v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def seed_fleet(session, *, serving_age_min: int = 120, agent_model: str | None = "Qwen-3.5") -> dict:
    spark1 = Host(slug="spark-a", display_name="DGX Spark 1", kind="ssh", ssh_host="203.0.113.54",
                  agent_device_state={"gpu_mode": "eco"})
    spark2 = Host(slug="spark-b", display_name="DGX Spark 2", kind="ssh", ssh_host="203.0.113.55")
    wol_box = Host(slug="wol-box", display_name="RTX 5090", kind="flask_wol", power_managed=True)
    session.add_all([spark1, spark2, wol_box])
    await session.flush()
    since = _now() - timedelta(minutes=serving_age_min)
    slot = Runtime(slug="dgx-spark-slot", display_name="Spark slot", runtime_type="openai_compatible",
                   endpoint=SPARK_EP, model_identifier=DS, host_id=spark1.id, is_slot=True,
                   serving_since=since, ui_order=1, max_context_len=131072)
    glm = Runtime(slug="glm53-recipe", display_name="GLM recipe", runtime_type="vllm_docker",
                  endpoint=SPARK_EP, model_identifier=GLM, host_id=spark1.id,
                  serving_since=since, ui_order=2)
    wol_rt = Runtime(slug="wol-slot", display_name="WoL slot", runtime_type="openai_compatible",
                         endpoint=WOL_EP, model_identifier="qwen3-coder", host_id=wol_box.id,
                         is_slot=True, ui_order=3)
    ollama = Runtime(slug="ollama-ds", display_name="Ollama DS", runtime_type="openai_compatible",
                     endpoint="https://ollama.com/v1", model_identifier=CLOUD_DS,
                     api_key_secret_id=uuid.uuid4(), ui_order=10)
    paid = Runtime(slug="paid-api", display_name="Paid", runtime_type="openai_compatible",
                   endpoint="https://api.example-llm.dev/v1", model_identifier=PAID,
                   api_key_secret_id=uuid.uuid4(), ui_order=11, role_tags=["bench:api_price"])
    session.add_all([slot, glm, wol_rt, ollama, paid])
    await session.flush()
    session.add(RuntimeHost(runtime_id=slot.id, host_id=spark2.id, role="worker", node_rank=1))
    board = Board(name="B", slug="b")
    session.add(board)
    await session.flush()
    omp_agent = Agent(name="Alpha", board_id=board.id, harness="omp", runtime_id=slot.id, model=agent_model)
    session.add(omp_agent)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all([
        ModelPrice(model_pattern="*", input_per_mtok=0, output_per_mtok=0, valid_from=t0, priority=0),
        ModelPrice(model_pattern="deepseek*", input_per_mtok=0.27, output_per_mtok=1.10, valid_from=t0,
                   priority=10),
        ModelPrice(model_pattern="DeepSeek*", input_per_mtok=0.27, output_per_mtok=1.10, valid_from=t0,
                   priority=10),
        ModelPrice(model_pattern="example-frontier*", input_per_mtok=15, output_per_mtok=75,
                   valid_from=t0, priority=10),
    ])
    await session.commit()
    return {"slot": slot, "glm": glm, "wol": wol_rt, "ollama": ollama, "paid": paid,
            "omp_agent": omp_agent, "spark1": spark1, "board": board}


@pytest.fixture
def engines(monkeypatch):  # noqa: F811
    """Spark serves DeepSeek only; the WoL box does not answer."""
    state = {"served": {SPARK_EP: frozenset({DS})}, "in_use": {SPARK_EP: False}}

    async def served(ep):
        return state["served"].get(ep)

    async def in_use(ep):
        return state["in_use"].get(ep)

    monkeypatch.setattr(w1_plan, "_served", served)
    monkeypatch.setattr(w1_plan, "_in_use", in_use)
    return state





# ── A fake world for the runner: engine (SSE), renderer, recorder ────────

import json  # noqa: E402

import httpx  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402

from tests.conftest import test_engine  # noqa: E402

PAGE = "<!doctype html><html><body><canvas></canvas><script>/* pagoda */</script></body></html>"


def factory() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


class FakeClock:
    """time.monotonic stand-in: every call is 50 ms later."""

    def __init__(self, step: float = 0.05):
        self.t = 1000.0
        self.step = step

    def monotonic(self) -> float:
        self.t += self.step
        return self.t


class World:
    """Records every request; answers like an OpenAI-compatible engine
    (streaming with include_usage, plus the plain probe requests) and like
    the mc-playwright renderer (/card, /compose)."""

    def __init__(self, *, tokens_out=(6607, 6400, 6900), finish=("stop", "stop", "stop"), usage=True,
                 reasoning_tokens=2140, think_by_default=True, sse=True, fail_runs=0):
        self.tokens_out = list(tokens_out)
        self.finish = list(finish)
        self.usage = usage
        self.reasoning_tokens = reasoning_tokens
        self.think = think_by_default
        self.sse = sse
        self.fail_runs = fail_runs
        self.chat: list[dict] = []
        self.probes: list[dict] = []
        self.renderer: list[tuple[str, dict]] = []
        self.records: list[str] = []
        self.order: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content or b"{}")
        if url.endswith("/card") or url.endswith("/compose"):
            path = url.rsplit("/", 1)[1]
            self.renderer.append((path, body))
            out = body["output_path"]
            import pathlib

            pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(out).write_bytes(b"\x89PNG fake" if path == "card" else b"fake mp4")
            return httpx.Response(200, json={"output_path": out, "bytes": 8, "inputs": len(body.get("inputs", []))})
        if not body.get("stream"):
            self.probes.append(body)
            effort = body.get("reasoning_effort")
            think = self.think if effort is None else effort != "none"
            msg = {"content": "42", "reasoning_content": "17+25=42" if think else None}
            return httpx.Response(200, json={"model": body["model"], "choices": [{"message": msg, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 20, "completion_tokens": 30 if think else 2,
                                                       "completion_tokens_details": {"reasoning_tokens": 28 if think else 0}}})
        self.chat.append(body)
        self.order.append("A")
        i = len(self.chat) - 1
        if i < self.fail_runs:
            return httpx.Response(500, json={"error": "engine hiccup"})
        n = self.tokens_out[min(i, len(self.tokens_out) - 1)] if self.tokens_out else 0
        finish = self.finish[min(i, len(self.finish) - 1)]
        if not self.sse:
            return httpx.Response(200, json={
                "model": body["model"], "choices": [{"message": {"content": PAGE}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 300, "completion_tokens": n}})
        lines = []
        lines.append({"model": body["model"], "choices": [{"delta": {"reasoning_content": "plan the pagoda"}}]})
        for part in (PAGE[:30], PAGE[30:60], PAGE[60:]):
            lines.append({"model": body["model"], "choices": [{"delta": {"content": part}}]})
        lines.append({"model": body["model"], "choices": [{"delta": {}, "finish_reason": finish}]})
        if self.usage:
            lines.append({"model": body["model"], "choices": [], "usage": {
                "prompt_tokens": 300, "completion_tokens": n,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens}}})
        text = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Engines + renderer + recorder faked; artefacts under tmp_path."""
    from app.verticals.bench_studio import orchestrator as orch
    from app.verticals.bench_studio import w1_runner

    w = World()
    monkeypatch.setattr(w1_runner, "_transport", httpx.MockTransport(lambda r: w.handler(r)))
    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path)

    _real_client = httpx.AsyncClient

    def client(*a, **kw):  # the effort probe uses its own client
        kw["transport"] = httpx.MockTransport(lambda r: w.handler(r))
        return _real_client(*a, **kw)

    from app.verticals.bench_studio import effort_probe

    monkeypatch.setattr(effort_probe.httpx, "AsyncClient", client)

    async def features():
        return {"branding_effort", "bench_card_v1", "seeded_random", "page_check"}

    monkeypatch.setattr(orch, "renderer_features", features)

    async def record(entry, challenge):
        w.records.append(str(entry.id))
        out = orch.entry_dir(entry)
        out.mkdir(parents=True, exist_ok=True)
        (out / "recording.mp4").write_bytes(b"rec")
        return {"video_path": str(out / "recording.mp4"), "screenshot_path": "", "network_blocked": True,
                "network_attempts": 0, "libraries_inlined": [], "html_bytes": len(PAGE), "page_blank": False}

    monkeypatch.setattr(orch, "record_entry", record)
    clock = FakeClock()
    monkeypatch.setattr(w1_runner, "_clock", clock.monotonic)
    w.tmp = tmp_path
    return w


async def no_sleep(_s):
    return None

@pytest.mark.asyncio
async def test_fleet_seed_has_the_live_shape(session):
    fleet = await seed_fleet(session)
    assert fleet["slot"].is_slot and fleet["glm"].endpoint == fleet["slot"].endpoint


def test_fleet_names_are_documentation_addresses():
    assert SPARK_EP.startswith("http://192.0.2.") and WOL_EP.startswith("http://192.0.2.")
