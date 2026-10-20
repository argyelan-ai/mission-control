"""Live chat view over an agent's Claude Code transcript — HTTP history page
plus a live SSE tail. Parsing/session-resolution lives in
``services/transcript_chat.py`` (A1-A3); this router only wires auth, the
404 gating contract, and the tailer's acquire/release lifecycle around it.
"""
from __future__ import annotations

import asyncio
import json
import dataclasses
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

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
    ElicitationNotPendingError,
    EffortSwitchFailedError,
    EffortSwitchRejectedError,
    InputNotSupportedError,
    InstallCommandBlockedError,
    QuestionOpenError,
    answer_elicitation,
    can_receive_input,
    clear_queue,
    effort_capabilities,
    mode_capabilities,
    model_options_capabilities,
    send_keys,
    send_text,
    set_effort,
    set_mode,
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
from app.models.activity import ActivityEvent
from app.services import chat_actions, chat_media, commit_llm
from app.services.activity import emit_event
from app.services.chat_changes import aggregate_changes
from app.services.chat_diff import capture_texts
from app.services.chat_task_activity import task_activity
from app.services.sse import _sse_generator, sse_response
from app.services.transcript_adapters import adapter_for
from app.services.checklist_todo_sync import chat_todo_sync
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
logger = logging.getLogger("mc.agent_chat")

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


class ChatElicitationAnswerBody(BaseModel):
    """P3: answer to a question card. ``content`` only with ``accept`` —
    keys and values must match the question's schema (checked by the chat
    daemon before omp sees them)."""

    action: str
    content: dict[str, Any] | None = None


class ChatModeBody(BaseModel):
    mode: str


#: A card id as the chat daemon mints it (``el-`` + 12 hex). Checked before
#: it travels into a ``docker exec`` argument.
_ELICITATION_ID_RE = re.compile(r"^el-[0-9a-f]{12}$")
#: An answer is one short form; anything larger is not a click.
_MAX_ELICITATION_CONTENT_BYTES = 32_000


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
        **await mode_capabilities(agent),
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
            # A fresh chat has no list of its own — but a task the agent
            # is working on still shows its list.
            "todos": await chat_todo_sync.chat_todos(session, agent, adapter, None),
            "capabilities": capabilities,
        }

    observed_windows = await get_observed_model_windows()
    # In a thread: with P2 a history read may store tool pictures (disk +
    # PIL); the event loop must never wait on that.
    history = await asyncio.to_thread(
        read_history,
        path,
        adapter,
        limit=limit,
        before_uuid=before_uuid,
        observed_windows=observed_windows,
        media=chat_media.context_for(agent, path),
    )
    history["session"]["aliveness"] = await resolve_aliveness(agent, path, adapter)
    # While the agent works a task, the panel shows THAT task's list: an ACP
    # task run writes its plan into its own session file, not into the chat
    # session this page reads (live 12.10.2026).
    history["todos"] = await chat_todo_sync.chat_todos(session, agent, adapter, history.get("todos"))
    history["capabilities"] = capabilities
    return history


