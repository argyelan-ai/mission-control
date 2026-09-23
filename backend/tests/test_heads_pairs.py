"""A9 — pairs: local first, never a cloud fallback, crosswise on one engine."""
from __future__ import annotations

import pytest

from app.models.host import Host
from app.models.runtime import Runtime
from app.services import runtime_protocols as rp
from app.services.heads import engine, pairs

EP = "http://192.0.2.10:8000/v1"


@pytest.fixture(autouse=True)
def _clean():
    rp.clear_cache()
    engine.clear_cache()
    yield
    rp.clear_cache()
    engine.clear_cache()


@pytest.fixture
def probes(monkeypatch):
    state = {"served": {EP: frozenset({"glm"})}, "anthropic": True, "running": None}

    async def served(endpoint, now=None):
        return state["served"].get(endpoint)

    async def running(endpoint):
        return state["running"]

    async def anth(endpoint):
        return state["anthropic"]

    monkeypatch.setattr(engine, "served_models", served)
    monkeypatch.setattr(engine, "running_requests", running)
    monkeypatch.setattr(rp, "probe_anthropic_route", anth)
    return state


async def _seed(session):
    box = Host(slug="box-a", display_name="BOX-A", kind="ssh", ssh_host="192.0.2.10")
    session.add(box)
    await session.commit()
    await session.refresh(box)
    rows = [
        Runtime(slug="box-a-slot", display_name="Local slot", runtime_type="openai_compatible",
                endpoint=EP, model_identifier="glm", host_id=box.id, is_slot=True, ui_order=1),
        Runtime(slug="glm-recipe", display_name="GLM recipe", runtime_type="vllm_docker",
                endpoint=EP, model_identifier="glm", host_id=box.id, ui_order=5),
        Runtime(slug="qwen-recipe", display_name="Qwen recipe", runtime_type="vllm_docker",
                endpoint=EP, model_identifier="qwen", host_id=box.id, ui_order=6),
        Runtime(slug="ollama-cloud", display_name="Ollama cloud", runtime_type="cloud",
                endpoint="https://ollama.invalid/v1", model_identifier="glm-5", ui_order=2),
        Runtime(slug="anthropic-claude-opus", display_name="Claude", runtime_type="cloud",
                endpoint="https://api.anthropic.com/v1/messages", model_identifier="claude-opus", ui_order=0),
        Runtime(slug="kimi-cloud", display_name="Kimi", runtime_type="kimi",
                endpoint="https://kimi.invalid", model_identifier="k3"),
    ]
    for r in rows:
        session.add(r)
    await session.commit()
    return box


def _find(listing, harness, slug):
    return next(p for p in listing["pairs"] if p["harness"] == harness and p["runtime_slug"] == slug)


async def test_two_harnesses_on_one_local_engine(session, probes):
    await _seed(session)
    listing = await pairs.list_pairs(session)
    omp = _find(listing, "omp", "box-a-slot")
    claude = _find(listing, "claude", "box-a-slot")
    assert (omp["status"], omp["locality"], omp["live"]) == ("ok", "local", True)
    assert claude["status"] == "experimental"
    # the recipe row with the same engine + model is folded into the slot row
    assert not any(p["runtime_slug"] == "glm-recipe" for p in listing["pairs"])
    # a row whose model is not served right now is not startable
    assert _find(listing, "omp", "qwen-recipe")["reason_code"] == "engine_not_ready"


async def test_default_is_local_omp(session, probes):
    await _seed(session)
    listing = await pairs.list_pairs(session)
    d = listing["default_pair"]
    assert (d["harness"], d["runtime_slug"], d["locality"], d["startable"]) == ("omp", "box-a-slot", "local", True)


async def test_no_live_engine_keeps_local_default_never_cloud(session, probes):
    probes["served"] = {}
    await _seed(session)
    listing = await pairs.list_pairs(session)
    d = listing["default_pair"]
    assert d["locality"] == "local"
    assert d["startable"] is False and d["reason_code"] == "engine_not_ready"


