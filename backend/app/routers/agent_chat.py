"""Live chat view over an agent's Claude Code transcript — HTTP history page
plus a live SSE tail. Parsing/session-resolution lives in
``services/transcript_chat.py`` (A1-A3); this router only wires auth, the
404 gating contract, and the tailer's acquire/release lifecycle around it.
"""
from __future__ import annotations

import asyncio
import json
import dataclasses
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.auth import Role, require_role, require_user
from app.database import get_session, release_session
from app.models.agent import Agent
from app.models.task import Task
from app.redis_client import RedisKeys
from app.services.acp_chat_transport import AcpChatUnreachableError
from app.services.agent_chat_input import (
    AcpChatRefusedError,
    AgentBusyError,
    AgentStartingError,
    BossDeliveryError,
    EffortSwitchFailedError,
    EffortSwitchRejectedError,
    InputNotSupportedError,
    can_receive_input,
    clear_queue,
    effort_capabilities,
    model_options_capabilities,
    send_keys,
    send_text,
    set_effort,
    slash_command_capabilities,
)
from app.services.harness_catalog import get_observed_model_windows
from app.services.reference_ingest import (
    ReferenceIngestError,
    ReferenceTooLargeError,
    is_image_reference,
    serialize_reference,
    store_reference,
)
from app.services.sse import _sse_generator
from app.services.transcript_adapters import adapter_for
from app.services import fresh_session
from app.services.transcript_chat import (
    read_history,
    resolve_aliveness,
    tailer_manager,
)
from app.services.workspace_diff import (
    NoWorkspaceError,
    choose_repo,
    cwd_boundary,
    display_path,
    host_path_for_agent_cwd,
    resolve_workspace_path,
    source_info,
    workspace_diff,
)

router = APIRouter(prefix="/api/v1", tags=["agent-chat"])

_NO_TRANSCRIPT = {"reason": "no_transcript"}
_NO_WORKSPACE = {"reason": "no_workspace"}
_INPUT_NOT_SUPPORTED = {"reason": "input_not_supported"}
_EFFORT_SWITCH_FAILED = {"reason": "effort_switch_failed"}
_AGENT_BUSY = {"reason": "agent_busy"}
_AGENT_STARTING = {"reason": "agent_starting"}
# 502: die host-pty-bridge hat einen Boss-Tastendruck nicht bestaetigt
# (nicht erreichbar, kein Ack, tmux-Fehler). Das Echo im Chat wird
# zurueckgenommen — nie 204 fuer etwas, das nirgends ankam (05.09.2026).
_BOSS_DELIVERY_FAILED = "boss_delivery_failed"


def _boss_delivery_failed(e: BossDeliveryError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"reason": _BOSS_DELIVERY_FAILED, "detail": str(e)[:300]},
    )


# 502: der ACP-Chat-Daemon eines kopflosen Agenten hat nicht geantwortet
# (Container weg, Socket tot, hermes-bridge aus). Bewusst NICHT dieselbe 409
# wie eine inhaltliche Absage: "der Agent lehnt ab" und "da ist gerade
# niemand" sind fuer den Operator zwei verschiedene Lagen.
_ACP_UNREACHABLE = "acp_unreachable"


def _acp_unreachable(e: AcpChatUnreachableError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"reason": _ACP_UNREACHABLE, "detail": str(e)[:300]},
    )
_MAX_TEXT_LEN = 20000
_MAX_KEYS_LEN = 16

# C0 control chars other than \t (0x09) and \n (0x0a) — NUL in particular
# makes ``subprocess.run`` raise ValueError deep inside delivery, which would
# otherwise surface as an unhandled 500 instead of a clean 422 (fix round 1).
_DISALLOWED_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f]")


class ChatInputBody(BaseModel):
    text: str


class ChatKeysBody(BaseModel):
    keys: list[str]


class ChatEffortBody(BaseModel):
    level: str


