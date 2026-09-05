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

Zweibox-Start (P3, 04.09.2026)
------------------------------
``start_recipe_on_host`` startet jetzt auch ``nodes>=2``. MC spricht dabei
weiter NUR mit dem Head (ADR-077, Regel 5): das Rezept holt seinen Worker
selbst dazu. MC tut genau drei Dinge zusätzlich — die Worker-Box wählen (aus
``candidate_workers``, oder die vom Betreiber genannte), die Adressen in die
`.env` des Rezepts schreiben (``services/recipe_env``) und die Mitgliedschaft
in ``runtime_hosts`` festhalten. Kein neuer Startpfad, kein zweiter
Lebenszyklus: gestartet wird weiter über ``runtime_manager.start_runtime``
auf dem Head.

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
REASON_RECIPE_HIDDEN = "Rezept ist ausgeblendet"
#: P3 — ein Zweibox-Rezept ohne .env-Zuordnung kann MC nicht starten: es
#: wüsste nicht, wohin es die Adresse der zweiten Box schreiben soll. Das
#: steht von Anfang an fest, also grau in der Liste und 422 beim Start.
REASON_NO_ENV_MAP = (
    "Rezept hat keine Umgebungs-Zuordnung (env_file/env_map) — Katalog nachtragen."
)
#: P3 — keine Box übrig, die Worker sein könnte.
REASON_NO_FREE_WORKER = (
    "Keine freie Worker-Box mit SSH-Zugang — alle anderen Boxen sind belegt "
    "oder MC kann sie nicht erreichen"
)


def reason_box_is_worker(runtime_name: str) -> str:
    """P3 — Solo-Start auf einer Box, die gerade Worker eines Verbunds ist."""
    return (
        f"Diese Box arbeitet gerade als zweite Box für '{runtime_name}' — "
        f"erst den Verbund stoppen"
    )


def reason_worker_not_free(slug: str) -> str:
    return f"Box '{slug}' steht als zweite Box nicht zur Verfügung (belegt, aus oder ohne SSH-Zugang)"


def reason_host_unreachable(slug: str) -> str:
    return f"Box '{slug}' ist per SSH nicht erreichbar — Start abgebrochen, bevor etwas angefasst wird"


#: Eine Host-Engine (``ssh_process``) startet MC über SSH und findet sie danach
#: nur an einem Namen wieder: Prozessname (``pgrep -x``) oder Containername
#: (``docker inspect``) — viele Rezept-Startskripte starten in Wahrheit einen
#: Container. Ohne einen der beiden wäre der Start ein Einbahnweg: gestartet,
#: aber nie wieder sichtbar oder stoppbar. Darum grau in der Liste und 422
#: beim Start, BEVOR eine Instanz angelegt oder eine Box freigeräumt wird
#: (Vorfall 03.09.2026: die Verdrängung lief vor der Prüfung).
REASON_NO_HANDLE = (
    "Kein Prozess-/Container-Name hinterlegt — MC könnte starten, "
    "aber nicht sehen oder stoppen"
)


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

