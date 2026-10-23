"""Chat ueber ACP — der Steuerkanal zum Chat-Daemon eines kopflosen Agenten.

Ein Agent mit ACP-Treiber hat KEINE bedienbare TUI mehr: der Sessions-Chat ist
seine einzige Oberflaeche (docs/specs/chat-over-acp.md). Statt Tastendruecken
in einen tmux-Pane (``agent_chat_input``'s TUI-Pfade) spricht das Backend hier
ein kleines Protokoll mit dem Chat-Daemon, der die eine lange ACP-Sitzung
haelt:

===============  ======================================  ==========================
``prompt``       ``{"text": "...", "mode"?: "queue",``   ``{"ok":true,"turn":n}`` ·
                 ``"origin"?: "operator"}``
                                                         ``{"ok":true,"queued":true,...}`` ·
                                                         ``{"ok":false,"error":"busy"}``
``new_session``  ``{"mode"?: "queue",``                 ``{"ok":true,"previousSessionId":...}``
                 ``"requestId"?: "…"}``
``queue_clear``  —                                       ``{"ok":true,"dropped":[...]}``
``cancel``       —                                       ``{"ok":true}``
``config``       ``{"id":"thinking","value":"high"}``    ``{"ok":true,"configOptions":[...]}``
``state``        —                                       ``{"ok":true, ...state}``
``answer``       ``{"id":"el-…","action":"accept",``     ``{"ok":true,"status":"answered"}`` ·
                 ``"content"?:{...}}`` (P3)              ``{"ok":false,"error":"not_pending"}`` ·
                                                         ``{"ok":false,"error":"bad_content",...}``
===============  ======================================  ==========================

``config`` traegt die id, die der Agent selbst meldet: die Denk-Stufe heisst
bei omp ``thinking``, bei Hermes ``reasoning_effort`` (beide Kategorie
``thought_level``, ``agent_chat_input._acp_thinking_option``).

Plan-Modus braucht keine eigene Op: ``config`` mit ``{"id":"mode","value":
"plan"|"default"}`` (omp meldet ``mode`` in ``configOptions``).

``mode: "queue"`` ist die Folge-Nachricht des Chats: laeuft gerade ein Zug,
haelt der Daemon den Auftrag zurueck und liefert ihn, sobald der Zug endet.
Ohne ``mode`` bleibt es bei der Absage ``busy`` (die Maschinen-Aufrufer der
Bridge versuchen es beim naechsten Poll selbst).

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
import time
from typing import Any, Protocol

import httpx

from app.config import settings
from app.services.harness_compat import omp_driver_for

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



def _with_mode(payload: dict[str, Any], mode: str | None) -> dict[str, Any]:
    """``mode`` nur, wenn gesetzt — ohne bleibt die Anfrage byte-gleich mit
    der vor der Warteschlange (ein aelterer Daemon kennt das Feld nicht)."""
    return {**payload, "mode": mode} if mode else payload


#: ``prompt``'s ``origin`` for the operator's own message (the composer) —
#: the daemon's ``acp_chat.ORIGIN_OPERATOR``; the only text a card ever
#: offers to send again.
ORIGIN_OPERATOR = "operator"


def _prompt_payload(text: str, mode: str | None, origin: str | None,
                    context: str | None = None) -> dict[str, Any]:
    """``prompt``: ``origin`` says who wrote the text — ``"operator"`` for the
    composer. The chat daemon offers "Send again" on a card only for the
    operator's own text (fail-closed: no origin = unknown = no button). Only
    when set — without it the request stays byte-identical (an older daemon
    ignores the field anyway).

    ``context`` (``chat_run_context``): a block the daemon puts in front of
    ``text`` on the way to the model only — the chat bubble stays ``text``
    (round-1 user test t1, MUST-1). Only when set; an older daemon ignores
    it, the server-side guard holds either way."""
    payload: dict[str, Any] = {"text": text}
    if origin:
        payload["origin"] = origin
    if context:
        payload["context"] = context
    return _with_mode(payload, mode)


def _new_session_payload(mode: str | None, request_id: str | None) -> dict[str, Any]:
    """``new_session``: ``requestId`` names MC's request in the daemon's card
    and its ``resets`` record (incident 21.10.2026, ``chat_reset_audit``).
    Only when set — an older daemon ignores the field anyway."""
    payload: dict[str, Any] = {"requestId": request_id} if request_id else {}
    return _with_mode(payload, mode)


class AcpChatUnreachableError(Exception):
    """Der Chat-Daemon hat nicht geantwortet — Container weg, Socket tot,
    Bridge aus. NICHT dasselbe wie ``{"ok": false}``: dort hat der Daemon
    geantwortet und abgelehnt."""


class ChatTransport(Protocol):
    """Die Operationen des Steuerkanals. Jede liefert die Antwort des
    Daemons als ``dict`` (inkl. ``ok:false``) oder wirft
    ``AcpChatUnreachableError``."""

    async def prompt(self, text: str, mode: str | None = None,
                     origin: str | None = None, context: str | None = None) -> dict: ...

    async def new_session(self, mode: str | None = None,
                          request_id: str | None = None) -> dict: ...

    async def queue_clear(self) -> dict: ...

    async def cancel(self) -> dict: ...

    async def config(self, id: str, value: str) -> dict: ...

    async def state(self) -> dict: ...

    async def answer(self, id: str, action: str, content: dict | None = None) -> dict: ...


def _answer_payload(id: str, action: str, content: dict | None) -> dict[str, Any]:
    """``answer``-Op (P3): ``content`` nur bei ``accept`` — der Daemon prueft
    es gegen das Schema der Frage."""
    payload: dict[str, Any] = {"id": id, "action": action}
    if content is not None:
        payload["content"] = content
    return payload


def headless_chat_kind(agent) -> str | None:
    """``"acp-docker"`` / ``"acp-http"`` / ``None`` — fuehrt dieser Agent
    seinen Chat ueber ACP?

    Zwei Faelle, beide aus der Spezifikation (ADR-084 hat den Slug-Listen-
    Check von ADR-081 ersetzt — der Treiber ist eine Eigenschaft des
    omp-Harness, ``OMP_DRIVER_DEFAULT`` ist der einzige globale Schalter):

    - ``cli-bridge`` + Harness ``omp`` + ``omp_driver_for("omp") == "acp"``:
      der omp-Bridge im Container faehrt ``OMP_DRIVER=acp``, im Container
      laeuft der Chat-Daemon.
    - ``host`` + Harness ``hermes`` + ``HERMES_DRIVER=acp``: die hermes-bridge
      haelt den Daemon auf dem Host.

    Enten-typisiert auf ``agent.agent_runtime`` / ``agent.harness`` wie
    ``agent_chat_input._target_kind``, damit Tests und das Modell selbst
    (``Agent.headless_chat``) dieselbe Funktion nutzen."""
    runtime = getattr(agent, "agent_runtime", None)
    harness = getattr(agent, "harness", None)

    if runtime == "cli-bridge" and harness == "omp" and omp_driver_for(harness) == "acp":
        return "acp-docker"
    if runtime == "host" and harness == "hermes" and settings.hermes_driver == "acp":
        return "acp-http"
    return None


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
        argv = self._argv(op, payload)
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

        if (
            proc.returncode == _CTL_EXIT_NOT_OK
            and not stdout.strip()
            and b"invalid choice" in stderr
        ):
            # Agent-Image aelter als die Op: der alte Shim verwirft sie schon
            # in argparse (Exit 2, leeres stdout, "invalid choice" auf stderr).
            # Das ist eine ANTWORT ("kenne ich nicht") und kein toter Socket —
            # sonst wuerde aus /new oder Zurueckziehen eine 502 statt der
            # ehrlichen 409 (Review #777).
            return {"ok": False, "error": "unknown_op", "detail": op}

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

    async def prompt(self, text: str, mode: str | None = None,
                     origin: str | None = None, context: str | None = None) -> dict:
        return await self._call("prompt", _prompt_payload(text, mode, origin, context))

    async def new_session(self, mode: str | None = None,
                          request_id: str | None = None) -> dict:
        return await self._call("new_session", _new_session_payload(mode, request_id))

    async def queue_clear(self) -> dict:
        return await self._call("queue_clear")

    async def cancel(self) -> dict:
        return await self._call("cancel")

    async def config(self, id: str, value: str) -> dict:
        return await self._call("config", {"id": id, "value": value})

    async def state(self) -> dict:
        return await self._call("state")

    async def answer(self, id: str, action: str, content: dict | None = None) -> dict:
        return await self._call("answer", _answer_payload(id, action, content))


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

    async def prompt(self, text: str, mode: str | None = None,
                     origin: str | None = None, context: str | None = None) -> dict:
        return await self._call("prompt", _prompt_payload(text, mode, origin, context))

    async def new_session(self, mode: str | None = None,
                          request_id: str | None = None) -> dict:
        return await self._call("new_session", _new_session_payload(mode, request_id))

    async def queue_clear(self) -> dict:
        return await self._call("queue_clear")

    async def cancel(self) -> dict:
        return await self._call("cancel")

    async def config(self, id: str, value: str) -> dict:
        return await self._call("config", {"id": id, "value": value})

    async def state(self) -> dict:
        return await self._call("state")

    async def answer(self, id: str, action: str, content: dict | None = None) -> dict:
        # Die Host-Bruecke routet ``answer`` (noch) nicht — ihr Chat-Daemon
        # fragt auch nie (Elicitation ist dort aus).
        return await self._call("answer", _answer_payload(id, action, content))


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
    gewinnt (``omp_chat.chat_state_file``).

    Blockierendes I/O; Aufrufer aus dem Request-Pfad wickeln es in
    ``asyncio.to_thread``. Wirft nie: eine fehlende Datei ist der NORMALE
    Zustand, solange der Daemon noch nicht geschrieben hat — die Capabilities
    antworten darauf mit leeren Listen und einem Grund, nicht mit einem
    Fehler."""
    from app.services.omp_chat import chat_state_file, resolve_transcript_dir

    root = resolve_transcript_dir(agent)
    if root is None:
        return None

    # Dieselbe Datei, die ``omp_chat.find_active_session`` als Zeiger auf die
    # Chat-Sitzung liest — eine Suchregel, nicht zwei.
    newest = chat_state_file(root)
    if newest is None:
        return None
    try:
        parsed = json.loads(newest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        logger.warning("acp chat: Zustandsdatei unlesbar: %s", newest, exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None


#: The omp bridge's task phase file (docker/omp-bridge/bridge.py::
#: TaskStateFile), at the ROOT of the sessions tree (task runs use many cwds).
_TASK_STATE_FILENAME = "acp-task-state.json"
#: The bridge's heartbeater re-stamps the file every ~30 s while a run lasts;
#: older than this = the bridge died mid-run, the phase no longer holds.
_TASK_STATE_MAX_AGE_S = 120.0
#: Clock skew between the agent container and the backend we still accept.
_TASK_STATE_MAX_SKEW_S = 60.0


def read_acp_task_phase(agent) -> str | None:
    """``"working"`` / ``"delivered"`` / ``None`` — what the agent's TASK
    channel does right now (follow-up to ADR-097: while a task ran, the chat
    said "Ready", because only the chat daemon's ``busy`` was read; a task
    runs through the bridge's own ACP client). None for idle and for a
    missing, garbled, stale or future-dated file. Never raises."""
    from datetime import datetime, timezone

    from app.services.omp_chat import resolve_transcript_dir

    root = resolve_transcript_dir(agent)
    if root is None:
        return None
    try:
        data = json.loads((root / _TASK_STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    phase = data.get("phase")
    if phase not in ("working", "delivered"):
        return None
    try:
        stamp = datetime.fromisoformat(str(data.get("updatedAt")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if age > _TASK_STATE_MAX_AGE_S or age < -_TASK_STATE_MAX_SKEW_S:
        return None
    return phase


def read_acp_turn_status(
    agent, *, with_questions: bool = False, with_task: bool = False,
) -> str | None:
    """``"working"`` / ``"idle"`` / ``None`` — der Zug-Zustand eines
    kopflosen ACP-Agenten, direkt aus ``busy`` in ``read_acp_chat_state``.

    ``None`` heisst "kein frischer Zustand bekannt" (Datei fehlt/kaputt,
    ``busy`` fehlt oder ist kein bool) — der Aufrufer (``transcript_chat.
    ChatTailerManager._compute_pane_state``) faellt dann auf die
    mtime-Heuristik zurueck, genau wie der pane-lose Boss/host-Zweig.

    Das ist die EINZIGE Zug-Zustands-Quelle, die ein ACP-Agent hat: kein
    TUI-Pane zum Sondieren. Unter ``OMP_DRIVER=acp`` laeuft in Fenster 0
    keine interaktive TUI mehr, nur eine statische Banner-Zeile
    (``docker/omp-bridge/entrypoint.sh``) — gegen die klassifizierte JEDER
    TUI-Pane-Parser (``pane_state.parse_pane_state``, ``omp_chat.
    parse_pane_state``) die Sitzung dauerhaft als ``unknown``. Das war der
    Operator-Befund 02.10.2026: ein omp/ACP-Agent antwortete im Chat normal, die
    Statuszeile zeigte trotzdem fortwaehrend "Status unknown — check the
    terminal" ueber dem Composer."""
    state = read_acp_chat_state(agent)
    # with_task (opt-in, the chat's status line): a task runs through the
    # bridge's own ACP client, not the chat daemon. Its phase file says
    # "working" (the model answered -> "task_working", NOT "working": the
    # chat itself is free, so no Stop button and no "queued" echo) or
    # "delivered" (picked up, the model has not answered ->
    # "waiting_model"). A busy CHAT turn still wins.
    if with_task:
        chat_busy = isinstance(state, dict) and state.get("busy") is True
        task_phase = None if chat_busy else read_acp_task_phase(agent)
        if task_phase is not None:
            return "task_working" if task_phase == "working" else "waiting_model"
    if state is None:
        return None
    busy = state.get("busy")
    if not isinstance(busy, bool):
        return None
    if busy and with_questions and state.get("elicitations"):
        # P3: the turn waits on the OPERATOR (an open question card), not
        # on the model — the chat's status line must say so, not "Thinking"
        # (live 13.10.2026). Opt-in: other callers (voice stop) keep the
        # plain working/idle answer.
        return "waiting_input"
    return "working" if busy else "idle"


#: How long the chat's model facts (a card waiting in the inbox, does the
#: model answer) are kept per agent. The tailer probes every ~2 s per open
#: chat; the readiness itself is cached in Redis for 15 s anyway.
_MODEL_WAIT_CACHE_S = 10.0
_model_wait_cache: dict[str, tuple[float, bool, bool]] = {}


async def _model_wait_facts(agent) -> tuple[bool, bool]:
    """(an assigned card waits in the agent's inbox, the agent's model
    answers). The card is one the poll would deliver by itself
    (``run_control`` empty, same filter as the poll claim); the readiness
    is the poll's own gate (``runtime_ready_for_agent``, local runtimes
    included, cached, fail-open) as a read: no timeline note."""
    from sqlmodel import select

    import app.database
    from app.models.task import Task
    from app.services.runtime_readiness import runtime_ready_for_agent

    async with app.database.async_session_maker() as session:
        card = (
            await session.exec(
                select(Task.id).where(
                    Task.assigned_agent_id == agent.id,
                    Task.status == "inbox",
                    Task.run_control.is_(None),
                ).limit(1)
            )
        ).first()
        # Pool hygiene: the gate awaits Redis and maybe an HTTP probe —
        # release the read transaction first (nothing uncommitted).
        await session.commit()
        ready, _reason = await runtime_ready_for_agent(
            agent, session, include_local=True, notify=False,
        )
    return card is not None, bool(ready)


async def refine_status_by_model(agent, status: str | None) -> str | None:
    """The chat's status, corrected by what MC knows about the agent's MODEL
    (review Z2/Z5 of the ADR-097 follow-up, 20.10.2026). The bridge's phase
    file is not the whole truth:

    * ``idle`` while an assigned card waits in the inbox and the model does
      not answer -> ``waiting_model``. omp gave up on the dead endpoint,
      MC's ACK ladder returned the card for the model and the poll gate
      holds it — the chat said "Ready", the task card "waiting for the
      model" (the 19.10. case: box off).
    * ``task_working`` while the model does not answer -> ``waiting_model``.
      The engine died after the claim (task 43788774): omp retries for
      minutes and the phase stays "working".

    Every other status passes through (a busy chat turn, a question, an
    unknown state). An agent without a runtime has nothing to wait for and
    costs no read. Cached per agent (``_MODEL_WAIT_CACHE_S``); any error
    keeps ``status`` — a broken read never invents a state."""
    if status not in ("idle", "task_working"):
        return status
    if not getattr(agent, "runtime_id", None):
        return status
    key = str(getattr(agent, "id", ""))
    now = time.monotonic()
    hit = _model_wait_cache.get(key)
    if hit is not None and now - hit[0] < _MODEL_WAIT_CACHE_S:
        waiting_card, ready = hit[1], hit[2]
    else:
        try:
            waiting_card, ready = await _model_wait_facts(agent)
        except Exception as e:  # noqa: BLE001 — never break the status line
            logger.debug("chat model facts skipped for %s: %s", key, e)
            return status
        if len(_model_wait_cache) > 512:
            _model_wait_cache.clear()
        _model_wait_cache[key] = (now, waiting_card, ready)
    if ready:
        return status
    if status == "task_working" or waiting_card:
        return "waiting_model"
    return status
