"""The agent read says WHICH task its run works on (round-4 K5, r1 N2).

"Stop run" on a review card asked only "is the agent working?" — a worker's
``current_task_id`` is never set for a review card, so a reviewer with two
delivered review cards showed "Stop run" on both. Stopping card B sets the
agent idle and the poll's ``stopped`` state makes the bridge end the session
— the run of card A.

The bridge already records the task its run works on
(``acp-task-state.json``, ``taskId``). The agents list and detail now carry it
as ``run_task_id`` (fresh ``working``/``delivered`` phase only), so the card
can offer Stop only for its own run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


class _StubAgent:
    def __init__(self, slug="acp-one", agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness


def _iso(delta_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_s)).isoformat()


@pytest.fixture
def home(tmp_path, monkeypatch):
    from app.services import omp_chat

    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    return tmp_path


def _write(home, slug: str, phase: str, task_id: str | None, age_s: float = 0.0) -> None:
    root = home / ".mc" / "agents" / slug / "omp-sessions"
    root.mkdir(parents=True, exist_ok=True)
    (root / "acp-task-state.json").write_text(json.dumps({
        "version": 1, "phase": phase, "taskId": task_id,
        "since": _iso(age_s), "updatedAt": _iso(age_s),
    }))


def _run_id(agent):
    from app.services.acp_chat_transport import read_acp_task_run_id

    return read_acp_task_run_id(agent)


@pytest.mark.parametrize("phase", ["working", "delivered"])
def test_a_live_task_phase_names_its_task(home, phase):
    _write(home, "acp-one", phase, "card-a")
    assert _run_id(_StubAgent()) == "card-a"


def test_idle_names_no_task(home):
    _write(home, "acp-one", "idle", "card-a")
    assert _run_id(_StubAgent()) is None


def test_a_stale_phase_names_no_task(home):
    # A crashed bridge's last phase ages out like read_acp_task_phase does.
    _write(home, "acp-one", "working", "card-a", age_s=3600)
    assert _run_id(_StubAgent()) is None


def test_no_file_and_garbage_name_no_task(home):
    assert _run_id(_StubAgent()) is None
    root = home / ".mc" / "agents" / "acp-one" / "omp-sessions"
    root.mkdir(parents=True)
    (root / "acp-task-state.json").write_text("{not json")
    assert _run_id(_StubAgent()) is None


def test_non_omp_agents_have_no_run_record(home):
    _write(home, "acp-one", "working", "card-a")
    assert _run_id(_StubAgent(harness="claude")) is None


def test_phase_reader_is_unchanged(home):
    from app.services.acp_chat_transport import read_acp_task_phase

    _write(home, "acp-one", "working", "card-a")
    assert read_acp_task_phase(_StubAgent()) == "working"


@pytest.mark.asyncio
async def test_agents_list_and_detail_carry_run_task_id(auth_client: AsyncClient, async_session, home):
    from app.models.agent import Agent

    reviewer = Agent(name="Checker", slug="k5-checker", agent_runtime="cli-bridge", harness="omp", status="working")
    other = Agent(name="Plain", slug="k5-plain", agent_runtime="cli-bridge", harness="claude", status="working")
    async_session.add(reviewer)
    async_session.add(other)
    await async_session.commit()
    await async_session.refresh(reviewer)
    await async_session.refresh(other)
    _write(home, "k5-checker", "working", "card-a")
    _write(home, "k5-plain", "working", "card-a")

    listed = await auth_client.get("/api/v1/agents")
    assert listed.status_code == 200, listed.text
    rows = {r["id"]: r for r in listed.json()}
    assert rows[str(reviewer.id)]["run_task_id"] == "card-a"
    assert rows[str(other.id)]["run_task_id"] is None

    one = await auth_client.get(f"/api/v1/agents/{reviewer.id}")
    assert one.status_code == 200, one.text
    assert one.json()["run_task_id"] == "card-a"
