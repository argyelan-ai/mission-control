"""Rezept-Umschalter — Backend-Logik hinter ``GET/POST /api/v1/hosts/{id}/recipes``
(Vertrag P0+P1, 02.09.2026: docs/plans/2026-09-02-rezept-umschalter-vertrag.md).

Warum es dieses Modul gibt
--------------------------
Bisher gab es ZWEI Wege, ein Rezept auf eine Box zu bringen: den Box-Wizard
(Katalog → ``POST /runtimes`` → ``POST /runtimes/{id}/start``) und den
sparkrun-Sonderweg (Rezeptliste per SSH vom Wrapper → eigener Wechsel-Endpunkt). Der
zweite Weg hing an einem einzigen Werkzeug und einer einzigen Gerätefamilie —
für andere MC-Nutzer war er wertlos. Der Vertrag macht daraus EIN Modell:
Ein Rezept = Engine · Startbefehl · Port · Topologie (Anzahl Boxen). Ein
sparkrun-Rezept ist damit nur noch ein gewöhnlicher Startbefehl.

Was hier passiert
-----------------
* :func:`list_host_recipes` — die eine Quelle für beide Umschalter in der
  Oberfläche. Das Backend rechnet ``fit`` / ``startable`` / ``reason`` fertig
  aus; das Frontend rechnet NICHTS nach (das war die Wurzel der alten
  Uneinigkeit zwischen Frontend und Backend beim Runtime-Wechsel).
* :func:`start_recipe_on_host` — komponiert NUR bestehende Logik: Instanz aus
  dem Katalog anlegen (dieselben Felder wie der Box-Wizard), dann
  ``runtime_manager.start_runtime`` (SSH, ``nohup bash -lc``, Verifikation
  über das Label ``mc.runtime.slug``, ``exclusive_memory``-Verdrängung).
  Kein zweiter Lebenszyklus.

Was hier bewusst NICHT passiert (P3)
-----------------------------------
Zweibox-Start (``nodes=2`` → 409 mit Satz), Schreibpfad ``runtime_hosts``,
Worker-Wahl. ``candidate_workers`` (seit P2 mit Rolle, Worker zuerst) wird
schon geliefert, damit P3 die Liste nicht neu erfinden muss.

Gründe (``reason``) sind SÄTZE in einfacher Sprache, keine Codes — die
Oberfläche zeigt sie unverändert an. Das Backend hat keine i18n-Schicht für
API-Antworten (die Detail-Texte der Router sind heute schon deutsch), darum
stehen die Sätze hier als Konstanten an einer Stelle.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.services import launch_template
from app.services.host_resolver import ResolvedHost, resolved_host_from_row, ssh_capable

logger = logging.getLogger("mc.recipe_switcher")

# ── Grau-Gründe als Sätze (Vertrag: "Sätze, keine Codes") ────────────────────
REASON_NO_COMMAND = "Startbefehl fehlt"
REASON_NO_SECOND_BOX = "braucht 2 Boxen — keine freie zweite Box"
REASON_RUNNING = "läuft bereits auf dieser Box"
REASON_NO_SSH = "Box hat keinen SSH-Zugang — MC kann hier nichts starten"
REASON_DUO_PHASE3 = "Zweibox-Start kommt in Phase 3"
REASON_RECIPE_HIDDEN = "Rezept ist ausgeblendet"


def reason_port_busy(port: int, blocker_name: str) -> str:
    return f"Port {port} auf dieser Box belegt durch {blocker_name}"


#: Standard-Port, wenn weder Katalog noch Instanz einen nennen. 8000 ist der
#: Port, den vLLM und die üblichen Rezept-Wrapper ohne Angabe öffnen — ein
#: Rezept mit anderem Port trägt ihn im Katalog (``local_recipes.port``).
DEFAULT_PORT = 8000

#: Startbefehl-Pflicht gilt nur für Runtimes, die MC selbst über einen
#: Befehl startet. LM Studio startet über ``lms load``, Cloud-Runtimes gar
#: nicht — für die wäre ein Pflichtfeld eine Lüge.
COMMAND_DRIVEN_RUNTIME_TYPES: frozenset[str] = frozenset(
    {"vllm_docker", "llamacpp_docker", "ssh_process"}
)


class RecipeStartError(Exception):
    """Ein Start, der ehrlich abgelehnt wird. ``status`` ist der HTTP-Code,
    ``detail`` der Satz für die Oberfläche."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── Kleine, reine Helfer (einzeln testbar) ───────────────────────────────────