#: Die Engine, die keinen Docker-Daemon hinter sich hat — ihr Lebenszyklus
#: hängt an einem Namen auf der Box (siehe REASON_NO_HANDLE).
SSH_PROCESS_ENGINE = "ssh_process"


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

    Eine SLOT-Zeile ist NIE die Instanz eines Rezepts (ADR-078). Sie trägt per
    Definition den ``model_identifier`` des gerade laufenden Rezepts — Signal 3
    würde also genau dieses Rezept matchen, und weil die Slot-Zeile keinen
    Startbefehl hat, wäre das laufende Rezept danach nie wieder startbar
    (422 „Startbefehl fehlt"). Derselbe Ausschluss macht auch das „läuft"-Badge
    der Katalogseite wieder ehrlich (``local_registry._running_matcher`` benutzt
    dieselbe Regel).
    """
    if runtime.is_slot:
        return False
    topology = runtime.topology if isinstance(runtime.topology, dict) else {}
    backlink = topology.get("recipe_slug")
    if backlink:
        # Ein expliziter Rückverweis entscheidet — in BEIDE Richtungen. Eine
        # Instanz, die auf ein anderes Rezept zeigt, ist nie unsere, auch wenn
        # ihr ``model_identifier`` zufällig passt (Live-Befund 04.09.2026: zwei
        # Rezepte auf demselben Port, der Wächter schrieb der gestoppten
        # Instanz das Modell der laufenden zu — und der Umschalter hielt das
        # laufende Rezept für gestoppt).
        return backlink == recipe.slug
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


def recipe_needs_handle(recipe: LocalRecipe) -> bool:
    """Gilt die Handle-Pflicht für dieses Rezept?

    Nur für Host-Engines. Docker-Engines bekommen ihren Containernamen aus
    dem Template bzw. werden über das Label ``mc.runtime.slug`` wiedergefunden
    — für die wäre ein Pflichtfeld eine Lüge.
    """
    return recipe.engine == SSH_PROCESS_ENGINE


def recipe_handle(recipe: LocalRecipe, instance: Runtime | None) -> str:
    """Der hinterlegte Prozess- oder Containername, Instanz schlägt Katalog."""
    if instance is not None:
        return (instance.process_name or instance.container_name or "").strip()
    return (recipe.process_name or "").strip()


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


async def probe_running(
    runtime: Runtime, *, host: ResolvedHost | None = None
) -> bool:
    """Ist DIESE Instanz gesund? Nie geraten — und für Host-Engines nie am
    Port allein entschieden.

    WARUM DER PORT ALLEIN NICHT REICHT (Live-Befunde 03.09.2026)
    ------------------------------------------------------------
    Mehrere Rezepte einer Box benutzen denselben Port. Antwortet dort eine
    FREMDE Engine, hielt die Liste eine Instanz für laufend, die nie
    gestartet war — zuerst gesehen bei einer Host-Engine ohne Handle, zwei
    Stunden später bei einem verdrängten Docker-Rezept, dessen Container
    längst beendet war, während der Nachfolger auf demselben Port antwortete.

    Beide Male dieselbe Ursache und darum dieselbe Regel wie in
    ``runtime_manager.get_runtime_state``: der ANKER muss auf der Box laufen
    UND der Port antworten. Anker heisst Containername bei Docker-Engines,
    Handle bei Host-Engines (``runtime_manager.runtime_anchor_names``).

    Zwei bewusste Ausnahmen:

    * Eine Host-Engine OHNE hinterlegtes Handle ist nie „laufend" — es gibt
      keinen Beleg, und ein unbelegtes „läuft" ist genau die Lüge, die den
      Umschalter blockiert hat.
    * Eine Runtime ohne Anker oder ohne SSH-Box (Cloud, LM Studio, lokal
      laufende Container) behält die reine Port-Probe: dort gibt es nichts
      Besseres zu fragen, und eine strengere Regel würde sie unsichtbar
      machen, ohne je eine Lüge verhindert zu haben.

    Eigene Funktion, damit Tests sie ersetzen können, ohne ins Netz zu gehen.
    """
    from app.services import runtime_manager

    registry = runtime.to_registry_dict()
    names = runtime_manager.runtime_anchor_names(registry)
    if not names and runtime.runtime_type == SSH_PROCESS_ENGINE:
        return False
    if names and host is not None:
        try:
            alive = await runtime_manager.anchor_running(registry, host=host)
        except Exception as exc:  # noqa: BLE001
            # Box nicht erreichbar = kein Beleg. „Läuft" behaupten wir nur,
            # wenn wir es gesehen haben.
            logger.debug("probe: Anker-Prüfung fehlgeschlagen für %s: %s", runtime.slug, exc)
            return False
        if not alive:
            return False
    elif names and runtime.runtime_type == SSH_PROCESS_ENGINE:
        # Ohne aufgelöste Box würde die SSH-Prüfung auf den Standard-Host der
        # Einstellungen zurückfallen und die falsche Kiste befragen. Lieber
        # kein Beleg als ein Beleg von woanders.
        return False

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
                    # ADR-078: eine Slot-Zeile ist kein Motor, sondern der
                    # Platzhalter für den, der gerade läuft. Sie darf eine Box
                    # NIE belegen — sonst hielte jede Box sich selbst besetzt.
                    Runtime.is_slot == False,  # noqa: E712
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


def runtime_host_ids(runtime: Runtime, members: dict[uuid.UUID, list[RuntimeHost]]) -> list[uuid.UUID]:
    """Alle Boxen, die diese Instanz belegt: Head plus Mitglieder.

    Ein Verbund belegt BEIDE Boxen, auch wenn nur der Head in
    ``runtimes.host_id`` steht — sonst gälte die Worker-Box als frei und der
    nächste Start würde sie unter dem laufenden Verbund wegziehen.
    """
    ids = [runtime.host_id] if runtime.host_id else []
    ids.extend(m.host_id for m in members.get(runtime.id, []))
    return ids


class FleetState:
    """Ein Blick auf die Flotte: wer läuft gerade wo (eine Probe je Instanz).

    Eigene kleine Klasse, weil sowohl die Liste als auch der Start dieselbe
    Frage stellen — „welche Box ist belegt?" — und zwei Antworten darauf genau
    die Uneinigkeit wären, die ADR-077 abgeschafft hat.
    """

    def __init__(
        self,
        recipes: list[LocalRecipe],
        hosts: list[Host],
        runtimes: list[Runtime],
        members: dict[uuid.UUID, list[RuntimeHost]],
        running_by_id: dict[uuid.UUID, bool],
    ) -> None:
        self.recipes = recipes
        self.hosts = hosts
        self.runtimes = runtimes
        self.members = members
        self.running_by_id = running_by_id
        self.host_by_id = {h.id: h for h in hosts}
        self.occupied: dict[uuid.UUID, list[Runtime]] = {}
        for rt in runtimes:
            if not running_by_id.get(rt.id):
                continue
            for hid in runtime_host_ids(rt, members):
                self.occupied.setdefault(hid, []).append(rt)
        self.exclusive_busy = {
            hid for hid, rts in self.occupied.items() if any(rt.exclusive_memory for rt in rts)
        }

    def hosts_of(self, runtime: Runtime) -> list[uuid.UUID]:
        return runtime_host_ids(runtime, self.members)

    def worker_member_runtimes(self, host_id: uuid.UUID) -> list[Runtime]:
        """Laufende Instanzen, die diese Box als MITGLIED belegen (ihr Head ist
        eine andere Box) — die Belegung, die ``runtimes.host_id`` nicht zeigt."""
        return [
            rt
            for rt in self.occupied.get(host_id, [])
            if rt.host_id != host_id
        ]


async def load_fleet_state(session: AsyncSession) -> FleetState:
    """Flotte laden und JEDE Instanz einmal probieren (parallel).

    Alle Probes laufen gleichzeitig; eine Box mit 20 Runtimes wartet 5 s,
    nicht 100.
    """
    recipes, hosts, runtimes, members = await _load_fleet(session)
    resolved_by_host_id = {h.id: resolved_host_from_row(h) for h in hosts if host_can_ssh(h)}
    probes = await asyncio.gather(
        *(probe_running(rt, host=resolved_by_host_id.get(rt.host_id)) for rt in runtimes),
        return_exceptions=True,
    )
    running_by_id = {rt.id: (result is True) for rt, result in zip(runtimes, probes)}
    return FleetState(recipes, hosts, runtimes, members, running_by_id)


def recipe_env_ready(recipe: LocalRecipe) -> bool:
    """Kann MC für dieses Rezept die Adressen der Boxen hinterlegen?

    Nur für Zweibox-Rezepte eine echte Frage — ein Solo-Rezept braucht keine
    zweite Adresse und ist darum immer „bereit".
    """
    if recipe_nodes(recipe.topology) < 2:
        return True
    return bool((recipe.env_file or "").strip()) and bool(recipe.env_map)


async def list_host_recipes(session: AsyncSession, host: Host) -> list[dict[str, Any]]:
    """Alle freigegebenen Rezepte aus Sicht EINER Box — exakt das Vertrags-Schema.

    Belegung kommt aus ``runtimes.host_id`` (Head) + ``runtime_hosts``
    (Mitglieder) + einer laufenden Health-Probe je Instanz.
    """
    state = await load_fleet_state(session)
    recipes, hosts, runtimes = state.recipes, state.hosts, state.runtimes
    running_by_id = state.running_by_id
    host_slug_by_id = {h.id: h.slug for h in hosts}
    occupied = state.occupied

    def _hosts_of(rt: Runtime) -> list[uuid.UUID]:
        return state.hosts_of(rt)

    # Wirklich freie Boxen (Worker-Rolle zuerst, P2) — für einen NEUEN Start.
    # P3: nur Boxen mit SSH-Zugang, denn der Duo-Start prüft vorher, ob BEIDE
    # Boxen antworten. Eine Box, die im Dialog wählbar ist und beim Klick
    # abgelehnt wird, wäre genau die Unehrlichkeit, die ADR-077 abgeschafft hat.
    free_workers = worker_candidates(
        [h for h in hosts if host_can_ssh(h)], host, foreign_exclusive_busy(state, host)
    )
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
        handle_ok = not recipe_needs_handle(recipe) or bool(recipe_handle(recipe, instance))
        if command_ok and not handle_ok:
            reason = REASON_NO_HANDLE
        # P3: ein Zweibox-Rezept ohne .env-Zuordnung kann MC nicht starten —
        # das steht ohne Netzzugriff fest, also grau statt „klick und scheitere".
        env_ready = recipe_env_ready(recipe)
        if command_ok and handle_ok and not env_ready:
            reason = REASON_NO_ENV_MAP
        startable = command_ok and handle_ok and env_ready and fit != "none"
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
                # P3: sagt der Oberfläche, ob der Duo-Dialog überhaupt etwas
                # zu wählen hat. Solo-Rezepte sind immer bereit.
                "env_ready": env_ready,
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


def foreign_exclusive_busy(state: FleetState, head: Host) -> set[uuid.UUID]:
    """Boxen, die durch FREMDE exklusive Instanzen belegt sind.

    Fremd = der Head der belegenden Instanz ist eine andere Box. Instanzen
    mit diesem Head verdrängt ein Start von hier ohnehin (Worker zuerst, dann
    Head) — ihre Worker-Box ist darum ein gültiger Kandidat. Ohne diese
    Unterscheidung liesse sich ein laufender Verbund nie durch einen anderen
    ersetzen: „keine freie zweite Box", obwohl die Box nur vom eigenen
    Vorgänger belegt ist (Live-Befund 04.09.2026).
    """
    return {
        hid
        for hid, rts in state.occupied.items()
        if any(rt.exclusive_memory and rt.host_id != head.id for rt in rts)
    }


def duo_worker_candidates(
    state: FleetState, head: Host, instance: Runtime | None
) -> list[dict[str, Any]]:
    """Die Boxen, die JETZT Worker werden könnten — aus Sicht dieses Starts.

    Unterschied zur Liste: die eigene laufende Instanz zählt hier NICHT als
    Belegung. Sonst könnte man einen laufenden Verbund nie neu starten — seine
    Worker-Box wäre durch ihn selbst blockiert.
    """
    ssh_hosts = [h for h in state.hosts if host_can_ssh(h)]
    return worker_candidates(ssh_hosts, head, foreign_exclusive_busy(state, head))


async def _require_ssh_alive(host: Host) -> None:
    """Antwortet die Box überhaupt? Ein ``true`` über SSH, sonst 502 mit Satz.

    Läuft VOR jeder Verdrängung: eine Box freizuräumen und danach zu merken,
    dass die zweite gar nicht erreichbar ist, wäre genau der Vorfall vom
    03.09.2026 in gross (Modell tot, Verbund nie gestartet).
    """
    from app.services import runtime_manager

    try:
        _, _, code = await runtime_manager._ssh_run(  # noqa: SLF001 — die eine SSH-Primitive
            "true", host=resolved_host_from_row(host), timeout=15
        )
    except Exception as exc:  # noqa: BLE001
        raise RecipeStartError(502, reason_host_unreachable(host.slug)) from exc
    if code != 0:
        raise RecipeStartError(502, reason_host_unreachable(host.slug))


async def set_runtime_members(
    session: AsyncSession, runtime: Runtime, head: Host, worker: Host
) -> None:
    """Der Schreibpfad für ``runtime_hosts`` — idempotent.

    Bis P3 wurde diese Tabelle nur GELESEN (ADR-077, Befund 2). Geschrieben
    wird sie hier und nur hier: erst die alten Mitgliedschaften DIESER Instanz
    weg, dann Head (rank 0) und Worker (rank 1) neu. Löschen-dann-schreiben
    statt Upsert, weil sonst der Wechsel der Worker-Box in die Unique-Regel
    (runtime_id, node_rank) liefe.
    """
    existing = (
        await session.exec(select(RuntimeHost).where(RuntimeHost.runtime_id == runtime.id))
    ).all()
    for row in existing:
        await session.delete(row)
    await session.flush()
    session.add(RuntimeHost(runtime_id=runtime.id, host_id=head.id, role="head", node_rank=0))
    session.add(RuntimeHost(runtime_id=runtime.id, host_id=worker.id, role="worker", node_rank=1))
    await session.commit()


async def start_recipe_on_host(
    session: AsyncSession,
    host: Host,
    recipe: LocalRecipe,
    worker_host_id: str | None = None,
) -> dict[str, Any]:
    """Ein Rezept auf einer Box starten — Solo wie Verbund (P3).

    ``host`` ist immer der HEAD: die Box, mit der MC redet. Bei einem
    Zweibox-Rezept kommen drei Schritte dazu, mehr nicht (ADR-077, Regel 5 —
    kein neuer Multi-Host-Startcode):

    1. Worker-Box wählen (``worker_host_id`` oder der erste freie Kandidat),
    2. die Adressen in die `.env` des Rezepts auf dem Head schreiben —
       das Startskript holt sich seinen Worker daraus selbst,
    3. die Mitgliedschaft in ``runtime_hosts`` festhalten, damit die nächste
       Frage „ist diese Box frei?" beide Boxen sieht.

    Reihenfolge mit Absicht: ALLES, was ohne Netzzugriff feststeht (Rezept
    freigegeben, Startbefehl, Handle, .env-Zuordnung, freie Worker-Box), wird
    geprüft, BEVOR irgendetwas auf einer Box angefasst wird. Danach erst
    Erreichbarkeit, Verdrängung (Worker zuerst, dann Head), `.env`, Start.

    Raises :class:`RecipeStartError` mit HTTP-Status + Satz.
    """
    from app.services import (
        recipe_env,
        runtime_grace,
        runtime_manager,
        runtime_readiness,
        slot_runtimes,
    )

    if not recipe.enabled:
        raise RecipeStartError(409, REASON_RECIPE_HIDDEN)
    if not host_can_ssh(host):
        raise RecipeStartError(409, REASON_NO_SSH)

    nodes = recipe_nodes(recipe.topology)
    resolved = resolved_host_from_row(host)
    state = await load_fleet_state(session)
    instance = next(
        (
            rt
            for rt in state.runtimes
            if rt.host_id == host.id and recipe_matches_runtime(recipe, rt)
        ),
        None,
    )

    # VOR dem Anlegen der Instanz: eine Host-Engine ohne Handle darf gar nicht
    # erst in den Startpfad. Sonst legt MC eine Zeile an, räumt die Box frei
    # (Verdrängung) — und scheitert danach an einer Bedingung, die von Anfang
    # an feststand (Vorfall 03.09.2026).
    command_ok = (
        bool((instance.launch_command or "").strip())
        if instance is not None
        else has_launch_command(recipe)
    )
    if command_ok and recipe_needs_handle(recipe) and not recipe_handle(recipe, instance):
        raise RecipeStartError(422, REASON_NO_HANDLE)

    worker: Host | None = None
    env_values: dict[str, str] = {}
    if nodes >= 2:
        if not recipe_env_ready(recipe):
            raise RecipeStartError(422, REASON_NO_ENV_MAP)
        candidates = duo_worker_candidates(state, host, instance)
        if worker_host_id:
            match = next((c for c in candidates if c["host_id"] == str(worker_host_id)), None)
            if match is None:
                named = state.host_by_id.get(_as_uuid(worker_host_id))
                raise RecipeStartError(
                    409, reason_worker_not_free(named.slug if named else str(worker_host_id))
                )
            worker = state.host_by_id[_as_uuid(match["host_id"])]
        elif candidates:
            worker = state.host_by_id[_as_uuid(candidates[0]["host_id"])]
        else:
            raise RecipeStartError(409, REASON_NO_FREE_WORKER)
        try:
            env_values = recipe_env.render_env_map(recipe.env_map, host, worker)
        except ValueError as exc:
            raise RecipeStartError(422, str(exc)) from exc
        if not env_values:
            raise RecipeStartError(422, REASON_NO_ENV_MAP)
    else:
        # Solo auf einer Box, die gerade als zweite Box eines laufenden
        # Verbunds arbeitet: der Verbund steht nicht in ``runtimes.host_id``
        # dieser Box, belegt sie aber trotzdem (Mitgliedschaft, siehe
        # runtime_host_ids). Ohne diese Prüfung zöge ein Solo-Start dem
        # laufenden Verbund die Box unter den Füssen weg.
        blocker = next(iter(state.worker_member_runtimes(host.id)), None)
        if blocker is not None:
            raise RecipeStartError(409, reason_box_is_worker(blocker.display_name))

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

    env_written: list[str] = []
    if worker is not None:
        # Die Topologie der Instanz hält fest, WELCHE zweite Box gemeint ist —
        # der Wächter startet den Verbund später mit genau dieser wieder.
        topology = dict(instance.topology or {})
        topology.update(
            {"nodes": nodes, "recipe_slug": recipe.slug, "worker_host_id": str(worker.id)}
        )
        instance.topology = topology
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        await set_runtime_members(session, instance, host, worker)

        await _require_ssh_alive(host)
        await _require_ssh_alive(worker)

        # Verdrängung über alle Mitglieder: erst die Worker-Box, dann der Head
        # (den räumt ``start_runtime`` selbst über ``ensure_exclusive_host``).
        # Andersherum stünde der Head kurz leer, während die Worker-Box noch
        # das alte Modell hält — und der Start liefe in eine halb belegte Box.
        if instance.exclusive_memory:
            freed = await runtime_manager.ensure_exclusive_host(
                instance.model_dump(),
                host=resolved_host_from_row(worker),
                session=session,
                host_id=worker.id,
            )
            if not freed.get("ok"):
                raise RecipeStartError(409, str(freed.get("message")))

        env_written = await recipe_env.upsert_env_file(
            resolved_host_from_row(host), recipe.env_file or "", env_values
        )

    # Das Autostart-Rezept der Box wird NICHT hier gesetzt, sondern erst,
    # wenn der Wächter die neue Instanz antworten sieht
    # (``runtime_watcher._confirm_autostart_recipe``). Ein Rezept, das nie
    # hochkommt, darf den Vorgänger nicht aus dem Autostart verdrängen
    # (Live 05.09.2026: DeepSeek scheiterte, GLM kam nie zurück).
    # ── Slot-Zeile der Head-Box: Übergangs-Marker VOR dem Start (ADR-078) ────
    # Der Grace-Marker hängt am Slug (``runtime_grace``). Der Umschalter setzt
    # ihn auf die REZEPT-Zeile — die Slot-Zeile hat einen anderen Slug und
    # zählte während der 8–30 min Ladezeit Fehlversuche, feuerte nach drei
    # Proben ``runtime.unreachable`` und rief die Auto-Recovery. Bei JEDEM
    # Wechsel (Architektur-Review 05.09.2026, B4). Also markieren wir sie hier
    # mit — der Wächter räumt den Marker selbst weg, sobald sie antwortet.
    slot = await slot_runtimes.find_slot_runtime(session, host.id)
    if slot is not None:
        await runtime_grace.mark_switching(
            slot.slug, runtime_grace.PHASE_LOADING, runtime_grace.SOURCE_SWITCH
        )

    try:
        result = await runtime_manager.start_runtime(instance.model_dump(), host=resolved)
    except Exception:
        if slot is not None:
            await runtime_grace.clear_switching(slot.slug)
        raise
    if not result.get("ok"):
        # Kein Start = kein Wechsel: der Marker muss weg, sonst sieht die
        # Oberfläche 20 Minuten lang ein „wechselt gerade", das nie endet.
        if slot is not None:
            await runtime_grace.clear_switching(slot.slug)
        raise RecipeStartError(400, str(result.get("message") or "Start fehlgeschlagen"))

    # Ziel-Modell SOFORT in die Slot-Zeile, nicht erst nach zwei Wächter-Proben
    # (bis zu drei Minuten, in denen jeder Agent den alten Namen anfragt und
    # 404 bekommt). Der Wächter korrigiert später, falls die Engine doch etwas
    # anderes serviert als der Katalog sagt — Engine führt, MC folgt.
    if slot is not None:
        try:
            await slot_runtimes.write_slot_state(
                session,
                host.id,
                model=recipe.model_identifier,
                context_len=recipe.context_len,
            )
        except Exception:  # noqa: BLE001 — ein erfolgreicher Start bleibt erfolgreich
            logger.exception("slot: Sofort-Schreiben für Box %s fehlgeschlagen", host.slug)

    await runtime_readiness.invalidate_readiness(instance.slug)
    return {
        "ok": True,
        "message": result.get("message"),
        "runtime_id": str(instance.id),
        "runtime_slug": instance.slug,
        "created": created,
        "worker_host_id": str(worker.id) if worker is not None else None,
        "worker_slug": worker.slug if worker is not None else None,
        "env_written": env_written,
    }


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
