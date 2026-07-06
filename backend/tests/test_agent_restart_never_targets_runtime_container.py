"""ADR-059 regression guard: the agent-restart propagation path must never be
able to restart a sparkrun/model container on the Spark host.

Context: a live incident restarted a sparkrun-managed vLLM container via
`docker restart`, which reset it to its `sleep infinity` PID1 without the
vLLM process the recipe had injected — silently killing a healthy model.
That specific `docker restart` came from `runtime_manager.restart_runtime()`
(the manual /runtimes restart button, host-scoped SSH, entirely separate from
this path). This test locks in that the OTHER restart path — the automated
agent-restart propagation used by `runtime_watcher`/`runtime_propagation` to
reload a cli-bridge agent's OpenAI client after a model change — can never
reach a sparkrun/model container by construction: it always restarts a LOCAL
`mc-agent-<slug>` Docker container (derived only from the agent's own name),
never anything derived from a runtime or model identifier.
"""
from unittest.mock import patch, MagicMock

import pytest

from app.models.agent import Agent
from app.services import docker_agent_sync


def _mk_agent(name: str) -> Agent:
    return Agent(name=name, agent_runtime="cli-bridge")


@pytest.mark.parametrize(
    "name",
    [
        "Sparky",
        # Adversarial names that look like sparkrun/model container names —
        # even then, the derived container must stay in the mc-agent-* space.
        "sparkrun_abc123_solo",
        "vllm_node",
    ],
)
def test_restart_always_targets_mc_agent_container(name):
    agent = _mk_agent(name)
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=proc) as run_mock:
        result = docker_agent_sync.restart_docker_agent_container(agent)

    assert result["container"].startswith("mc-agent-")
    # The actual `docker restart` argv must carry the same mc-agent-* name —
    # never the raw agent name / a sparkrun-style identifier.
    argv = run_mock.call_args.args[0]
    assert argv[-1].startswith("mc-agent-")
    assert argv[-1] == result["container"]