async def _resolve_transcript_path(
    agent_id: uuid.UUID, session: AsyncSession
) -> tuple[Agent, Path, Any] | JSONResponse:
    """Loads the agent and its live session's transcript path, or the exact
    404 body the frontend keys on (``{"reason": "no_transcript"}``) for
    every "nothing to show" case: no transcript dir for this agent/runtime,
    no ``.jsonl`` session in that dir yet, or the Boss privacy gate
    rejecting the newest session's cwd. A genuinely unknown ``agent_id``
    raises a plain 404 instead — that's a routing error, not a "no session
    yet" state the frontend renders specially.

    Welcher Adapter das beantwortet, entscheidet der Harness des Agenten
    (``transcript_adapters.adapter_for``) — Claude Code liest flach aus
    ``~/.mc/agents/<slug>/claude-config/projects/…``, omp eine Ebene tief
    aus ``~/.mc/agents/<slug>/omp-sessions/<cwd>/…``. Der Adapter wird
    mitgegeben, damit History-Seite und Tailer garantiert denselben
    benutzen.
    """
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    adapter = adapter_for(agent)

    tdir = adapter.resolve_transcript_dir(agent)
    if tdir is None:
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    # to_thread: find_active_session liest jetzt Datei-Enden (Inhalts-Rangfolge)
    # — das darf den Event-Loop nicht blockieren. Alle anderen Aufrufer machen
    # es bereits so.
    active = await asyncio.to_thread(adapter.find_active_session, tdir)
    if active is None:
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    path, _meta = active
    if not adapter.transcript_allowed(agent, path):
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    return agent, path, adapter


@router.get("/agents/{agent_id}/chat/history")
async def get_chat_history(
    agent_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=1000),
    before_uuid: str | None = Query(None),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """History page plus a ``capabilities`` block
    (``{"effortLevels": [...], "canSwitchEffort": bool, "slashCommands":
    [...], "modelOptions": [...]}``) so the composer can build its effort
    chip, command palette, and model dropdown from what this agent's
    harness actually supports, instead of a hardcoded frontend list — see
    ``agent_chat_input.effort_capabilities`` / ``slash_command_capabilities``
    / ``model_options_capabilities`` for each derivation (the latter two,
    and each ``usage`` event's ``contextWindow`` estimate, use the
    harness-catalog Redis-backed discovery + observed-window map, fetched
    once here and threaded into ``read_history`` — see
    ``harness_catalog``'s module docstring). Also stamps
    ``session.aliveness`` (``"active" | "idle" | "ended"`` —
    ``transcript_chat.resolve_aliveness``): the old ``session.live`` alone
    (mtime<60s) read an idle-but-still-running CLI as "ended" everywhere,
    an operator-visible bug; ``live`` is kept unchanged for backward
    compat (== ``aliveness == "active"``)."""
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return resolved

    agent, path, adapter = resolved
    capabilities = {
        **await effort_capabilities(agent),
        **await slash_command_capabilities(agent),
        **await model_options_capabilities(agent),
    }

    # Frische Sitzung ohne Datei (omp ``/new``, siehe ``fresh_session``):
    # die neueste Datei ist noch die ALTE. Sie hier auszuliefern brachte den
    # alten Verlauf direkt nach ``session_changed`` zurueck — genau der
    # gemeldete Fehler. Leer antworten, mit einer Kennung, die nicht die
    # alte ist, damit das Frontend die Seite als neue Sitzung einspeist.
    marked_at = fresh_session.marked_at(str(agent_id))
    if marked_at is not None and await asyncio.to_thread(
        fresh_session.is_stale, str(agent_id), path
    ):
        return {
            "events": [],
            "session": {
                "sessionId": f"fresh-{int(marked_at)}",
                "live": True,
                "startedAt": None,
                "aliveness": "active",
            },
            "hasMore": False,
            "subagentRuns": [],
            "capabilities": capabilities,
        }

    observed_windows = await get_observed_model_windows()
    history = read_history(
        path,
        adapter,
        limit=limit,
        before_uuid=before_uuid,
        observed_windows=observed_windows,
    )
    history["session"]["aliveness"] = await resolve_aliveness(agent, path, adapter)
    history["capabilities"] = capabilities
    return history