async def test_claude_local_needs_the_anthropic_route(session, probes):
    probes["anthropic"] = False
    await _seed(session)
    listing = await pairs.list_pairs(session)
    assert _find(listing, "claude", "box-a-slot")["reason_code"] == "protocol_mismatch"


async def test_cloud_and_claude_pairs_are_blocked_with_codes(session, probes):
    await _seed(session)
    listing = await pairs.list_pairs(session)
    assert _find(listing, "omp", "anthropic-claude-opus")["reason_code"] == "needs_operator_decision"
    assert _find(listing, "omp", "ollama-cloud")["reason_code"] == "unproven"
    assert _find(listing, "claude", "anthropic-claude-opus")["reason_code"] == "unproven"
    assert _find(listing, "openclaude", "box-a-slot")["reason_code"] == "unproven"


async def test_kimi_hermes_grok_are_not_offered(session, probes):
    await _seed(session)
    listing = await pairs.list_pairs(session)
    assert {p["harness"] for p in listing["pairs"]} <= {"omp", "claude", "openclaude"}
    assert not any(p["runtime_slug"] == "kimi-cloud" for p in listing["pairs"])


async def test_busy_box_marks_local_pairs_busy(session, probes):
    box = await _seed(session)
    occ = {str(box.id): {"run_id": "r1", "task_id": "t1", "title": "Other job"}}
    listing = await pairs.list_pairs(session, occ)
    omp = _find(listing, "omp", "box-a-slot")
    assert omp["reason_code"] == "box_busy" and omp["busy_by"]["title"] == "Other job"


async def test_engine_in_use_hint(session, probes):
    probes["running"] = 2
    await _seed(session)
    listing = await pairs.list_pairs(session)
    assert _find(listing, "omp", "box-a-slot")["engine_in_use"] is True


def test_pair_status_matrix_is_static_and_explicit():
    local = Runtime(slug="l", display_name="l", runtime_type="vllm_docker", endpoint=EP,
                    model_identifier="m", host_id=__import__("uuid").uuid4())
    assert pairs.pair_status("omp", local, {"openai"}) == ("ok", None)
    assert pairs.pair_status("claude", local, {"openai", "anthropic"}) == ("experimental", None)
    assert pairs.pair_status("kimi", local, {"openai"}) == ("blocked", "harness_not_supported")


def test_default_never_falls_back_to_a_startable_cloud_pair():
    """Even once a cloud pair becomes startable (later), a dead local engine
    must not silently turn the default into cloud."""
    def mk(harness, slug, locality, status, live, reason=None):
        return pairs.Pair(harness=harness, harness_label=harness, runtime_slug=slug, runtime_label=slug,
                          model="m", locality=locality, status=status, reason_code=reason, live=live, box_keys=[])

    listing = [
        mk("omp", "ollama-cloud", "cloud", "ok", True),
        mk("claude", "anthropic", "cloud", "ok", True),
        mk("omp", "local-slot", "local", "blocked", False, "engine_not_ready"),
    ]
    d = pairs.default_pair(listing)
    assert (d.runtime_slug, d.locality, d.startable) == ("local-slot", "local", False)



@pytest.mark.parametrize("runtime_type,endpoint", [
    ("cloud", "https://api.anthropic.com/v1/messages"),
    ("cloud", "https://ollama.com/v1"),
    ("openai_compatible", "https://api.example.invalid/v1"),
])
def test_no_cloud_pair_is_startable_without_the_quota_gate(runtime_type, endpoint):
    """Security review: a cloud pair would hand a real API key to the head
    (head.env / omp models.yml are readable by it) and spend the operator's
    quota. Before any cloud pair leaves "blocked", a key proxy and the 30 %
    quota gate (spec §10) must exist — this test must then be changed on
    purpose, together with that gate."""
    rt = Runtime(slug="c", display_name="C", runtime_type=runtime_type, endpoint=endpoint, model_identifier="m")
    for harness in pairs.OFFERED_HARNESSES:
        for protocols in (set(), {"openai"}, {"anthropic"}, {"openai", "anthropic"}):
            status, _ = pairs.pair_status(harness, rt, protocols)
            assert status == "blocked", (harness, runtime_type, protocols)
