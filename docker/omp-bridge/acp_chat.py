#!/usr/bin/env python3
"""
acp_chat.py — long-lived chat daemon for the OMP_DRIVER=acp path.

WHY: tasks already run over ACP (`bridge.run_acp_once`, one process per
attempt), but the Sessions chat still typed into the native TUI in tmux
window 0 — two brains, and every composer control (effort chip, /model, Stop)
was a key press against a console an ACP agent should not need. This daemon
gives the chat its OWN long-lived ACP session: one `omp acp` child, ONE ACP
session that survives restarts via `session/load`, one turn at a time.

Two things ACP itself does not give the chat, so the daemon does them:

- a follow-up QUEUE: `prompt` with `mode: "queue"` holds a message sent while
  a reply runs and delivers it when that turn ends (FIFO, `queue_clear` takes
  the held ones back). omp has no steer over ACP — a second `session/prompt`
  mid-turn CANCELS the running turn (omp `AcpAgent.prompt`, 18.1.10, re-checked
  in the 18.10.1 source) — so the
  daemon never forwards one early. Steering needs omp's RPC mode (`steer`).
- `new_session` for `/new` and `/clear`: omp does not advertise either over
  ACP, so typed as text they only reached the model. A fresh `session/new`
  with its own transcript file is what a real reset looks like.

Spec: docs/specs/chat-over-acp.md. Contract in one picture:

    acp_chat_ctl.py <op>  ──unix socket──►  ChatSocketServer
                                                 │
                                                 ▼
                                            ChatSession
                                              │      │
                          transcript *.jsonl ◄┘      └─► previews/*.jsonl
                          (what the backend chat reader tails — both under
                          `<session folder>/.mc/`, where omp never looks)

Everything the operator must SEE is a transcript line, written through the
SAME mapper the task path uses (`acp_chat_events.ACPEventMapper`): the user
turn, the streamed preview, exactly ONE final assistant line — and failures
as `chat_error` lines, because an error only the log knows about is an error
the operator debugs blind.

Run: `python3 acp_chat.py --serve [--socket PATH]` (tmux window 3).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import random
import re
import socket
import socketserver
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import acp_chat_events  # noqa: E402
import acp_client  # noqa: E402
import acp_model  # noqa: E402
import model_errors  # noqa: E402
import sink_migration  # noqa: E402

logger = logging.getLogger("acp_chat")

#: Provider/quota failures worth their own code — the operator's fix is a key
#: or a budget, not a retry, so the chat must say so instead of "rpc_error".
_PROVIDER_ERROR_RE = re.compile(
    r"\b(401|403|429)\b|unauthor|forbidden|quota|insufficient|rate.?limit|credit",
    re.IGNORECASE,
)

#: Preview flush throttle, same numbers as bridge.run_acp_once: one line per
#: chunk is pointless I/O for a replace-me slot.
_PREVIEW_GROWTH_CHARS = 200
_PREVIEW_INTERVAL_S = 0.25

_STATE_VERSION = 1

#: Session updates that only change the daemon's STATE (mirrored into
#: acp-chat-state.json), never a chat line of their own. While omp replays a
#: loaded session they are the only updates applied (`_muted_replay`).
_STATE_UPDATES = frozenset({
    "config_option_update", "available_commands_update",
    "current_mode_update", "session_info_update",
})

#: `prompt`/`new_session` modes. `now` refuses while a turn runs — the
#: contract of the machine callers (bridge wake-ups retry on their own poll).
#: `queue` holds the job and runs it when the running turn ends — the chat.
MODE_NOW = "now"
MODE_QUEUE = "queue"
_MODES = (MODE_NOW, MODE_QUEUE)

#: Held jobs at most. A cap the operator never meets by typing, but a stuck
#: turn plus a retrying caller must not grow the queue without bound.
MAX_QUEUE = 20
#: The state mirror shows a held message's text up to this length.
_QUEUE_PREVIEW_CHARS = 2000
#: How much of omp's own session file the end of a turn reads at most
#: (S1-R6): one turn's lines, written moments ago.
_OMP_TURN_READ_BYTES = 2 * 1024 * 1024
#: The longest text a card offers to send again (`data.retryText`, the
#: backend's `_RETRY_TEXT_MAX_CHARS`). A longer one is left out — never cut:
#: "Send again" must send exactly what the operator wrote (S1-R5, F3).
RETRY_TEXT_MAX_CHARS = 8000

#: The card when a daemon restart cut a running reply off (S1-R5). Nothing
#: is ever sent again on its own; the card offers the text instead.
TURN_INTERRUPTED = "turn_interrupted"
_TURN_INTERRUPTED_TEXT = (
    "The chat restarted while a reply was running — nothing was resent "
    "automatically."
)
#: The same card for a turn the operator did not write (or nobody knows who
#: did): no claim about resending — hermes-bridge delivers a task again on
#: its own after a restart.
_TURN_INTERRUPTED_NEUTRAL_TEXT = "The chat restarted while a reply was running."

#: The card when the model's endpoint does not answer (S1-R6): omp gave up
#: and its error line is the turn's only "answer". The chat itself is at
#: rest again — there is no "waiting for the model" chat state; the card
#: says it. Like `turn_interrupted`, the promise about the message only for
#: the operator's own text, which the card then offers to send again.
MODEL_UNREACHABLE = model_errors.MODEL_UNREACHABLE
_MODEL_UNREACHABLE_TEXT = (
    "The model is not answering — check the Runtimes page. "
    "Your message is not lost."
)
_MODEL_UNREACHABLE_NEUTRAL_TEXT = "The model is not answering — check the Runtimes page."

#: Who wrote a prompt (`prompt(..., origin=)`, the control protocol's
#: `origin` field). ORIGIN_OPERATOR: the operator's own message (the
#: composer) — the only text a card ever offers to send again. "task" /
#: "nudge": Mission Control wrote it (the names of the task path,
#: `ACPEventMapper.map_user_prompt`); the chat line is then an MC event, not
#: an operator bubble. Anything else, or nothing (an older caller,
#: hermes-bridge's dispatch, the omp bridge's wake-up): unknown — shown as
#: before, never offered again. Fail-closed: a caller that forgets to say
#: who wrote it costs a button, never a second run of a task.
ORIGIN_OPERATOR = "operator"
_MC_ORIGINS = ("task", "nudge")
_STATE_FILENAME = "acp-chat-state.json"
_PERSIST_FILENAME = "acp-chat.json"
_DEFAULT_SOCKET_NAME = "acp-chat.sock"

#: omp (since 18.6.3) refuses `session/load` under ACP when the session's
#: last model is not in models.yml — no fallback without a UI (omp 18.10.1:
#: `throw new Error(`Could not restore model ${provider/id}`)`). Every model
#: switch makes the open conversation unloadable after a restart (ADR-086;
#: P4 decides the fix). The model is the first word after the sentence.
_MODEL_MISSING_RE = re.compile(r"Could not restore model\b[ \t]*(?P<model>[^\s()\"'`,;]*)")

#: Where `start()` found the session to resume (`_read_persisted`).
RESUME_POINTER = "pointer"
RESUME_STATE = "state"
RESUME_NONE = "none"

#: The `chat_notice` when nothing names the open conversation but the tree
#: holds an earlier chat (frontend: chat.notice.chat_started_fresh).
CHAT_STARTED_FRESH = "chat_started_fresh"
_CHAT_STARTED_FRESH_TEXT = (
    "The chat could not tell which conversation was open — a new one "
    "started. Earlier conversations are kept."
)

#: The `chat_notice` on top of a conversation the daemon opened IN PLACE of
#: another one (incident 21.10.2026: a `new_session` job left the operator
#: an empty chat and nothing said why). Frontend: chat.notice.new_conversation
#: ("New conversation started · 17:07 · the previous one is saved").
#: `previous`/`current` are the two session ids — the slot the later "show
#: previous" link (P4) reads —, `via` says how (VIA_*), `reason` why a resume
#: failed, `requestId` which request of MC's backend asked (none: not MC).
#: `kept` says whether the previous conversation's copy is on disk: only then
#: does the card promise that it is saved — but it comes either way, no reset
#: is silent.
NEW_CONVERSATION = "new_conversation"
_NEW_CONVERSATION_TEXT = "New conversation started — the previous one is saved."
_NEW_CONVERSATION_NOT_KEPT_TEXT = "New conversation started."
#: `via` of a reset (card and the state mirror's `resets`): the control op
#: (`/new`, `/clear`), a daemon start that could not resume (daemon restart,
#: container recreate), a respawned omp child that could not resume.
VIA_NEW_SESSION = "new_session"
VIA_START = "start"
VIA_CHILD_RESTART = "child_restart"
_VIAS = (VIA_NEW_SESSION, VIA_START, VIA_CHILD_RESTART)
#: What became of a reset (`resets[].outcome`): a new conversation began;
#: omp refused `session/new` (the current one goes on); a held `/new` was
#: taken back (`queue_clear`); a restart dropped it before it ran.
RESET_DONE = "done"
RESET_FAILED = "failed"
RESET_WITHDRAWN = "withdrawn"
RESET_LOST = "lost"
_RESET_OUTCOMES = (RESET_DONE, RESET_FAILED, RESET_WITHDRAWN, RESET_LOST)
#: How many resets the state mirror keeps (`resets`, newest last). The
#: backend reads them into activity events every few seconds; a burst
#: larger than this between two reads loses the oldest from the mirror.
RESET_HISTORY_MAX = 20
#: Shapes the daemon accepts for what it keeps about a reset. The request id
#: comes from whoever reaches the socket, the history from a file on the
#: agent's own mount: anything else is dropped, never stored.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RESET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_REASON_RE = re.compile(r"[a-z_]{1,40}")
_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_socket_path() -> str:
    """`$OMP_HOME/acp-chat.sock`, with the image's OMP_HOME as fallback."""
    home = os.environ.get("OMP_HOME") or "/home/agent/.omp"
    return os.path.join(home, _DEFAULT_SOCKET_NAME)


def default_cwd() -> str:
    """The ACP working directory — the same one the task path pins, so chat
    and tasks land in ONE sessions folder (see acp_chat_events.session_dir)."""
    return (
        os.environ.get("OMP_ACP_CWD")
        or os.environ.get("OMP_DEFAULT_CWD")
        or "/workspace"
    )