#: Erlaubte Gestalt einer ``runId``. Der Wert wird SYNTAKTISCH geprueft, bevor
#: er irgendwo hinkommt — obwohl er gleich danach ohnehin gegen die gescannte
#: Lauf-Liste geprueft wird. Zwei Schranken, weil dieser Endpunkt als erster
#: ueberhaupt in die Unterordner einer Sitzung greift: ``find_active_session``
#: steigt bewusst nie dorthin ab, es gibt hier also keine Vorgaenger-
#: Absicherung, die einen Fehler auffangen wuerde.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


@router.get("/agents/{agent_id}/chat/subagent/{run_id}")
async def get_subagent_history(
    agent_id: uuid.UUID,
    run_id: str,
    limit: int = Query(200, ge=1, le=1000),
    before_uuid: str | None = Query(None),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Der Verlauf EINES Subagenten dieser Sitzung.

    Claude Code legt je Subagent eine eigene Transkript-Datei unter
    ``<sitzung>/subagents/`` an; im Hauptstrom steht davon nichts (live
    gemessen: 0 Zeilen mit ``isSidechain: true``). Ohne diesen Endpunkt ist
    ein delegierter Auftrag im Chat nur ein Werkzeugaufruf ohne Inhalt.

    Gelesen wird mit DEMSELBEN Parser wie alles andere — die Dateien haben
    dasselbe Format (679 Stueck fehlerfrei gegengelesen).

    ── Warum hier vier Schranken stehen ──────────────────────────────────
    Der Baum, in den dieser Endpunkt greift, enthaelt bei einem Host-Agenten
    AUCH die persoenlichen Sitzungen des Operators, und
    ``transcript_allowed`` gibt fuer cli-bridge-Agenten blind ``True``
    zurueck. Ein Pfad, der aus der URL zusammengebaut wuerde, waere damit ein
    Leseschluessel auf fremde Gespraeche. Darum:

    1. ``run_id`` syntaktisch, auf dem ROHEN Wert (kein ``/``, kein ``..``);
    2. Aufloesung ausschliesslich per Nachschlag in der Liste, die
       ``subagent_runs`` selbst gescannt hat — der Pfad entsteht nie aus
       Nutzereingabe;
    3. Eindaemmung auf dem UNAUFGELOESTEN Pfad, plus ausdrueckliche
       Abweisung von Symlinks auf das Sitzungsverzeichnis, den
       ``subagents``-Ordner und die Zieldatei. Die Grenze zu ``resolve()``
       verschob sie bei einem Ordner-Symlink mit — genau das war die Luecke;
    4. ``transcript_allowed`` ZUSAETZLICH auf der Kind-Datei: Eltern- und
       Kindurteil koennen auseinandergehen, weil ein Subagent das
       Arbeitsverzeichnis wechseln kann.

    Jede Ablehnung liefert denselben Koerper wie ueberall
    (``{"reason": "no_transcript"}``) — nie 403, nie eine Meldung, die einen
    Pfad verraet.
    """
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return resolved

    agent, path, adapter = resolved

    if not _RUN_ID_RE.match(run_id or ""):
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    run = next((r for r in adapter.subagent_runs(path) if r["runId"] == run_id), None)
    if run is None:
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    session_dir = path.parent / path.stem
    subroot = session_dir / "subagents"

    # Die Grenze wird auf dem UNAUFGELOESTEN Pfad gebildet. Die Vorfassung
    # rechnete ``(… / "subagents").resolve()`` — das loeste den Ordner SELBST
    # mit auf, und ein Symlink darauf verschob die Grenze einfach mit: der
    # Vergleich weiter unten war danach trivial wahr, und keine der anderen
    # Schranken sah es (die Gestalt-Pruefung schaut nur auf Zeichen, der
    # Nachschlag folgte demselben Symlink, ``transcript_allowed`` sagt fuer
    # cli-bridge blind Ja). Der Endpunkt war damit ein Leseschluessel auf die
    # Subagenten-Protokolle FREMDER Agenten. Reproduziert und behoben am
    # 22.08.2026 (Review-Befund).
    if session_dir.is_symlink() or subroot.is_symlink():
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    target = subroot / f"agent-{run_id}.jsonl"
    if target.is_symlink():
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)
    try:
        # Zusaetzlich zur Symlink-Abweisung: die aufgeloesten Pfade muessen
        # ineinander liegen. Faengt, was oben durchrutschen koennte.
        if not target.resolve().is_relative_to(subroot.resolve()):
            return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)
    except OSError:
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)
    if not target.is_file():
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    if not adapter.transcript_allowed(agent, target):
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    # ``stamp_usage`` stillgelegt: es leitet die Config-Wurzel ueber
    # ``parent.parent.parent`` aus dem Pfad ab und unterstellt das Layout
    # ``<root>/projects/<cwd>/<sitzung>.jsonl``. Eine Subagenten-Datei liegt
    # zwei Ebenen tiefer — der Zeiger landete in einem FREMDEN Verzeichnis.
    # Preis, ehrlich benannt: das Kontextfenster eines Subagenten bleibt eine
    # Schaetzung. Lieber geschaetzt als aus der falschen Datei behauptet.
    quiet = dataclasses.replace(adapter, stamp_usage=lambda ev, p: None)

    try:
        data = read_history(
            target,
            quiet,
            limit=limit,
            before_uuid=before_uuid,
            observed_windows=await get_observed_model_windows(),
        )
    except OSError:
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    # Kein ``capabilities``-Block und kein ``aliveness``: die beschreiben einen
    # steuerbaren Live-Agenten. Ein Subagenten-Lauf ist ein abgeschlossenes
    # Protokoll, in das niemand hineintippen kann.
    data.pop("subagentRuns", None)
    data["subagent"] = run
    return data


@router.get("/agents/{agent_id}/chat/stream")
async def stream_agent_chat(
    agent_id: uuid.UUID,
    request: Request,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """SSE tail of the agent's live transcript. Wraps the shared
    ``_sse_generator`` — acquires the tailer (starting its poll task on the
    first connected client for this agent) before the first frame, and
    releases it in ``finally`` once the client disconnects (cancelling the
    poll task if this was the last client)."""
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return resolved

    # All DB work of this endpoint happens above. Release the connection
    # BEFORE the stream starts: FastAPI would otherwise unwind
    # Depends(get_session) only after the SSE response finishes — pinning
    # the connection and its implicit transaction for the whole stream
    # (up to 405 s, pool exhaustion incident 2026-09-14 / finding 2026-09-16).
    await release_session(session, route=request.url.path)

    agent, path, _adapter = resolved
    channel = RedisKeys.agent_chat_channel(str(agent_id))

    async def _generator():
        await tailer_manager.acquire(str(agent_id), path, agent)
        try:
            # ``state`` geht nur bei Aenderung ueber den Kanal. Laeuft der
            # Tailer schon (ein anderer Client sieht zu), bekaeme dieser
            # Client bis zur naechsten Aenderung keinen Zustand — „Status
            # unklar" waehrend der Agent sichtbar arbeitet (04.09.2026).
            cached = tailer_manager.last_state(str(agent_id))
            if cached is not None:
                yield {"event": "chat_event", "data": json.dumps({"kind": "state", **cached})}
            async for frame in _sse_generator([channel]):
                yield frame
        finally:
            await tailer_manager.release(str(agent_id))

    return EventSourceResponse(_generator())


async def _load_agent_or_404(agent_id: uuid.UUID, session: AsyncSession) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _chat_session_cwd(agent: Agent) -> str | None:
    """The working directory of the agent's live chat session, as its CLI
    recorded it — or ``None`` (no transcript, harness records none, privacy
    gate closed). Same adapter chain and the same fail-closed gate as the
    history endpoint: a Boss session that isn't MC work never lends its
    folder to the panel. Synchronous (file reads) — run via ``to_thread``.
    A transcript that can't be read is "no session folder", never a 500:
    the panel then falls back to the agent's other folders."""
    adapter = adapter_for(agent)
    try:
        tdir = adapter.resolve_transcript_dir(agent)
        if tdir is None:
            return None
        active = adapter.find_active_session(tdir)
        if active is None:
            return None
        path, _meta = active
        if not adapter.transcript_allowed(agent, path):
            return None
        return adapter.session_cwd(path)
    except (OSError, ValueError):
        return None


@router.get("/agents/{agent_id}/chat/diff")
async def get_chat_diff(
    agent_id: uuid.UUID,
    scope: str = Query("worktree", pattern="^(worktree|last-commit)$"),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Structured git diff of the repository that shows what the agent is
    doing: uncommitted changes incl. new files (``scope=worktree``, default)
    or the most recent commit (``scope=last-commit``). The payload carries a
    ``source`` block (``kind``/``repo``/``branch``/``path``) so the panel
    can say WHICH repository it shows and why. 404 ``{"reason":
    "no_workspace"}`` when no candidate folder holds a git repository.

    Which repository: ``workspace_diff.choose_repo`` — running task first,
    then the chat session's own folder (``TranscriptAdapter.session_cwd``,
    harness-neutral), then the most recently git-active repository among the
    session folder, the agent's workspace root and its last task. Both
    scopes resolve the SAME repository, so the two tabs never show two
    different projects."""
    agent = await _load_agent_or_404(agent_id, session)

    running_task_raw: str | None = None
    if agent.current_task_id:
        current = await session.get(Task, agent.current_task_id)
        if current and current.workspace_path:
            running_task_raw = current.workspace_path
    latest_raw = (
        await session.exec(
            select(Task.workspace_path)
            .where(Task.assigned_agent_id == agent.id, Task.workspace_path.is_not(None))
            .order_by(Task.updated_at.desc())
            .limit(1)
        )
    ).first()

    runtime = agent.agent_runtime
    root = resolve_workspace_path(agent.workspace_path) if agent.workspace_path else None
    cwd = await asyncio.to_thread(_chat_session_cwd, agent)
    session_dir = host_path_for_agent_cwd(runtime, root, cwd)
    boundary = cwd_boundary(runtime, root)

    def _pick_and_diff() -> dict[str, Any]:
        choice = choose_repo(
            running_task=resolve_workspace_path(running_task_raw) if running_task_raw else None,
            session_dir=session_dir,
            boundary=boundary,
            fallbacks=(
                root,
                resolve_workspace_path(latest_raw) if latest_raw else None,
            ),
        )
        result = workspace_diff(choice.path, scope)
        result["source"] = source_info(choice, display_path(runtime, root, choice.path))
        return result

    try:
        return await asyncio.to_thread(_pick_and_diff)
    except NoWorkspaceError:
        return JSONResponse(status_code=404, content=_NO_WORKSPACE)


@router.post("/agents/{agent_id}/chat/input", status_code=204)
async def post_chat_input(
    agent_id: uuid.UUID,
    body: ChatInputBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Types ``body.text`` into the agent's live session (tmux send-keys for
    cli-bridge agents, host-pty-bridge WS for Boss). 422 for empty/oversized
    text; 409 ``{"reason":"input_not_supported"}`` for host agents other than
    Boss (mirrors A2's runtime gating); 409 ``{"reason":"agent_starting"}``
    (docker only) when the pane never became ready within ``send_text``'s
    readiness gate — the CLI is still booting/loading plugins or a recycler
    respawn is mid-flight, and nothing was typed (see
    ``agent_chat_input._wait_for_send_readiness``). Headless (ACP) agents:
    409 ``{"reason":"acp_refused","error":code}`` when the chat daemon refused
    without putting a card into the chat itself."""
    agent = await _load_agent_or_404(agent_id, session)

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")
    if len(body.text) > _MAX_TEXT_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"text too long (max {_MAX_TEXT_LEN} chars)",
        )
    if _DISALLOWED_CONTROL_CHARS.search(body.text):
        raise HTTPException(
            status_code=422, detail="text contains disallowed control characters"
        )

    try:
        await send_text(agent, body.text)
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
    except AgentStartingError:
        return JSONResponse(status_code=409, content=_AGENT_STARTING)
    except AgentBusyError:
        # Kopflose Agenten (ACP): der Chat reiht eine Nachricht waehrend eines
        # laufenden Zugs ein (``mode=queue``). ``busy`` kommt nur noch von
        # einem Daemon, der die Warteschlange nicht kennt (altes Image) —
        # eine Absage, keine 500.
        return JSONResponse(status_code=409, content=_AGENT_BUSY)
    except AcpChatRefusedError as e:
        # Eine Absage des Chat-Daemons, die er NICHT selbst als Karte zeigt —
        # der Composer muss sie melden, sonst ginge die Nachricht spurlos weg.
        return JSONResponse(
            status_code=409, content={"reason": "acp_refused", "error": e.code}
        )
    except AcpChatUnreachableError as e:
        return _acp_unreachable(e)
    except BossDeliveryError as e:
        return _boss_delivery_failed(e)


