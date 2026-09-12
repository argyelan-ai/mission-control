"""Neustart einer Multi-Node-Runtime (Verbund) — Vorfall 12.09.2026.

Was passiert war: der Restart-Knopf einer Runtime, die als vLLM TP=2 über ZWEI
Boxen lief (Head-Container auf der Head-Box, Worker-Container auf der zweiten
Box, NCCL dazwischen), führte im Backend nur ``docker restart <head>`` aus.
Der Worker behielt seine alte NCCL-Gruppe, der frische Head hing im Rendezvous
und starb 12 Minuten später am NCCL-Timeout. Zurück kam das Modell erst, als
der Autostart-Wächter das Startskript des Rezepts fuhr — das erzeugt BEIDE
Container neu.

Abgesichert wird darum:

  * Solo bleibt Solo — ``docker restart <container>``, unverändert.
  * Verbund geht über Stop (eigener ``stop_command``, nimmt den Worker mit)
    und Start (``launch_command`` — derselbe Weg wie der Autostart), und
    fasst NIE ``docker restart`` an. Das ist die Sabotage-Probe: sobald
    jemand den alten Head-Restart zurückbaut, wird dieser Test rot.
  * Fehlt MC das Wissen über den Verbund (kein stop_command/launch_command),
    gibt es einen klaren Fehler statt eines halben Neustarts.
  * Der Endpunkt schreibt das Ereignis ``runtime.restart_multinode`` mit Head
    und Worker namentlich.

Kein Netz: SSH und Lebenszyklus-Aufrufe sind ersetzt.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.models.host import Host
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.services import runtime_manager, runtime_multinode

_SOLO = {
    "id": "solo-rt",
    "slug": "solo-rt",
    "display_name": "Solo vLLM",
    "runtime_type": "vllm_docker",
    "endpoint": "http://192.0.2.10:8000/v1",
    "container_name": "engine_solo_abc",
    "topology": None,
}

_DUO = {
    "id": "duo-rt",
    "slug": "duo-rt",
    "display_name": "Duo vLLM TP=2",
    "runtime_type": "vllm_docker",
    "endpoint": "http://192.0.2.10:8000/v1",
    "container_name": "engine_duo_head",
    "topology": {"nodes": 2, "tp_total": 2, "roles": ["head", "worker"]},
    "stop_command": "/opt/recipes/duo/stop.sh",
    "launch_command": "/opt/recipes/duo/start.sh",
}


# ── Erkennung ────────────────────────────────────────────────────────────────


def test_is_multi_node_detects_two_nodes():
    assert runtime_manager.is_multi_node(_DUO) is True


@pytest.mark.parametrize(
    "topology", [None, {}, {"nodes": 1}, {"nodes": "kaputt"}, "kein-dict"]
)
def test_is_multi_node_false_for_solo_shapes(topology):
    assert runtime_manager.is_multi_node({**_SOLO, "topology": topology}) is False


# ── Solo: unverändert ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_solo_restart_still_uses_docker_restart():
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.restart_runtime(_SOLO)

    assert result["ok"] is True
    commands = [c.args[0] for c in ssh.await_args_list]
    assert commands == ["docker restart engine_solo_abc"]


# ── Verbund: Stop + Launch statt docker restart ──────────────────────────────


@pytest.mark.asyncio
async def test_duo_restart_goes_through_stop_and_launch():
    stop = AsyncMock(return_value={"ok": True, "message": "Verbund gestoppt"})
    start = AsyncMock(return_value={"ok": True, "message": "Verbund startet"})
    ssh = AsyncMock(return_value=("", "", 0))
    with (
        patch.object(runtime_manager, "stop_runtime", stop),
        patch.object(runtime_manager, "start_runtime", start),
        patch.object(runtime_manager, "_ssh_run", ssh),
    ):
        result = await runtime_manager.restart_runtime(_DUO)

    assert result["ok"] is True
    assert stop.await_count == 1
    assert start.await_count == 1
    # Sabotage-Probe: ein zurückgebauter Head-Restart würde hier auftauchen.
    assert ssh.await_count == 0


@pytest.mark.asyncio
async def test_duo_restart_never_runs_docker_restart_on_the_head():
    """Wie oben, aber ohne Ersatz für stop/start: der echte Verbund-Pfad läuft
    über den eigenen stop_command und den launch_command des Rezepts —
    ``docker restart`` darf in keinem abgesetzten Befehl vorkommen."""
    calls: list[str] = []

    async def _ssh(command, *_a, **_kw):
        calls.append(command)
        # Stop-Befehl ok; der Anker-Check danach meldet "kein Container mehr";
        # der Start meldet ebenfalls Erfolg.
        return ("", "", 0)

    with (
        patch.object(runtime_manager, "_ssh_run", AsyncMock(side_effect=_ssh)),
        patch.object(
            runtime_manager, "anchor_running", AsyncMock(return_value=False)
        ),
        patch.object(
            runtime_manager,
            "_start_runtime_impl",
            AsyncMock(return_value={"ok": True, "message": "gestartet"}),
        ),
    ):
        result = await runtime_manager.restart_runtime(_DUO)

    assert result["ok"] is True
    assert any("/opt/recipes/duo/stop.sh" in c for c in calls)
    assert not any("docker restart" in c for c in calls)


@pytest.mark.asyncio
async def test_duo_restart_aborts_when_stop_fails():
    stop = AsyncMock(return_value={"ok": False, "message": "Worker hängt"})
    start = AsyncMock()
    with (
        patch.object(runtime_manager, "stop_runtime", stop),
        patch.object(runtime_manager, "start_runtime", start),
    ):
        result = await runtime_manager.restart_runtime(_DUO)

    assert result["ok"] is False
    assert result["message"] == "Worker hängt"
    assert start.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["stop_command", "launch_command"])
async def test_duo_without_own_commands_refuses_with_clear_message(missing):
    runtime = {**_DUO, missing: ""}
    ssh = AsyncMock(return_value=("", "", 0))
    with patch.object(runtime_manager, "_ssh_run", ssh):
        result = await runtime_manager.restart_runtime(runtime)

    assert result["ok"] is False
    assert result["message"] == runtime_manager.MULTI_NODE_RESTART_UNSUPPORTED
    assert "Stop + Start" in result["message"]
    assert ssh.await_count == 0


# ── Endpunkt: Ereignis runtime.restart_multinode ─────────────────────────────


async def _mk_duo_rows(session):
    head = Host(slug="box-head", display_name="Box Head", kind="ssh", ssh_host="192.0.2.10")
    worker = Host(slug="box-worker", display_name="Box Worker", kind="ssh", ssh_host="192.0.2.11")
    session.add(head)
    session.add(worker)
    await session.commit()
    await session.refresh(head)
    await session.refresh(worker)

    rt = Runtime(
        slug="duo-endpoint-rt",
        display_name="Duo Endpoint",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/duo",
        enabled=True,
        host_id=head.id,
        container_name="engine_duo_head",
        stop_command="/opt/recipes/duo/stop.sh",
        launch_command="/opt/recipes/duo/start.sh",
        topology={"nodes": 2, "tp_total": 2, "roles": ["head", "worker"]},
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    session.add(RuntimeHost(runtime_id=rt.id, host_id=head.id, role="head", node_rank=0))
    session.add(RuntimeHost(runtime_id=rt.id, host_id=worker.id, role="worker", node_rank=1))
    await session.commit()
    return rt, head, worker


@pytest.mark.asyncio
async def test_describe_nodes_lists_head_once_and_workers(async_session):
    rt, head, worker = await _mk_duo_rows(async_session)

    nodes = await runtime_multinode.describe_nodes(async_session, rt)

    assert nodes["head"]["slug"] == head.slug
    assert [w["slug"] for w in nodes["workers"]] == [worker.slug]
    assert nodes["workers"][0]["role"] == "worker"
    assert nodes["workers"][0]["node_rank"] == 1


@pytest.mark.asyncio
async def test_restart_endpoint_emits_multinode_event(async_session, auth_client):
    rt, head, worker = await _mk_duo_rows(async_session)
    emitted: list[tuple] = []

    async def _capture(_session, event_type, message, **kwargs):
        emitted.append((event_type, message, kwargs))

    async def _ok(*_a, **_kw):
        return {"ok": True, "message": "stub"}

    with (
        patch(
            "app.services.runtime_manager.restart_runtime",
            new=AsyncMock(side_effect=_ok),
        ),
        patch("app.services.runtime_multinode.emit_event", new=AsyncMock(side_effect=_capture)),
    ):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/restart")

    assert resp.status_code == 200, resp.text
    types = [e[0] for e in emitted]
    assert "runtime.restart_multinode" in types
    event = next(e for e in emitted if e[0] == "runtime.restart_multinode")
    assert head.slug in event[1]
    assert worker.slug in event[1]
    detail = event[2]["detail"]
    assert detail["head"]["slug"] == head.slug
    assert [w["slug"] for w in detail["workers"]] == [worker.slug]


@pytest.mark.asyncio
async def test_restart_endpoint_stays_quiet_for_solo(async_session, auth_client):
    rt = Runtime(
        slug="solo-endpoint-rt",
        display_name="Solo Endpoint",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/solo",
        enabled=True,
        container_name="engine_solo_abc",
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    emitted: list[str] = []

    async def _capture(_session, event_type, _message, **_kwargs):
        emitted.append(event_type)

    async def _ok(*_a, **_kw):
        return {"ok": True, "message": "stub"}

    with (
        patch(
            "app.services.runtime_manager.restart_runtime",
            new=AsyncMock(side_effect=_ok),
        ),
        patch("app.services.runtime_multinode.emit_event", new=AsyncMock(side_effect=_capture)),
    ):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/restart")

    assert resp.status_code == 200, resp.text
    assert emitted == []


@pytest.mark.asyncio
async def test_restart_endpoint_treats_member_rows_as_verbund_without_topology(
    async_session, auth_client
):
    """Sicherheitsnetz: eine Zeile MIT Worker-Box, aber OHNE topology (von Hand
    angelegt) darf nicht als Solo durchrutschen — sonst wäre es wieder der
    halbe Neustart nur am Head."""
    rt, head, worker = await _mk_duo_rows(async_session)
    rt.topology = None
    async_session.add(rt)
    await async_session.commit()

    seen: list[dict] = []

    async def _capture_restart(runtime, **_kw):
        seen.append(runtime)
        return {"ok": True, "message": "stub"}

    async def _capture_event(_session, event_type, _message, **_kwargs):
        seen.append({"event": event_type})

    with (
        patch(
            "app.services.runtime_manager.restart_runtime",
            new=AsyncMock(side_effect=_capture_restart),
        ),
        patch(
            "app.services.runtime_multinode.emit_event",
            new=AsyncMock(side_effect=_capture_event),
        ),
    ):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/restart")

    assert resp.status_code == 200, resp.text
    passed = seen[0]
    assert runtime_manager.is_multi_node(passed) is True
    assert passed["topology"]["nodes"] == 2
    assert {"event": "runtime.restart_multinode"} in seen