class ChatSession:
    """One long-lived ACP chat session: prompts in, transcript lines out.

    `client_factory` is injected so tests drive an in-memory fake; production
    passes a factory building an `acp_client.ACPClient`. Thread-safe: the
    socket server calls prompt/cancel/config from connection threads while a
    turn runs in the worker thread.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        cwd: str,
        state_dir: Path | str,
        sessions_dir: Path | str,
        *,
        driver: str = "omp",
        permission_policy: Optional[str] = None,
        prompt_timeout: float = 3600.0,
        task_id: str = "",
        model: Optional[str] = None,
        model_resolver: Optional[Callable[[], Optional[str]]] = None,
        elicitation: bool = False,
    ):
        self._client_factory = client_factory
        self._cwd = str(cwd)
        # The fully-qualified `<provider>/<model>` every session is pinned
        # to right after session/new AND session/load (see _pin_model).
        # None = the agent binary picks (Hermes: `hermes acp` has its own
        # model config, there is nothing to pin).
        self._model = (model or "").strip() or None
        # Re-reads the selector at pin time (acp_model.current_selector in
        # production): a runtime switch re-renders omp.env inside the running
        # container, and this daemon lives for days — the selector of its
        # boot environment goes stale (incident 19.10.2026). None = only the
        # fixed `model` above (tests, Hermes).
        self._model_resolver = model_resolver
        self._state_dir = Path(state_dir)
        self._sessions_dir = Path(sessions_dir)
        self._driver = driver
        self._permission_policy = permission_policy or os.environ.get(
            "OMP_ACP_PERMISSIONS", "ask"
        )
        self._prompt_timeout = prompt_timeout
        self._task_id = task_id
        # P3: ask the operator (question cards, plan approval). Opt-in per
        # daemon: the omp container chat turns it on (build_session); the
        # host-side chat bridge has no `answer` route yet and stays off.
        self._elicitation_enabled = bool(elicitation)

        self._client: Any = None
        self._session_id: Optional[str] = None
        # Where the last `_connect` found the session it tried to resume:
        # RESUME_POINTER / RESUME_STATE / RESUME_NONE ("restart" after a
        # crash of the omp child). Logged; the live check after a deploy
        # reads it from the log.
        self._resume_source = RESUME_NONE
        self._mapper = acp_chat_events.ACPEventMapper()
        # Entry ids are the tailer's dedup key. A fresh mapper starts at
        # seq=0, so a restarted daemon appending to the SAME transcript file
        # would re-issue acp00001… and the backend's seen-set would swallow
        # the new lines. A random start offset makes a collision across
        # restarts practically impossible.
        self._mapper.seq = random.randrange(1 << 28)
        # ACP names no tool; omp's own file of this session does (P3).
        self._mapper.tool_name_resolver = self._resolve_tool_name
        self._sink: Optional[acp_chat_events.ChatEventSink] = None
        self._preview: Optional[acp_chat_events.PreviewEventSink] = None

        self._lock = threading.RLock()
        self._idle = threading.Event()
        self._idle.set()
        self._busy = False
        self._turn = 0
        self._config_options: list[dict] = []
        # Counts every option list the agent hands over on its own (session
        # result, config_option_update). config() compares it across its
        # request: a push that arrived meanwhile beats the reply.
        self._config_pushes = 0
        # One config request at a time, request AND apply: replies are not
        # ordered against each other, only against pushes. Never held by the
        # ACP reader thread (it only takes `_lock` for pushes), so waiting
        # for a reply under it cannot block that reply. Taken before `_lock`.
        self._config_lock = threading.Lock()
        self._commands: list[dict] = []
        self._last_error: Optional[dict] = None
        # Jobs waiting behind the running turn, oldest first:
        # {"id", "kind": "prompt"|"new_session", "text", "at"}.
        self._queue: list[dict] = []
        # The job running right now, as the state mirror shows it
        # (`_current_entry`), None when idle. A daemon that stops mid-turn
        # leaves it on disk; the next start turns it into a card (S1-R5).
        self._current: Optional[dict] = None
        # The job a PREVIOUS daemon was running when it stopped, from its
        # mirror, until start() has written its card: the mirror keeps it as
        # `interrupted` meanwhile, so a stop in between never loses it.
        # `{}` = busy without `current` (an image before S1-R5).
        self._unreported: Optional[dict] = None
        # omp's own session file and its size when the running prompt was
        # claimed — the turn's lines start there (S1-R6, `_omp_turn_line`).
        self._omp_mark: tuple[Optional[Path], int] = (None, 0)
        self._worker: Optional[threading.Thread] = None
        self._closed = False

        # P3: open questions omp asked (`elicitation/create`), by OUR card id:
        # {"id", "rpcId", "message", "schema", "at"}. In memory only — a
        # restarted daemon cannot answer its predecessor's JSON-RPC request,
        # so start() marks whatever the old state mirror still listed as
        # expired (same pattern as the lost queue).
        self._elicitations: dict[str, dict] = {}
        # Last `session_info_update` (title/updatedAt) — mirrored into the
        # state file, never a chat line.
        self._session_info: dict = {}
        # Every reset job and what became of it, and every reset the daemon
        # did on its own (`_record_reset`), newest last, at most
        # RESET_HISTORY_MAX — mirrored as `resets`. Taken over from the
        # previous daemon's mirror at start, so a restart does not hide one
        # from the backend's activity feed (incident 21.10.2026).
        self._resets: list[dict] = []

        self._full_text: list[str] = []
        self._throttle: dict = {"chars": -10 ** 9, "at": 0.0, "pending": None}
        # True while `session/load` runs (`_muted_replay`). omp answers a
        # load by replaying the WHOLE stored conversation as session/update
        # notifications before it replies (live 20.10.2026: 96 for one chat,
        # 66 of them tool lines). Nothing of it is new: none of it may reach
        # the transcript, the preview or the turn text. Counted per kind.
        self._replaying = False
        self._replayed: dict[str, int] = {}
        # P1: trailing flush of withheld live tool output (see
        # acp_chat_events.ToolOutputPump) — one per daemon, started in
        # start(), stopped in close().
        self._pump: Optional[acp_chat_events.ToolOutputPump] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the child, handshake, and resume or open the ACP session."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        # S1-R3: MC copies an older image left next to omp's files move into
        # `.mc/` BEFORE the pointer is read (`_connect` remaps it). Only a
        # safety net — the real move runs on the host in the deploy window.
        migration_problem = self._migrate_sinks()
        persisted = self._read_persisted()
        # Read BEFORE anything rewrites the mirror: the queue lives in memory,
        # so whatever a previous daemon still held is gone after a restart.
        lost = self._previous_queue()
        lost_questions = self._previous_elicitations()
        # The same for the job it was running: its reply never came (S1-R5).
        # It stays in the mirror (`interrupted`) until its card is written —
        # _connect rewrites the mirror long before that.
        interrupted = self._previous_current()
        resets = self._previous_resets()
        with self._lock:
            self._unreported = interrupted
            self._resets = resets
        if interrupted is not None and interrupted.get("kind") == "new_session":
            # A `/new` that never ran is one more job the restart dropped —
            # it belongs on the card that lists those, first in line.
            lost = [{"kind": "new_session", "text": "",
                     "requestId": interrupted.get("requestId")}, *lost]
            interrupted = None
        for job in lost:
            if job.get("kind") == "new_session":
                # On record too: whoever asked for it must not read "runs
                # when the reply ends" for ever (the backend's event).
                self._record_reset(VIA_NEW_SESSION, RESET_LOST,
                                   previous=persisted.get("sessionId"),
                                   request_id=job.get("requestId"))
        self._connect(persisted)
        self._write_state()
        if migration_problem:
            # Visible, not only a log line (F7): old copies still sit where
            # omp can mistake them for its own sessions.
            self._emit_notice(
                "sink_migration_skipped",
                "Older chat copies could not be moved out of omp's session "
                f"folder ({migration_problem}). The chat works; resuming an "
                "older conversation can fail until the move has run.",
                {"reason": migration_problem},
            )
        if self._pump is None:
            self._pump = acp_chat_events.ToolOutputPump(
                self._mapper, self._emit_preview
            ).start()
        if interrupted is not None:
            # The running turn came before anything held behind it.
            self._report_interrupted_turn(interrupted)
        if lost:
            self._report_lost_queue(lost)
        if self._unreported is not None:
            # Its card is on disk now. A stop right here shows it once more
            # on the next start — twice is better than never.
            with self._lock:
                self._unreported = None
            self._write_state()
        for elicitation_id in lost_questions:
            # Their JSON-RPC request died with the old child: the card must
            # say "no longer open" instead of offering buttons that can
            # never reach omp.
            self._emit_transcript(
                self._mapper.map_elicitation_result(elicitation_id, "expired")
            )

    def _migrate_sinks(self) -> Optional[str]:
        """Move MC's old copies under the sessions root into `.mc/`
        (`sink_migration.migrate`). Returns why it did not run — `locked`,
        `error` — or None. Never raises: a failed move must not cost the
        chat."""
        root = self._sessions_dir.parent
        try:
            report = sink_migration.migrate(root, runner="daemon")
        except Exception as exc:  # noqa: BLE001 — the chat starts anyway
            logger.warning("sink migration under %s failed: %s: %s",
                           root, type(exc).__name__, exc)
            return "error"
        counts = report.counts()
        if report.status != "ok":
            logger.warning("sink migration under %s did not run: %s (%s)",
                           root, report.status, report.detail)
            return report.status
        unsafe = [e["path"] for e in report.skipped if e.get("reason") == "unsafe_target"]
        if unsafe:
            # A symlink or a file where `.mc/` belongs: nothing moved there,
            # and new copies cannot be written there either.
            logger.warning("sink migration under %s refused unsafe targets: %s",
                           root, ", ".join(unsafe))
        if counts["moved"] or counts["media"] or counts["skipped"]:
            logger.info("sink migration: moved %d copies (%d mixed, %d task), "
                        "%d pictures, skipped %d", counts["moved"], counts["mixed"],
                        counts["task"], counts["media"], counts["skipped"])
        return None

    def _connect(self, persisted: dict) -> None:
        client = self._client_factory()
        client.on_event(self._on_event)
        client.on_permission(self._on_permission)
        # P3: only the chat registers an elicitation handler (and with it
        # advertises `elicitation.form`). Task runs (bridge.run_acp_once)
        # never do — an unattended run must not stop on a question.
        if self._elicitation_enabled and hasattr(client, "on_elicitation"):
            # No `elicitation/complete` handler: omp (18.10.1) sends it for
            # URL mode only, which _on_elicitation declines. A form question
            # omp gives up on (its timeout) ends with the turn instead —
            # _run_turn closes the card then.
            client.on_elicitation(self._on_elicitation)
        client._ensure_process()
        client.initialize()
        self._client = client

        stored_id = persisted.get("sessionId")
        stored_transcript = persisted.get("transcript")
        self._resume_source = persisted.get("source") or RESUME_NONE
        reset_detail = ""
        reset_text = ("Previous chat session could not be loaded — "
                      "started a new one (history starts over).")
        reset_data: dict = {}
        # The copy of the conversation that could not be resumed — on disk,
        # so the new conversation's card may say it is kept. None when it is
        # gone (`transcript_missing`): the card comes without the promise.
        previous_copy: Optional[Path] = None
        started_fresh = False
        session_id: Optional[str] = None
        sink: Optional[acp_chat_events.ChatEventSink] = None
        if stored_id and stored_transcript:
            # A pointer written before the move names the old place.
            transcript = _find_transcript(stored_transcript)
            if transcript is not None:
                try:
                    with self._muted_replay():
                        client.load_session(stored_id, self._cwd)
                    sink = acp_chat_events.ChatEventSink.appending(transcript, stored_id)
                    session_id = stored_id
                except Exception as exc:  # noqa: BLE001 — any load failure is a reset
                    previous_copy = transcript
                    model = missing_model(f"{exc}")
                    if model is None:
                        reset_detail = f"{type(exc).__name__}: {exc}"
                    else:
                        reset_detail, reset_text = "model_missing", _model_missing_text(model)
                        # The card in the UI language fills the name into its
                        # own sentence; it never shows the English text.
                        reset_data = {"model": acp_model.display_name(model) or None}
            else:
                # Neither at the named place nor in `.mc/`: the chat cannot
                # show this conversation — say so instead of starting over
                # silently. omp's own file may still hold it; loading it
                # with an empty chat view is P4's job, not a restart's.
                reset_detail = "transcript_missing"
                reset_text = ("The history of the last conversation was not "
                              "found — a new conversation started.")
        elif not persisted:
            # Nothing names the open conversation (start without pointer).
            # Decided BEFORE the new session writes its own copy.
            started_fresh = self._earlier_chat_exists()
        logger.info("chat session to resume: %s (from %s)%s", stored_id or "none",
                    self._resume_source, f" — {reset_detail}" if reset_detail else "")
        if session_id is None or sink is None:
            session_id = client.new_session(self._cwd)
            sink = acp_chat_events.ChatEventSink(self._sessions_dir, session_id)
        # ONE locked step: switch AND pointer. The socket threads write the
        # state mirror under this lock (a `prompt` while the omp child is
        # respawned); none may name the new session before the pointer does,
        # or a crash right then resumes the previous one (critic S1-R4).
        # The pointer comes before the model pin below, which writes the
        # mirror too.
        with self._lock:
            self._session_id = session_id
            self._mapper.set_session_id(session_id)
            self._sink = sink
            # Next to the transcript in use — the backend reads the preview
            # from the transcript's folder. A resumed copy the move skipped
            # still sits at the old place; its preview must sit there too.
            self._preview = acp_chat_events.PreviewEventSink.beside(sink.path, session_id)
            self._write_persisted()
        if reset_detail:
            # First line of the new conversation: it began, and whether the
            # old one is kept (incident 21.10.2026). The red card below says
            # why the old one did not resume.
            self._note_new_conversation(
                stored_id, session_id,
                VIA_CHILD_RESTART if self._resume_source == "restart" else VIA_START,
                reason=reset_detail if reset_detail in ("model_missing", "transcript_missing")
                else "load_failed",
                previous_copy=previous_copy,
            )
        elif started_fresh:
            # Its own card below (`chat_started_fresh`) — no id to name.
            self._note_new_conversation(None, session_id, VIA_START, reason="no_pointer")
        self._absorb_session_result(getattr(client, "last_session_result", None))
        self._pin_model()
        if reset_detail:
            # The operator loses the old history here — that is an EVENT,
            # not a log line (Boss: errors must be visible in the chat).
            self._emit_error("session_reset", reset_detail, text=reset_text, data=reset_data)
        elif started_fresh:
            self._emit_notice(CHAT_STARTED_FRESH, _CHAT_STARTED_FRESH_TEXT)

    def _earlier_chat_exists(self) -> bool:
        """True when the sessions tree holds an earlier CHAT: MC's copy (header
        `bridge: "acp"`) without a `taskId`, in any session folder under the
        root or its `.mc/`. A task's copy is no earlier chat, omp's own files
        are not MC's chat, and a brand-new agent has neither — no card then.
        Read-only, never follows a symlink, never raises."""
        root = self._sessions_dir.parent
        try:
            folders = sorted(p for p in root.iterdir()
                             if not p.name.startswith(".") and not p.is_symlink() and p.is_dir())
        except OSError:
            return False
        for folder in folders:
            for place in (folder / acp_chat_events.MC_SINK_DIRNAME, folder):
                try:
                    if place.is_symlink() or not place.is_dir():
                        continue
                    names = sorted(place.iterdir())
                except OSError:
                    continue
                for path in names:
                    if path.name.startswith(".") or not path.name.endswith(".jsonl") \
                            or path.is_symlink():
                        continue
                    header = acp_chat_events.session_header(path)
                    if acp_chat_events.is_mc_header(header) and not header.get("taskId"):
                        return True
        return False

    @contextlib.contextmanager
    def _muted_replay(self) -> Iterator[None]:
        """Mute the chat while omp replays a session it loads.

        The ACP client handles stdout strictly in order on its reader thread
        and calls the event callbacks synchronously; omp replies to
        `session/load` only after the last replayed update. So everything
        `_on_event` sees inside this block is replay: only state updates are
        applied (`_STATE_UPDATES`), nothing goes through the mapper.

        Every caller of `_connect` gets this — start() after a daemon
        restart AND `_restart_child` after a crash of the omp child, where
        the transcript is the one the chat already shows (before S1-R2 the
        reload appended every old tool line to it a second time).

        `finally`: the switch also closes when the load fails, or the next
        session's turns would be muted for good. After it, no turn is in
        flight, so the mapper's turn buffers are emptied: the next card
        (queue_lost, session_reset, ...) must never carry a thought that
        was open before the load (#902 flushes a thought before a card)."""
        with self._lock:
            self._replaying = True
            self._replayed = {}
        try:
            yield
        finally:
            with self._lock:
                self._replaying = False
                counts, self._replayed = self._replayed, {}
            self._mapper.begin_turn()
            muted = {k: v for k, v in counts.items() if k not in _STATE_UPDATES}
            kept = {k: v for k, v in counts.items() if k in _STATE_UPDATES}
            logger.info(
                "load replayed %d updates (muted): %s%s",
                sum(counts.values()), _format_counts(muted) or "none",
                f"; applied as state: {_format_counts(kept)}" if kept else "",
            )

    def _previous_queue(self) -> list[dict]:
        """The jobs a previous daemon was holding, from its state mirror.
        Never blocks (a FIFO in its place on the mount), never raises."""
        queue = _read_json_object(self.state_file).get("queue")
        return [j for j in queue if isinstance(j, dict)] if isinstance(queue, list) else []

    def _previous_current(self) -> Optional[dict]:
        """The job a previous daemon was running when it stopped: the state
        mirror's `current` while `busy` was true. `{}` when it was busy but
        names no job (an image before S1-R5 wrote no `current`) — None when
        it was idle, or the mirror is missing or unreadable. An
        `interrupted` job a start before could not report yet comes first
        (no prompt is served before start() returns, so both cannot be
        set). Never blocks, never raises."""
        state = _read_json_object(self.state_file)
        pending = state.get("interrupted")
        if isinstance(pending, dict):
            return dict(pending)
        if state.get("busy") is not True:
            return None
        current = state.get("current")
        return dict(current) if isinstance(current, dict) else {}

    def _report_interrupted_turn(self, current: dict) -> None:
        """One card for the reply a restart cut off. It offers the text
        (`retryText`) only when the operator wrote it and the mirror kept all
        of it; nothing is ever sent again on its own — not here, not
        anywhere in start(). Only for the operator's turn does the card say
        that nothing was resent: a task hermes-bridge dispatched comes again
        on its own, and an older image did not say who wrote the turn."""
        turn = current.get("turn")
        detail = f"turn {turn}" if isinstance(turn, int) and not isinstance(turn, bool) \
            else "turn unknown"
        by_operator = current.get("kind") == "prompt" \
            and current.get("origin") == ORIGIN_OPERATOR
        self._emit_error(
            TURN_INTERRUPTED, detail,
            text=_TURN_INTERRUPTED_TEXT if by_operator else _TURN_INTERRUPTED_NEUTRAL_TEXT,
            data=retry_data(current.get("text")) if by_operator else None,
        )

    def _previous_resets(self) -> list[dict]:
        """`resets` of a previous daemon's state mirror — only rows in the
        shape this daemon writes (`_clean_reset_row`), at most
        RESET_HISTORY_MAX. Never raises."""
        rows = _read_json_object(self.state_file).get("resets")
        if not isinstance(rows, list):
            return []
        cleaned = (_clean_reset_row(r) for r in rows[-RESET_HISTORY_MAX:])
        return [r for r in cleaned if r is not None]

    def _record_reset(self, via: str, outcome: str, *, previous: Any = None,
                      current: Any = None, reason: Optional[str] = None,
                      request_id: Any = None, kept: Optional[bool] = None) -> None:
        """One row of `resets` (the mirror is written by the caller's next
        `_write_state`). Every field in the shape `_clean_reset_row` keeps;
        a value of another shape is left out, never stored."""
        row = _clean_reset_row({
            "id": uuid.uuid4().hex[:16], "at": _now_iso(), "via": via,
            "outcome": outcome, "previousSessionId": previous, "sessionId": current,
            "reason": reason, "requestId": _request_id_of(request_id), "kept": kept,
        }, partial=True)
        if row is None:
            return
        with self._lock:
            self._resets = [*self._resets, row][-RESET_HISTORY_MAX:]

    def _note_new_conversation(self, previous: Optional[str], current: str, via: str,
                               reason: Optional[str] = None,
                               previous_copy: Optional[Path] = None,
                               request_id: Optional[str] = None) -> None:
        """A new conversation replaced `previous` (incident 21.10.2026).

        Always a row in `resets` and a log line — the backend turns the row
        into an activity event. The card (`new_conversation`, the FIRST line
        of the new transcript) whenever there WAS a previous conversation;
        it promises "the previous one is saved" only while that copy is on
        disk (`kept`) — without it, it still says that a new one began: no
        reset is silent. Called right after the switch, before the model pin
        can write a notice of its own."""
        kept = False
        if previous_copy is not None:
            try:
                kept = previous_copy.is_file()
            except OSError:
                kept = False
        request_id = _request_id_of(request_id)
        self._record_reset(via, RESET_DONE, previous=previous, current=current,
                           reason=reason, request_id=request_id,
                           kept=kept if previous else None)
        logger.info("new conversation %s (was %s) via %s%s%s", current, previous or "none",
                    via, f" — {reason}" if reason else "",
                    f" — request {request_id}" if request_id else " — no request id")
        if not previous or previous == current:
            return
        self._emit_notice(
            NEW_CONVERSATION,
            _NEW_CONVERSATION_TEXT if kept else _NEW_CONVERSATION_NOT_KEPT_TEXT,
            {"previous": previous, "current": current, "via": via, "reason": reason,
             "kept": kept, "requestId": request_id},
        )

    def _previous_elicitations(self) -> list[str]:
        """Card ids a previous daemon still held open, from its state mirror."""
        rows = _read_json_object(self.state_file).get("elicitations")
        if not isinstance(rows, list):
            return []
        return [str(r["id"]) for r in rows
                if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]]

    def _report_lost_queue(self, lost: list[dict]) -> None:
        """One card naming what a restart dropped — the operator typed it and
        must see that it never ran (spec: the queue is in memory only). A
        `/new` that was running when the daemon stopped is listed first."""
        texts = ["/new" if j.get("kind") == "new_session" else str(j.get("text") or "")
                 for j in lost]
        listed = "\n".join(f"- {t}" for t in texts if t)
        self._emit_error(
            "queue_lost", f"{len(lost)} queued job(s) dropped by a chat restart",
            text=f"The chat restarted — {len(lost)} queued message(s) were not sent:\n{listed}",
        )

    def _pin_model(self) -> None:
        """Pin the session to the MC model (twin of bridge.py's #483 fix).

        `omp acp` inherits the bridge environment; OPENAI_API_KEY activates
        omp's BUILT-IN openai provider, and without an explicit selector a
        fresh session opens on openai/gpt-5.5 with the shim key — live
        14.09.2026 the first chat turn after a container recreate answered
        "401 Incorrect API key provided: sk-noauth". launch-omp.sh always
        passes --model, bridge.py sets it per task session; the chat daemon
        has to do the same on every session/new and session/load. A refused
        pin is a chat-visible error, not a crash: the daemon still serves
        state/cancel, and the operator sees WHY in the chat."""
        previous = self._model
        fresh = None
        if self._model_resolver is not None:
            try:
                fresh = (self._model_resolver() or "").strip() or None
            except Exception:  # noqa: BLE001 — an unreadable env file keeps the last selector
                logger.warning("model resolver failed; keeping %s", previous, exc_info=True)
        wanted = fresh or previous
        if not wanted:
            return
        with self._lock:
            options = json.loads(json.dumps(self._config_options))
        offered = acp_model.offered_models(options)
        value, replaced = acp_model.choose_model(wanted, options)
        if replaced is None and previous and previous != value and offered \
                and previous not in offered:
            # The selector moved on (runtime switch) and the old one is gone:
            # the operator saw the old model in this chat — say so once.
            replaced = previous
        self._model = value
        answer = self.config("model", value)
        if replaced and answer.get("ok"):
            self._emit_notice(
                acp_model.MODEL_FALLBACK_CODE,
                f"Previously chosen model {acp_model.display_name(replaced)} is "
                f"not available anymore — using {acp_model.display_name(value)}.",
                {"previous": acp_model.display_name(replaced),
                 "current": acp_model.display_name(value)},
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            client = self._client
            pump, self._pump = self._pump, None
        if pump is not None:
            pump.stop()
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass

    # ------------------------------------------------------------------
    # control ops (the socket protocol)
    # ------------------------------------------------------------------

    def prompt(self, text: str, mode: str = MODE_NOW, origin: Any = None) -> dict:
        """Start ONE turn — or, with `mode="queue"` while a turn runs, hold
        the message and run it when that turn ends. Returns immediately; turns
        run in the worker.

        `origin`: who wrote it — ORIGIN_OPERATOR, "task"/"nudge", or nothing
        (unknown). Only the operator's text is ever offered to send again
        (`_origin_of`)."""
        text = str(text or "")
        mode = mode or MODE_NOW
        origin = _origin_of(origin)
        if mode not in _MODES:
            return {"ok": False, "error": "bad_mode", "detail": str(mode)}
        with self._lock:
            if self._busy:
                if mode == MODE_QUEUE and not self._closed:
                    return self._enqueue_locked("prompt", text, origin)
                # Rejected, and the operator sees why — silently dropping a
                # typed message is the worst possible failure here.
                self._emit_error(
                    "busy", "a turn is already running",
                    text="A reply is already running — wait or press Stop.",
                )
                return {"ok": False, "error": "busy"}
            if self._closed or self._client is None:
                return {"ok": False, "error": "not_started"}
            job = self._claim_locked(_job("prompt", text, origin))
        self._write_state()
        self._spawn(job)
        return {"ok": True, "turn": job["turn"]}

    def new_session(self, mode: str = MODE_NOW, request_id: Any = None) -> dict:
        """`/new` and `/clear`: open a fresh ACP session (`session/new`) with
        its own transcript file. The old file stays as it is — the chat view
        follows the new one as a rollover, the history is not deleted.

        Runs in the worker like a turn (session/new can take seconds while
        omp connects MCP servers), so it never overlaps one: while a turn
        runs it is refused (`busy`), or held in the queue with
        `mode="queue"`. A failure keeps the current session and lands as a
        `new_session_failed` card.

        `request_id`: the id MC's backend sends with the op — it names the
        activity event of the request, and the job carries it to the card and
        to `resets`. Nothing (a shell, the agent's own tools) means "not MC";
        a value of another shape counts as nothing (`_request_id_of`)."""
        mode = mode or MODE_NOW
        if mode not in _MODES:
            return {"ok": False, "error": "bad_mode", "detail": str(mode)}
        request_id = _request_id_of(request_id)
        with self._lock:
            if self._closed or self._client is None:
                return {"ok": False, "error": "not_started"}
            if self._busy:
                if mode == MODE_QUEUE:
                    return self._enqueue_locked("new_session", "", request_id=request_id)
                return {"ok": False, "error": "busy"}
            previous = self._session_id
            job = self._claim_locked(_job("new_session", "", request_id=request_id))
        self._write_state()
        self._spawn(job)
        return {"ok": True, "previousSessionId": previous}

    def queue_clear(self) -> dict:
        """Take every held job back, oldest first. The composer's withdraw
        and Stop put the texts back into the input — nothing held is lost
        without the operator seeing it."""
        with self._lock:
            dropped, self._queue = self._queue, []
            current = self._session_id
        for job in dropped:
            if job.get("kind") == "new_session":
                # It never ran: on record, so whoever asked does not read
                # "runs when the reply ends" for ever.
                self._record_reset(VIA_NEW_SESSION, RESET_WITHDRAWN, previous=current,
                                   request_id=job.get("requestId"))
        if dropped:
            self._write_state()
        return {"ok": True, "dropped": [dict(job) for job in dropped]}

    def cancel(self) -> dict:
        """`session/cancel` — a no-op when idle, never an error."""
        with self._lock:
            client, sid, busy = self._client, self._session_id, self._busy
        if client is not None and sid and busy:
            try:
                client.cancel(sid)
            except Exception as exc:  # noqa: BLE001 — cancel is best-effort
                logger.warning("cancel failed: %s", exc)
        return {"ok": True}

    def config(self, option_id: str, value: Any) -> dict:
        """Set one config option (`thinking`, `model`, `mode`, ...).

        `mode` is refused while a question is open or a turn runs (P3 live
        test 13.10.2026): switching plan mode off under an open plan card
        left omp's request unanswered, the turn hung until the prompt
        timeout, and the model then looped on a proposal nobody awaited.

        The reply is ACP's `configOptions`: the full option set after the
        change. A model switch changes more than its own row — the new model
        brings its own thinking levels. The reply's set is taken unless the
        agent pushed a `config_option_update` while the request was out or
        right after it: that push is the agent's own word and stays. Live
        20.10.2026: omp 18.10.1 drops the push when a model switch starts
        inside its 50 ms bootstrap guard — the pin in `_pin_model` right
        after session/new — and the effort chip kept the levels of the
        model the session opened on. A reply without a full set (`{}`, or
        `[]` from an agent with no typed options) keeps today's way: only
        the value just set is written into the known list.

        When the push stays, the value written into it is the one the reply
        names, not the requested one: an agent may run another value than
        asked (omp clamps a level the model lacks — `medium` on a
        low/high/max model comes back as `low`), and only the reported value
        is on the ladder. The requested value is the fallback.

        Calls run one at a time (`_config_lock`): with two requests out at
        once — the operator's chip while `_pin_model` runs — a thinking
        reply built before a model switch could land after the model reply
        and bring back the old model and its ladder."""
        with self._config_lock:
            return self._config_locked(option_id, value)

    def _config_locked(self, option_id: str, value: Any) -> dict:
        """config() body. Caller holds `_config_lock`, not `_lock`."""
        with self._lock:
            client, sid = self._client, self._session_id
            pushes = self._config_pushes
            if option_id == "mode":
                if self._elicitations:
                    return {"ok": False, "error": "question_open"}
                if self._busy:
                    return {"ok": False, "error": "busy"}
        if client is None or not sid:
            return {"ok": False, "error": "not_started"}
        try:
            answer = client.set_config_option(sid, option_id, value)
        except Exception as exc:  # noqa: BLE001 — any failure is chat-visible
            detail = f"{exc}"
            code = "provider_error" if _PROVIDER_ERROR_RE.search(detail) else "rpc_error"
            self._emit_error(code, detail,
                             text=f"Einstellung {option_id}={value!r} abgelehnt: {detail}")
            return {"ok": False, "error": code, "detail": detail}
        reply = self._full_option_set(answer, option_id)
        with self._lock:
            if reply is not None and self._config_pushes == pushes:
                self._config_options = reply
            else:
                current = next(
                    (o.get("currentValue") for o in reply or []
                     if o.get("id") == option_id and o.get("currentValue") is not None),
                    value,
                )
                for opt in self._config_options:
                    if opt.get("id") == option_id:
                        opt["currentValue"] = current
            options = json.loads(json.dumps(self._config_options))
        self._write_state()
        return {"ok": True, "configOptions": options}

    def answer(self, elicitation_id: Any, action: Any, content: Any = None) -> dict:
        """P3: the operator's answer to an open question card.

        ``action`` is ``accept`` (with ``content`` matching the question's
        schema), ``decline`` or ``cancel``. The JSON-RPC reply goes to omp,
        the outcome lands as an ``acp-elicitation-result`` line. A card that
        is no longer open (answered elsewhere, turn over, daemon restarted)
        answers ``not_pending`` — a clear refusal, never a crash."""
        if action not in ELICITATION_ACTIONS:
            return {"ok": False, "error": "bad_action", "detail": str(action)}
        elicitation_id = str(elicitation_id or "")
        with self._lock:
            pending = self._elicitations.get(elicitation_id)
            if pending is None:
                return {"ok": False, "error": "not_pending"}
            if action == "accept":
                problem = validate_content(pending["schema"], content)
                if problem:
                    return {"ok": False, "error": "bad_content", "detail": problem}
            del self._elicitations[elicitation_id]
            client = self._client
        if action == "accept":
            reply = {"action": "accept", "content": content}
            status = "answered"
        else:
            reply = {"action": action}
            status = "declined" if action == "decline" else "cancelled"
        try:
            client.respond(pending["rpcId"], reply)
        except Exception as exc:  # noqa: BLE001 — omp gone: the card is dead
            logger.warning("elicitation reply failed: %s", exc)
            self._emit_transcript(
                self._mapper.map_elicitation_result(elicitation_id, "expired")
            )
            self._write_state()
            return {"ok": False, "error": "not_pending", "detail": f"{exc}"}
        self._emit_transcript(self._mapper.map_elicitation_result(
            elicitation_id, status, content if action == "accept" else None,
        ))
        self._write_state()
        if action == "cancel" and pending.get("planApproval"):
            # omp's plan approval knows only "approve" and "everything else":
            # a plain cancel means "keep planning", and the model proposes
            # again at once (live 13.10.2026). Cancel on THIS card means stop
            # — same path as the Stop button (`session/cancel`).
            self.cancel()
            return {"ok": True, "status": status, "stopped": True}
        return {"ok": True, "status": status}

    def _expire_elicitations(self) -> None:
        """Close every open card: the turn that asked is over (omp timed the
        question out, or Stop/restart ended it — form mode has no
        `elicitation/complete`). omp gets a `cancel` (it drops a late reply
        silently), the chat gets an ``expired`` line."""
        with self._lock:
            pending, self._elicitations = list(self._elicitations.values()), {}
            client = self._client
        for item in pending:
            if client is not None:
                try:
                    client.respond(item["rpcId"], {"action": "cancel"})
                except Exception:  # noqa: BLE001 — best effort, omp may be gone
                    pass
            self._emit_transcript(
                self._mapper.map_elicitation_result(item["id"], "expired")
            )
        if pending:
            self._write_state()

    def state(self) -> dict:
        """The `acp-chat-state.json` payload (also the `state` op's body)."""
        with self._lock:
            return {
                "version": _STATE_VERSION,
                "driver": self._driver,
                "sessionId": self._session_id,
                "busy": self._busy,
                "turn": self._turn,
                "transcript": str(self.transcript_path) if self.transcript_path else None,
                "configOptions": json.loads(json.dumps(self._config_options)),
                "commands": json.loads(json.dumps(self._commands)),
                "queue": [
                    {**job, "text": job["text"][:_QUEUE_PREVIEW_CHARS]}
                    for job in self._queue
                ],
                "current": dict(self._current) if self._current else None,
                "interrupted": dict(self._unreported)
                if self._unreported is not None else None,
                "elicitations": [
                    {"id": e["id"], "at": e["at"], "message": e["message"][:200]}
                    for e in self._elicitations.values()
                ],
                "sessionInfo": dict(self._session_info),
                "resets": [dict(r) for r in self._resets],
                "updatedAt": _now_iso(),
                "lastError": json.loads(json.dumps(self._last_error))
                if self._last_error else None,
            }

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no turn is running. True when idle, False on timeout."""
        return self._idle.wait(timeout)

    # ------------------------------------------------------------------
    # paths
    # ------------------------------------------------------------------

    @property
    def transcript_path(self) -> Optional[Path]:
        return self._sink.path if self._sink is not None else None

    @property
    def preview_path(self) -> Optional[Path]:
        return self._preview.path if self._preview is not None else None

    @property
    def state_file(self) -> Path:
        return self._sessions_dir / _STATE_FILENAME

    @property
    def persist_file(self) -> Path:
        return self._state_dir / _PERSIST_FILENAME

    # ------------------------------------------------------------------
    # the worker: one job, then everything queued behind it
    # ------------------------------------------------------------------

    def _enqueue_locked(self, kind: str, text: str, origin: Optional[str] = None,
                        request_id: Optional[str] = None) -> dict:
        """Hold one job behind the running turn. Caller holds `_lock`."""
        if len(self._queue) >= MAX_QUEUE:
            self._emit_error(
                "queue_full", f"{MAX_QUEUE} messages already waiting",
                text=f"Not sent — {MAX_QUEUE} messages are already waiting. "
                     "Wait for the reply or take some back.",
            )
            return {"ok": False, "error": "queue_full"}
        job = {"id": uuid.uuid4().hex[:12], **_job(kind, text, origin, request_id),
               "at": _now_iso()}
        self._queue.append(job)
        position = len(self._queue)
        self._write_state()
        return {"ok": True, "queued": True, "queueId": job["id"],
                "position": position, "at": job["at"]}

    def _claim_locked(self, job: dict) -> dict:
        """Mark the session busy for `job`; a prompt gets its turn number
        here, when it STARTS — not when it was queued. `job` becomes the
        mirror's `current` (S1-R5). Caller holds `_lock`."""
        self._busy = True
        self._idle.clear()
        if job["kind"] == "prompt":
            self._turn += 1
            job = {**job, "turn": self._turn}
            # omp is idle between jobs: its file ends where this turn's
            # lines will start (S1-R6). Taken here, not in the worker, so
            # the turn itself starts no later than before.
            self._omp_mark = self._omp_file_mark()
        self._current = _current_entry(job)
        return job

    def _spawn(self, job: dict) -> None:
        worker = threading.Thread(
            target=self._work, args=(job,), name="acp-chat-turn", daemon=True,
        )
        with self._lock:
            self._worker = worker
        worker.start()

    def _work(self, job: Optional[dict]) -> None:
        """Run `job`, then each queued job in order, then go idle. `_busy`
        stays set from the first job to the last, so a prompt arriving in
        between queues behind them instead of racing the next turn."""
        while job is not None:
            try:
                if job["kind"] == "new_session":
                    self._renew_session(job)
                else:
                    self._run_turn(job["text"], job["turn"], job.get("origin"))
            except Exception:  # noqa: BLE001 — one broken job must not strand the queue busy
                logger.exception("chat job %s failed", job.get("kind"))
            with self._lock:
                closed = self._closed
                # The job is over; the next queued one (if any) is current.
                self._current = None
                if closed or not self._queue:
                    self._busy = False
                    job = None
                else:
                    job = self._claim_locked(self._queue.pop(0))
            if closed:
                # A retired session touches nothing shared (see the closed
                # branch in _run_turn) — only the idle flag opens. Its
                # mirror keeps `busy` + `current`: that is how the next
                # daemon learns which reply never came (S1-R5).
                break
            # The mirror must be on disk BEFORE the idle flag opens: a waiter
            # that reads acp-chat-state.json the moment it wakes would
            # otherwise see the stale busy=true snapshot of the turn it just
            # waited out.
            self._write_state()
        self._idle.set()

    def _renew_session(self, job: Optional[dict] = None) -> None:
        """The `new_session` job: a fresh `session/new` on the same child.

        Model and thinking level carry over — `/clear` forgets the
        conversation, not the operator's settings. The replaced session is
        closed (`session/close`, best-effort) so the child does not keep one
        live session per reset; its transcript stays on disk.

        Order matters: close the old session BEFORE the new transcript file
        is created. The chat view follows the file whose LAST entry is newest
        (omp_chat.find_active_session), and omp may still persist into the old
        session's own file while disposing it — the new header must be later
        than anything that teardown writes.

        `job["requestId"]`: who asked (MC's backend) — on the card and in
        `resets`, whatever the outcome."""
        request_id = (job or {}).get("requestId")
        # A question of the old session can never be answered in the new one.
        self._expire_elicitations()
        with self._lock:
            client, previous = self._client, self._session_id
            previous_copy = self.transcript_path
            thinking_id = self._thinking_option_id()
            thinking = self._config_value(thinking_id) if thinking_id else None
        try:
            new_id = client.new_session(self._cwd)
        except Exception as exc:  # noqa: BLE001 — the old session keeps serving
            self._record_reset(VIA_NEW_SESSION, RESET_FAILED, previous=previous,
                               request_id=request_id)
            self._emit_error(
                "new_session_failed", f"{exc}",
                text="Could not start a new session — the current one continues.",
            )
            return
        result = getattr(client, "last_session_result", None)
        if previous and previous != new_id and hasattr(client, "close_session"):
            try:
                client.close_session(previous)
            except Exception as exc:  # noqa: BLE001 — a leaked session is not a failed reset
                logger.warning("session/close %s failed: %s", previous, exc)
        with self._lock:
            self._session_id = new_id
            self._mapper.set_session_id(new_id)
            self._sink = acp_chat_events.ChatEventSink(self._sessions_dir, new_id)
            self._preview = acp_chat_events.PreviewEventSink.beside(self._sink.path, new_id)
            # Inside the same locked step, before the pin and the thinking
            # carry-over below write the mirror — see _connect.
            self._write_persisted()
        # The first line of the new conversation (incident 21.10.2026: the
        # chat was empty and nothing said why).
        self._note_new_conversation(previous, new_id, VIA_NEW_SESSION,
                                    previous_copy=previous_copy, request_id=request_id)
        self._absorb_session_result(result)
        self._pin_model()
        offered = self._config_value(thinking_id) if thinking_id else None
        if thinking is not None and offered is not None and offered != thinking:
            self.config(thinking_id, thinking)

    def _thinking_option_id(self) -> Optional[str]:
        """The id of the thinking-level option, harness-neutral: omp's
        `thinking` first (as before), else the first option with the ACP
        standard category `thought_level` (Hermes: `reasoning_effort`).
        None when the agent offers no thinking level. Twin of the backend's
        `agent_chat_input._acp_thinking_option` — the chip reads and writes
        the same row this carry-over keeps."""
        with self._lock:
            rows = [o for o in self._config_options if isinstance(o, dict)]
        for opt in rows:
            if opt.get("id") == "thinking":
                return "thinking"
        for opt in rows:
            if opt.get("category") == "thought_level" and isinstance(opt.get("id"), str):
                return opt["id"]
        return None

    def _config_value(self, option_id: str) -> Any:
        """`currentValue` of one config option, None when not offered."""
        with self._lock:
            for opt in self._config_options:
                if opt.get("id") == option_id:
                    return opt.get("currentValue")
        return None

    # ------------------------------------------------------------------
    # the turn
    # ------------------------------------------------------------------

    def _run_turn(self, text: str, turn: int, origin: Optional[str] = None) -> None:
        result = None
        error_text = ""
        self._mapper.begin_turn()
        self._full_text = []
        self._throttle = {"chars": -10 ** 9, "at": time.monotonic(), "pending": None}
        # Where omp's own file of this session ended when the job was
        # claimed: what omp writes after it is this turn (S1-R6).
        with self._lock:
            omp_mark, self._omp_mark = self._omp_mark, (None, 0)
        try:
            with self._lock:
                client, sid = self._client, self._session_id
            # A prompt Mission Control wrote is its event in the chat, not
            # an operator bubble — the same line the task path writes.
            self._emit_transcript(self._mapper.map_user_prompt(
                text, origin=origin if origin in _MC_ORIGINS else None))
            result = client.prompt(sid, text, timeout=self._prompt_timeout)
        except Exception as exc:  # noqa: BLE001 — a dead turn must still report
            error_text = f"{exc}"
        finally:
            # The LAST preview state always reaches the sink — the preview
            # slot must never rest on a stale snapshot.
            if self._throttle.get("pending") is not None:
                self._emit_preview([self._throttle["pending"]])
                self._throttle["pending"] = None

        with self._lock:
            closed = self._closed
        if closed:
            # close() ran while this turn was in flight (a `/restart` that
            # tore down the child mid-turn — the whole point of this branch,
            # see ChatDaemon.restart). client.prompt() only returned because
            # close() force-woke the pending RPC wait, NOT because the turn
            # actually finished — result/error_text describe nothing real.
            #
            # This session is retired: ChatDaemon already swapped in a
            # DIFFERENT ChatSession object (new client, new session id) that
            # may already be writing to shared paths (`sessions_dir` and
            # `state_dir` are workspace-scoped, not session-object-scoped —
            # a loaded session even APPENDS to the same transcript file).
            # Emitting a transcript line, calling `_restart_child()` (which
            # spawns yet another real child process nobody will ever close),
            # or writing acp-chat-state.json / the persist file here would
            # race the active session's own writes and can clobber its
            # sessionId with this dead session's — kill the turn silently and
            # stop touching anything (_work only opens the idle flag).
            return

        # A question still open when its turn ends will never be answered
        # (omp's own timeout fired, or Stop) — close its card first.
        self._expire_elicitations()

        final_text = "".join(self._full_text)
        stop_reason = getattr(result, "stopReason", "") if result is not None else ""
        alive = self._client_alive()
        # Did the model answer at all (S1-R6)? Decided BEFORE the reply line
        # is written: omp's error line is cut off the end of the reply (the
        # card below says it instead); when nothing is left, there is no
        # reply bubble.
        verdict = self._judge_turn(
            stop_reason, final_text, error_text,
            getattr(result, "usage", None) if result is not None else None, omp_mark,
        ) if alive else model_errors.NOTHING
        if result is not None:
            self._mapper.set_prompt_usage(getattr(result, "usage", None))
            # An empty text still goes through the mapper: it writes no
            # bubble but closes a thought that is still open (#902).
            self._emit_transcript(self._mapper.map_final_assistant_message(
                final_text if verdict.reply is None else verdict.reply,
                stop_reason="stop" if stop_reason == "end_turn" else "",
            ))

        if not alive:
            # The card offers the text of the turn that died — the
            # operator's, and only when its reply never came (`result` is
            # None): a child that exits right after a complete reply leaves
            # nothing to send again. The respawn below only reloads the
            # session — it never sends it again.
            offer = result is None and origin == ORIGIN_OPERATOR
            self._emit_error(
                "process_exit",
                error_text or f"child exited during turn {turn}",
                text="The ACP process died — reconnecting the session.",
                data=retry_data(text) if offer else None,
            )
            self._restart_child()
        else:
            code, detail = _classify_turn(stop_reason, final_text, error_text, verdict)
            if code == MODEL_UNREACHABLE:
                # Same rule as `turn_interrupted`: only the operator's own
                # text is offered again (and only whole).
                operator = origin == ORIGIN_OPERATOR
                self._emit_error(
                    code, detail,
                    text=_MODEL_UNREACHABLE_TEXT if operator else _MODEL_UNREACHABLE_NEUTRAL_TEXT,
                    data=retry_data(text) if operator else None,
                )
            elif code:
                self._emit_error(code, detail)

    def _client_alive(self) -> bool:
        """False only when a REAL child process died. An injected/in-memory
        client (tests, Hermes in-process) has no `_proc` and counts as alive."""
        proc = getattr(self._client, "_proc", None)
        if proc is None:
            return True
        try:
            return proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def _restart_child(self) -> None:
        """Respawn the child and re-`session/load` — history keeps its file."""
        self._expire_elicitations()
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
        persisted = {
            "sessionId": self._session_id,
            "transcript": str(self.transcript_path) if self.transcript_path else "",
            "source": "restart",
        }
        self._session_id = None
        try:
            self._connect(persisted)
        except Exception as exc:  # noqa: BLE001 — a failed respawn stays visible
            logger.error("child restart failed: %s", exc)
            self._emit_error("process_exit", f"restart failed: {exc}")

    # ------------------------------------------------------------------
    # ACP events
    # ------------------------------------------------------------------

    def _on_event(self, params: dict) -> None:
        try:
            update = params.get("update") or {}
            su = update.get("sessionUpdate")
            with self._lock:
                replaying = self._replaying
                if replaying:
                    kind = su if isinstance(su, str) and su else "unknown"
                    self._replayed[kind] = self._replayed.get(kind, 0) + 1
            # isinstance first: `su` comes from the agent, and a list or a
            # dict there would make the set lookup raise.
            if isinstance(su, str) and su in _STATE_UPDATES:
                self._apply_state_update(update, su)
            if replaying:
                # A load's replay (see _muted_replay): state only — no
                # mapper, no transcript, no preview, no turn text.
                return
            if su == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    self._full_text.append(content.get("text") or "")

            for entry in self._mapper.map_update(params, stream=True):
                if not acp_chat_events.is_volatile(entry):
                    self._emit_transcript([entry])
                    continue
                if entry.get("customType") == acp_chat_events.TOOL_OUTPUT_CUSTOM_TYPE:
                    # Live tool output (P1): own per-call throttle in the
                    # mapper, never the text-preview throttle below.
                    self._emit_preview([entry])
                    continue
                body = entry.get("content") or ""
                grew = len(body) - self._throttle["chars"] > _PREVIEW_GROWTH_CHARS
                elapsed = time.monotonic() - self._throttle["at"] >= _PREVIEW_INTERVAL_S
                if grew or elapsed:
                    self._throttle["chars"] = len(body)
                    self._throttle["at"] = time.monotonic()
                    self._throttle["pending"] = None
                    self._emit_preview([entry])
                else:
                    self._throttle["pending"] = entry
        except Exception:  # noqa: BLE001 — a broken update must not kill the turn
            logger.exception("event handling failed")

    def _apply_state_update(self, update: dict, su: str) -> None:
        """One of `_STATE_UPDATES` -> in-memory state + the state mirror.
        No chat line here: outside a replay the mapper writes the quiet
        `acp-mode` line for `current_mode_update` (the live chip)."""
        if su == "config_option_update":
            self._set_config_options(update.get("configOptions"))
            self._write_state()
        elif su == "available_commands_update":
            self._set_commands(update.get("availableCommands"))
            self._write_state()
        elif su == "current_mode_update":
            # P3: plan mode on/off. The configOptions row is what the
            # backend's mode capabilities read.
            mode_id = update.get("currentModeId")
            if isinstance(mode_id, str) and mode_id:
                with self._lock:
                    for opt in self._config_options:
                        if opt.get("id") == "mode":
                            opt["currentValue"] = mode_id
                self._write_state()
        elif su == "session_info_update":
            info = {k: update.get(k) for k in ("title", "updatedAt")
                    if isinstance(update.get(k), str)}
            if info:
                with self._lock:
                    self._session_info = {**self._session_info, **info}
                self._write_state()

    def _on_permission(self, params: dict) -> str:
        """Delegate to the bridge's policy helper — ONE decision path for
        tasks and chat. An unimportable bridge falls back to the same rule
        the helper applies for `yolo`, and rejects otherwise (fail-closed).

        `yolo` means nobody is actually being asked — the request/decision
        pair still gets written to the transcript (`auto_approved=True`
        marks them `display: False`, see the mapper docstrings) so the
        chat view stops filling with self-answered permission cards
        between every tool call (task 663f70fb)."""
        auto_approved = self._permission_policy == "yolo"
        try:
            self._emit_transcript(
                self._mapper.map_permission_request(params, auto_approved=auto_approved)
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            import bridge  # lazy: the daemon must boot without the driver

            choice = bridge._acp_permission_decision(
                params, policy=self._permission_policy, task_id=self._task_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("permission helper unavailable (%s); fail-closed", exc)
            choice = (acp_client.ALLOW_ALWAYS
                      if self._permission_policy == "yolo"
                      else acp_client.REJECT_ONCE)
        try:
            self._emit_transcript(
                self._mapper.map_permission_outcome(params, choice, auto_approved=auto_approved)
            )
        except Exception:  # noqa: BLE001
            pass
        return choice

    #: omp persists the assistant message (with the call) at message end,
    #: just before the tool starts — a short wait covers a slow flush.
    _NAME_LOOKUP_TRIES = 3
    _NAME_LOOKUP_WAIT_S = 0.05

    def _omp_session_file(self) -> Optional[Path]:
        """omp's OWN file of the current session: the file named after the
        session in the session folder. Since S1-R3 MC's copy lives in `.mc/`,
        so that folder holds only omp's files. In a tree not moved yet MC's
        old copy can still sit there (same id, often the younger file):
        neither our own sink nor any file with MC's header is taken. Only
        regular files: this runs during a turn, and opening a FIFO named
        like the session would stall it."""
        sid, own = self._session_id, self.transcript_path
        if not sid:
            return None
        suffix = f"_{sid}.jsonl"
        try:
            candidates = [
                p for p in self._sessions_dir.iterdir()
                if p.name.endswith(suffix) and p != own and p.is_file()
                and not acp_chat_events.is_mc_sink(p)
            ]
            return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
        except OSError:
            return None

    def _resolve_tool_name(self, tool_call_id: str) -> Optional[str]:
        for attempt in range(self._NAME_LOOKUP_TRIES):
            name = acp_chat_events.omp_tool_name(self._omp_session_file(), tool_call_id)
            if name:
                return name
            if attempt + 1 < self._NAME_LOOKUP_TRIES:
                time.sleep(self._NAME_LOOKUP_WAIT_S)
        return None

    #: How often and how long the end of a turn waits for omp's final line
    #: of that turn (S1-R6). omp writes it when the message ends; whether
    #: that is always on disk before the prompt reply is not proven.
    _OMP_LINE_TRIES = 5
    _OMP_LINE_WAIT_S = 0.1

    def _omp_file_mark(self) -> tuple[Optional[Path], int]:
        """omp's own file of this session and its size now — the next
        turn's lines start there. `(None, 0)` when omp has no file yet (the
        session's first turn: then the whole file is this turn)."""
        path = self._omp_session_file()
        if path is None:
            return None, 0
        try:
            return path, path.stat().st_size
        except OSError:
            return None, 0

    def _omp_turn_line(self, mark: tuple[Optional[Path], int]
                       ) -> "Optional[model_errors.OmpTurnEnd]":
        """omp's line that ended this turn (and its retried attempts), from
        omp's OWN session file — None when there is none (no file,
        unreadable, a file that shrank, or no final line within the short
        wait)."""
        for attempt in range(self._OMP_LINE_TRIES):
            path = self._omp_session_file()
            if path is not None:
                start = mark[1] if mark[0] == path else 0
                lines = _read_lines_from(path, start)
                if lines is not None:
                    end = model_errors.omp_turn_end(lines)
                    if end is not None:
                        return end
            if attempt + 1 < self._OMP_LINE_TRIES:
                time.sleep(self._OMP_LINE_WAIT_S)
        return None

    def _judge_turn(self, stop_reason: str, final_text: str, error_text: str,
                    usage: Any, mark: tuple[Optional[Path], int]
                    ) -> "model_errors.ChatTurnVerdict":
        """`model_errors.classify_chat_turn` with omp's line of this turn —
        read only for a turn that ends like a lost model or has no text."""
        end = None
        if model_errors.needs_omp_line(stop_reason, final_text, error_text):
            end = self._omp_turn_line(mark)
        return model_errors.classify_chat_turn(
            stop_reason=stop_reason, final_text=final_text, error_text=error_text,
            usage=usage, omp_line=end.message if end is not None else None,
            omp_retried=end.retried if end is not None else (),
        )

    def _on_elicitation(self, rpc_id: Any, params: dict) -> None:
        """P3: omp asks (`elicitation/create`). Called on the ACP reader
        thread — record, write the card line, return; the operator answers
        through the `answer` op. Only form mode is supported: a URL-mode
        request (or a schema we cannot render) is declined at once, so omp
        falls back to its own default instead of waiting forever."""
        schema = clean_schema(params.get("requestedSchema")) \
            if params.get("mode", "form") == "form" else None
        client = self._client
        if schema is None:
            logger.warning("elicitation declined: mode=%r, unusable schema",
                           params.get("mode"))
            if client is not None:
                client.respond(rpc_id, {"action": "decline"})
            return
        elicitation_id = "el-" + uuid.uuid4().hex[:12]
        message = str(params.get("message") or "").strip()[:_MAX_MESSAGE_CHARS]
        plan_approval = is_plan_approval(message, schema)
        with self._lock:
            self._elicitations[elicitation_id] = {
                "id": elicitation_id, "rpcId": rpc_id, "message": message,
                "schema": schema, "at": _now_iso(), "planApproval": plan_approval,
            }
        self._emit_transcript(self._mapper.map_elicitation(
            elicitation_id, message, schema, plan_approval=plan_approval,
        ))
        self._write_state()

    # ------------------------------------------------------------------
    # sinks + state
    # ------------------------------------------------------------------

    def _emit_transcript(self, entries: list[dict]) -> None:
        """Under `_lock`, like `_write_state`: once close() has set
        `_closed`, no thread of this session — the worker, the ACP reader,
        the pump — writes into the transcript the next session shares."""
        if not entries:
            return
        with self._lock:
            if self._closed or self._sink is None:
                return
            try:
                # P2: tool pictures land next to the transcript (media/), the
                # line keeps only their reference.
                self._sink.write(acp_chat_events.ACPEventMapper.dump(
                    entries, media_writer=self._sink.write_media
                ))
            except Exception:  # noqa: BLE001 — the chat view never kills the turn
                logger.exception("transcript write failed")

    def _emit_preview(self, entries: list[dict]) -> None:
        if not entries:
            return
        with self._lock:
            if self._closed or self._preview is None:
                return
            try:
                self._preview.write(acp_chat_events.ACPEventMapper.dump(entries))
            except Exception:  # noqa: BLE001
                logger.exception("preview write failed")

    def _emit_error(self, code: str, detail: str = "", text: Optional[str] = None,
                    data: Optional[dict] = None) -> None:
        """`data`: extra fields of the card (e.g. `model` for
        `session_reset`/`model_missing`, `retryText`) — never `code` or
        `detail`, never a None value (`ACPEventMapper.map_chat_error`)."""
        with self._lock:
            self._last_error = {"code": code, "detail": str(detail)[:2000],
                                "at": _now_iso()}
        self._emit_transcript(self._mapper.map_chat_error(code, detail, text, data=data))
        self._write_state()

    def _emit_notice(self, code: str, text: str, data: Optional[dict] = None) -> None:
        self._emit_transcript(self._mapper.map_chat_notice(code, text, data))

    def _absorb_session_result(self, result: Optional[dict]) -> None:
        """`session/new`/`session/load` carry the initial configOptions. Live
        omp sends `availableCommands` only as a notification; reading it here
        too costs nothing and helps a server that does include it."""
        if not isinstance(result, dict):
            return
        self._set_config_options(result.get("configOptions"))
        self._set_commands(result.get("availableCommands"))

    def _set_config_options(self, options: Any) -> None:
        if not isinstance(options, list):
            return
        # A copy: config() edits currentValue in place, and that must never
        # write through into the client's session result.
        copied = json.loads(json.dumps([o for o in options if isinstance(o, dict)]))
        with self._lock:
            self._config_options = copied
            self._config_pushes += 1

    @staticmethod
    def _full_option_set(answer: Any, option_id: str) -> Optional[list]:
        """The option set from a `session/set_config_option` reply, as a
        copy — or `None` when the reply carries no full set.

        A full set names the option that was just set. Anything else is a
        placeholder, not the agent's state: `hermes acp` answers every set
        with `configOptions: []` (no typed options yet), and taking that
        would wipe the model row and the effort chip."""
        options = answer.get("configOptions") if isinstance(answer, dict) else None
        if not isinstance(options, list):
            return None
        rows = [o for o in options if isinstance(o, dict)]
        if not any(o.get("id") == option_id for o in rows):
            return None
        return json.loads(json.dumps(rows))

    def _set_commands(self, commands: Any) -> None:
        if not isinstance(commands, list):
            return
        with self._lock:
            self._commands = [c for c in commands if isinstance(c, dict)]

    def _write_state(self) -> None:
        """Mirror the state next to the transcript, where the backend reads
        it (same mount). Atomic rename: a reader never sees half a file.

        Under `_lock`, with a tmp name of its own: the worker writes outside
        the lock while socket handlers write under it, and two writers
        sharing ONE tmp file truncated each other's half-written file and
        renamed it into place (torn JSON, review #777). The lock also keeps
        snapshot and rename in order, so an older snapshot can never land
        after a newer one.

        A retired session (`_closed`) writes nothing — from any thread. Its
        ACP reader still hands over buffered updates while close() waits
        for the child; one of them rewriting the mirror with `busy: false`
        would hide the reply it cut off from the next start (S1-R5)."""
        target = self.state_file
        with self._lock:
            if self._closed:
                return
            payload = self.state()
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, target)
            except OSError:
                logger.warning("state mirror unavailable: %s", target)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _read_persisted(self) -> dict:
        """The session to resume at start: `{"sessionId", "transcript",
        "source"}`, or `{}` when nothing names one (S1-R4).

        A chain — the first source naming BOTH a session id and a
        transcript wins:

        1. the pointer `acp-chat.json` in `state_dir`. The container keeps it
           in the sessions folder, on the mount (`build_session`), so it
           survives a recreate; Hermes keeps it in its workspace.
        2. the state mirror `acp-chat-state.json` next to it — the way over
           the deploy of S1-R4: the old image kept its pointer in the
           unmounted `$OMP_HOME`, but already wrote this file on the mount.

        One exception: a state mirror strictly NEWER than the pointer that
        names ANOTHER session wins over it (the pointer went stale, see
        below). A newer mirror naming the same session is normal operation
        and changes nothing.

        The old place `$OMP_HOME/acp-chat.json` is never read: a new image
        always means a recreate, so it is empty there — and a stale one
        must never win over the mirror. The caller follows a moved
        transcript (`sink_migration.remap`)."""
        pointer, pointer_at = _named_session(self.persist_file)
        mirror, mirror_at = _named_session(self.state_file)
        if pointer and mirror and mirror[0] != pointer[0] and mirror_at > pointer_at:
            # The pointer went stale. Every switch writes it first, so a
            # NEWER mirror naming ANOTHER session means it stopped being
            # written: a rollback to an image that writes only the mirror
            # (and a forward deploy after it), or a pointer write that
            # failed (only a log line). Reopening the pointer's session would
            # silently undo the conversation the operator had open last.
            logger.warning("the pointer names %s, the newer state mirror %s — "
                           "following the state mirror", pointer[0], mirror[0])
            pointer = None
        for source, named in ((RESUME_POINTER, pointer), (RESUME_STATE, mirror)):
            if named is not None:
                return {"sessionId": named[0], "transcript": named[1], "source": source}
        return {}

    def _write_persisted(self) -> None:
        """Point at the current session. Only `sessionId` and `transcript`
        are rewritten: fields this daemon does not know (P4 adds
        `homeSessionId`) stay as they are. Never copies the state mirror."""
        target = self.persist_file
        with self._lock:
            if self._closed:
                # Retired: the pointer belongs to the next session now.
                return
            payload = _read_json_object(target)
            payload["sessionId"] = self._session_id
            payload["transcript"] = str(self.transcript_path) if self.transcript_path else ""
            tmp = target.with_suffix(".json.tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, target)
            except OSError:
                logger.warning("session id not persisted: %s", target)


def retry_data(text: Any) -> dict:
    """`{"retryText": text}` for a card that offers "Send again" — only for
    a non-empty text of at most RETRY_TEXT_MAX_CHARS; `{}` otherwise. Never
    a shortened text: the button sends exactly what the operator wrote, or
    the card has no button."""
    if isinstance(text, str) and text.strip() and len(text) <= RETRY_TEXT_MAX_CHARS:
        return {"retryText": text}
    return {}


def _origin_of(value: Any) -> Optional[str]:
    """A prompt's `origin` as the session keeps it: ORIGIN_OPERATOR, one of
    `_MC_ORIGINS`, or None (unknown — anything else, a missing field, a
    value of the wrong type from the socket)."""
    if value == ORIGIN_OPERATOR or value in _MC_ORIGINS:
        return str(value)
    return None


def _job(kind: str, text: str, origin: Optional[str] = None,
         request_id: Optional[str] = None) -> dict:
    """A job for the worker; `origin` and `requestId` only when known, so a
    job of an unknown author looks exactly as it did before."""
    job = {"kind": kind, "text": text}
    if origin is not None:
        job["origin"] = origin
    if request_id is not None:
        job["requestId"] = request_id
    return job


def _request_id_of(value: Any) -> Optional[str]:
    """A `new_session` request id as the daemon keeps it: a short token
    (`_REQUEST_ID_RE`) or None. Anyone who reaches the socket picks it, and
    it lands in the mirror and on the card."""
    if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value):
        return value
    return None


def _clean_reset_row(raw: Any, partial: bool = False) -> Optional[dict]:
    """One `resets` row in the shape the daemon writes, or None.

    Required: `id`, `via` (VIA_*), `outcome` (RESET_*). Optional: `at`,
    `previousSessionId`, `sessionId`, `reason`, `requestId`, `kept` (bool).
    Unknown keys are dropped. A known key of another shape drops the whole
    row (a row the previous daemon left on the mount) — or, with `partial`
    (a row this daemon is writing), only that value."""
    if not isinstance(raw, dict):
        return None
    shapes = (
        ("id", _RESET_ID_RE), ("at", _AT_RE), ("previousSessionId", _SESSION_ID_RE),
        ("sessionId", _SESSION_ID_RE), ("reason", _REASON_RE), ("requestId", _REQUEST_ID_RE),
    )
    row: dict = {}
    for key, shape in shapes:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str) and shape.fullmatch(value):
            row[key] = value
        elif not partial:
            return None
    if raw.get("via") not in _VIAS or raw.get("outcome") not in _RESET_OUTCOMES \
            or "id" not in row:
        return None
    row["via"], row["outcome"] = raw["via"], raw["outcome"]
    kept = raw.get("kept")
    if isinstance(kept, bool):
        row["kept"] = kept
    elif kept is not None and not partial:
        return None
    return row


def _current_entry(job: dict) -> dict:
    """The mirror's `current` for a claimed job: `{kind, turn, text,
    truncated, startedAt, origin}`. `text` is the whole prompt, or None with
    `truncated: true` when it is longer than RETRY_TEXT_MAX_CHARS. Same
    protection class as the held texts in `queue`. `origin` decides at the
    next start whether the card may offer the text again."""
    kind = job.get("kind")
    text = str(job.get("text") or "") if kind == "prompt" else ""
    whole = len(text) <= RETRY_TEXT_MAX_CHARS
    return {
        "kind": kind,
        "turn": job.get("turn"),
        "text": text if whole else None,
        "truncated": not whole,
        "startedAt": _now_iso(),
        "origin": job.get("origin"),
        **({"requestId": job["requestId"]} if job.get("requestId") else {}),
    }


def _read_lines_from(path: Path, start: int) -> Optional[list[str]]:
    """The lines of `path` from byte `start` on (at most the last
    `_OMP_TURN_READ_BYTES`, without a cut first line), or None when the file
    is unreadable, no regular file, or shorter than `start` (rewritten: its
    end no longer marks this turn). Never blocks (`open_regular`)."""
    fh = acp_chat_events.open_regular(path)
    if fh is None:
        return None
    try:
        with fh:
            raw = fh.buffer
            size = os.fstat(raw.fileno()).st_size
            if size < start:
                return None
            begin = max(start, size - _OMP_TURN_READ_BYTES)
            raw.seek(begin)
            data = raw.read(size - begin)
    except (OSError, ValueError):
        return None
    lines = data.decode("utf-8", errors="replace").split("\n")
    return lines[1:] if begin > start else lines


def _format_counts(counts: Mapping[str, int]) -> str:
    """`{"tool_call": 33, ...}` -> `"tool_call=33, ..."`, sorted by kind."""
    return ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))