@router.post("/agents/{agent_id}/chat/queue/clear")
async def post_chat_queue_clear(
    agent_id: uuid.UUID,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Takes back the follow-up messages a headless (ACP) agent's chat daemon
    is holding behind the running turn: ``{"dropped": [text, ...]}``, oldest
    first, straight from the daemon — the composer puts them back into the
    input (withdraw, edit, Stop). 409 ``{"reason":"input_not_supported"}``
    for TUI agents (their CLI keeps its own queue, see ``/chat/keys``) and for
    an agent image without the op; 502 ``acp_unreachable`` when the daemon
    does not answer."""
    agent = await _load_agent_or_404(agent_id, session)
    try:
        dropped = await clear_queue(agent)
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
    except AcpChatUnreachableError as e:
        return _acp_unreachable(e)
    return {"dropped": dropped}


@router.post("/agents/{agent_id}/chat/attachment", status_code=201)
async def post_chat_attachment(
    agent_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Nimmt eine Datei entgegen und gibt den absoluten Pfad zurueck, unter
    dem der Agent sie lesen kann.

    Abgelegt wird sie als Agenten-Referenz — dieselbe Ablage, die der
    Slack-Datei-Ingest schon benutzt (``reference_files.agent_id``, Migration
    0172). Das ist keine Bequemlichkeit, sondern der Grund, aus dem es die
    Besitz-Art ueberhaupt gibt: eine Datei, die der Operator top-level im
    Chat schickt, gehoert dem AGENTEN und keiner Aufgabe. Sie wird damit
    automatisch mit ihm geloescht (``delete_references_for(agent_id=…)`` in
    routers/agents.py) statt verwaist liegen zu bleiben.

    Der Composer haengt den Pfad danach an die Nachricht — die CLI liest die
    Datei selbst. Es gibt bewusst KEINE Typen-Beschraenkung und keinen
    20er-Deckel (Operator-Entscheid 19.08.2026): ob ein Agent eine Datei
    versteht, ist seine Sache, das UI legt nur ab. Gefaehrlich ist das nicht
    — aktive Inhalte liefert ``fs_service.read_stream`` grundsaetzlich als
    Download aus, nie inline.

    409 ``{"reason":"input_not_supported"}`` fuer Agenten, die ueberhaupt
    keinen Chat-Text annehmen (Host-Agenten ausser Boss): dort waere die
    Datei nur Platte ohne Empfaenger. 413 wenn zu gross, 422 bei einem
    unbrauchbaren oder leeren Upload."""
    agent = await _load_agent_or_404(agent_id, session)

    if not can_receive_input(agent):
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)

    contents = await file.read()
    if not contents:
        # Frueh und eigenstaendig: eine leere Datei ist kein Ingest-Problem,
        # sondern eine Auswahl, die niemandem nuetzt — der Agent bekaeme
        # einen Pfad auf 0 Bytes.
        raise HTTPException(status_code=422, detail="Die Datei ist leer.")

    try:
        ref = await store_reference(
            session,
            contents=contents,
            filename=file.filename or "",
            mime=file.content_type,
            agent_id=agent.id,
            uploaded_by="chat",
            # Die zwei Huerden, die fuer einen laufenden Chat nicht passen.
            allowed_mimes=None,
            max_files=None,
        )
    except ReferenceTooLargeError as exc:
        # Zu gross ist die einzige Ablehnung, die der Nutzer beim Auswaehlen
        # nicht sehen konnte — sie bekommt darum ihren eigenen Status, damit
        # das UI sie als Hinweis statt als Fehler zeigen kann. Eigene
        # Fehlerklasse statt Textsuche: ein Umformulieren der Meldung darf
        # den Status nie kippen.
        raise HTTPException(status_code=413, detail=str(exc))
    except ReferenceIngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "path": serialize_reference(ref)["abs_path"],
        "name": ref.original_name,
        "bytes": ref.size,
        "isImage": is_image_reference(ref.original_name),
        # Root + Unterpfad direkt aus der Ablage: das Frontend holt die Bytes
        # ueber den Files-Endpunkt und muss sie sonst aus dem absoluten Pfad
        # zurueckrechnen (siehe ChatAttachmentTile.toFilesRef).
        "root": "references",
        "subpath": ref.rel_path,
    }