@router.get("/agents/{agent_id}/chat/task-activity")
async def get_chat_task_activity(
    agent_id: uuid.UUID,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """What the agent's RUNNING task does, for the chat's task block.

    Since #898 the agent chat shows the chat daemon's session only; an ACP
    task runs in a session of its own, and its delivery, tools and thoughts
    had left the chat (operator finding 20.10.2026). This read brings them
    back as one separate block (``chat_task_activity``): ``deliveredAt``,
    the newest few ``events`` of the task session (same normalized events
    as the chat), the task session's live ``preview`` (a running thought:
    ``block: "thinking"``) and ``queued`` = more inbox tasks of this agent.
    ``{"applies": false}`` when the agent has no task or its harness runs
    the task inside the chat itself. Reads only (files + one count)."""
    agent = await _load_agent_or_404(agent_id, session)
    return await task_activity(session, agent)


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


@router.get("/agents/{agent_id}/chat/media/{media_id}")
async def get_chat_media(
    agent_id: uuid.UUID,
    media_id: str,
    variant: str = Query("thumb", pattern="^(thumb|full)$"),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """One picture an agent's tool produced (chat package P2): ``thumb`` (a
    small WebP for the tool card) or ``full`` (re-encoded, all metadata —
    EXIF, GPS, text chunks — dropped; the stored original is never sent).

    Same auth as the chat history. The id is the picture's sha256 and is
    looked up only in THIS agent's folder: another agent's id answers 404
    like an unknown one, and no answer names a path. ``nosniff`` + a
    sandbox CSP keep a picture a picture."""
    await _load_agent_or_404(agent_id, session)
    if not chat_media.is_media_id(media_id):
        raise HTTPException(status_code=404, detail="not found")
    found = await asyncio.to_thread(chat_media.open_variant, str(agent_id), media_id, variant)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    path, mime = found
    ext = chat_media.MIME_EXT.get(mime, "img")
    return FileResponse(
        path,
        media_type=mime,
        headers={
            "Cache-Control": "private, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f'inline; filename="mc-image-{media_id[:12]}.{ext}"',
        },
    )


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

    return sse_response(_generator())


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


@dataclasses.dataclass(frozen=True)
class _ChatFolders:
    """What MC knows about where a chat works (collected async, used in a
    thread). Calling it picks the repository: ``(RepoChoice,
    display_path)`` or ``NoWorkspaceError`` — shared by the diff, changes
    and action endpoints, so all of them name the same repository.

    ``session_dir`` is the host path of the chat session's folder (``None``
    when it recorded none or it is not readable here); ``cwd`` the folder
    as the agent itself names it."""

    runtime: str | None
    root: Path | None
    cwd: str | None
    session_dir: Path | None
    boundary: Path | None
    running_task: Path | None

    def __call__(self):
        choice = choose_repo(
            running_task=self.running_task,
            session_dir=self.session_dir,
            boundary=self.boundary,
            # A recorded folder that maps to nothing readable is NOT "no
            # folder": the root then must not stand in for it.
            workspace_root=self.root if self.cwd is None else None,
        )
        return choice, display_path(self.runtime, self.root, choice.path)


async def _chat_repo_picker(agent: Agent, session: AsyncSession) -> _ChatFolders:
    """Collects what the repo choice needs (DB + transcript, async) and
    returns a SYNCHRONOUS picker for ``asyncio.to_thread`` that yields
    ``(RepoChoice, display_path)`` or raises ``NoWorkspaceError``. Shared by
    the diff and the changes endpoint, so both name the same repository."""
    running_task_raw: str | None = None
    if agent.current_task_id:
        current = await session.get(Task, agent.current_task_id)
        if current and current.workspace_path:
            running_task_raw = current.workspace_path

    runtime = agent.agent_runtime
    root = resolve_workspace_path(agent.workspace_path) if agent.workspace_path else None
    cwd = await asyncio.to_thread(_chat_session_cwd, agent)
    return _ChatFolders(
        runtime=runtime,
        root=root,
        cwd=cwd,
        session_dir=host_path_for_agent_cwd(runtime, root, cwd),
        boundary=cwd_boundary(runtime, root),
        running_task=resolve_workspace_path(running_task_raw) if running_task_raw else None,
    )


@router.get("/agents/{agent_id}/chat/diff")
async def get_chat_diff(
    agent_id: uuid.UUID,
    scope: str = Query("worktree", pattern="^(worktree|last-commit)$"),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Structured git diff of the repository the chat works in: uncommitted
    changes incl. new files (``scope=worktree``, default) or the most recent
    commit (``scope=last-commit``). The payload carries a ``source`` block
    (``kind``/``repo``/``branch``/``path``/``ahead``/``behind``) so the
    panel can say WHICH repository it shows. 404 ``{"reason":
    "no_workspace"}`` when the chat's folder is not a git repository.

    Which repository: ``_chat_repo_picker`` — running task, else the chat session's
    own folder (``TranscriptAdapter.session_cwd``, harness-neutral). Never a
    "last used" repository found somewhere below (12.10.2026). Both scopes
    resolve the SAME repository, so the tabs never show two projects."""
    agent = await _load_agent_or_404(agent_id, session)
    pick = await _chat_repo_picker(agent, session)

    def _pick_and_diff() -> dict[str, Any]:
        choice, display = pick()
        result = workspace_diff(choice.path, scope)
        result["source"] = source_info(choice, display)
        return result

    try:
        return await asyncio.to_thread(_pick_and_diff)
    except NoWorkspaceError:
        return JSONResponse(status_code=404, content=_NO_WORKSPACE)


#: ``read_history`` page size that means "the whole session" — the changes
#: list is about everything this chat did, not the last page of it.
_WHOLE_SESSION = 10**9


@router.get("/agents/{agent_id}/chat/changes")
async def get_chat_changes(
    agent_id: uuid.UUID,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """What THIS chat session changed, per file (``chat_changes``), plus the
    repository block the git tabs would show (``repo``: the diff endpoint's
    ``source`` shape, or ``None`` when the chat's folder is no repository —
    the panel then shows only this list). Built from the per-edit diffs in
    the transcript, so it works for every harness and without git.

    No transcript yet, or a fresh session (``fresh_session``): an empty
    list, 200 — "nothing changed yet" is a state, not an error. Read-only.
    """
    agent = await _load_agent_or_404(agent_id, session)
    pick = await _chat_repo_picker(agent, session)

    def _repo() -> dict[str, Any] | None:
        try:
            choice, display = pick()
        except NoWorkspaceError:
            return None
        return source_info(choice, display)

    repo = await asyncio.to_thread(_repo)
    empty = {"files": [], "stats": {"files": 0, "additions": 0, "deletions": 0}, "repo": repo}

    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return empty
    _agent, path, adapter = resolved
    if fresh_session.marked_at(str(agent_id)) is not None and await asyncio.to_thread(
        fresh_session.is_stale, str(agent_id), path
    ):
        return empty

    def _read() -> dict[str, Any]:
        # capture_texts(): aggregate_changes needs each edit's whole-file
        # before/after to report the NET +/- (chat_changes module docstring)
        # instead of summing every edit's own — never a problem for THIS
        # endpoint's one-shot response, which never puts a diff's raw
        # ``_texts`` into what it returns.
        with capture_texts():
            history = read_history(path, adapter, limit=_WHOLE_SESSION)
        try:
            cwd = adapter.session_cwd(path)
        except (OSError, ValueError):
            cwd = None
        return aggregate_changes(history["events"], cwd)

    changes = await asyncio.to_thread(_read)
    changes["repo"] = repo
    recorded = await _recorded_reverts(session, agent.id, path.stem)
    if recorded:
        await asyncio.to_thread(_mark_reverted, changes["files"], recorded, pick, path, adapter)
    if repo is not None:
        changes["actions"] = await asyncio.to_thread(_action_availability, pick)
    return changes


# ══════════════════════════════════════════════════════════════════════════
# Changes panel actions (Changes panel, part 3): revert file · commit · open PR
# ══════════════════════════════════════════════════════════════════════════
#
# Operator-initiated only (``require_user``: an agent token can't call
# these), each behind an explicit confirmation the server insists on
# (``confirm: true``), each audited as an activity event with actor
# "operator" — refusals included. ADR-089: the operator's tap is the
# approval; the never-list holds (no force push, no push to the default
# branch, no hook bypass). Logic and reasons: ``services/chat_actions.py``.

_REVERTED_EVENT = "chat.file_reverted"


class ChatRevertBody(BaseModel):
    path: str
    confirm: bool = False


class ChatCommitBody(BaseModel):
    message: str
    confirm: bool = False


class ChatPrBody(BaseModel):
    title: str
    body: str | None = None
    confirm: bool = False


def _refused(e: chat_actions.Refused) -> JSONResponse:
    return JSONResponse(status_code=e.status, content=e.body())


def _actor(user: Any) -> dict[str, Any]:
    return {"actor": "operator", "userId": str(getattr(user, "id", "") or "") or None}


async def _audit(session: AsyncSession, agent: Agent, event_type: str, title: str,
                 detail: dict[str, Any], *, severity: str = "info") -> None:
    """One activity event per action or refusal — never lets an audit
    hiccup turn a done action into a 500 (the action already happened)."""
    try:
        await emit_event(session, event_type, title, severity=severity,
                         agent_id=agent.id, detail=detail)
    except Exception:  # noqa: BLE001 — audit is best effort AFTER the act
        logger.warning("chat action audit failed: %s", event_type, exc_info=True)


def _agent_busy(agent_id: uuid.UUID) -> bool:
    """The chat is mid-turn (as the live stream last saw it): writing into
    its files now would race the agent's own edits."""
    state = tailer_manager.last_state(str(agent_id))
    # task_working: a TASK turn edits the same files (follow-up to ADR-097).
    return isinstance(state, dict) and state.get("status") in ("working", "task_working")


async def _recorded_reverts(
    session: AsyncSession, agent_id: uuid.UUID, session_id: str
) -> dict[str, tuple[str, int]]:
    """``{agent-side path: (content sha after the revert, edits it undid)}``
    for this chat session — the newest record per file wins."""
    rows = (
        await session.exec(
            select(ActivityEvent)
            .where(ActivityEvent.agent_id == agent_id, ActivityEvent.event_type == _REVERTED_EVENT)
            .order_by(ActivityEvent.created_at.desc())
            .limit(500)
        )
    ).all()
    out: dict[str, tuple[str, int]] = {}
    for row in rows:
        d = row.detail or {}
        if d.get("session") != session_id or not isinstance(d.get("key"), str):
            continue
        out.setdefault(d["key"], (str(d.get("resultSha")), int(d.get("toolCalls") or 0)))
    return out


def _allowed_roots(folders: "_ChatFolders") -> list[Path]:
    """Folders an action may touch: the chat session's own folder, and a
    running task's repository."""
    roots: list[Path] = []
    if folders.session_dir is not None:
        roots.append(folders.session_dir)
    try:
        choice, _display = folders()
    except NoWorkspaceError:
        choice = None
    if choice is not None and choice.kind == "task":
        roots.append(choice.path)
    return roots


def _mark_reverted(files: list[dict[str, Any]], recorded: dict[str, tuple[str, int]],
                   folders: "_ChatFolders", path: Path, adapter: Any) -> None:
    """``reverted: true`` on a listed file MC put back in this session and
    that still holds exactly that content (and no new edit came since)."""
    try:
        cwd = adapter.session_cwd(path)
    except (OSError, ValueError):
        cwd = None
    roots = _allowed_roots(folders)
    for f in files:
        key = chat_actions._normalise(f["path"], cwd)
        rec = recorded.get(key)
        if rec is None or rec[1] != f.get("edits"):
            continue
        try:
            target, _root = chat_actions.resolve_target(
                key, runtime=folders.runtime, workspace_root=folders.root, allowed_roots=roots)
            f["reverted"] = chat_actions.sha256_text(chat_actions.read_current(target)) == rec[0]
        except chat_actions.Refused:
            continue


def _action_availability(folders: "_ChatFolders") -> dict[str, Any]:
    """Whether Commit / Open PR can run for the chat's repository, and if
    not, why (a code) — so the panel never offers a button that can only
    fail."""
    try:
        choice, _display = folders()
    except NoWorkspaceError:
        return {"commit": {"ok": False, "reason": "no_repo"}, "pr": {"ok": False, "reason": "no_repo"}}
    out: dict[str, Any] = {}
    try:
        chat_actions.commit_guard(choice.path)
        pending = len(chat_actions.pending_files(choice.path))
        out["commit"] = {"ok": pending > 0, "reason": None if pending else "nothing_to_commit",
                         "files": pending}
    except chat_actions.Refused as e:
        out["commit"] = {"ok": False, "reason": e.reason}
    try:
        chat_actions.pr_guard(choice.path)
        out["pr"] = {"ok": True, "reason": None}
    except chat_actions.Refused as e:
        out["pr"] = {"ok": False, "reason": e.reason}
    return out


@router.post("/agents/{agent_id}/chat/revert")
async def post_chat_revert(
    agent_id: uuid.UUID,
    body: ChatRevertBody,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Puts one file this chat changed back to its state before the chat's
    edits (``chat_actions.revert_file``). Only a file in this chat's
    Changes list, only inside the chat's folder; refused — never
    overwritten — when the file changed since the chat's last edit
    (``changed_externally``). A repeat after success answers ``already``."""
    agent = await _load_agent_or_404(agent_id, session)
    audit = {**_actor(current_user), "path": body.path[:500]}
    if not body.confirm:
        return JSONResponse(status_code=400, content={"reason": "confirm_required"})
    if _agent_busy(agent_id):
        return JSONResponse(status_code=409, content={"reason": "agent_busy"})

    folders = await _chat_repo_picker(agent, session)
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return JSONResponse(status_code=404, content={"reason": "not_changed_by_chat"})
    _agent, tpath, adapter = resolved
    recorded = await _recorded_reverts(session, agent.id, tpath.stem)

    def _do() -> dict[str, Any]:
        with capture_texts():
            history = read_history(tpath, adapter, limit=_WHOLE_SESSION)
        try:
            cwd = adapter.session_cwd(tpath)
        except (OSError, ValueError):
            cwd = None
        found = chat_actions.collect_edits(history["events"], cwd, body.path)
        target, root = chat_actions.resolve_target(
            found.key, runtime=folders.runtime, workspace_root=folders.root,
            allowed_roots=_allowed_roots(folders))
        rec = recorded.get(found.key)
        if rec is not None and rec[1] == found.tool_calls:
            if chat_actions.sha256_text(chat_actions.read_current(target)) == rec[0]:
                return {"status": "already", "key": found.key, "toolCalls": found.tool_calls,
                        "edits": len(found.edits), "resultSha": rec[0]}
        result = chat_actions.revert_file(target, root, found.edits)
        return {**result, "key": found.key, "toolCalls": found.tool_calls, "edits": len(found.edits)}

    try:
        result = await asyncio.to_thread(_do)
    except chat_actions.Refused as e:
        await _audit(session, agent, "chat.action_refused", f"Revert refused: {body.path[:120]}",
                     {**audit, "action": "revert", "reason": e.reason}, severity="warning")
        return _refused(e)

    if result["status"] == "reverted":
        await _audit(session, agent, _REVERTED_EVENT, f"File reverted: {body.path[:120]}", {
            **audit, "session": tpath.stem, "key": result["key"], "method": result.get("method"),
            "deleted": result.get("deleted", False), "toolCalls": result["toolCalls"],
            "resultSha": result["resultSha"],
        })
    return {
        "status": result["status"],
        "path": body.path,
        "method": result.get("method"),
        "deleted": result.get("deleted", False),
        "edits": result["toolCalls"],
    }


async def _chat_repo_or_refuse(agent: Agent, session: AsyncSession) -> tuple[Path, "_ChatFolders"]:
    folders = await _chat_repo_picker(agent, session)

    def _pick() -> Path:
        try:
            choice, _display = folders()
        except NoWorkspaceError:
            raise chat_actions.Refused("no_repo", 404) from None
        return choice.path

    return await asyncio.to_thread(_pick), folders


async def _recent_user_messages(agent_id: uuid.UUID, session: AsyncSession) -> list[str]:
    """The chat's last few operator messages, for the LLM suggestion's
    intent context (``commit_llm``). Best effort: any resolution failure
    (no transcript, odd adapter, …) just loses that context — it never
    breaks the draft, which has a mechanical fallback regardless."""
    try:
        resolved = await _resolve_transcript_path(agent_id, session)
        if isinstance(resolved, JSONResponse):
            return []
        _agent, tpath, adapter = resolved

        def _read() -> list[str]:
            history = read_history(tpath, adapter, limit=_WHOLE_SESSION)
            return commit_llm.extract_user_messages(history["events"])

        return await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001 — best-effort context, never breaks the draft
        logger.warning("commit draft: could not read chat history for LLM context", exc_info=True)
        return []


@router.get("/agents/{agent_id}/chat/commit/draft")
async def get_chat_commit_draft(
    agent_id: uuid.UUID,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """What a commit would take and a suggested message (editable in the
    confirmation sheet): ``{branch, files: [{path, status, additions,
    deletions}], message, source, mechanicalMessage?}``. ``message`` is the
    local model's suggestion (``commit_llm``) when it answered something
    usable in time; otherwise the deterministic mechanical message
    (``source: "fallback"``) — the sheet always has something to show and
    always lets the operator edit it. Read-only (the LLM call makes no
    change, just reads the diff)."""
    agent = await _load_agent_or_404(agent_id, session)
    try:
        repo, _folders = await _chat_repo_or_refuse(agent, session)

        def _draft() -> dict[str, Any]:
            branch = chat_actions.commit_guard(repo)
            files = chat_actions.pending_files(repo)
            return {"branch": branch, "files": files,
                    "message": chat_actions.suggest_commit_message(files), "source": "fallback"}

        draft = await asyncio.to_thread(_draft)
    except chat_actions.Refused as e:
        return _refused(e)

    if not draft["files"]:
        return draft

    diff_text = await asyncio.to_thread(chat_actions.diff_text_for_commit, repo, draft["files"])
    user_messages = await _recent_user_messages(agent_id, session)
    suggestion = await commit_llm.suggest_commit_message(
        branch=draft["branch"], files=draft["files"], diff_text=diff_text, user_messages=user_messages,
    )
    if suggestion:
        draft["mechanicalMessage"] = draft["message"]
        draft["message"] = suggestion
        draft["source"] = "llm"
    return draft


@router.post("/agents/{agent_id}/chat/commit")
async def post_chat_commit(
    agent_id: uuid.UUID,
    body: ChatCommitBody,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Commits every uncommitted change of the chat's repository with the
    operator's message. Local only — nothing is pushed."""
    agent = await _load_agent_or_404(agent_id, session)
    if not body.confirm:
        return JSONResponse(status_code=400, content={"reason": "confirm_required"})
    if _agent_busy(agent_id):
        return JSONResponse(status_code=409, content={"reason": "agent_busy"})
    audit = _actor(current_user)
    try:
        repo, _folders = await _chat_repo_or_refuse(agent, session)
        result = await asyncio.to_thread(chat_actions.commit_all, repo, body.message[:10_000])
    except chat_actions.Refused as e:
        await _audit(session, agent, "chat.action_refused", "Commit refused",
                     {**audit, "action": "commit", "reason": e.reason}, severity="warning")
        return _refused(e)
    if result["status"] == "committed":
        await _audit(session, agent, "chat.committed", f"Committed {result['sha']} on {result['branch']}", {
            **audit, "repo": repo.name, "branch": result["branch"], "sha": result["sha"],
            "subject": result.get("subject"),
        })
    return result


@router.get("/agents/{agent_id}/chat/pr/draft")
async def get_chat_pr_draft(
    agent_id: uuid.UUID,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """What a pull request would be: ``{branch, base, title, ahead,
    uncommitted}`` (title = the newest commit's subject, editable).
    Read-only, local refs only."""
    agent = await _load_agent_or_404(agent_id, session)
    try:
        repo, _folders = await _chat_repo_or_refuse(agent, session)
        return await asyncio.to_thread(chat_actions.pr_guard, repo)
    except chat_actions.Refused as e:
        return _refused(e)


@router.post("/agents/{agent_id}/chat/pr")
async def post_chat_pr(
    agent_id: uuid.UUID,
    body: ChatPrBody,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Pushes the chat's branch (a normal push, never ``--force``, never
    the default branch) and opens a pull request into the default branch
    through ``GitService`` — the path MC's task flow uses. An open PR for
    the branch is returned instead of a second one."""
    agent = await _load_agent_or_404(agent_id, session)
    if not body.confirm:
        return JSONResponse(status_code=400, content={"reason": "confirm_required"})
    if not body.title.strip():
        return JSONResponse(status_code=400, content={"reason": "title_required"})
    if _agent_busy(agent_id):
        return JSONResponse(status_code=409, content={"reason": "agent_busy"})
    audit = _actor(current_user)
    from app.services.git_service import git_service  # lazy: dispatch import cycle

    try:
        repo, _folders = await _chat_repo_or_refuse(agent, session)
        plan = await asyncio.to_thread(chat_actions.pr_guard, repo)
        cwd = str(repo)
        try:
            await git_service.push_branch(cwd, plan["branch"])
        except RuntimeError as exc:
            raise chat_actions.Refused("push_failed", 502, detail=_redact_git_error(exc)) from None
        existing = await _open_pr_for_branch(git_service, cwd, plan["branch"])
        if existing:
            url, status = existing, "exists"
        else:
            pr_body = (body.body or "").strip() or "Opened from a Mission Control chat."
            try:
                url = await git_service.create_pr(cwd, title=body.title.strip()[:256],
                                                  body=pr_body[:20_000], base=plan["base"])
            except RuntimeError as exc:
                raise chat_actions.Refused("pr_failed", 502, detail=_redact_git_error(exc)) from None
            status = "opened"
    except chat_actions.Refused as e:
        await _audit(session, agent, "chat.action_refused", "Open PR refused",
                     {**audit, "action": "pr", "reason": e.reason}, severity="warning")
        return _refused(e)
    url = _first_url(url)
    await _audit(session, agent, "chat.pr_opened", f"PR {status}: {url}", {
        **audit, "repo": repo.name, "branch": plan["branch"], "base": plan["base"],
        "url": url, "status": status,
    })
    return {"status": status, "url": url, "branch": plan["branch"], "base": plan["base"]}


_URL_RE = re.compile(r"https://\S+")
_TOKEN_RE = re.compile(r"(https?://)[^/\s@]+@")


def _first_url(text: str) -> str:
    m = _URL_RE.search(text or "")
    return m.group(0) if m else (text or "").strip()


def _redact_git_error(exc: Exception) -> str:
    """git/gh's message without credentials embedded in a URL."""
    return _TOKEN_RE.sub(r"\1***@", str(exc))[:300]


async def _open_pr_for_branch(git_service: Any, cwd: str, branch: str) -> str | None:
    """The URL of an OPEN pull request for ``branch``, or ``None``."""
    try:
        out = await git_service._run_cmd(
            "gh", "pr", "list", "--head", branch, "--state", "open",
            "--json", "url", "--jq", ".[0].url // empty", cwd=cwd,
        )
    except RuntimeError:
        return None
    return out.strip() or None


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
    without putting a card into the chat itself. 409
    ``{"reason":"install_blocked","command":str}`` when the message's first
    slash command would install/modify a plugin, marketplace, MCP server or
    skill — the operator-approval-card flow for that does not exist yet
    (decided 12.10.2026), so chat refuses it outright rather than letting
    the agent's own CLI act on it unsupervised."""
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
    except InstallCommandBlockedError as e:
        return JSONResponse(
            status_code=409,
            content={"reason": "install_blocked", "command": e.command},
        )
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


@router.post("/agents/{agent_id}/chat/elicitation/{elicitation_id}")
async def post_chat_elicitation_answer(
    agent_id: uuid.UUID,
    elicitation_id: str,
    body: ChatElicitationAnswerBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Answers a question card (chat package P3) of a headless (ACP) agent:
    ``action`` = ``accept`` (with ``content``) | ``decline`` | ``cancel``.
    Same operator gate as every other chat control (admin role). 200
    ``{"status": "answered"|"declined"|"cancelled", "stopped": bool}``
    (``stopped``: cancel on omp's plan approval also stopped the turn —
    there a plain cancel means "keep planning"); 422 for a malformed id
    or an answer that does not fit the question; 409
    ``{"reason":"elicitation_not_pending"}`` when the card is no longer open
    (answered elsewhere, its turn ended, the chat daemon restarted); 409
    ``input_not_supported`` for agents without an ACP chat (or an image
    without the op); 502 ``acp_unreachable`` when the daemon does not
    answer."""
    agent = await _load_agent_or_404(agent_id, session)
    if not _ELICITATION_ID_RE.match(elicitation_id):
        raise HTTPException(status_code=422, detail="malformed elicitation id")
    if body.content is not None and len(
        json.dumps(body.content, ensure_ascii=False)
    ) > _MAX_ELICITATION_CONTENT_BYTES:
        raise HTTPException(status_code=422, detail="answer too large")

    try:
        outcome = await answer_elicitation(agent, elicitation_id, body.action, body.content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ElicitationNotPendingError:
        return JSONResponse(status_code=409, content={"reason": "elicitation_not_pending"})
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
    except AgentStartingError:
        return JSONResponse(status_code=409, content=_AGENT_STARTING)
    except AgentBusyError:
        return JSONResponse(status_code=409, content=_AGENT_BUSY)
    except AcpChatRefusedError as e:
        return JSONResponse(status_code=409, content={"reason": "acp_refused", "error": e.code})
    except AcpChatUnreachableError as e:
        return _acp_unreachable(e)
    return outcome


@router.post("/agents/{agent_id}/chat/mode", status_code=204)
async def post_chat_mode(
    agent_id: uuid.UUID,
    body: ChatModeBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Switches a headless (ACP) agent's session mode (chat package P3:
    ``plan`` / ``default``) through ``config id=mode``. Only modes the agent
    itself reports in ``capabilities.modeOptions``: 422 for any other value,
    409 ``input_not_supported`` when the agent reports no mode switch, 409
    ``question_open`` while a question card is open, 409 ``agent_busy``
    while a turn runs (switching then left an open plan question
    unanswered — live 13.10.2026), 409 ``acp_refused`` when the agent
    declined (the chat shows its own error card), 502 ``acp_unreachable``
    when the daemon does not answer."""
    agent = await _load_agent_or_404(agent_id, session)
    try:
        await set_mode(agent, body.mode)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except QuestionOpenError:
        return JSONResponse(status_code=409, content={"reason": "question_open"})
    except AgentBusyError:
        return JSONResponse(status_code=409, content=_AGENT_BUSY)
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
    except AcpChatRefusedError as e:
        return JSONResponse(status_code=409, content={"reason": "acp_refused", "error": e.code})
    except AcpChatUnreachableError as e:
        return _acp_unreachable(e)