def _read_json_object(path: Path) -> dict:
    """A JSON object from `path`, `{}` when the file is missing, unreadable,
    no regular file or holds anything else. Never blocks: the pointer and
    the state mirror sit in the sessions folder, the agent's own mount,
    where a FIFO in their place would otherwise stall the chat's start
    (`open_regular`)."""
    return _read_json_stamped(path)[0]


def _read_json_stamped(path: Path) -> tuple[dict, int]:
    """`_read_json_object` plus the file's mtime in ns (0 when unread),
    taken from the same open file."""
    fh = acp_chat_events.open_regular(path)
    if fh is None:
        return {}, 0
    try:
        with fh:
            stamp = os.fstat(fh.fileno()).st_mtime_ns
            data = json.load(fh)
    except (OSError, ValueError):
        return {}, 0
    return (data, stamp) if isinstance(data, dict) else ({}, 0)


def _named_session(path: Path) -> tuple[Optional[tuple[str, str]], int]:
    """`(sessionId, transcript)` a pointer or state mirror names — None
    unless both are non-empty strings — and the file's mtime in ns."""
    data, stamp = _read_json_stamped(path)
    sid, transcript = data.get("sessionId"), data.get("transcript")
    if isinstance(sid, str) and sid and isinstance(transcript, str) and transcript:
        return (sid, transcript), stamp
    return None, stamp


