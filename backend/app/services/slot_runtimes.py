"""Slot-Runtimes — „ein Agent hängt an der Box, nicht am Rezept" (ADR-078).

Das Problem in einem Satz
-------------------------
Auf einer Head-Box hören ALLE Rezepte auf derselben URL (Konvention: Port
8000). Der Rezept-Umschalter tauscht das Modell dahinter. Ein Agent, der an der
Runtime-Zeile eines BESTIMMTEN Rezepts hängt, fragt danach weiter den alten
Modellnamen an — die Engine kennt ihn nicht mehr und antwortet 404. Bis heute
half nur ein Agenten-Runtime-Switch (Container-Neustart), teuer und fragil.

Die Lösung
----------
Je Head-Box EINE ankerlose Slot-Zeile (``runtimes.is_slot = true``). Agenten
hängen dort. Der Drift-Wächter schreibt in diese Zeile, was die Box gerade
serviert (``runtime_watcher._served_answer_is_own`` lässt ankerlose Zeilen der
Engine bewusst folgen: „drift IS the feature"), und der Umschalter schreibt
Modell + Fenster sofort nach einem erfolgreichen Start hinein, damit niemand
auf zwei Wächter-Proben warten muss.

Was dieses Modul macht
----------------------
* :func:`ensure_slot_runtimes` — beim Backend-Start (Lifespan), idempotent:
  legt fehlende Slot-Zeilen an und hängt die Agenten der Box darauf um.
* :func:`find_slot_runtime` / :func:`slot_display_name` / :func:`write_slot_state`
  — die Helfer, die Umschalter und Wächter benutzen.

Generisch, keine Gerätedaten
----------------------------
Welche Boxen ein Betreiber hat, steht in SEINER Datenbank. Dieses Modul liest
``hosts`` und ``runtimes`` und leitet alles daraus ab — im Repo steht kein
einziger Hostname, keine IP, kein Rezeptname (Regel 7, ADR-077).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.runtime import Runtime

logger = logging.getLogger("mc.slot_runtimes")

#: Der Suffix, an dem eine Slot-Zeile auch im Log erkennbar ist. Die WAHRHEIT
#: ist immer ``runtimes.is_slot`` — der Slug ist nur ein lesbarer Name.
SLOT_SLUG_SUFFIX = "-slot"

#: Der Port, den eine Box ohne jede laufende Instanz bekommt. 8000 ist der
#: Port, den vLLM und die üblichen Rezept-Wrapper ohne Angabe öffnen (dieselbe
#: Annahme wie ``recipe_switcher.DEFAULT_PORT``).
DEFAULT_SLOT_PORT = 8000

#: Runtime-Typen, die MC über einen BEFEHL auf einer Box startet. Eine Box, auf
#: der mindestens eine solche Zeile hängt, ist eine Box, auf der Rezepte
#: wechseln — also eine Box, die eine Slot-Zeile braucht.
COMMAND_DRIVEN_TYPES: frozenset[str] = frozenset(
    {"vllm_docker", "llamacpp_docker", "ssh_process"}
)

#: Die Agenten-Art, die ihren Provider aus der Runtime-Zeile bezieht (Container
#: mit gerendertem ``OPENAI_BASE_URL``/``OPENAI_MODEL``). Cloud-Harnesses
#: (claude, kimi, grok) hängen an eigenen Zeilen und werden nie angefasst.
REBINDABLE_AGENT_RUNTIME = "cli-bridge"


def slot_slug(host_slug: str) -> str:
    """``<box>-slot``, auf 64 Zeichen gekappt (Spaltenbreite von ``runtimes.slug``)."""
    base = (host_slug or "box").strip()
    return f"{base[: 64 - len(SLOT_SLUG_SUFFIX)]}{SLOT_SLUG_SUFFIX}"


def slot_display_name(host: Host, endpoint: str, model: str | None) -> str:
    """„<Box> :8000 (aktuell: <Modell>)" — der Name, den die Oberfläche zeigt.

    Bewusst SERVERSEITIG zusammengesetzt und an jeder Stelle neu gebildet, an
    der sich das Modell der Zeile ändert (Anlegen, Umschalter-Start,
    Wächter-Drift). Ein Name, der einmal geschrieben und nie wieder angefasst
    wird, wäre nach dem ersten Rezeptwechsel eine Lüge — und genau solche
    Lügen sollte ADR-078 abschaffen.
    """
    port = None
    try:
        port = urlsplit(endpoint).port
    except ValueError:
        port = None
    label = f"{host.display_name or host.slug} :{port or DEFAULT_SLOT_PORT}"
    if model:
        return f"{label} (aktuell: {model})"
    return f"{label} (aktuell: kein Modell)"


def _host_address(host: Host) -> str | None:
    """Die Adresse, unter der MC die Box fragt.

    ``ssh_host`` zuerst (das ist die Adresse, über die MC ohnehin startet und
    stoppt), ``tailscale_host`` als Rückfall. Beides leer = die Box hat keine
    Adresse, für die eine Slot-Zeile Sinn ergäbe.
    """
    return (host.ssh_host or "").strip() or (host.tailscale_host or "").strip() or None


async def find_slot_runtime(
    session: AsyncSession, host_id: uuid.UUID | None
) -> Runtime | None:
    """Die Slot-Zeile EINER Box, oder ``None``.

    Gesucht wird über ``is_slot`` + ``host_id``, nie über den Slug — der Slug
    ist Kosmetik, das Kennzeichen ist die Wahrheit.
    """
    if host_id is None:
        return None
    return (
        await session.exec(
            select(Runtime).where(
                Runtime.is_slot == True,  # noqa: E712
                Runtime.host_id == host_id,
            )
        )
    ).first()


async def write_slot_state(
    session: AsyncSession,
    host_id: uuid.UUID | None,
    *,
    model: str | None,
    context_len: int | None = None,
) -> Runtime | None:
    """Modell (+ Fenster) der Slot-Zeile einer Box sofort setzen.

    Warum „sofort" und nicht „der Wächter macht das schon": der Wächter
    bestätigt eine Drift erst nach ZWEI gleichen Proben
    (``runtime_watcher._handle_drift``) — bei einem 90-s-Takt bis zu drei
    Minuten, in denen jeder Agent den alten Modellnamen anfragt und 404
    bekommt. Der Umschalter weiss das Ziel schon vorher; also schreibt er es.

    Gibt die geänderte Zeile zurück (oder ``None``, wenn die Box keine hat).
    """
    slot = await find_slot_runtime(session, host_id)
    if slot is None:
        return None
    host = await session.get(Host, host_id)
    changed = False
    if model and slot.model_identifier != model:
        slot.model_identifier = model
        changed = True
    if context_len and slot.max_context_len != context_len:
        slot.max_context_len = context_len
        # ``preferred`` folgt nur, wo es „nimm das ganze Fenster" ausdrückte
        # oder das neue Maximum überschreiten würde — dieselbe Regel wie in
        # runtime_watcher._handle_context_drift.
        if (
            slot.preferred_context_len is None
            or slot.preferred_context_len > context_len
        ):
            slot.preferred_context_len = context_len
        changed = True
    if host is not None:
        name = slot_display_name(host, slot.endpoint, slot.model_identifier)
        if slot.display_name != name:
            slot.display_name = name
            changed = True
    if changed:
        session.add(slot)
        await session.commit()
        await session.refresh(slot)
        from app.services.runtime_model_resolver import invalidate_cached_model

        try:
            await invalidate_cached_model(slot.slug)
        except Exception:  # noqa: BLE001 — ein Cache darf den Start nicht kosten
            logger.debug("slot: Cache-Invalidierung für %s fehlgeschlagen", slot.slug)
    return slot


async def refresh_slot_display_name(session: AsyncSession, slot: Runtime) -> None:
    """Den „(aktuell: …)"-Teil nachziehen, nachdem der Wächter gedriftet ist.

    Best effort: eine kosmetische Zeile darf niemals eine Wächter-Runde kosten.
    """
    if not slot.is_slot or slot.host_id is None:
        return
    host = await session.get(Host, slot.host_id)
    if host is None:
        return
    name = slot_display_name(host, slot.endpoint, slot.model_identifier)
    if name != slot.display_name:
        slot.display_name = name
        session.add(slot)
        await session.commit()


# ── Der Start-Lauf ───────────────────────────────────────────────────────────


def _needs_slot(host: Host, host_runtimes: list[Runtime]) -> bool:
    """Braucht diese Box eine Slot-Zeile?

    Zwei Wege, beide vom Betreiber gesetzt und keiner geraten:
      * die Box ist ausdrücklich ein Head (``hosts.role == "head"``), oder
      * es hängt mindestens eine befehlsgetriebene Runtime an ihr — dann
        wechseln dort Rezepte, auch wenn niemand je eine Rolle vergeben hat.
    """
    if (host.role or "").strip().lower() == "head":
        return True
    return any(rt.runtime_type in COMMAND_DRIVEN_TYPES for rt in host_runtimes)


def _slot_endpoint(host: Host, host_runtimes: list[Runtime]) -> str | None:
    """Die URL, unter der die Box serviert.

    Erste Wahl ist der Endpunkt einer eingeschalteten befehlsgetriebenen
    Instanz der Box — das ist die Adresse, die dort NACHWEISLICH schon benutzt
    wird (inklusive Port und richtiger Adressart; eine LAN-Adresse statt einer
    Tailscale-Adresse hat uns schon einmal einen halben Tag gekostet). Erst
    wenn es die nicht gibt, wird aus der Box-Adresse + Standard-Port eine
    gebaut.
    """
    for rt in host_runtimes:
        if rt.enabled and rt.runtime_type in COMMAND_DRIVEN_TYPES and rt.endpoint:
            return rt.endpoint
    for rt in host_runtimes:
        if rt.runtime_type in COMMAND_DRIVEN_TYPES and rt.endpoint:
            return rt.endpoint
    address = _host_address(host)
    if not address:
        return None
    return f"http://{address}:{DEFAULT_SLOT_PORT}/v1"


def _current_model(host_runtimes: list[Runtime]) -> tuple[str | None, int | None]:
    """Modell + Fenster der eingeschalteten Instanz der Box — als Startwert.

    Nur ein Startwert: ab der ersten Probe hat der Wächter das letzte Wort.
    """
    for rt in host_runtimes:
        if rt.enabled and rt.runtime_type in COMMAND_DRIVEN_TYPES and rt.model_identifier:
            return rt.model_identifier, rt.max_context_len
    return None, None


async def _unique_slug(session: AsyncSession, base: str) -> str:
    taken = set((await session.exec(select(Runtime.slug))).all())
    candidate = base
    counter = 2
    while candidate in taken:
        suffix = f"-{counter}"
        candidate = base[: 64 - len(suffix)] + suffix
        counter += 1
    return candidate


async def ensure_slot_runtimes(session: AsyncSession) -> dict[str, Any]:
    """Slot-Zeilen anlegen und Agenten umhängen — idempotent (Lifespan).

    Läuft bei JEDEM Backend-Start, wie ``repair_legacy_sparkrun_rows``. Beim
    zweiten Lauf passiert nichts mehr: eine vorhandene Slot-Zeile wird nicht
    neu angelegt, und ein Agent, der schon an ihr hängt, wird nicht angefasst.

    Umgehängt wird ein Agent NUR, wenn alle vier Bedingungen stimmen:
      1. ``agent_runtime == "cli-bridge"`` (er bezieht seinen Provider aus der
         Runtime-Zeile — Cloud-/Kimi-/Claude-Agenten tun das nicht),
      2. er hängt an einer host-gebundenen, befehlsgetriebenen Runtime DIESER
         Box (auch an einer mit ``enabled = false`` — genau die stillgelegten
         Rezept-Zeilen sind der Grund für diesen Umbau),
      3. sein Harness verträgt das Protokoll der Slot-Zeile
         (``harness_compat.is_compatible`` — ein claude-Agent landet nie an
         einer OpenAI-Zeile),
      4. die Box hat überhaupt eine Slot-Zeile.

    Der Rückweg steht in ``backend/scripts/slot_rollback.py``: die alte
    Bindung wird je Agent als Aktivitäts-Ereignis festgehalten
    (``agent.slot_rebound`` mit ``previous_runtime_slug``), damit
    Zurückhängen ein Befehl ist und keine Handarbeit.

    Alles best effort im Aufrufer: ein Fehler hier darf den Backend-Start
    nicht verhindern (siehe ``main._ensure_slot_runtimes``).
    """
    from app.services.activity import emit_event
    from app.services.harness_compat import derive_harness, is_compatible

    hosts = list((await session.exec(select(Host).where(Host.enabled == True))).all())  # noqa: E712
    runtimes = list((await session.exec(select(Runtime))).all())
    by_host: dict[uuid.UUID, list[Runtime]] = {}
    for rt in runtimes:
        if rt.host_id is not None and not rt.is_slot:
            by_host.setdefault(rt.host_id, []).append(rt)

    created: list[str] = []
    rebound: list[str] = []
    skipped_no_endpoint: list[str] = []

    for host in hosts:
        host_runtimes = by_host.get(host.id, [])
        if not _needs_slot(host, host_runtimes):
            continue
        slot = await find_slot_runtime(session, host.id)
        if slot is None:
            endpoint = _slot_endpoint(host, host_runtimes)
            if not endpoint:
                # Ohne Adresse wäre die Zeile ein Zeiger ins Nichts — und die
                # Agenten daran hätten gar kein Modell mehr.
                skipped_no_endpoint.append(host.slug)
                continue
            model, ctx = _current_model(host_runtimes)
            slot = Runtime(
                slug=await _unique_slug(session, slot_slug(host.slug)),
                display_name="",  # gleich unten aus Box + Endpunkt + Modell
                # Vertrag ADR-078: openai_compatible ist der einzige Typ, für
                # den der Drift-Wächter eine ankerlose Zeile bedenkenlos der
                # Engine folgen lässt (_served_answer_is_own else-Zweig) — und
                # der einzige, für den routers/internal.py zusammen mit einer
                # gesetzten host_id die langen omp-Zeitgeber rendert.
                runtime_type="openai_compatible",
                endpoint=endpoint,
                model_identifier=model,
                max_context_len=ctx,
                preferred_context_len=ctx,
                # KEIN Anker, KEIN Startbefehl: diese Zeile wird nie gestartet,
                # gestoppt oder wiederbelebt — sie zeigt nur, was läuft.
                container_name=None,
                process_name=None,
                launch_command=None,
                stop_command=None,
                exclusive_memory=False,
                autostart_supported=False,
                host_id=host.id,
                is_slot=True,
                enabled=True,
                supports_tools=True,
                supports_streaming=True,
                supports_reasoning=True,
                ui_order=0,
            )
            slot.display_name = slot_display_name(host, endpoint, model)
            session.add(slot)
            await session.commit()
            await session.refresh(slot)
            created.append(slot.slug)
            logger.info("slot: Zeile %s für Box %s angelegt (%s)", slot.slug, host.slug, endpoint)

        recipe_runtime_ids = {
            rt.id for rt in host_runtimes if rt.runtime_type in COMMAND_DRIVEN_TYPES
        }
        if not recipe_runtime_ids:
            continue
        agents = list(
            (
                await session.exec(
                    select(Agent).where(Agent.runtime_id.in_(recipe_runtime_ids))
                )
            ).all()
        )
        runtime_by_id = {rt.id: rt for rt in host_runtimes}
        for agent in agents:
            if agent.agent_runtime != REBINDABLE_AGENT_RUNTIME:
                continue
            harness = agent.harness or derive_harness(runtime_by_id.get(agent.runtime_id))
            if not is_compatible(harness, slot):
                logger.info(
                    "slot: %s bleibt an seiner Zeile — Harness %r passt nicht zu %s",
                    agent.name, harness, slot.slug,
                )
                continue
            previous = runtime_by_id.get(agent.runtime_id)
            agent.runtime_id = slot.id
            if slot.model_identifier:
                agent.model = slot.model_identifier
            # Der Container muss sein gerendertes Modell neu holen — dieselbe
            # Fahne, die der Drift-Wächter setzt.
            agent.pending_runtime_sync = True
            session.add(agent)
            await session.commit()
            rebound.append(agent.name)
            try:
                await emit_event(
                    session,
                    "agent.slot_rebound",
                    f"{agent.name}: hängt jetzt an der Box-Zeile {slot.slug} "
                    f"statt am Rezept {previous.slug if previous else 'n/a'}",
                    severity="info",
                    agent_id=agent.id,
                    detail={
                        "agent_name": agent.name,
                        "slot_runtime_slug": slot.slug,
                        "previous_runtime_slug": previous.slug if previous else None,
                        "previous_runtime_id": str(previous.id) if previous else None,
                    },
                )
            except Exception:  # noqa: BLE001 — Buchhaltung darf den Start nicht kosten
                logger.exception("slot: Ereignis für %s fehlgeschlagen", agent.name)

    return {
        "created": created,
        "rebound": rebound,
        "skipped_no_endpoint": skipped_no_endpoint,
    }