def recipe_nodes(topology: dict[str, Any] | None) -> int:
    """Anzahl Boxen aus ``topology``. NULL/kaputt = 1 (Solo), wie heute."""
    try:
        nodes = int((topology or {}).get("nodes", 1))
    except (TypeError, ValueError):
        return 1
    return nodes if nodes >= 1 else 1


def endpoint_port(endpoint: str | None) -> int | None:
    """Port aus einer Endpoint-URL, oder None wenn keiner drinsteht."""
    if not endpoint:
        return None
    try:
        return urlsplit(endpoint).port
    except ValueError:
        return None


def recipe_matches_runtime(recipe: LocalRecipe, runtime: Runtime) -> bool:
    """Ist diese Runtime eine Instanz dieses Rezepts?

    Drei Signale, vom stärksten zum schwächsten:
      1. ``runtimes.topology.recipe_slug`` — der explizite Verweis, den
         :func:`build_runtime_from_recipe` beim Anlegen setzt.
      2. ``recipe_ref`` steht im ``launch_command`` — so sehen die schon
         vorhandenen sparkrun-Runtimes aus (Vertrag: bleiben unverändert).
      3. gleicher ``model_identifier`` — dasselbe Signal, das die Katalog-
         seite für ihr „läuft"-Badge schon immer benutzt hat.
    """
    topology = runtime.topology if isinstance(runtime.topology, dict) else {}
    if topology.get("recipe_slug") == recipe.slug:
        return True
    ref = (recipe.recipe_ref or "").strip()
    if ref and runtime.launch_command and ref in runtime.launch_command:
        return True
    model = (recipe.model_identifier or "").strip().lower()
    return bool(model) and (runtime.model_identifier or "").strip().lower() == model


def has_launch_command(recipe: LocalRecipe) -> bool:
    """Kann aus diesem Katalogeintrag ein Startbefehl entstehen?

    Entweder bringt er ein eigenes ``launch_template`` mit, oder seine Engine
    hat einen Standard (``launch_template.DEFAULT_TEMPLATES``). ssh_process
    hat keinen Standard: wie eine Host-Engine startet, weiss nur sie selbst.
    """
    if (recipe.launch_template or "").strip():
        return True
    return recipe.engine in launch_template.DEFAULT_TEMPLATES


def host_can_ssh(host: Host) -> bool:
    """MC startet über SSH — ohne SSH-Zugang kann es hier nichts starten.

    P2: eine ``kind=agent``-Box mit ``ssh_host`` zählt (host_resolver.ssh_capable
    ist die eine Regel dafür).
    """
    return ssh_capable(host)


def recipe_is_exclusive(recipe: LocalRecipe) -> bool:
    """Belegt dieses Rezept die Box exklusiv?

    P2: Das Katalogfeld ``exclusive`` ist die Wahrheit, sobald es gesetzt ist.
    Die Heuristik „min_vram_gb gesetzt → exklusiv" bleibt NUR der Fallback
    für Rezepte, die dazu nichts sagen (alle Zeilen von vor P2).
    """
    if recipe.exclusive is not None:
        return bool(recipe.exclusive)
    return recipe.min_vram_gb is not None


def recipe_claims_box(recipe: LocalRecipe, instance: Runtime | None) -> bool:
    """Braucht dieses Rezept die Box exklusiv? Bei einer vorhandenen Instanz
    entscheidet deren Flag; sonst dieselbe Regel wie beim Anlegen."""
    if instance is not None:
        return bool(instance.exclusive_memory)
    return recipe_is_exclusive(recipe)


def worker_candidates(hosts: list[Host], head: Host, exclusive_busy: set[uuid.UUID]) -> list[dict[str, Any]]:
    """Freie Boxen als mögliche Worker — mit Rolle aus ``hosts.role``.

    Reihenfolge (Vertrag P2): Boxen mit ``role=worker`` zuerst, dann die
    übrigen; innerhalb stabil nach ``ui_order``/``slug``. Die Rolle ist nur
    eine Vorbelegung — eine Head-Box steht trotzdem in der Liste, sie
    kommt nur später.
    """
    free = [h for h in hosts if h.id != head.id and h.id not in exclusive_busy]
    free.sort(key=lambda h: (0 if h.role == "worker" else 1, h.ui_order, h.slug))
    return [{"host_id": str(h.id), "slug": h.slug, "role": h.role} for h in free]


