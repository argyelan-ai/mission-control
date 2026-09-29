"""Verbund-Helfer für den Neustart einer Multi-Node-Runtime (12.09.2026).

Eine Multi-Node-Runtime ("Verbund") läuft auf mehr als einer Box: der Head
steht in ``runtimes.host_id``, die Worker in ``runtime_hosts`` (siehe
``models/runtime_host.py``). Für den Lebenszyklus heisst das: ein
``docker restart`` am Head allein ist kein Neustart des Verbunds — der Worker
behält seine alte NCCL-Gruppe und der neue Head verhungert im Rendezvous.

Hier liegt nur, was der Router dafür aus der DB braucht: wer Head und wer
Worker ist, und das Ereignis, das den Neustart nachvollziehbar macht. Die
eigentliche Ausführung bleibt in ``runtime_manager`` (Stop über
``stop_command``, Start über ``launch_command``).
"""

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.services.activity import emit_event

logger = logging.getLogger("mc.runtime_multinode")


def _host_ref(host: Host | None) -> dict | None:
    if host is None:
        return None
    return {"id": str(host.id), "slug": host.slug, "display_name": host.display_name}


async def worker_host_ids(session: AsyncSession, runtime_id, head_host_id) -> list:
    """Die Boxen aus ``runtime_hosts``, die NICHT der Head sind.

    ``set_runtime_members`` schreibt den Head bewusst mit in die Tabelle, also
    wird er hier abgezogen — sonst zählte eine Solo-Zeile mit Head-Eintrag als
    Verbund.
    """
    rows = (
        await session.exec(
            select(RuntimeHost).where(RuntimeHost.runtime_id == runtime_id)
        )
    ).all()
    return [r.host_id for r in rows if head_host_id is None or r.host_id != head_host_id]


async def resolve_multi_node(
    session: AsyncSession, runtime: dict, runtime_id
) -> tuple[dict, bool]:
    """Ist diese Runtime ein Verbund — und weiss ihr Dict das auch?

    ``topology.nodes`` ist die erklärte Wahrheit und entscheidet normalerweise.
    ``runtime_hosts`` ist das Sicherheitsnetz: eine Zeile mit Worker-Box, deren
    ``topology`` nie gefüllt wurde (von Hand angelegt, oder aus der Zeit vor
    dem Feld), würde sonst als Solo behandelt — also genau der halbe Neustart,
    den dieser Pfad verhindern soll. In dem Fall bekommt das Dict die fehlende
    Topologie mit, damit der Lebenszyklus dieselbe Entscheidung trifft.
    """
    from app.services import runtime_manager

    if runtime_manager.is_multi_node(runtime):
        return runtime, True
    workers = await worker_host_ids(session, runtime_id, runtime.get("host_id"))
    if not workers:
        return runtime, False
    topology = dict(runtime.get("topology") or {})
    topology["nodes"] = len(workers) + 1
    logger.warning(
        "Runtime %s hat %s Worker-Box(en) in runtime_hosts, aber keine "
        "topology.nodes >= 2 — wird für den Neustart als Verbund behandelt.",
        runtime.get("slug"),
        len(workers),
    )
    return {**runtime, "topology": topology}, True


async def describe_nodes(session: AsyncSession, runtime: Runtime) -> dict:
    """Head + Worker-Boxen eines Verbunds, so wie das Ereignis sie zeigt.

    Der Head kommt aus ``runtime.host_id`` (die einzige Wahrheit dafür,
    ADR-048). Aus ``runtime_hosts`` kommen nur die ÜBRIGEN Boxen — eine Zeile,
    die den Head dort noch einmal nennt, wird übersprungen, damit er nicht
    doppelt in der Liste steht.
    """
    head = await session.get(Host, runtime.host_id) if runtime.host_id else None
    rows = (
        await session.exec(
            select(RuntimeHost, Host)
            .join(Host, RuntimeHost.host_id == Host.id)  # type: ignore[arg-type]
            .where(RuntimeHost.runtime_id == runtime.id)
            .order_by(RuntimeHost.node_rank)  # type: ignore[arg-type]
        )
    ).all()
    workers: list[dict] = []
    for membership, member_host in rows:
        if runtime.host_id is not None and member_host.id == runtime.host_id:
            continue
        ref = _host_ref(member_host)
        assert ref is not None
        workers.append({**ref, "role": membership.role, "node_rank": membership.node_rank})
    return {"head": _host_ref(head), "workers": workers}


async def emit_restart_multinode(
    session: AsyncSession, runtime: Runtime, nodes: dict
) -> None:
    """Ereignis ``runtime.restart_multinode`` — Head und Worker namentlich.

    Bewusst eigenes Ereignis statt eines stillen Restarts: wer später fragt
    „warum war der Verbund 5 Minuten weg?", sieht hier, dass BEIDE Boxen neu
    hochgefahren sind, und nicht nur der Head angefasst wurde.
    """
    head = (nodes.get("head") or {}).get("slug") or "?"
    worker_slugs = [w.get("slug") or "?" for w in nodes.get("workers") or []]
    workers = ", ".join(worker_slugs) or "—"
    await emit_event(
        session,
        "runtime.restart_multinode",
        f"{runtime.slug}: Verbund neu gestartet (Head {head}, Worker {workers})",
        severity="info",
        detail={
            "runtime_id": str(runtime.id),
            "slug": runtime.slug,
            "head": nodes.get("head"),
            "workers": nodes.get("workers") or [],
        },
    )