def _find_transcript(stored: str) -> Optional[Path]:
    """The stored transcript, followed to `.mc/` (`sink_migration.remap`),
    or None when it is in neither place — or the file system refuses the
    path (too long, a folder the daemon may not enter): a missing
    transcript then, never a crash at boot."""
    try:
        transcript = sink_migration.remap(stored)
        return transcript if transcript.exists() else None
    except (OSError, ValueError) as exc:
        logger.warning("stored transcript unusable (%s): %.200s", type(exc).__name__, stored)
        return None


def missing_model(error: str) -> Optional[str]:
    """omp's "Could not restore model X" in a load failure: the selector X
    (`""` when the sentence names none), None for any other failure."""
    match = _MODEL_MISSING_RE.search(error or "")
    if match is None:
        return None
    return match.group("model").rstrip(".:")


def _model_missing_text(selector: str) -> str:
    """The card sentence for `model_missing`, naming the model when known."""
    name = acp_model.display_name(selector)
    model = f"the model {name}" if name else "a model"
    return (f"The last conversation ran on {model}, which is not set up for "
            "this agent right now — a new conversation started. The old one "
            "is kept.")


def _classify_turn(stop_reason: str, final_text: str, error_text: str,
                   verdict: Optional["model_errors.ChatTurnVerdict"] = None) -> tuple[
        Optional[str], str]:
    """Turn outcome -> (chat_error code, detail). `None` = nothing to report.

    `cancelled` is deliberately NOT an error: the operator pressed Stop —
    and neither is a turn omp's own line calls aborted. The model not
    answering (`verdict`, S1-R6) comes first: before `provider_error`,
    `rpc_error` and `empty_turn`.
    """
    if verdict is not None and verdict.kind == MODEL_UNREACHABLE:
        return MODEL_UNREACHABLE, verdict.detail
    if verdict is not None and verdict.kind == model_errors.ABORTED:
        return None, ""
    if error_text:
        code = "provider_error" if _PROVIDER_ERROR_RE.search(error_text) else "rpc_error"
        return code, error_text
    if stop_reason == "cancelled":
        return None, ""
    if stop_reason == "error":
        code = "provider_error" if _PROVIDER_ERROR_RE.search(final_text) else "rpc_error"
        return code, final_text[:1000] or "stopReason=error"
    if not final_text.strip():
        # A turn that produced nothing is a failure the operator must see —
        # a silent empty bubble reads as "the agent ignored me".
        if _PROVIDER_ERROR_RE.search(final_text):
            return "provider_error", final_text[:1000]
        return "empty_turn", f"stopReason={stop_reason!r}, no agent text"
    return None, ""