async def probe_running(runtime: Runtime) -> bool:
    """Ist die Instanz gesund? Live-HTTP-Probe am Head, nie geraten.

    Eigene Funktion, damit Tests sie ersetzen können, ohne ins Netz zu gehen.
    """
    from app.services import runtime_manager

    return await runtime_manager._probe_http(  # noqa: SLF001 — dieselbe Probe wie get_runtime_state
        runtime.endpoint, runtime.healthcheck_path or "/v1/models"
    )


# ── Die Liste ────────────────────────────────────────────────────────────────


async def _load_fleet(
    session: AsyncSession,
) -> tuple[list[LocalRecipe], list[Host], list[Runtime], dict[uuid.UUID, list[RuntimeHost]]]:
    recipes = list(
        (await session.exec(select(LocalRecipe).where(LocalRecipe.enabled == True))).all()  # noqa: E712
    )
    hosts = list((await session.exec(select(Host).where(Host.enabled == True))).all())  # noqa: E712
    runtimes = list(
        (
            await session.exec(
                select(Runtime).where(
                    Runtime.enabled == True,  # noqa: E712
                    Runtime.host_id.is_not(None),
                )
            )
        ).all()
    )
    members: dict[uuid.UUID, list[RuntimeHost]] = {}
    if runtimes:
        rows = (
            await session.exec(
                select(RuntimeHost).where(RuntimeHost.runtime_id.in_([rt.id for rt in runtimes]))
            )
        ).all()
        for row in rows:
            members.setdefault(row.runtime_id, []).append(row)
    return recipes, hosts, runtimes, members


async def list_host_recipes(session: AsyncSession, host: Host) -> list[dict[str, Any]]:
    """Alle freigegebenen Rezepte aus Sicht EINER Box — exakt das Vertrags-Schema.

    Belegung kommt aus ``runtimes.host_id`` (Head) + ``runtime_hosts``
    (Mitglieder) + einer laufenden Health-Probe je Instanz. Alle Probes laufen
    parallel; eine Box mit 20 Runtimes wartet 5 s, nicht 100.
    """
    recipes, hosts, runtimes, members = await _load_fleet(session)
    host_slug_by_id = {h.id: h.slug for h in hosts}

    probes = await asyncio.gather(*(probe_running(rt) for rt in runtimes), return_exceptions=True)
    running_by_id: dict[uuid.UUID, bool] = {
        rt.id: (result is True) for rt, result in zip(runtimes, probes)
    }

    def _hosts_of(rt: Runtime) -> list[uuid.UUID]:
        ids = [rt.host_id] if rt.host_id else []
        ids.extend(m.host_id for m in members.get(rt.id, []))
        return ids

    # Welche Box wird gerade von welcher laufenden Runtime belegt?
    occupied: dict[uuid.UUID, list[Runtime]] = {}
    for rt in runtimes:
        if not running_by_id[rt.id]:
            continue
        for hid in _hosts_of(rt):
            occupied.setdefault(hid, []).append(rt)

    exclusive_busy = {
        hid for hid, rts in occupied.items() if any(rt.exclusive_memory for rt in rts)
    }
    # Wirklich freie Boxen (Worker-Rolle zuerst, P2) — für einen NEUEN Start.
    free_workers = worker_candidates(hosts, host, exclusive_busy)
    ssh_ok = host_can_ssh(host)

    entries: list[dict[str, Any]] = []
    for recipe in recipes:
        matching = [rt for rt in runtimes if recipe_matches_runtime(recipe, rt)]
        instance = next((rt for rt in matching if rt.host_id == host.id), None)
        running = bool(instance is not None and running_by_id[instance.id])

        busy_hosts = sorted(
            {
                host_slug_by_id[hid]
                for rt in matching
                if running_by_id[rt.id]
                for hid in _hosts_of(rt)
                if hid in host_slug_by_id
            }
        )

        nodes = recipe_nodes(recipe.topology)
        reason: str | None = None
        own_busy = {hid for rt in matching if running_by_id[rt.id] for hid in _hosts_of(rt)}
        # Kandidaten = wirklich freie Boxen (für einen NEUEN Start, P3). Für die
        # Frage „passt das Rezept auf dieses Paar?" zählt zusätzlich die Box,
        # die das EIGENE laufende Duo belegt — sonst stünde ein laufender
        # Verbund als „keine freie zweite Box" da (Live-Befund 02.09.).
        candidate_workers = free_workers if nodes >= 2 else []
        own_other_boxes = own_busy - {host.id}
        if nodes >= 2:
            fit = "duo" if (candidate_workers or own_other_boxes) else "none"
            if fit == "none":
                reason = REASON_NO_SECOND_BOX
        else:
            fit = "solo"

        command_ok = bool((instance.launch_command or "").strip()) if instance else has_launch_command(recipe)
        if not command_ok:
            reason = REASON_NO_COMMAND
        startable = command_ok and fit != "none"
        if running:
            # Ein laufendes Rezept ist gesetzt, nicht startbar — der Grund sagt
            # das, statt „keine freie Box" oder „Port belegt" zu behaupten.
            startable = False
            reason = REASON_RUNNING

        # Port-Kollision auf DIESER Box: nur ein Blocker, den der Start nicht
        # selbst wegräumt, zählt. Eine exklusive Runtime wird von einem
        # exklusiven Start verdrängt (ensure_exclusive_host) — das ist der
        # normale Rezept-WECHSEL, keine Kollision.
        port = recipe.port
        if startable and not running and port is not None:
            claims = recipe_claims_box(recipe, instance)
            for other in occupied.get(host.id, []):
                if instance is not None and other.id == instance.id:
                    continue
                if endpoint_port(other.endpoint) != port:
                    continue
                if claims and other.exclusive_memory:
                    continue
                startable = False
                reason = reason_port_busy(port, other.display_name)
                break

        if startable and not ssh_ok:
            startable = False
            reason = REASON_NO_SSH

        entries.append(
            {
                "slug": recipe.slug,
                "display_name": recipe.display_name,
                "engine": recipe.engine,
                "topology": {"nodes": nodes},
                "port": port,
                "instance_runtime_id": str(instance.id) if instance else None,
                "running": running,
                "startable": startable,
                "fit": fit,
                "reason": reason,
                "busy_hosts": busy_hosts,
                "candidate_workers": candidate_workers if nodes >= 2 else [],
            }
        )

    # Vertrag: laufend zuerst, dann startbar, dann grau — innerhalb nach Name.
    entries.sort(
        key=lambda e: (0 if e["running"] else 1 if e["startable"] else 2, e["display_name"].lower())
    )
    return entries


