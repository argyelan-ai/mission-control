"""Chat ueber ACP — der Steuerkanal zum Chat-Daemon eines kopflosen Agenten.

Ein Agent mit ACP-Treiber hat KEINE bedienbare TUI mehr: der Sessions-Chat ist
seine einzige Oberflaeche (docs/specs/chat-over-acp.md). Statt Tastendruecken
in einen tmux-Pane (``agent_chat_input``'s TUI-Pfade) spricht das Backend hier
ein winziges Vier-Operationen-Protokoll mit dem Chat-Daemon, der die eine
lange ACP-Sitzung haelt:

===========  ==========================================  ==========================
``prompt``   ``{"text": "..."}``                         ``{"ok":true,"turn":n}`` ·
                                                         ``{"ok":false,"error":"busy"}``
``cancel``   —                                           ``{"ok":true}``
``config``   ``{"id":"thinking","value":"high"}``        ``{"ok":true,"configOptions":[...]}``
``state``    —                                           ``{"ok":true, ...state}``
===========  ==========================================  ==========================

Zwei Kanaele, dieselbe Schema-Sprache:

- ``DockerCtlTransport`` — ``docker exec mc-agent-<slug> python3
  /opt/omp-bridge/acp_chat_ctl.py <op> --json '<payload>'``. Der Shim redet
  ueber den Unix-Socket des Daemons im Container und uebersetzt dessen Antwort
  in Exit-Code + JSON auf stdout: 0 = ``ok``, 2 = ``ok:false`` (eine ANTWORT,
  kein Transportfehler — ``busy`` kommt so herein), 3 = Socket unerreichbar.
- ``HttpCtlTransport`` — ``POST <base>/chat/<op>`` gegen die hermes-bridge auf
  dem Host (Port 18794, derselbe Dienst, den ``cli_terminal``'s
  Host-Lebenszyklus schon anspricht).

``AcpChatUnreachableError`` ist ausdruecklich die vierte Antwortsorte: der
Daemon hat NICHT geantwortet (Container weg, Socket tot, Bridge aus). Sie ist
von ``ok:false`` getrennt, weil das Frontend beides verschieden erklaeren
muss — "der Agent lehnt ab" vs. "da ist gerade niemand".
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import omp_acp_agents, settings

logger = logging.getLogger(__name__)

#: Pfad des CLI-Shims IM Container (docker/omp-bridge wird nach /opt/omp-bridge
#: kopiert — s. das omp-Image).
CTL_PATH = "/opt/omp-bridge/acp_chat_ctl.py"

#: Exit-Code des Shims fuer "Socket nicht erreichbar" (Spezifikation).
_CTL_EXIT_UNREACHABLE = 3
#: Exit-Code fuer eine inhaltliche Absage (``ok:false``) — eine Antwort.
_CTL_EXIT_NOT_OK = 2

#: ``prompt`` kehrt sofort zurueck (der Zug laeuft asynchron weiter), ``state``
#: liest nur Speicher — 10s sind grosszuegig und decken einen kurz haengenden
#: Docker-Daemon ab, ohne einen Request ewig festzuhalten (gleiche Sorge wie
#: ``agent_chat_input._run_docker_exec``'s ``timeout=5``).
_CTL_TIMEOUT_SECONDS = 10.0
_HTTP_TIMEOUT_SECONDS = 10.0

#: Die hermes-bridge auf dem Host. Aus dem Backend-Container zeigt
#: ``host.docker.internal`` auf den Mac; der Port ist derselbe wie in
#: ``routers/cli_terminal.py`` (18794).
HERMES_BRIDGE_BASE_URL = "http://host.docker.internal:18794"

#: Name der Zustandsdatei, die der Daemon nach jeder Aenderung neu schreibt.
_STATE_FILENAME = "acp-chat-state.json"


class AcpChatUnreachableError(Exception):
    """Der Chat-Daemon hat nicht geantwortet — Container weg, Socket tot,
    Bridge aus. NICHT dasselbe wie ``{"ok": false}``: dort hat der Daemon
    geantwortet und abgelehnt."""


class ChatTransport(Protocol):
    """Die vier Operationen des Steuerkanals. Jede liefert die Antwort des
    Daemons als ``dict`` (inkl. ``ok:false``) oder wirft
    ``AcpChatUnreachableError``."""

    async def prompt(self, text: str) -> dict: ...

    async def cancel(self) -> dict: ...

    async def config(self, id: str, value: str) -> dict: ...

    async def state(self) -> dict: ...


def headless_chat_kind(agent) -> str | None:
    """``"acp-docker"`` / ``"acp-http"`` / ``None`` — fuehrt dieser Agent
    seinen Chat ueber ACP?

    Zwei Faelle, beide aus der Spezifikation:

    - ``cli-bridge`` + Harness ``omp`` + Slug in ``OMP_ACP_AGENT_SLUGS``: der
      omp-Bridge im Container faehrt ``OMP_DRIVER=acp`` (ADR-081), im
      Container laeuft der Chat-Daemon. Der Slug MUSS in der Liste stehen —
      Runtime und Harness allein wuerden auch jeden omp-Agenten auf dem
      TUI-Pfad in einen Kanal umlenken, den sein Container nie bedient.
    - ``host`` + Harness ``hermes`` + ``HERMES_DRIVER=acp``: die hermes-bridge
      haelt den Daemon auf dem Host.

    Enten-typisiert auf ``agent.slug`` / ``agent.agent_runtime`` /
    ``agent.harness`` wie ``agent_chat_input._target_kind``, damit Tests und
    das Modell selbst (``Agent.headless_chat``) dieselbe Funktion nutzen."""
    runtime = getattr(agent, "agent_runtime", None)
    harness = getattr(agent, "harness", None)
    slug = getattr(agent, "slug", None)

    if runtime == "cli-bridge" and harness == "omp" and slug and slug in omp_acp_agents():
        return "acp-docker"
    if runtime == "host" and harness == "hermes" and settings.hermes_driver == "acp":
        return "acp-http"
    return None


def _payload_for(op: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    return payload or None


class DockerCtlTransport:
    """Steuerkanal in einen Agenten-Container, ueber den CLI-Shim.

    Bewusst ``asyncio.create_subprocess_exec`` (nicht ``to_thread`` +
    ``subprocess.run`` wie die Tastendruck-Pfade): hier gibt es eine Antwort,
    auf die gewartet wird — die gehoert auf die Event-Loop, nicht in den
    geteilten Thread-Pool des Tailers."""

    def __init__(self, slug: str):
        self._slug = slug

    def _argv(self, op: str, payload: dict[str, Any] | None) -> list[str]:
        # ``-u agent`` und ``LANG=C.UTF-8`` wie in ``agent_chat_input._docker_argv``:
        # ohne die richtige Locale verstuemmelt der Container mehrbytige Zeichen
        # im Prompt-Text.
        argv = [
            "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent",
            f"mc-agent-{self._slug}",
            "python3", CTL_PATH, op,
        ]
        if payload:
            argv += ["--json", json.dumps(payload)]
        return argv

    async def _call(self, op: str, payload: dict[str, Any] | None = None) -> dict:
        argv = self._argv(op, _payload_for(op, payload))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), _CTL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as e:
            raise AcpChatUnreachableError(f"acp_chat_ctl {op}: timeout") from e
        except OSError as e:
            raise AcpChatUnreachableError(f"acp_chat_ctl {op}: {e}") from e

        if proc.returncode == _CTL_EXIT_UNREACHABLE:
            raise AcpChatUnreachableError(
                f"acp_chat_ctl {op}: socket unreachable in mc-agent-{self._slug}"
            )

        try:
            answer = json.loads(stdout.decode(errors="replace"))
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            # Kein auswertbares JSON heisst: der Shim kam gar nicht zum Zug
            # (Container weg, Datei fehlt, docker-Fehler auf stderr). Das ist
            # "keine Antwort", nicht "Antwort nein".
            raise AcpChatUnreachableError(
                f"acp_chat_ctl {op}: rc={proc.returncode} "
                f"stderr={stderr.decode(errors='replace')[:200]}"
            )
        if proc.returncode not in (0, _CTL_EXIT_NOT_OK):
            logger.warning(
                "acp chat: unerwarteter Exit-Code %s von acp_chat_ctl %s",
                proc.returncode, op,
            )
        return answer

    async def prompt(self, text: str) -> dict:
        return await self._call("prompt", {"text": text})

    async def cancel(self) -> dict:
        return await self._call("cancel")

    async def config(self, id: str, value: str) -> dict:
        return await self._call("config", {"id": id, "value": value})

    async def state(self) -> dict:
        return await self._call("state")


class HttpCtlTransport:
    """Steuerkanal zur hermes-bridge auf dem Host (``POST /chat/<op>``).

    ``transport`` ist der Testhaken (``httpx.MockTransport``) — live bleibt er
    ``None`` und httpx nimmt seinen eigenen."""

    def __init__(self, base_url: str | None = None, *, transport=None):
        self._base = (base_url or HERMES_BRIDGE_BASE_URL).rstrip("/")
        self._transport = transport

    async def _call(self, op: str, payload: dict[str, Any] | None = None) -> dict:
        url = f"{self._base}/chat/{op}"
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.post(url, json=payload or {})
        except httpx.HTTPError as e:
            raise AcpChatUnreachableError(f"hermes bridge {op}: {e}") from e

        if response.status_code >= 500:
            # Die Bridge antwortet 502, wenn SIE den Daemon nicht erreicht —
            # derselbe Zustand wie Exit 3 auf dem Docker-Pfad.
            raise AcpChatUnreachableError(
                f"hermes bridge {op}: HTTP {response.status_code}"
            )
        try:
            answer = response.json()
        except ValueError as e:
            raise AcpChatUnreachableError(f"hermes bridge {op}: kein JSON") from e
        if not isinstance(answer, dict):
            raise AcpChatUnreachableError(f"hermes bridge {op}: kein JSON-Objekt")
        return answer

    async def prompt(self, text: str) -> dict:
        return await self._call("prompt", {"text": text})

    async def cancel(self) -> dict:
        return await self._call("cancel")

    async def config(self, id: str, value: str) -> dict:
        return await self._call("config", {"id": id, "value": value})

    async def state(self) -> dict:
        return await self._call("state")


def transport_for(agent) -> ChatTransport:
    """Der Steuerkanal dieses Agenten. ``InputNotSupportedError`` fuer jeden,
    der gar nicht kopflos chattet — dieselbe Ausnahme und derselbe Klassierer
    (``_target_kind``), mit dem die Tastendruck-Pfade ihre Kanaele waehlen.

    Import funktionslokal: ``agent_chat_input`` importiert dieses Modul auf
    Modulebene, andersherum waere es ein Zirkel."""
    from app.services.agent_chat_input import InputNotSupportedError, _target_kind

    kind = _target_kind(agent)
    if kind == "acp-docker":
        return DockerCtlTransport(agent.slug)
    if kind == "acp-http":
        return HttpCtlTransport()
    raise InputNotSupportedError()


def read_acp_chat_state(agent) -> dict | None:
    """Die Zustandsdatei des Chat-Daemons, oder ``None``.

    Der Daemon schreibt sie neben die Transkripte, in den Ordner des
    Arbeitsverzeichnisses (``<sessions-wurzel>/<kodiertes-cwd>/``). Ein Agent
    kann mehrere solcher Ordner haben (verschiedene cwds) — die juengste Datei
    gewinnt, gleiche Regel wie ``omp_chat.find_active_session``.

    Blockierendes I/O; Aufrufer aus dem Request-Pfad wickeln es in
    ``asyncio.to_thread``. Wirft nie: eine fehlende Datei ist der NORMALE
    Zustand, solange der Daemon noch nicht geschrieben hat — die Capabilities
    antworten darauf mit leeren Listen und einem Grund, nicht mit einem
    Fehler."""
    from app.services.omp_chat import resolve_transcript_dir

    root = resolve_transcript_dir(agent)
    if root is None:
        return None

    newest: Path | None = None
    newest_mtime = -1.0
    for pattern in (_STATE_FILENAME, f"*/{_STATE_FILENAME}"):
        for candidate in root.glob(pattern):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest = candidate

    if newest is None:
        return None
    try:
        parsed = json.loads(newest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        logger.warning("acp chat: Zustandsdatei unlesbar: %s", newest, exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None