# ---------------------------------------------------------------------------
# P3: question cards (form elicitation)
# ---------------------------------------------------------------------------

#: The three answers omp understands (ACP `CreateElicitationResponse`).
ELICITATION_ACTIONS = ("accept", "decline", "cancel")
_MAX_MESSAGE_CHARS = 4000
_MAX_PROPERTIES = 24
_MAX_CHOICES = 50
_MAX_LABEL_CHARS = 500
_MAX_ANSWER_CHARS = 20_000
_PROPERTY_TYPES = ("string", "boolean", "number", "integer", "array")


def _label(value: Any) -> Optional[str]:
    return value.strip()[:_MAX_LABEL_CHARS] if isinstance(value, str) and value.strip() else None


def _choices(raw: Any) -> list[dict]:
    """`oneOf`/`anyOf` entries -> [{const, title, description?}] (strings only)."""
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("const"), str):
            continue
        clean = {"const": entry["const"][:_MAX_LABEL_CHARS],
                 "title": _label(entry.get("title")) or entry["const"][:_MAX_LABEL_CHARS]}
        if _label(entry.get("description")):
            clean["description"] = _label(entry.get("description"))
        out.append(clean)
        if len(out) >= _MAX_CHOICES:
            break
    return out


def clean_schema(raw: Any) -> Optional[dict]:
    """omp's `requestedSchema` -> the fail-closed subset the card renders, or
    None when nothing usable is left. Only the fields omp 18.10.1 actually
    sends survive (type, title, description, enum, oneOf, default, items);
    anything else stays out of the transcript."""
    if not isinstance(raw, dict) or not isinstance(raw.get("properties"), dict):
        return None
    properties: dict[str, dict] = {}
    for key, prop in raw["properties"].items():
        if not isinstance(key, str) or not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        if ptype not in _PROPERTY_TYPES:
            continue
        clean: dict[str, Any] = {"type": ptype}
        for field in ("title", "description"):
            if _label(prop.get(field)):
                clean[field] = _label(prop.get(field))
        if isinstance(prop.get("enum"), list):
            clean["enum"] = [v[:_MAX_LABEL_CHARS] for v in prop["enum"]
                             if isinstance(v, str)][:_MAX_CHOICES]
        if prop.get("oneOf") is not None:
            clean["oneOf"] = _choices(prop.get("oneOf"))
        if isinstance(prop.get("default"), (str, bool)):
            clean["default"] = prop["default"]
        if ptype == "array":
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            clean["items"] = {"anyOf": _choices(items.get("anyOf") or items.get("oneOf"))}
        properties[key[:64]] = clean
        if len(properties) >= _MAX_PROPERTIES:
            break
    if not properties:
        return None
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    required = [r for r in raw.get("required") or [] if isinstance(r, str) and r in properties]
    if required:
        schema["required"] = required
    return schema