# ── Der Start ────────────────────────────────────────────────────────────────


async def _unique_runtime_slug(session: AsyncSession, base: str) -> str:
    """``<rezept>-<box>``, auf 64 Zeichen gekappt, bei Kollision mit Suffix."""
    base = "".join(c if c.isalnum() or c in "-_" else "-" for c in base).strip("-") or "recipe"
    taken = set((await session.exec(select(Runtime.slug))).all())
    candidate = base[:64]
    counter = 2
    while candidate in taken:
        suffix = f"-{counter}"
        candidate = base[: 64 - len(suffix)] + suffix
        counter += 1
    return candidate


def _endpoint_for(host: ResolvedHost, port: int) -> str:
    from app.services import runtime_manager

    return f"http://{runtime_manager._host_ip(host)}:{port}/v1"  # noqa: SLF001 — dieselbe Adresswahl wie vllm/discover


async def build_runtime_from_recipe(
    session: AsyncSession, recipe: LocalRecipe, host: Host, resolved: ResolvedHost
) -> Runtime:
    """Die Instanz eines Rezepts auf einer Box — dieselben Felder, die der
    Box-Wizard heute über ``POST /runtimes`` anlegt, plus ``topology`` aus dem
    Katalog kopiert (Vertrag) und ``recipe_slug`` als expliziter Rückverweis.

    Raises ValueError, wenn kein Startbefehl entstehen kann (kein Template,
    kein Engine-Standard, kaputter Platzhalter) — der Aufrufer macht daraus
    ein 422 „Startbefehl fehlt".
    """
    runtime_slug = await _unique_runtime_slug(session, f"{recipe.slug}-{host.slug}")
    port = int(recipe.port or DEFAULT_PORT)
    template = recipe.launch_template or launch_template.DEFAULT_TEMPLATES.get(recipe.engine)
    # Ein Container-Name nur dort, wo der Befehl ihn auch vergibt. Wrapper,
    # die ihre Container selbst benennen (``uvx sparkrun run``), bekommen
    # keinen — sonst würde ``docker start <name>`` ins Leere greifen und die
    # Label-Suche ist ohnehin der Weg, den start_runtime für sie nimmt.
    container_name = (
        f"mc-{runtime_slug}" if template and "{container_name}" in template else None
    )
    command = launch_template.build_launch_command(
        engine=recipe.engine,
        model_identifier=recipe.model_identifier,
        slug=runtime_slug,
        port=port,
        launch_template=recipe.launch_template,
        container_name=container_name,
        ctx=recipe.context_len,
        env=recipe.env,
    )
    stop_command = (
        launch_template.render_launch_template(
            recipe.stop_template,
            {
                "port": port,
                "model": recipe.model_identifier,
                "slug": runtime_slug,
                "container_name": container_name or f"mc-{runtime_slug}",
                "image": "-",
                "src_dir": launch_template.DEFAULT_SRC_DIR,
                "gguf_dir": launch_template.DEFAULT_GGUF_DIR,
                "ctx": recipe.context_len or 0,
                "env_yaml": launch_template.render_compose_env(recipe.env),
            },
        )
        if recipe.stop_template
        else None
    )
    return Runtime(
        slug=runtime_slug,
        display_name=recipe.display_name,
        runtime_type=recipe.engine,
        endpoint=_endpoint_for(resolved, port),
        # None lässt runtime_manager den Engine-Standard wählen (/health für
        # llama.cpp, /v1/models sonst) — wie im Box-Wizard.
        healthcheck_path=None,
        model_identifier=recipe.model_identifier,
        container_name=container_name,
        launch_command=command,
        stop_command=stop_command,
        process_name=recipe.process_name,
        # Katalogfeld ``exclusive`` = Wahrheit, Heuristik (min_vram_gb
        # gesetzt) = Fallback — siehe recipe_is_exclusive. Nur so greift die
        # Verdrängung (ensure_exclusive_host) auch für Instanzen, die nicht
        # von Hand angelegt wurden.
        exclusive_memory=recipe_is_exclusive(recipe),
        host_id=host.id,
        max_context_len=recipe.context_len,
        topology={"nodes": recipe_nodes(recipe.topology), "recipe_slug": recipe.slug},
        enabled=True,
    )


