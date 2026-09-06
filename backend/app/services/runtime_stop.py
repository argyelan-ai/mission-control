"""Stop ↔ Autostart coupling + Dispatch-Gate (PR 2, Buehne v2 Spec §5/§7).

Zwei unabhaengige Dinge, die der Router beim Stoppen einer host-gebundenen
Runtime braucht — beide sind reine DB/Redis-Operationen, kein SSH, damit sie
in pytest ohne echte Boxen testbar bleiben:

1. ``check_dispatch_gate`` — LAEUFT VOR dem eigentlichen Stop. Ein Agent, der
   gerade an dieser Runtime (oder ihrer Slot-Zeile auf derselben Box, Duo)
   dispatcht, darf nicht mitten im Zug gekappt werden. ``force=True``
   uebersteuert bewusst.
2. ``apply_stop_autostart_coupling`` — LAEUFT NACH einem ERFOLGREICHEN Stop.
   War ``hosts.autostart_enabled`` an, geht es aus — sonst holt der Waechter
   (``runtime_watcher._maybe_auto_recover``) das gerade gestoppte Modell
   innerhalb von 15 Minuten zurueck (das war exakt der Vorfall, der den
   Skill ``mc-runtime-pause`` noetig machte).

Bei Duo (Head+Worker) haengt Autostart an der Box, auf der die Runtime-Zeile
selbst sitzt (``runtime.host_id``) — das ist immer die Head-Box, weil der
Katalog-Switcher die Duo-Instanz dort anlegt (P3, ``recipe_switcher``).
"""

import logging
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.runtime import Runtime
from app.services.activity import emit_event

logger = logging.getLogger("mc.runtime_stop")


async def find_busy_agents(
    session: AsyncSession, runtime_id: uuid.UUID, host_id: uuid.UUID | None
) -> list[Agent]:
    """Agents dispatching against this runtime, or against the Slot-Zeile of
    the same host (Duo: Agents binden auf die Slot-Runtime, nicht auf die
    Rezept-Instanz selbst — ADR-078)."""
    runtime_ids: set[uuid.UUID] = {runtime_id}
    if host_id is not None:
        slot_ids = (
            await session.exec(
                select(Runtime.id).where(
                    Runtime.host_id == host_id, Runtime.is_slot == True  # noqa: E712
                )
            )
        ).all()
        runtime_ids.update(slot_ids)
    agents = (
        await session.exec(
            select(Agent).where(
                Agent.runtime_id.in_(runtime_ids),  # type: ignore[union-attr]
                Agent.current_task_id.is_not(None),
            )
        )
    ).all()
    return list(agents)


def busy_agents_detail(agents: list[Agent]) -> list[dict]:
    return [
        {"name": a.name, "slug": a.slug, "task_id": str(a.current_task_id)}
        for a in agents
    ]


async def emit_stop_forced(session: AsyncSession, runtime: Runtime, agents: list[Agent]) -> None:
    names = ", ".join(a.name for a in agents) or "—"
    await emit_event(
        session,
        "runtime.stop_forced",
        f"{runtime.slug}: Stop erzwungen trotz laufender Arbeit ({names})",
        severity="warning",
        detail={
            "runtime_id": str(runtime.id),
            "slug": runtime.slug,
            "agents": busy_agents_detail(agents),
        },
    )


async def apply_stop_autostart_coupling(
    session: AsyncSession, host_id: uuid.UUID | None
) -> dict:
    """Nach erfolgreichem Stop: Autostart der Box ausschalten, wenn es an war.

    Gibt immer ein Dict zurueck (auch wenn nichts zu tun war), damit der
    Router es unbesehen in die Antwort mergen kann.
    """
    if host_id is None:
        return {"autostart_disabled": False, "host_slug": None}
    host = await session.get(Host, host_id)
    if host is None:
        return {"autostart_disabled": False, "host_slug": None}
    if not host.autostart_enabled:
        return {"autostart_disabled": False, "host_slug": host.slug}
    host.autostart_enabled = False
    session.add(host)
    await session.commit()
    await emit_event(
        session,
        "host.autostart_disabled_by_stop",
        f"{host.slug}: Autostart ausgeschaltet, weil das Modell manuell gestoppt wurde",
        severity="info",
        detail={"host_id": str(host.id), "slug": host.slug},
    )
    return {"autostart_disabled": True, "host_slug": host.slug}