#: omp 18.10.1 `#requestAcpPlanApprovalChoice`: `Approve plan "<title>" and
#: start implementation?` + a select of exactly two values (approve/refine).
_PLAN_APPROVAL_PREFIX = 'Approve plan "'


def is_plan_approval(message: str, schema: dict) -> bool:
    """THE one place that recognises omp's plan approval question. Both
    signals must hold — the fixed message prefix AND a single select field
    with exactly two values — so an ordinary question that merely starts
    with those words is not taken for it."""
    if not isinstance(message, str) or not message.startswith(_PLAN_APPROVAL_PREFIX):
        return False
    properties = (schema or {}).get("properties") or {}
    if set(properties) != {"value"}:
        return False
    prop = properties["value"]
    return prop.get("type") == "string" and len(prop.get("enum") or []) == 2


def _allowed_values(prop: dict) -> Optional[set]:
    if prop.get("enum"):
        return set(prop["enum"])
    if prop.get("oneOf"):
        return {c["const"] for c in prop["oneOf"]}
    return None


def validate_content(schema: dict, content: Any) -> Optional[str]:
    """None when ``content`` is a valid answer to ``schema``, else why not.
    The daemon checks before it replies — a malformed answer must stay a
    refusal on OUR side, not a confused agent."""
    if not isinstance(content, dict):
        return "content must be an object"
    properties = schema.get("properties") or {}
    for key, value in content.items():
        prop = properties.get(key)
        if prop is None:
            return f"unknown field {key!r}"
        ptype = prop["type"]
        if ptype == "string":
            if not isinstance(value, str) or len(value) > _MAX_ANSWER_CHARS:
                return f"{key!r} must be a string"
            allowed = _allowed_values(prop)
            if allowed is not None and value not in allowed:
                return f"{key!r} is not one of the offered choices"
        elif ptype == "boolean":
            if not isinstance(value, bool):
                return f"{key!r} must be true or false"
        elif ptype in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{key!r} must be a number"
        elif ptype == "array":
            choices = {c["const"] for c in (prop.get("items") or {}).get("anyOf") or []}
            if not isinstance(value, list) or not all(
                isinstance(v, str) and (not choices or v in choices) for v in value
            ):
                return f"{key!r} must be a list of the offered choices"
    for key in schema.get("required") or []:
        if key not in content:
            return f"{key!r} is required"
    return None