async def start_recipe_on_host(
    session: AsyncSession, host: Host, recipe: LocalRecipe
) -> dict[str, Any]:
    """Solo-Start: Instanz anlegen, falls für diese Box keine existiert, dann
    ``runtime_manager.start_runtime``. Duo wird in P1 ehrlich abgelehnt.

    Raises :class:`RecipeStartError` mit HTTP-Status + Satz.
    """
    if not recipe.enabled:
        raise RecipeStartError(409, REASON_RECIPE_HIDDEN)
    if recipe_nodes(recipe.topology) >= 2:
        raise RecipeStartError(409, REASON_DUO_PHASE3)
    if not host_can_ssh(host):
        raise RecipeStartError(409, REASON_NO_SSH)

    resolved = resolved_host_from_row(host)
    runtimes = (
        await session.exec(
            select(Runtime).where(Runtime.enabled == True, Runtime.host_id == host.id)  # noqa: E712
        )
    ).all()
    instance = next((rt for rt in runtimes if recipe_matches_runtime(recipe, rt)), None)

    created = False
    if instance is None:
        try:
            instance = await build_runtime_from_recipe(session, recipe, host, resolved)
        except ValueError as exc:
            # Kein Template und kein Standard → der Vertragssatz; jeder andere
            # Template-Fehler (Platzhalter, Label) bleibt lesbar dahinter.
            raise RecipeStartError(422, f"{REASON_NO_COMMAND} — {exc}") from exc
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        created = True
    elif not (instance.launch_command or "").strip():
        raise RecipeStartError(422, REASON_NO_COMMAND)

    from app.services import runtime_manager, runtime_readiness

    result = await runtime_manager.start_runtime(instance.model_dump(), host=resolved)
    if not result.get("ok"):
        raise RecipeStartError(400, str(result.get("message") or "Start fehlgeschlagen"))
    await runtime_readiness.invalidate_readiness(instance.slug)
    return {
        "ok": True,
        "message": result.get("message"),
        "runtime_id": str(instance.id),
        "runtime_slug": instance.slug,
        "created": created,
    }