@router.post("/agents/{agent_id}/chat/keys", status_code=204)
async def post_chat_keys(
    agent_id: uuid.UUID,
    body: ChatKeysBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Sends a sequence of allowlisted control keys (Escape/Enter/Up/Down/
    digits/y/n) to the agent's live session. 422 on any non-allowlisted key;
    409 ``{"reason":"input_not_supported"}`` for host agents other than
    Boss."""
    agent = await _load_agent_or_404(agent_id, session)

    if len(body.keys) > _MAX_KEYS_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"too many keys (max {_MAX_KEYS_LEN} per request)",
        )

    try:
        await send_keys(agent, body.keys)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
    except AcpChatUnreachableError as e:
        return _acp_unreachable(e)
    except BossDeliveryError as e:
        return _boss_delivery_failed(e)


@router.post("/agents/{agent_id}/chat/effort", status_code=204)
async def post_chat_effort(
    agent_id: uuid.UUID,
    body: ChatEffortBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Switches the agent's effort level via ``/effort <level>`` (v1:
    cli-bridge/docker agents only — Boss and every other host agent get 409
    ``{"reason":"input_not_supported"}``, no pane probe exists for them).
    422 on a non-allowlisted level; 409 ``{"reason":"agent_busy"}`` when the
    pane shows a working turn or an open permission prompt (refused before
    touching the TUI at all — Escape is this app's INTERRUPT key, not a
    neutral cleanup, wave-review I-1); 409
    ``{"reason":"effort_switch_rejected","message":str}`` when the CLI
    EXPLICITLY declined the switch (its own ``"Kept effort level as <X>"``
    wording, live-verified on Davinci — see
    ``agent_chat_input.EffortSwitchRejectedError``'s docstring) — the CLI's
    own message is included so the UI can show the operator WHY, distinct
    from 409 ``{"reason":"effort_switch_failed"}`` when verification simply
    timed out with no explicit answer either way (see
    ``agent_chat_input.set_effort``).

    NOTE (Phase-0 discovery, empirically verified): this also changes the
    agent's PERSISTED default effort level in its ``settings.json`` — Claude
    Code 2.1.233 has no way to change effort session-only, not even via the
    ``/model`` picker's "s" option (which does correctly scope a MODEL
    choice to the session, just not effort). Every chat-triggered effort
    switch is a durable change to what a fresh session for this agent starts
    at, until switched again."""
    agent = await _load_agent_or_404(agent_id, session)

    try:
        await set_effort(agent, body.level)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
    except AgentBusyError:
        return JSONResponse(status_code=409, content=_AGENT_BUSY)
    except EffortSwitchRejectedError as e:
        return JSONResponse(
            status_code=409,
            content={"reason": "effort_switch_rejected", "message": e.cli_message},
        )
    except EffortSwitchFailedError:
        return JSONResponse(status_code=409, content=_EFFORT_SWITCH_FAILED)
    except AcpChatUnreachableError as e:
        return _acp_unreachable(e)
    except BossDeliveryError as e:
        return _boss_delivery_failed(e)