# ---------------------------------------------------------------------------
# control protocol
# ---------------------------------------------------------------------------


def dispatch(session: ChatSession, request: dict) -> dict:
    """One control request -> one response (spec's op table)."""
    op = request.get("op")
    if op == "prompt":
        # `origin` (optional): who wrote the text — see ORIGIN_OPERATOR.
        return session.prompt(request.get("text") or "", request.get("mode") or MODE_NOW,
                              origin=request.get("origin"))
    if op == "new_session":
        # `requestId` (optional): which request of MC's backend asked.
        return session.new_session(request.get("mode") or MODE_NOW,
                                   request_id=request.get("requestId"))
    if op == "queue_clear":
        return session.queue_clear()
    if op == "cancel":
        return session.cancel()
    if op == "config":
        return session.config(request.get("id"), request.get("value"))
    if op == "answer":
        return session.answer(request.get("id"), request.get("action"),
                              request.get("content"))
    if op == "state":
        return {"ok": True, **session.state()}
    return {"ok": False, "error": "unknown_op", "detail": str(op)}


class _Handler(socketserver.StreamRequestHandler):
    """One connection = one request line = one response line."""

    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            response = {"ok": False, "error": "bad_request", "detail": str(exc)}
        else:
            try:
                response = dispatch(self.server.chat_session, request)
            except Exception as exc:  # noqa: BLE001 — never drop a connection
                logger.exception("dispatch failed")
                response = {"ok": False, "error": "internal",
                            "detail": f"{type(exc).__name__}: {exc}"}
        try:
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ChatSocketServer:
    """Newline-delimited JSON over a Unix socket in front of a ChatSession."""

    def __init__(self, session: ChatSession, socket_path: str):
        self._path = str(socket_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # A stale socket file from a killed daemon would block bind().
        if os.path.exists(self._path):
            try:
                os.unlink(self._path)
            except OSError:
                pass
        self._server = _UnixServer(self._path, _Handler)
        self._server.chat_session = session
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    @property
    def path(self) -> str:
        return self._path

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.1)

    def shutdown(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
            try:
                os.unlink(self._path)
            except OSError:
                pass


def request(socket_path: str, payload: dict, timeout: float = 30.0) -> dict:
    """Client side of the protocol — used by acp_chat_ctl.py and Hermes."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while b"\n" not in b"".join(chunks):
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise ConnectionError("daemon closed the connection without a response")
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# entrypoint (tmux window 3)
# ---------------------------------------------------------------------------


def model_selector(env: "Mapping[str, str]") -> Optional[str]:
    """Alias of :func:`acp_model.selector_from` — the ONE selector rule the
    task bridge and launch-omp.sh follow too, so chat and task sessions of
    one agent can never drift onto different models. None when nothing is
    set (the caller decides whether that is a boot error)."""
    return acp_model.selector_from(env)


def build_session() -> ChatSession:
    """The production wiring: an `omp acp` child on the pinned ACP cwd, with
    the transcript written where the backend's chat reader tails it."""
    cwd = default_cwd()
    # The CURRENT omp.env, not only the boot env (acp_model docstring).
    model = acp_model.current_selector(os.environ)
    if model is None:
        # No baked-in default: a missing model is a boot error, not a silent
        # fallback to omp's built-in provider catalog (ADR-054, same rule as
        # acp_model.current_selector(required=True) on the task path).
        raise RuntimeError(
            "OMP_MODEL_SELECTOR / OPENAI_MODEL not set — entrypoint must "
            "render models.yml first; refusing to open a chat session on "
            "omp's built-in default model"
        )
    sessions_dir = acp_chat_events.session_dir(cwd=cwd)
    if sessions_dir is None:
        raise RuntimeError(
            "no sessions directory for the chat transcript — neither "
            "OMP_HOME+OMP_PROFILE nor PI_CODING_AGENT_DIR is set (the "
            "entrypoint exports them)"
        )
    # The pointer (acp-chat.json) sits next to the state mirror in the
    # sessions folder — on the mount, so it survives a recreate. $OMP_HOME
    # is not mounted: every new image used to start the chat over (S1-R4).
    state_dir = sessions_dir
    return ChatSession(
        client_factory=lambda: acp_client.ACPClient(cwd=cwd),
        cwd=cwd,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        driver="omp",
        model=model,
        model_resolver=acp_model.current_selector,
        elicitation=True,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ACP chat daemon")
    parser.add_argument("--serve", action="store_true",
                        help="run the socket server (the only mode)")
    parser.add_argument("--socket", default=None,
                        help=f"socket path (default: $OMP_HOME/{_DEFAULT_SOCKET_NAME})")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("OMP_ACP_CHAT_LOGLEVEL", "INFO"),
        format="[acp-chat] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if not args.serve:
        parser.error("--serve is required")

    session = build_session()
    session.start()
    server = ChatSocketServer(session, args.socket or default_socket_path())
    sys.stderr.write(
        f"[acp-chat] ready: session={session.state()['sessionId']} "
        f"model={session._model} socket={server.path} "
        f"transcript={session.transcript_path}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
