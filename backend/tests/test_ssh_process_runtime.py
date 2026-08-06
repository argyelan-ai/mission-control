"""ssh_process runtime type + one-click recipe install (PR 6).

Three areas, all exercised through a fake ``_ssh_run`` — the tests assert WHICH
commands would run on the box and what the state machines make of the output.
The one real SSH round-trip belongs in the live gate, not here.

  1. lifecycle — the state matrix, an idempotent start, stop via the engine's
     own script vs the pkill fallback, and the verification that keeps a
     "started" from being a lie.
  2. exclusivity — a 110 GB model may not launch onto a box that still holds
     another one, across engine types.
  3. install job — the background runner's status machine, its disk warning
     and the cursor-based log.

Only RFC 5737 placeholder IPs (192.0.2.x) — public repo.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.host import Host
from app.models.local_recipe import ENGINES, LocalRecipe
from app.models.runtime import Runtime
from app.services import local_registry, recipe_install, runtime_grace, runtime_manager
from app.services.agent_runtime_switch import _PROBEABLE_RUNTIME_TYPES
from app.services.harness_compat import _OPENAI_TYPES, runtime_protocol
from app.services.host_resolver import ResolvedHost
from app.services.launch_template import build_install_command, build_launch_command
from app.services.task_runner import _SLOW_RUNTIME_TYPES

SSH_PROCESS = runtime_manager.SSH_PROCESS_TYPE

DS4_RT = {
    "id": "ds4-spark",
    "slug": "ds4-spark",
    "display_name": "DeepSeek V4 Flash (ds4)",
    "runtime_type": SSH_PROCESS,
    "endpoint": "http://192.0.2.10:8888/v1",
    "healthcheck_path": "/v1/models",
    "process_name": "ds4-server",
    "launch_command": "cd ~/code/ds4-wrapper && PORT=8888 ./start.sh",
    "stop_command": "cd ~/code/ds4-wrapper && PORT=8888 ./stop.sh",
    "exclusive_memory": True,
}


def _rt(**overrides) -> dict:
    return {**DS4_RT, **overrides}


def _host() -> ResolvedHost:
    return ResolvedHost(
        ssh_host="192.0.2.10", ssh_user="mcuser", ssh_key_path="/keys/id", kind="ssh",
        source="registry",
    )


@pytest.fixture(autouse=True)
def _fast_polls():
    """No real sleeping between verification polls."""
    saved = (
        runtime_manager._verify_poll_interval,
        runtime_manager._ssh_process_start_timeout,
        runtime_manager._ssh_process_stop_timeout,
    )
    runtime_manager._verify_poll_interval = 0
    runtime_manager._ssh_process_start_timeout = 0.05
    runtime_manager._ssh_process_stop_timeout = 0.05
    yield
    (
        runtime_manager._verify_poll_interval,
        runtime_manager._ssh_process_start_timeout,
        runtime_manager._ssh_process_stop_timeout,
    ) = saved


def _ssh(handler):
    """Patch _ssh_run with a handler mapping command → (stdout, stderr, exit)."""
    return patch.object(runtime_manager, "_ssh_run", AsyncMock(side_effect=handler))


# ── Type-set membership ──────────────────────────────────────────────────────


def test_ssh_process_is_registered_in_every_type_set():
    """An OpenAI-protocol engine missing from one of these sets loses agent
    binding, watcher probing or the slow-runtime timeout — silently."""
    assert SSH_PROCESS in _OPENAI_TYPES
    assert SSH_PROCESS in _PROBEABLE_RUNTIME_TYPES
    assert SSH_PROCESS in _SLOW_RUNTIME_TYPES
    assert SSH_PROCESS in ENGINES
    assert runtime_protocol(
        Runtime(slug="ds4", display_name="ds4", runtime_type=SSH_PROCESS,
                endpoint="http://192.0.2.10:8888/v1")
    ) == "openai"


def test_ssh_process_is_not_a_docker_engine_type():
    """It must never fall into the docker paths — those speak to a daemon that
    is not involved here (and the watcher's auto-recovery is docker-only)."""
    assert SSH_PROCESS not in runtime_manager.DOCKER_ENGINE_TYPES


def test_new_runtime_columns_exist_on_the_model():
    rt = Runtime(slug="ds4", display_name="ds4", runtime_type=SSH_PROCESS,
                 endpoint="http://192.0.2.10:8888/v1",
                 process_name="ds4-server", stop_command="./stop.sh",
                 exclusive_memory=True)
    registry = rt.to_registry_dict()
    assert registry["process_name"] == "ds4-server"
    assert registry["stop_command"] == "./stop.sh"
    assert registry["exclusive_memory"] is True
    # The two flags are independent — reusing single_instance would have made
    # this model unswitchable for agents (Phase 24 hard block).
    assert registry["single_instance"] is False


# ── State matrix ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_ready_when_process_and_http_are_up():
    async def handler(cmd, **kw):
        assert "pgrep -x ds4-server" in cmd
        return ("", "", 0)

    with _ssh(handler), patch.object(runtime_manager, "_probe_http", AsyncMock(return_value=True)):
        state = await runtime_manager.get_runtime_state(_rt(), host=_host())
    assert state["state"] == "ready"
    assert state["http_reachable"] is True


@pytest.mark.asyncio
async def test_state_warming_when_process_runs_but_port_is_silent():
    """110 GiB of weights take minutes. A live process with a dead port is a
    load in progress, not an outage — calling it "stopped" would invite a
    second start on top of the first."""
    with _ssh(lambda cmd, **kw: ("", "", 0)), \
            patch.object(runtime_manager, "_probe_http", AsyncMock(return_value=False)):
        state = await runtime_manager.get_runtime_state(_rt(), host=_host())
    assert state["state"] == "warming"
    assert state["container_status"] == "process_running"


@pytest.mark.asyncio
async def test_state_stopped_when_pgrep_finds_nothing():
    probe = AsyncMock(return_value=True)
    with _ssh(lambda cmd, **kw: ("", "", 1)), patch.object(runtime_manager, "_probe_http", probe):
        state = await runtime_manager.get_runtime_state(_rt(), host=_host())
    assert state["state"] == "stopped"
    # No process → no reason to spend a probe on the port.
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_state_unknown_when_ssh_is_broken():
    """"We cannot reach the box" and "nothing runs there" are different
    answers. Reporting the first as the second is how an eviction decides a
    full box is free."""
    with _ssh(AsyncMock(side_effect=OSError("connection refused"))):
        state = await runtime_manager.get_runtime_state(_rt(), host=_host())
    assert state["state"] == "unknown"
    assert state["container_status"] == "ssh_error"


@pytest.mark.asyncio
async def test_state_unknown_when_pgrep_itself_errors():
    """pgrep exit >1 is a usage/permission error, not "no match" (exit 1)."""
    with _ssh(lambda cmd, **kw: ("", "bad option", 2)):
        state = await runtime_manager.get_runtime_state(_rt(), host=_host())
    assert state["state"] == "unknown"


@pytest.mark.asyncio
async def test_state_unknown_without_a_process_name():
    state = await runtime_manager.get_runtime_state(_rt(process_name=None), host=_host())
    assert state["state"] == "unknown"
    assert state["container_status"] == "no_process_name"


@pytest.mark.asyncio
async def test_probe_path_does_not_double_the_v1_segment():
    """endpoint ".../v1" + health "/v1/models" probed ".../v1/v1/models" → 404
    on every healthy engine. Same normalization as unsloth_porsche."""
    seen = {}

    async def probe(endpoint, path):
        seen["path"] = path
        return True

    with _ssh(lambda cmd, **kw: ("", "", 0)), patch.object(runtime_manager, "_probe_http", probe):
        await runtime_manager.get_runtime_state(_rt(), host=_host())
    assert seen["path"] == "/models"


# ── Start ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_is_idempotent_when_already_ready():
    """No relaunch of a running 110 GB engine, ever."""
    calls = []

    async def handler(cmd, **kw):
        calls.append(cmd)
        return ("", "", 0)

    with _ssh(handler), patch.object(runtime_manager, "_probe_http", AsyncMock(return_value=True)):
        result = await runtime_manager._start_runtime_impl(_rt(), host=_host())

    assert result["ok"] is True
    assert "läuft bereits" in result["message"]
    assert all("nohup" not in c for c in calls)


@pytest.mark.asyncio
async def test_start_is_idempotent_while_still_warming():
    with _ssh(lambda cmd, **kw: ("", "", 0)), \
            patch.object(runtime_manager, "_probe_http", AsyncMock(return_value=False)):
        result = await runtime_manager._start_runtime_impl(_rt(), host=_host())
    assert result["ok"] is True
    assert "startet bereits" in result["message"]


@pytest.mark.asyncio
async def test_start_launches_detached_and_verifies_the_process():
    calls = []
    pgrep_results = iter([1, 0])  # not running before the launch, running after

    async def handler(cmd, **kw):
        calls.append(cmd)
        if cmd.startswith("pgrep"):
            return ("", "", next(pgrep_results, 0))
        return ("", "", 0)

    with _ssh(handler), patch.object(runtime_manager, "_probe_http", AsyncMock(return_value=False)):
        result = await runtime_manager._start_runtime_impl(_rt(), host=_host())

    assert result["ok"] is True
    launch = next(c for c in calls if "nohup" in c)
    assert "nohup bash -lc" in launch
    assert "./start.sh" in launch
    assert "~/.cache/mc/runtime-launch-ds4-spark.log" in launch


@pytest.mark.asyncio
async def test_start_fails_when_the_process_never_appears():
    """nohup returns 0 the moment the shell forks — whether or not the engine
    survived. Without this check a crash-on-startup reports success."""
    async def handler(cmd, **kw):
        if cmd.startswith("pgrep"):
            return ("", "", 1)
        return ("", "", 0)

    with _ssh(handler), patch.object(runtime_manager, "_probe_http", AsyncMock(return_value=False)):
        result = await runtime_manager._start_runtime_impl(_rt(), host=_host())

    assert result["ok"] is False
    assert "ds4-server" in result["message"]
    assert "runtime-launch-ds4-spark.log" in result["message"]


@pytest.mark.asyncio
async def test_start_refuses_without_process_name_or_launch_command():
    no_cmd = await runtime_manager._start_runtime_impl(_rt(launch_command=""), host=_host())
    assert no_cmd["ok"] is False
    assert "launch_command" in no_cmd["message"]

    no_name = await runtime_manager._start_runtime_impl(_rt(process_name=""), host=_host())
    assert no_name["ok"] is False
    assert "process_name" in no_name["message"]


@pytest.mark.asyncio
async def test_start_marks_grace_launching_then_loading():
    """The watcher must not read a multi-minute weight load as an outage
    (PR 5). ssh_process moves on to `loading` once the process is confirmed."""
    phases = []

    async def mark(slug, phase, source):
        phases.append((slug, phase, source))

    with patch.object(runtime_manager.runtime_grace, "mark_switching", mark), \
            patch.object(runtime_manager, "ensure_exclusive_host",
                         AsyncMock(return_value={"ok": True, "message": "", "stopped": []})), \
            patch.object(runtime_manager, "_start_runtime_impl",
                         AsyncMock(return_value={"ok": True, "message": "up"})), \
            patch.object(runtime_manager, "_emit_exclusive_event", AsyncMock()):
        await runtime_manager.start_runtime(_rt(), host=_host())

    assert [p for _, p, _ in phases] == [
        runtime_grace.PHASE_LAUNCHING, runtime_grace.PHASE_LOADING,
    ]
    assert all(source == runtime_grace.SOURCE_MANUAL for _, _, source in phases)


@pytest.mark.asyncio
async def test_failed_start_clears_the_grace_marker():
    cleared = []

    with patch.object(runtime_manager.runtime_grace, "mark_switching", AsyncMock()), \
            patch.object(runtime_manager.runtime_grace, "clear_switching",
                         AsyncMock(side_effect=lambda slug: cleared.append(slug))), \
            patch.object(runtime_manager, "ensure_exclusive_host",
                         AsyncMock(return_value={"ok": True, "message": "", "stopped": []})), \
            patch.object(runtime_manager, "_start_runtime_impl",
                         AsyncMock(return_value={"ok": False, "message": "boom"})), \
            patch.object(runtime_manager, "_emit_exclusive_event", AsyncMock()):
        await runtime_manager.start_runtime(_rt(), host=_host())

    assert cleared == ["ds4-spark"]


# ── Stop ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_prefers_the_engines_own_stop_command():
    """ds4's stop.sh waits until the port is actually free — pkill does not."""
    calls = []

    async def handler(cmd, **kw):
        calls.append(cmd)
        return ("", "", 0) if "stop.sh" in cmd else ("", "", 1)

    with _ssh(handler):
        result = await runtime_manager.stop_runtime(_rt(), host=_host())

    assert result["ok"] is True
    assert any("stop.sh" in c for c in calls)
    assert not any(c.startswith("pkill") for c in calls)


@pytest.mark.asyncio
async def test_stop_falls_back_to_pkill_without_a_stop_command():
    calls = []

    async def handler(cmd, **kw):
        calls.append(cmd)
        return ("", "", 1) if cmd.startswith("pgrep") else ("", "", 0)

    with _ssh(handler):
        result = await runtime_manager.stop_runtime(_rt(stop_command=None), host=_host())

    assert result["ok"] is True
    assert any(c.startswith("pkill -x ds4-server") for c in calls)


@pytest.mark.asyncio
async def test_stop_fails_when_the_process_survives():
    """A stop that reports success while 110 GB stay resident is worse than a
    failure: the next model is launched onto a full box."""
    async def handler(cmd, **kw):
        return ("", "", 0)  # stop "succeeds", pgrep keeps finding the process

    with _ssh(handler), patch.object(runtime_manager, "verify_ssh_process_stopped",
                                     AsyncMock(return_value=False)):
        result = await runtime_manager.stop_runtime(_rt(), host=_host())

    assert result["ok"] is False
    assert "läuft nach dem Stop-Befehl weiter" in result["message"]


@pytest.mark.asyncio
async def test_stop_reports_a_failing_stop_command():
    with _ssh(lambda cmd, **kw: ("", "no such file", 127)):
        result = await runtime_manager.stop_runtime(_rt(), host=_host())
    assert result["ok"] is False
    assert "no such file" in result["message"]


@pytest.mark.asyncio
async def test_stop_without_any_handle_is_an_honest_failure():
    result = await runtime_manager.stop_runtime(
        _rt(process_name=None, stop_command=None), host=_host()
    )
    assert result["ok"] is False
    assert "process_name" in result["message"]


@pytest.mark.asyncio
async def test_restart_is_stop_then_start():
    order = []

    async def fake_stop(rt, **kw):
        order.append("stop")
        return {"ok": True, "message": "stopped"}

    async def fake_start(rt, **kw):
        order.append("start")
        return {"ok": True, "message": "started"}

    with patch.object(runtime_manager, "stop_runtime", fake_stop), \
            patch.object(runtime_manager, "start_runtime", fake_start):
        result = await runtime_manager._restart_runtime_impl(_rt(), host=_host())

    assert result["ok"] is True
    assert order == ["stop", "start"]


@pytest.mark.asyncio
async def test_restart_aborts_when_the_stop_failed():
    """Starting on top of a process that would not die is the OOM we are
    trying to avoid."""
    with patch.object(runtime_manager, "stop_runtime",
                      AsyncMock(return_value={"ok": False, "message": "still there"})), \
            patch.object(runtime_manager, "start_runtime", AsyncMock()) as start:
        result = await runtime_manager._restart_runtime_impl(_rt(), host=_host())

    assert result["ok"] is False
    start.assert_not_awaited()


# ── Exclusivity across engine types ──────────────────────────────────────────


async def _make_runtimes(session, host_id=None):
    vllm = Runtime(
        slug="qwen-general", display_name="Qwen General", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", exclusive_memory=True, enabled=True,
        host_id=host_id,
    )
    ds4 = Runtime(
        slug="ds4-spark", display_name="DeepSeek V4 Flash (ds4)", runtime_type=SSH_PROCESS,
        endpoint="http://192.0.2.10:8888/v1", exclusive_memory=True, enabled=True,
        process_name="ds4-server", stop_command="./stop.sh", host_id=host_id,
    )
    session.add(vllm)
    session.add(ds4)
    await session.commit()
    return vllm, ds4


@pytest.mark.asyncio
async def test_starting_ds4_evicts_the_vllm_container(session):
    """Cross-engine: the docker neighbour goes through the eviction sweep, not
    through a docker stop on a container_name that is None after a switch."""
    vllm, ds4 = await _make_runtimes(session)
    evict = AsyncMock(return_value={"ok": True, "message": "free", "stopped": ["abc123"]})

    with patch.object(runtime_manager, "evict_spark_runtime_containers", evict), \
            patch.object(runtime_manager, "get_runtime_state",
                         AsyncMock(return_value={"state": "ready"})), \
            patch.object(runtime_manager, "resolve_host_from_runtime_fields",
                         lambda rt: _host()):
        result = await runtime_manager.ensure_exclusive_host(
            ds4.to_registry_dict(), host=_host(), session=session
        )

    assert result["ok"] is True
    assert result["stopped"] == ["qwen-general"]
    evict.assert_awaited_once()
    assert evict.await_args.args[0] == "qwen-general"


@pytest.mark.asyncio
async def test_starting_vllm_stops_the_ds4_process(session):
    """And the other direction — otherwise the first ds4 install would block
    every later recipe switch."""
    vllm, ds4 = await _make_runtimes(session)
    stop = AsyncMock(return_value={"ok": True, "message": "gone"})

    with patch.object(runtime_manager, "stop_ssh_process", stop), \
            patch.object(runtime_manager, "get_runtime_state",
                         AsyncMock(return_value={"state": "warming"})):
        result = await runtime_manager.ensure_exclusive_host(
            vllm.to_registry_dict(), host=_host(), session=session
        )

    assert result["ok"] is True
    assert result["stopped"] == ["ds4-spark"]
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_is_aborted_when_the_neighbour_cannot_be_stopped(session):
    """The whole point: never launch a second 110 GB model onto a box that is
    not verifiably free."""
    vllm, ds4 = await _make_runtimes(session)

    with patch.object(runtime_manager, "evict_spark_runtime_containers",
                      AsyncMock(return_value={"ok": False, "message": "still running", "stopped": []})), \
            patch.object(runtime_manager, "get_runtime_state",
                         AsyncMock(return_value={"state": "ready"})):
        result = await runtime_manager.ensure_exclusive_host(
            ds4.to_registry_dict(), host=_host(), session=session
        )

    assert result["ok"] is False
    assert "qwen-general" in result["message"]
    assert "abgebrochen" in result["message"]


@pytest.mark.asyncio
async def test_an_already_stopped_neighbour_is_not_touched(session):
    vllm, ds4 = await _make_runtimes(session)
    evict = AsyncMock()

    with patch.object(runtime_manager, "evict_spark_runtime_containers", evict), \
            patch.object(runtime_manager, "get_runtime_state",
                         AsyncMock(return_value={"state": "stopped"})):
        result = await runtime_manager.ensure_exclusive_host(
            ds4.to_registry_dict(), host=_host(), session=session
        )

    assert result["ok"] is True
    assert result["stopped"] == []
    evict.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_exclusive_runtime_evicts_nothing(session):
    """vLLM behaviour is unchanged for every existing row: the flag defaults to
    false, and without it this is a no-op."""
    await _make_runtimes(session)
    evict = AsyncMock()

    with patch.object(runtime_manager, "evict_spark_runtime_containers", evict):
        result = await runtime_manager.ensure_exclusive_host(
            {"slug": "small-helper", "runtime_type": "llamacpp_docker", "exclusive_memory": False},
            host=_host(), session=session,
        )

    assert result["ok"] is True
    evict.assert_not_awaited()


@pytest.mark.asyncio
async def test_exclusivity_is_scoped_to_one_box(session):
    """Box A's start may never stop box B's model — the same rule
    evict_spark_runtime_containers learned (ADR-048)."""
    box_a = Host(slug="spark-a", display_name="Spark A", kind="ssh", ssh_host="192.0.2.10")
    box_b = Host(slug="spark-b", display_name="Spark B", kind="ssh", ssh_host="192.0.2.11")
    session.add(box_a)
    session.add(box_b)
    await session.commit()

    session.add(Runtime(
        slug="a-vllm", display_name="A vLLM", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", exclusive_memory=True, host_id=box_a.id,
    ))
    session.add(Runtime(
        slug="b-vllm", display_name="B vLLM", runtime_type="vllm_docker",
        endpoint="http://192.0.2.11:8000/v1", exclusive_memory=True, host_id=box_b.id,
    ))
    ds4 = Runtime(
        slug="ds4-a", display_name="ds4 on A", runtime_type=SSH_PROCESS,
        endpoint="http://192.0.2.10:8888/v1", exclusive_memory=True,
        process_name="ds4-server", host_id=box_a.id,
    )
    session.add(ds4)
    await session.commit()

    evict = AsyncMock(return_value={"ok": True, "message": "free", "stopped": []})
    with patch.object(runtime_manager, "evict_spark_runtime_containers", evict), \
            patch.object(runtime_manager, "get_runtime_state",
                         AsyncMock(return_value={"state": "ready"})):
        result = await runtime_manager.ensure_exclusive_host(
            ds4.to_registry_dict(), host=_host(), session=session
        )

    assert result["stopped"] == ["a-vllm"]
    assert "b-vllm" not in result["stopped"]


@pytest.mark.asyncio
async def test_start_runtime_refuses_when_exclusivity_fails():
    impl = AsyncMock()
    with patch.object(runtime_manager, "ensure_exclusive_host",
                      AsyncMock(return_value={"ok": False, "message": "box is busy", "stopped": []})), \
            patch.object(runtime_manager, "_emit_exclusive_event", AsyncMock()), \
            patch.object(runtime_manager, "_start_runtime_impl", impl):
        result = await runtime_manager.start_runtime(_rt(), host=_host())

    assert result["ok"] is False
    assert result["message"] == "box is busy"
    impl.assert_not_awaited()


# ── Template rendering ───────────────────────────────────────────────────────


def test_ssh_process_launch_template_needs_no_container_label():
    """The label rule exists so lifecycle ops can find a CONTAINER again. A
    host process is found by process_name, so demanding the label would make
    every ssh_process entry unrenderable."""
    command = build_launch_command(
        engine=SSH_PROCESS,
        model_identifier="DeepSeek-V4-Flash",
        slug="ds4-spark",
        port=8888,
        launch_template="cd {src_dir}/repo && PORT={port} CTX={ctx} ./start.sh",
        src_dir="~/code/mc-engines",
        ctx=262144,
    )
    assert command == "cd ~/code/mc-engines/repo && PORT=8888 CTX=262144 ./start.sh"


def test_docker_engines_still_require_the_label():
    with pytest.raises(ValueError, match="mc.runtime.slug"):
        build_launch_command(
            engine="vllm_docker", model_identifier="x", slug="y", port=8000,
            launch_template="docker run -d {image} --model {model}",
        )


def test_ssh_process_without_a_template_says_so():
    with pytest.raises(ValueError, match="eigenes launch_template"):
        build_launch_command(
            engine=SSH_PROCESS, model_identifier="x", slug="y", port=8888,
        )


def test_install_template_renders_the_same_placeholders():
    command = build_install_command(
        slug="ds4-spark",
        install_template="git clone REPO {src_dir}/x && cd {src_dir}/x && PORT={port} CTX={ctx} ./start.sh",
        port=8888,
        ctx=262144,
        src_dir="~/code/mc-engines",
    )
    assert "{" not in command
    assert "PORT=8888 CTX=262144" in command


def test_install_command_refuses_an_unknown_placeholder():
    with pytest.raises(ValueError, match="Unbekannte Platzhalter"):
        build_install_command(
            slug="x", install_template="run --secret {api_key}",
        )


def test_install_command_without_a_template_is_a_clear_error():
    with pytest.raises(ValueError, match="nichts zu installieren"):
        build_install_command(slug="x", install_template="")


# ── Registry schema ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_carries_the_ds4_recipe_with_credits(session):
    await local_registry.seed_local_recipes(session)
    recipe = (
        await session.exec(select(LocalRecipe).where(LocalRecipe.slug == "deepseek-v4-flash-ds4"))
    ).first()

    assert recipe is not None
    assert recipe.engine == SSH_PROCESS
    assert recipe.process_name == "ds4-server"
    assert recipe.install_template and "git clone" in recipe.install_template
    assert recipe.stop_template and "stop.sh" in recipe.stop_template
    assert recipe.author and "MiaAI-Lab" in recipe.author
    assert recipe.author_url.startswith("https://github.com/MiaAI-Lab/")
    # Not run by us on a GB10 yet — the flag is the difference between "should
    # work" and "we watched it work".
    assert recipe.gb10_validated is False


@pytest.mark.asyncio
async def test_every_seeded_recipe_has_an_author(session):
    """Marks Wunsch: credits on every card, not only the new one."""
    await local_registry.seed_local_recipes(session)
    recipes = (await session.exec(select(LocalRecipe))).all()
    assert recipes
    missing = [r.slug for r in recipes if not r.author]
    assert missing == []


@pytest.mark.asyncio
async def test_refresh_round_trips_the_new_fields(session):
    """A remote registry must be able to ship an ssh_process entry — the seed
    file is a valid source payload, so both directions use one schema."""
    payload = [{
        "slug": "community-engine",
        "display_name": "Community Engine",
        "engine": "ssh_process",
        "model_identifier": "some/model",
        "install_template": "git clone https://example.invalid/x {src_dir}/x",
        "launch_template": "cd {src_dir}/x && PORT={port} ./run.sh",
        "stop_template": "cd {src_dir}/x && ./stop.sh",
        "process_name": "community-server",
        "author": "Someone",
        "author_url": "https://example.invalid",
        "source_registry": "community",
    }]

    with patch.object(local_registry, "registry_sources", lambda: ["https://example.invalid/r.json"]), \
            patch.object(local_registry, "_fetch_source", AsyncMock(return_value=payload)):
        result = await local_registry.refresh_from_sources(session)

    assert result.added == 1
    row = (
        await session.exec(select(LocalRecipe).where(LocalRecipe.slug == "community-engine"))
    ).first()
    assert row.process_name == "community-server"
    assert row.author == "Someone"
    assert row.install_template.startswith("git clone")


def test_unknown_engines_are_still_rejected():
    spec = local_registry.RecipeSpec(
        slug="x", display_name="X", engine="magic", model_identifier="m",
    )
    assert "unknown engine" in spec.validate_vocabulary()


# ── Install job ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_install_polls():
    interval = recipe_install._poll_interval
    recipe_install._poll_interval = 0
    yield
    recipe_install._poll_interval = interval


def _install_ssh(handler):
    return patch("app.services.runtime_manager._ssh_run", AsyncMock(side_effect=handler))


@pytest.mark.asyncio
async def test_install_run_reaches_done_on_exit_zero():
    """The MC_EXIT marker is the only way an exit code gets out of a detached
    process — this is the whole status machine in one test."""
    tail_output = iter([
        "Cloning into 'ds4'...\nBuilding...\n",
        "Downloading weights...\nMC_EXIT:0\n",
    ])

    async def handler(cmd, **kw):
        if cmd.startswith("df"):
            return ("Filesystem 1024-blocks Used Available Capacity\n/dev/sda 1000 100 838860800 10%", "", 0)
        if "nohup" in cmd:
            return ("4711", "", 0)
        if cmd.startswith("tail"):
            return (next(tail_output, ""), "", 0)
        return ("", "", 0)

    with _install_ssh(handler):
        await recipe_install.run_install(
            "host-1", "ds4", _host(), command="./install.sh", est_weights_gb=110.0,
            display_name="ds4",
        )

    status = await recipe_install.get_status("host-1", "ds4")
    assert status["status"] == recipe_install.STATUS_DONE
    log = await recipe_install.read_log("host-1", "ds4")
    texts = " ".join(line["text"] for line in log["lines"])
    assert "Cloning into" in texts
    assert "Downloading weights" in texts
    # The marker itself is bookkeeping, not output the operator should read.
    assert "MC_EXIT" not in texts


@pytest.mark.asyncio
async def test_install_run_fails_on_a_nonzero_exit():
    async def handler(cmd, **kw):
        if cmd.startswith("df"):
            return ("h\n/dev/sda 1 1 838860800 1%", "", 0)
        if "nohup" in cmd:
            return ("4711", "", 0)
        if cmd.startswith("tail"):
            return ("fatal: could not read from remote\nMC_EXIT:128\n", "", 0)
        return ("", "", 0)

    with _install_ssh(handler):
        await recipe_install.run_install("host-1", "ds4", _host(), command="./install.sh")

    status = await recipe_install.get_status("host-1", "ds4")
    assert status["status"] == recipe_install.STATUS_FAILED
    assert "exit 128" in status["message"]


@pytest.mark.asyncio
async def test_install_warns_when_the_disk_is_too_small():
    """A warning, not a block: est_weights_gb is an estimate and the operator
    may know about a mount we cannot see."""
    async def handler(cmd, **kw):
        if cmd.startswith("df"):
            # ~20 GB free, 110 GB wanted.
            return ("header\n/dev/sda 100 80 20971520 80%", "", 0)
        if "nohup" in cmd:
            return ("1", "", 0)
        if cmd.startswith("tail"):
            return ("MC_EXIT:0\n", "", 0)
        return ("", "", 0)

    with _install_ssh(handler):
        await recipe_install.run_install(
            "host-1", "ds4", _host(), command="./install.sh", est_weights_gb=110.0,
        )

    log = await recipe_install.read_log("host-1", "ds4")
    warnings = [line for line in log["lines"] if line["level"] == "warn"]
    assert any("20.0 GB frei" in line["text"] for line in warnings)
    # Warned, and still ran.
    status = await recipe_install.get_status("host-1", "ds4")
    assert status["status"] == recipe_install.STATUS_DONE


@pytest.mark.asyncio
async def test_install_does_not_warn_when_the_disk_is_big_enough():
    async def handler(cmd, **kw):
        if cmd.startswith("df"):
            return ("header\n/dev/sda 100 10 838860800 10%", "", 0)  # 800 GB
        if "nohup" in cmd:
            return ("1", "", 0)
        if cmd.startswith("tail"):
            return ("MC_EXIT:0\n", "", 0)
        return ("", "", 0)

    with _install_ssh(handler):
        await recipe_install.run_install(
            "host-1", "ds4", _host(), command="./install.sh", est_weights_gb=110.0,
        )

    log = await recipe_install.read_log("host-1", "ds4")
    assert not [line for line in log["lines"] if line["level"] == "warn"]


@pytest.mark.asyncio
async def test_install_log_is_cursor_based():
    """The UI polls with the cursor it got back and receives exactly the new
    lines — never a re-read of the whole log."""
    job = recipe_install.job_for("host-1", "ds4")
    await job.append("first")
    await job.append("second")

    first = await recipe_install.read_log("host-1", "ds4", 0)
    assert [line["text"] for line in first["lines"]] == ["first", "second"]

    await job.append("third")
    second = await recipe_install.read_log("host-1", "ds4", first["cursor"])
    assert [line["text"] for line in second["lines"]] == ["third"]
    assert second["cursor"] == first["cursor"] + 1


@pytest.mark.asyncio
async def test_install_log_is_idle_before_anything_ran():
    log = await recipe_install.read_log("host-unknown", "nothing")
    assert log["status"] == "idle"
    assert log["lines"] == []
    assert log["running"] is False


@pytest.mark.asyncio
async def test_install_detects_a_process_that_died_without_a_result():
    """kill -9 or a rebooted box: no marker will ever arrive, and waiting 8h
    for a timeout would be a lie about what MC knows."""
    async def handler(cmd, **kw):
        if cmd.startswith("df"):
            return ("h\n/dev/sda 1 1 838860800 1%", "", 0)
        if "nohup" in cmd:
            return ("4711", "", 0)
        if cmd.startswith("tail"):
            return ("still working\n", "", 0)
        if cmd.startswith("kill -0"):
            return ("", "", 1)
        return ("", "", 0)

    with _install_ssh(handler):
        await recipe_install.run_install("host-1", "ds4", _host(), command="./install.sh")

    status = await recipe_install.get_status("host-1", "ds4")
    assert status["status"] == recipe_install.STATUS_FAILED
    assert status["phase"] == "lost"


@pytest.mark.asyncio
async def test_install_reports_a_failed_launch():
    async def handler(cmd, **kw):
        if cmd.startswith("df"):
            return ("h\n/dev/sda 1 1 838860800 1%", "", 0)
        if "nohup" in cmd:
            return ("", "Permission denied", 126)
        return ("", "", 0)

    with _install_ssh(handler):
        await recipe_install.run_install("host-1", "ds4", _host(), command="./install.sh")

    status = await recipe_install.get_status("host-1", "ds4")
    assert status["status"] == recipe_install.STATUS_FAILED
    assert "Permission denied" in status["message"]


@pytest.mark.asyncio
async def test_install_jobs_are_scoped_per_box_and_recipe():
    """Two boxes installing the same engine are two jobs; one box installing
    two engines likewise. A shared key would cross the logs."""
    a = recipe_install.job_for("host-1", "ds4")
    b = recipe_install.job_for("host-2", "ds4")
    c = recipe_install.job_for("host-1", "other")
    assert len({a.log_key, b.log_key, c.log_key}) == 3

    await a.append("only for a")
    assert (await recipe_install.read_log("host-2", "ds4"))["lines"] == []


def test_free_gb_parser_survives_unparseable_df():
    assert recipe_install._parse_free_gb("") is None
    assert recipe_install._parse_free_gb("header only") is None
    assert recipe_install._parse_free_gb("h\n/dev/sda 1 1 notanumber 1%") is None
    assert recipe_install._parse_free_gb("h\n/dev/sda 100 10 1048576 10%") == 1.0


# ── Install endpoints ────────────────────────────────────────────────────────


async def _seeded_host(session) -> Host:
    host = Host(slug="spark", display_name="Spark", kind="ssh", ssh_host="192.0.2.10")
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


@pytest.mark.asyncio
async def test_install_endpoint_starts_a_job(auth_client, session):
    await local_registry.seed_local_recipes(session)
    await _seeded_host(session)
    started = {}

    async def fake_start(host_id, slug, resolved, **kwargs):
        started.update({"host_id": host_id, "slug": slug, **kwargs})

    with patch.object(recipe_install, "start_install", fake_start):
        response = await auth_client.post(
            "/api/v1/local-registry/deepseek-v4-flash-ds4/install",
            json={"host_id": "spark", "port": 8888, "ctx": 262144},
        )

    assert response.status_code == 202
    assert started["slug"] == "deepseek-v4-flash-ds4"
    # The command the box will run is rendered here, from the recipe — the
    # frontend never assembles a shell command.
    assert "PORT=8888 CTX=262144" in started["command"]
    assert "git clone" in started["command"]
    assert started["est_weights_gb"] == 110.0


@pytest.mark.asyncio
async def test_install_endpoint_rejects_a_second_run(auth_client, session):
    """Two installers sharing one clone and one weight directory produce a
    corrupted checkout, not a faster download."""
    await local_registry.seed_local_recipes(session)
    host = await _seeded_host(session)
    await recipe_install.job_for(str(host.id), "deepseek-v4-flash-ds4").set_status(
        recipe_install.STATUS_RUNNING, phase="install"
    )

    with patch.object(recipe_install, "start_install", AsyncMock()) as start:
        response = await auth_client.post(
            "/api/v1/local-registry/deepseek-v4-flash-ds4/install",
            json={"host_id": "spark"},
        )

    assert response.status_code == 409
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_install_endpoint_400s_without_an_install_template(auth_client, session):
    await local_registry.seed_local_recipes(session)
    await _seeded_host(session)

    response = await auth_client.post(
        "/api/v1/local-registry/laguna-s21-nvfp4/install", json={"host_id": "spark"}
    )
    assert response.status_code == 400
    assert "nichts zu installieren" in response.json()["detail"]


@pytest.mark.asyncio
async def test_install_endpoint_404s_on_unknown_recipe_or_host(auth_client, session):
    await local_registry.seed_local_recipes(session)
    await _seeded_host(session)

    assert (await auth_client.post(
        "/api/v1/local-registry/nope/install", json={"host_id": "spark"}
    )).status_code == 404
    assert (await auth_client.post(
        "/api/v1/local-registry/deepseek-v4-flash-ds4/install", json={"host_id": "ghost"}
    )).status_code == 404


@pytest.mark.asyncio
async def test_install_log_endpoint_serves_status_and_lines(auth_client, session):
    await local_registry.seed_local_recipes(session)
    host = await _seeded_host(session)
    job = recipe_install.job_for(str(host.id), "deepseek-v4-flash-ds4")
    await job.append("cloning")
    await job.set_status(recipe_install.STATUS_RUNNING, phase="install")

    response = await auth_client.get(
        "/api/v1/local-registry/deepseek-v4-flash-ds4/install/log?host_id=spark"
    )
    body = response.json()
    assert response.status_code == 200
    assert body["running"] is True
    assert [line["text"] for line in body["lines"]] == ["cloning"]
    assert body["cursor"] == 1


@pytest.mark.asyncio
async def test_recipe_list_exposes_the_credit_fields(auth_client, session):
    await local_registry.seed_local_recipes(session)
    body = (await auth_client.get("/api/v1/local-registry")).json()
    ds4 = next(r for r in body["recipes"] if r["slug"] == "deepseek-v4-flash-ds4")
    assert "MiaAI-Lab" in ds4["author"]
    assert ds4["author_url"].startswith("https://")
    assert ds4["process_name"] == "ds4-server"
    assert ds4["stop_template"]
    assert all(r["author"] for r in body["recipes"])


def test_migration_backfill_matches_the_seed_credits():
    """Two sources for the same fact: the seed file (fresh install) and the
    0177 backfill (existing DB). If they drift, the credit on Marks card
    depends on when he installed MC — which is exactly the bug this catches."""
    import json
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    seed = json.loads((backend / "config" / "local-recipes.json").read_text(encoding="utf-8"))
    migration = (
        backend / "alembic" / "versions" / "0177_ssh_process_runtime.py"
    ).read_text(encoding="utf-8")

    for entry in seed:
        # The ds4 entry is new in this migration — it arrives via the seeder,
        # never via a backfill, so it is deliberately not in that table.
        if entry["slug"] == "deepseek-v4-flash-ds4":
            continue
        assert f'"{entry["author"]}"' in migration, (
            f"{entry['slug']}: seed author is not what the migration backfills"
        )
        assert f'"{entry["author_url"]}"' in migration, entry["slug"]
