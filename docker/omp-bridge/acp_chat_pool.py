#!/usr/bin/env python3
"""
acp_chat_pool.py — many chats on one agent's chat daemon (chats slice S2).

WHY: the chat daemon (acp_chat.py) held exactly one ACP session — "the
agent's chat". Slice S2 of the chats-instead-of-fixed-agents plan lets one
agent hold several chats, each with its OWN native session, so two chats
used alternately each keep their own context, and a daemon restart puts
each one back where it was (`session/load`).

Contract:

* A control request WITHOUT `chatId` goes to the agent's default session,
  exactly as before. The backend only sends `chatId` while its
  `CHAT_RECORD_ENABLED` flag is on, so with the flag off nothing here runs.
* A request WITH `chatId` goes to that chat's own `ChatSession`
  (`PinnedChatSession`), opened on first use: MC's plan is read at once
  (refusals answer the request), the child starts in a thread and the
  message answers `opening` and runs once the session is open. MC's chat record is the truth
  about which native session a chat owns:
  `GET /api/v1/chat-record/agent/me/chats/{id}/resume` (agent token) says
  `session/load <id>` or `session/new`; after `session/new` the daemon pins
  the new id with `PUT …/native-session`. A chat MC does not know (404) is
  refused (`chat_unknown`) — the daemon never invents a chat. A pinned
  session that cannot be loaded is NOT replaced by a new one (MC refuses a
  second id, the chat would lose its context for good): `chat_load_failed`
  card in the chat's own copy, nothing sent.
* Each further chat runs its own `omp acp` child, its own pointer and state
  mirror under `<session folder>/.mc/chats/<chatId>/` (on the mount, deeper
  than the backend's pointer search, so the default chat view keeps
  following the default session), and writes its transcript copy into the
  same `.mc/` folder as every chat copy — the record's importer routes it
  to the chat by its native session id (#1041).
* At most `max_open` further chats hold a child at once. Opening one more
  closes the least recently used IDLE chat; its session is on disk and is
  loaded again (`session/load`) on its next message. All of them busy:
  `chats_busy`, nothing is dropped silently.
* `/new` (`new_session`) on a further chat is refused (`chat_pinned`): its
  native session is pinned for good. A new conversation is a new chat — the
  backend creates it in the record and sends its `chatId`.
* `switch` (S3 item C, ADR-105) is the one legitimate replacement of a
  pinned session: MC asked for it and names the switch row
  (`switchEventId`). The chat's child ends, a new one starts; for a
  `resume` MC asked to try (`tryNativeLoad`) `session/load` comes first and
  needs no package. Otherwise `session/new`, and the first prompt carries
  MC's handoff package as its `context` (cap `CONTEXT_MAX_CHARS_HANDOFF`).
  The outcome goes to MC's switch result route — never the pin route, which
  refuses a re-pin while a switch is pending. A failed switch puts the chat
  back on its old session (MC still pins that one). Open question cards end
  as `expired` with reason `expired_by_switch`. `stop_for_switch` stops a
  running turn for a switch and hands the held queue back to MC, which
  sends it again after the switch — the daemon never drops it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

import acp_chat
import acp_chat_events

logger = logging.getLogger("acp_chat_pool")

#: A chat id is the record's UUID — nothing else reaches a file path.
_CHAT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: Where a further chat keeps its pointer and state mirror, under the
#: chat copies' folder (`<session folder>/.mc/`).
CHATS_DIRNAME = "chats"
#: Further chats holding an `omp acp` child at once (env
#: OMP_ACP_CHAT_MAX_OPEN). A placeholder until the memory of one child is
#: measured (architecture study §4.2/§6.3), not a decision.
DEFAULT_MAX_OPEN = 3

#: The card when MC refused the pin (another chat holds the id, or the chat
#: is pinned to another session): the record would put this conversation
#: into the agent's default chat, so the operator must know.
CHAT_PIN_FAILED = "chat_pin_failed"
_CHAT_PIN_FAILED_TEXT = (
    "This chat could not be bound to its own session in Mission Control — "
    "its history may show up in the agent's main chat."
)


#: The card when the pinned session of a chat could not be loaded: the
#: daemon does NOT open a new one (MC would never route it to this chat).
CHAT_LOAD_FAILED = "chat_load_failed"
_CHAT_LOAD_FAILED_TEXT = (
    "This chat's conversation could not be loaded, so nothing was sent. "
    "Start a new chat to go on."
)
#: The card when a further chat could not open (child, handshake, MC).
CHAT_OPEN_FAILED = "chat_open_failed"
_CHAT_OPEN_FAILED_TEXT = (
    "This chat could not be opened — the message was not sent."
)


#: Ids MC sends for a switch: the `chat_events` row id (a UUID).
_SWITCH_ID_RE = _CHAT_ID_RE
#: A model selector as omp takes it (`provider/model`, `model:tag`) — the
#: backend's `chat_switch.MODEL_PATTERN`.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,199}$")
#: How long `stop_for_switch` waits for the stopped turn to end.
STOP_WAIT_SECONDS = 8.0
#: Pauses between tries of a switch report MC did not answer; after the last
#: one the report waits for the chat's next message (`prompt`).
REPORT_RETRY_DELAYS = (0.5, 2.0)
#: Codes MC's switch result route takes (`chat_switch.REPORTABLE_CODES`).
_REPORT_CODES = ("process_start_failed", "new_session_failed", "model_unreachable",
                 "provider_error")


def _report_code(code: str) -> str:
    """A turn's error code as MC's switch result route takes it."""
    if code in _REPORT_CODES:
        return code
    if code in ("process_exit", "closed"):
        return "process_start_failed"
    return "provider_error"  # rpc_error, empty_turn, … — the model's side refused


class McUnreachable(Exception):
    """MC did not answer (network, 5xx, bad JSON)."""


class ChatUnknown(Exception):
    """MC does not know this chat on this agent (404) — or the flag is off."""


class FollowAgent(Exception):
    """The chat is the agent's default chat: it is the default session."""


class PinConflict(Exception):
    """MC refused the pin (409): `already_pinned` / `pinned_by_other_chat`."""


class McChats:
    """The two agent-token routes of MC's chat record the daemon needs."""

    def __init__(self, api_url: Optional[str] = None,
                 token: Optional[Callable[[], str]] = None, timeout: float = 4.0):
        self._api_url = (api_url or os.environ.get("MC_API_URL") or "http://backend:8000").rstrip("/")
        # Read at call time: the entrypoint may refresh the token.
        self._token = token or (lambda: os.environ.get("MC_AGENT_TOKEN", ""))
        # Short: `prepare()` runs inside a control request the backend waits
        # 10 s for (acp_chat_transport._CTL_TIMEOUT_SECONDS).
        self._timeout = timeout

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self._api_url}/api/v1/chat-record/agent/me/chats/{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self._token()}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ChatUnknown(path) from None
            if exc.code == 409:
                reason = ""
                try:
                    detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail")
                    reason = detail.get("reason", "") if isinstance(detail, dict) else str(detail or "")
                except (ValueError, AttributeError):
                    pass
                raise PinConflict(reason or "conflict") from None
            raise McUnreachable(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise McUnreachable(f"{type(exc).__name__}: {exc}") from None
        try:
            parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError as exc:
            raise McUnreachable(f"bad JSON: {exc}") from None
        return parsed if isinstance(parsed, dict) else {}

    def resume(self, chat_id: str) -> dict:
        return self._call("GET", f"{chat_id}/resume")

    def pin(self, chat_id: str, native_session_id: str) -> dict:
        return self._call("PUT", f"{chat_id}/native-session",
                          {"nativeSessionId": native_session_id})

    def report(self, chat_id: str, switch_event_id: str, body: dict) -> dict:
        """How a switch ended (S3 item C): `PUT …/switch/{event}/result` —
        `{status: done|failed, nativeSessionId?, nativeResume?, realTokens?,
        error?: {code, detail}}`. The one route that re-pins a chat."""
        return self._call("PUT", f"{chat_id}/switch/{switch_event_id}/result", body)


def _plan_native(plan: dict) -> Optional[str]:
    """The native session the plan says to load, or None (`session/new`)."""
    acp = plan.get("acp") if isinstance(plan.get("acp"), dict) else {}
    if acp.get("method") == "session/load":
        sid = acp.get("sessionId") or plan.get("nativeSessionId")
        if isinstance(sid, str) and acp_chat._SESSION_ID_RE.fullmatch(sid):
            return sid
    return None


class PinnedChatSession(acp_chat.ChatSession):
    """A further chat of this agent: one ACP session, pinned in MC's record.

    Same daemon behaviour as the default session (queue, cards, replay mute,
    model pin, child restart) — only where the session comes from and where
    its files sit differ."""

    def __init__(self, chat_id: str, mc: McChats, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.chat_id = chat_id
        self._mc = mc
        # The native id MC holds for this chat, once known (plan or pin).
        self._pinned: Optional[str] = None
        # Where `start()` resumes from, read by `prepare()` (MC's plan).
        self._prepared: Optional[dict] = None
        # The pinned session could not be loaded: this chat runs nothing
        # until it is opened again (the pool drops it, a card says why).
        self.load_refused = False
        self.last_used = time.monotonic()
        # Ops running on this chat right now (the pool's eviction skips it).
        self.inflight = 0
        # A switch report MC did not take yet: `(switchEventId, body)`,
        # tried again before the chat's next message.
        self._pending_report: Optional[tuple[str, dict]] = None

    # -- files: the chat's own pointer AND state mirror ------------------

    @property
    def state_file(self) -> Path:
        # Next to the chat's own pointer, never the agent's mirror: the
        # default chat view follows `<session folder>/acp-chat-state.json`.
        return self._state_dir / acp_chat._STATE_FILENAME

    def _migrate_sinks(self) -> Optional[str]:
        # The default session moved the old copies at daemon start.
        return None

    def _earlier_chat_exists(self) -> bool:
        # A further chat is new on purpose — no "started fresh" card.
        return False

    # -- where the session comes from ------------------------------------

    def prepare(self) -> None:
        """Ask MC where this chat resumes (fast: one HTTP call, no child).
        Raises ChatUnknown / FollowAgent / McUnreachable (no local pointer)
        — the refusals the pool answers at once, before it accepts a
        message. `start()` then uses this answer."""
        self._prepared = self._plan_persisted()

    def _read_persisted(self) -> dict:
        if self._prepared is not None:
            prepared, self._prepared = self._prepared, None
            return prepared
        return self._plan_persisted()

    def _plan_persisted(self) -> dict:
        """MC's plan first; the chat's own pointer when MC is unreachable
        (a restart while the backend is down must not lose the chat)."""
        local = super()._read_persisted()
        try:
            plan = self._mc.resume(self.chat_id)
        except McUnreachable as exc:
            if local:
                logger.warning("chat %s: MC unreachable (%s) — resuming the local pointer",
                               self.chat_id, exc)
                return local
            raise
        if plan.get("action") == "follow_agent" or plan.get("agentDefault"):
            raise FollowAgent(self.chat_id)
        native = _plan_native(plan)
        if native is None:
            # Not pinned yet. A local pointer = a pin that never reached MC
            # (MC was down after session/new): load it, pin it after.
            return local
        self._pinned = native
        if local.get("sessionId") == native:
            return local
        return {"sessionId": native, "transcript": str(self._copy_of(native)),
                "source": "mc"}

    def _copy_of(self, native: str) -> Path:
        """The chat copy of session `native` in `.mc/` — the newest one, or a
        new file with its header when none is left (the pointer was lost):
        a load then appends to it instead of resetting the chat."""
        folder = self._sessions_dir / acp_chat_events.MC_SINK_DIRNAME
        try:
            found = sorted(p for p in folder.glob(f"*_{native}.jsonl") if p.is_file())
        except OSError:
            found = []
        if found:
            return found[-1]
        sink = acp_chat_events.ChatEventSink(self._sessions_dir, native)
        return sink.path or folder / f"missing_{native}.jsonl"

    def _may_replace_unloadable(self, session_id: str) -> bool:
        # MC holds this chat pinned to `session_id`, and MC refuses a second
        # id (`already_pinned`): a new session would never reach this chat.
        return not (self._pinned and session_id == self._pinned)

    def _connect(self, persisted: dict) -> None:
        try:
            super()._connect(persisted)
        except acp_chat.SessionLoadRefused as exc:
            self._refuse_unloadable(exc, persisted)
            raise
        self._ensure_pinned()

    def _refuse_unloadable(self, exc: "acp_chat.SessionLoadRefused", persisted: dict) -> None:
        """The card goes into the chat's OWN copy (the record routes it to
        this chat by its native id); the child goes away."""
        self.load_refused = True
        logger.error("chat %s: session %s could not be loaded (%s) — no new session",
                     self.chat_id, exc.session_id, exc.detail)
        if self._sink is None:
            transcript = persisted.get("transcript")
            path = Path(transcript) if transcript else self._copy_of(exc.session_id)
            with self._lock:
                self._mapper.set_session_id(exc.session_id)
                self._sink = acp_chat_events.ChatEventSink.appending(path, exc.session_id)
        self._emit_error(CHAT_LOAD_FAILED, exc.detail, text=_CHAT_LOAD_FAILED_TEXT,
                         data=exc.data or None)
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _ensure_pinned(self) -> None:
        """Tell MC which native session this chat runs on. MC unreachable:
        tried again before the next message. Refused: a visible card."""
        sid = self._session_id
        if not sid or sid == self._pinned:
            return
        try:
            self._mc.pin(self.chat_id, sid)
        except McUnreachable as exc:
            logger.warning("chat %s: pin of %s not delivered (%s) — retrying later",
                           self.chat_id, sid, exc)
            return
        except (PinConflict, ChatUnknown) as exc:
            logger.error("chat %s: MC refused the pin of %s: %s", self.chat_id, sid, exc)
            self._pinned = sid  # one card, not one per message
            self._emit_error(CHAT_PIN_FAILED, f"{exc}", text=_CHAT_PIN_FAILED_TEXT)
            return
        self._pinned = sid

    # -- ops -------------------------------------------------------------

    def prompt(self, *args: Any, **kwargs: Any) -> dict:
        if self.load_refused:
            return {"ok": False, "error": CHAT_LOAD_FAILED}
        self._retry_report()
        self._ensure_pinned()
        return super().prompt(*args, **kwargs)

    # -- switch (S3 item C, ADR-105) -------------------------------------

    def stop_for_switch(self) -> dict:
        """Before MC switches this chat while a turn runs
        (`stop_and_switch`): the held queue goes back to MC (`handedOver`,
        oldest first — MC sends it again after the switch, rule F8), open
        question cards end `expired_by_switch` (F9), the turn is stopped.
        Nothing is dropped: whatever was waiting is in the answer."""
        with self._lock:
            if self._closed:
                return {"ok": False, "error": "not_started"}
            handed, self._queue = self._queue, []
            busy = self._busy
        self._expire_elicitations(reason=acp_chat.EXPIRED_BY_SWITCH)
        idle = True
        if busy:
            self.cancel()
            idle = self.wait_idle(STOP_WAIT_SECONDS)
        if handed:
            self._write_state()
        return {"ok": True, "handedOver": [_handed(j) for j in handed],
                "stopped": busy, "idle": idle}

    def switch(self, request: dict) -> dict:
        """MC moves this chat to another pair. Refused while a turn runs
        (F1: `busy`, nothing changes) — MC stops it first
        (`stop_for_switch`). Runs in the worker like a turn; the answer
        comes at once, the outcome through MC's switch result route."""
        cleaned = _clean_switch(request)
        if "error" in cleaned:
            return {"ok": False, **cleaned}
        with self._lock:
            if self._closed:
                return {"ok": False, "error": "not_started"}
            if self._busy:
                return {"ok": False, "error": "busy"}
            # Empty while idle — but a job held here must never vanish.
            handed, self._queue = self._queue, []
            job = self._claim_locked({"kind": "switch", "text": "", "switch": cleaned})
        self._write_state()
        self._spawn(job)
        return {"ok": True, "switching": True, "switchEventId": cleaned["switchEventId"],
                "handedOver": [_handed(j) for j in handed]}

    def _run_switch(self, job: dict) -> None:
        req = job["switch"]
        event_id = req["switchEventId"]
        with self._lock:
            previous = self._session_id
            previous_transcript = self.transcript_path
            previous_model, previous_resolver = self._model, self._model_resolver
        # F9: a question of the old session can never be answered in the new one.
        self._expire_elicitations(reason=acp_chat.EXPIRED_BY_SWITCH)
        with self._lock:
            old_client, self._client = self._client, None
        if old_client is not None:
            try:
                old_client.close()
            except Exception:  # noqa: BLE001 — the old child goes either way
                pass
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        if self._pump is None:
            self._pump = acp_chat_events.ToolOutputPump(self._mapper, self._emit_preview).start()
        if req.get("model"):
            # The pair MC asked for, not the agent's omp.env selector.
            self._model, self._model_resolver = req["model"], None
        restore = dict(previous=previous, transcript=previous_transcript,
                       model=previous_model, resolver=previous_resolver)

        try:
            client = self._new_client()
        except Exception as exc:  # noqa: BLE001
            self._switch_failed(event_id, "process_start_failed", exc, None, **restore)
            return
        native = req.get("nativeSessionId")
        loaded = False
        if req["action"] == "resume" and native:
            try:
                with self._muted_replay():
                    client.load_session(native, self._cwd)
                loaded = True
            except Exception as exc:  # noqa: BLE001
                if not req["tryNativeLoad"]:
                    # Switch back: the old session itself, or nothing.
                    self._switch_failed(event_id, "new_session_failed",
                                        f"session/load {native}: {exc}", client, **restore)
                    return
                logger.info("chat %s: session/load %s for the switch failed (%s) — "
                            "a new session gets the handoff", self.chat_id, native, exc)
        if loaded:
            session_id = native
            sink = acp_chat_events.ChatEventSink.appending(self._copy_of(native), native)
        else:
            try:
                session_id = client.new_session(self._cwd)
            except Exception as exc:  # noqa: BLE001
                self._switch_failed(event_id, "new_session_failed", exc, client, **restore)
                return
            sink = acp_chat_events.ChatEventSink(self._sessions_dir, session_id)
        with self._lock:
            self._client = client
            self._session_id = session_id
            if not loaded:
                self._context = None
            self._mapper.set_session_id(session_id)
            self._sink = sink
            self._preview = acp_chat_events.PreviewEventSink.beside(sink.path, session_id)
            # MC learns this id from the switch report, never from the pin
            # route (it refuses a re-pin while the switch is pending).
            self._pinned = session_id
        # The pointer is written only once MC took the switch: until then
        # a restart resumes the session MC still pins.
        self._absorb_session_result(getattr(client, "last_session_result", None))
        self._pin_model()

        package = req.get("package")
        handoff = bool(package) and not (loaded and req["tryNativeLoad"])
        usage = None
        if handoff:
            text = acp_chat.handoff_text_of(req.get("handoffText"))
            with self._lock:
                self._turn += 1
                turn = self._turn
                self._omp_mark = self._omp_file_mark()
                self._current = acp_chat._current_entry(
                    {"kind": "prompt", "text": text, "turn": turn,
                     "origin": acp_chat.ORIGIN_HANDOFF})
            self._write_state()
            outcome = self._run_turn(text, turn, acp_chat.ORIGIN_HANDOFF,
                                     acp_chat.handoff_context_of(package))
            if outcome.get("code"):
                # F6: the new model refused the handoff turn.
                self._switch_failed(event_id, _report_code(outcome["code"]),
                                    outcome.get("detail") or outcome["code"], None, **restore)
                return
            usage = outcome.get("usage")
        self._write_persisted()
        body: dict = {"status": "done", "nativeSessionId": session_id,
                      "nativeResume": bool(loaded and not handoff)}
        tokens = usage.get("inputTokens") if isinstance(usage, dict) else None
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            body["realTokens"] = tokens
        self._report(event_id, body)

    def _new_client(self) -> Any:
        """A fresh child, handshaken — the first half of `_connect`."""
        client = self._client_factory()
        client.on_event(self._on_event)
        client.on_permission(self._on_permission)
        if self._elicitation_enabled and hasattr(client, "on_elicitation"):
            client.on_elicitation(self._on_elicitation)
        client._ensure_process()
        client.initialize()
        return client

    def _switch_failed(self, event_id: str, code: str, detail: Any, client: Any, *,
                       previous: Optional[str], transcript: Optional[Path],
                       model: Optional[str], resolver: Any) -> None:
        """F2/F6: MC keeps the old pair and the old session current — so
        does the daemon. The new child goes, the old session is loaded
        again (pinned: never replaced, `chat_load_failed` if it cannot load),
        and MC hears why."""
        detail = f"{type(detail).__name__}: {detail}" if isinstance(detail, BaseException) \
            else str(detail)
        logger.warning("chat %s: switch %s failed (%s): %s", self.chat_id, event_id, code, detail)
        with self._lock:
            current, self._client = self._client, None
            self._model, self._model_resolver = model, resolver
        for dead in {id(c): c for c in (client, current) if c is not None}.values():
            try:
                dead.close()
            except Exception:  # noqa: BLE001
                pass
        self._report(event_id, {"status": "failed",
                                "error": {"code": code, "detail": detail[:2000]}})
        try:
            if previous:
                self._pinned = previous
                self._session_id = None
                self._connect({"sessionId": previous,
                               "transcript": str(transcript) if transcript else "",
                               "source": "switch"})
            else:
                # Never open here before: MC's plan says where it stands.
                self._session_id = None
                self._connect(self._plan_persisted())
        except acp_chat.SessionLoadRefused:
            pass  # its card is out; the pool reopens it on the next message
        except Exception as exc:  # noqa: BLE001 — the pool reopens it from MC's plan
            logger.error("chat %s: back to its old session failed: %s", self.chat_id, exc)
            self.load_refused = True

    def _report(self, event_id: str, body: dict) -> None:
        """MC's switch result route, a few tries; what does not arrive waits
        for the chat's next message. A refusal (404/409) is final — MC's
        orphan sweep and the switch card own that case."""
        for delay in (0.0, *REPORT_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                self._mc.report(self.chat_id, event_id, body)
            except McUnreachable as exc:
                logger.warning("chat %s: switch report %s not delivered (%s)",
                               self.chat_id, event_id, exc)
                continue
            except (PinConflict, ChatUnknown) as exc:
                logger.error("chat %s: MC refused the switch report %s: %s",
                             self.chat_id, event_id, exc)
            with self._lock:
                self._pending_report = None
            return
        with self._lock:
            self._pending_report = (event_id, body)

    def _retry_report(self) -> None:
        with self._lock:
            pending = self._pending_report
        if pending is not None:
            event_id, body = pending
            try:
                self._mc.report(self.chat_id, event_id, body)
            except McUnreachable:
                return
            except (PinConflict, ChatUnknown) as exc:
                logger.error("chat %s: MC refused the switch report %s: %s",
                             self.chat_id, event_id, exc)
            with self._lock:
                if self._pending_report is pending:
                    self._pending_report = None

    def new_session(self, mode: str = acp_chat.MODE_NOW, request_id: Any = None) -> dict:
        # Its native session is pinned for good: a new conversation is a
        # new chat (the backend creates it and sends its chatId).
        return {"ok": False, "error": "chat_pinned"}


def _handed(job: dict) -> dict:
    """A held job as MC gets it back to send again: the text and who wrote
    it, its run context, when it was held."""
    out = {"text": str(job.get("text") or "")}
    for key in ("origin", "context", "at", "id"):
        if job.get(key):
            out[key] = job[key]
    return out


def _clean_switch(request: dict) -> dict:
    """The `switch` request as the worker uses it, or `{"error", "detail"}`."""
    event_id = request.get("switchEventId")
    if not isinstance(event_id, str) or not _SWITCH_ID_RE.match(event_id):
        return {"error": "bad_switch", "detail": "switchEventId"}
    action = request.get("action")
    if action not in ("resume", "start"):
        return {"error": "bad_switch", "detail": "action"}
    native = request.get("nativeSessionId")
    if native is not None and (not isinstance(native, str)
                               or not acp_chat._SESSION_ID_RE.fullmatch(native)):
        return {"error": "bad_switch", "detail": "nativeSessionId"}
    if action == "resume" and not native:
        return {"error": "bad_switch", "detail": "resume needs nativeSessionId"}
    package = request.get("package")
    if package is not None and not isinstance(package, str):
        return {"error": "bad_switch", "detail": "package"}
    model = request.get("model")
    if model is not None and (not isinstance(model, str) or not _MODEL_RE.match(model)):
        return {"error": "bad_switch", "detail": "model"}
    text = request.get("handoffText")
    if text is not None and not isinstance(text, str):
        return {"error": "bad_switch", "detail": "handoffText"}
    if request.get("runtimeEnv"):
        # One models.yml + omp.env per agent: a child of this daemon cannot
        # reach another runtime's endpoint (open point of S3 item C).
        return {"error": "runtime_env_unsupported"}
    return {
        "switchEventId": event_id.lower(),
        "action": action,
        "nativeSessionId": native,
        "tryNativeLoad": request.get("tryNativeLoad") is True,
        "package": package or None,
        "model": model or None,
        "handoffText": text,
    }


class _Opening:
    """A further chat between its first message and its open session: the
    messages that arrive meanwhile wait here, in order."""

    def __init__(self, chat: PinnedChatSession, first: Optional[dict]):
        self.chat = chat
        self.pending: list[dict] = [first] if first is not None else []
        # Set once MC's plan is in (`prepare`): until then a second request
        # for the same chat waits, so it gets the same answer as the first.
        self.planned = threading.Event()
        self.refusal: Optional[dict] = None
        self.follow_agent = False
        self.done = threading.Event()


#: How long a second request for a chat waits for MC's plan of the first.
_PLAN_WAIT_SECONDS = 8.0


class ChatPool:
    """The default session plus the further chats of one agent.

    Opening a further chat (the `omp acp` child, `initialize`, the
    `session/load` replay, the pin) runs in its own thread, never under the
    pool's lock and never inside the control request: the backend waits
    only seconds for an answer (`acp_chat_transport._CTL_TIMEOUT_SECONDS`),
    and a timed-out answer to a message that still runs invites sending it
    twice. The first message answers `opening` at once and runs when the
    session is open; a chat that cannot open says so as a card."""

    def __init__(self, default: acp_chat.ChatSession,
                 factory: Callable[[str], PinnedChatSession], *,
                 max_open: int = DEFAULT_MAX_OPEN):
        self.default = default
        self._factory = factory
        self._max_open = max(1, int(max_open))
        self._chats: dict[str, PinnedChatSession] = {}
        self._opening: dict[str, _Opening] = {}
        self._lock = threading.Lock()
        self._closed = False

    def open_chats(self) -> list[dict]:
        with self._lock:
            chats = list(self._chats.values())
            opening = list(self._opening.keys())
        listed = [{"chatId": c.chat_id, **{k: c.state().get(k) for k in ("sessionId", "busy")}}
                  for c in chats]
        listed += [{"chatId": cid, "sessionId": None, "busy": False, "opening": True}
                   for cid in opening]
        return listed

    def get(self, chat_id: str) -> Optional[PinnedChatSession]:
        """The open session of a further chat, or None (not open)."""
        with self._lock:
            return self._chats.get(str(chat_id).lower())

    def wait_open(self, chat_id: str, timeout: float = 10.0) -> Optional[PinnedChatSession]:
        """Wait until an opening chat is open (or failed); its session or None."""
        with self._lock:
            opening = self._opening.get(str(chat_id).lower())
        if opening is not None:
            opening.done.wait(timeout)
        return self.get(chat_id)

    # -- one request -------------------------------------------------------

    def route(self, chat_id: Any, request: dict) -> dict:
        if not isinstance(chat_id, str) or not _CHAT_ID_RE.match(chat_id):
            return {"ok": False, "error": "bad_chat_id"}
        chat_id = chat_id.lower()
        op = request.get("op")
        inner = {k: v for k, v in request.items() if k != "chatId"}
        if op == "switch":
            return self._switch(chat_id, inner)
        for _attempt in range(2):
            with self._lock:
                chat = self._chats.get(chat_id)
                if chat is not None and chat.load_refused:
                    # Its card is out; the next message tries to open it again.
                    del self._chats[chat_id]
                    stale, chat = chat, None
                else:
                    stale = None
                if chat is not None:
                    chat.inflight += 1
                    chat.last_used = time.monotonic()
                opening = self._opening.get(chat_id) if chat is None else None
                waiting = opening is not None and not opening.planned.is_set()
                if chat is None and opening is None and op in _STARTING_OPS:
                    if self._closed:
                        return {"ok": False, "error": "not_started"}
                    if len(self._chats) + len(self._opening) >= self._max_open \
                            and not self._evict_locked():
                        if stale is not None:
                            stale.close()
                        return {"ok": False, "error": "chats_busy"}
                    opening = _Opening(self._factory(chat_id), inner)
                    self._opening[chat_id] = opening
                    owner = True
                else:
                    owner = False
                if opening is not None and not owner and not waiting \
                        and opening.refusal is None and not opening.follow_agent:
                    # Planned, still opening: the request waits in line.
                    if op in _STARTING_OPS:
                        opening.pending.append(inner)
            if stale is not None:
                stale.close()
            if chat is not None:
                try:
                    answer = acp_chat.dispatch(chat, inner)
                finally:
                    with self._lock:
                        chat.inflight -= 1
                answer.setdefault("chatId", chat_id)
                return answer
            if owner:
                return self._begin(chat_id, opening)
            if opening is None:
                return _not_open(op, chat_id)
            if waiting:
                opening.planned.wait(_PLAN_WAIT_SECONDS)
                continue  # its plan is in: the same answer as the first
            if opening.follow_agent:
                return acp_chat.dispatch(self.default, inner)
            if opening.refusal is not None:
                return dict(opening.refusal)
            if op in _STARTING_OPS:
                return _accepted(chat_id)
            return _not_open(op, chat_id, opening=True)
        return {"ok": False, "error": "chat_opening"}

    def _switch(self, chat_id: str, inner: dict) -> dict:
        """`switch` on an open chat runs in its session; on a chat without a
        child it starts one for the switch — MC's request names the
        session, no resume plan is asked."""
        with self._lock:
            if self._closed:
                return {"ok": False, "error": "not_started"}
            if chat_id in self._opening:
                return {"ok": False, "error": "busy", "detail": "chat_opening"}
            chat = self._chats.get(chat_id)
            if chat is not None and chat.load_refused:
                del self._chats[chat_id]
                stale, chat = chat, None
            else:
                stale = None
            fresh = chat is None
            if fresh:
                if len(self._chats) >= self._max_open and not self._evict_locked():
                    answer: Optional[dict] = {"ok": False, "error": "chats_busy"}
                else:
                    answer = None
                    chat = self._factory(chat_id)
                    self._chats[chat_id] = chat
            else:
                answer = None
            if chat is not None and answer is None:
                chat.inflight += 1
                chat.last_used = time.monotonic()
        if stale is not None:
            stale.close()
        if answer is not None:
            return answer
        try:
            answer = acp_chat.dispatch(chat, inner)
        finally:
            with self._lock:
                chat.inflight -= 1
        if fresh and not answer.get("ok"):
            with self._lock:
                if self._chats.get(chat_id) is chat:
                    del self._chats[chat_id]
            chat.close()
        answer.setdefault("chatId", chat_id)
        return answer

    def _begin(self, chat_id: str, opening: _Opening) -> dict:
        """MC's plan now (fast), the child in a thread."""
        chat = opening.chat
        try:
            chat.prepare()
        except FollowAgent:
            opening.follow_agent = True
        except ChatUnknown:
            opening.refusal = {"ok": False, "error": "chat_unknown"}
        except McUnreachable as exc:
            opening.refusal = {"ok": False, "error": "mc_unreachable", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 — a chat that cannot start says why
            logger.exception("chat %s: no plan", chat_id)
            opening.refusal = {"ok": False, "error": "chat_start_failed",
                               "detail": f"{type(exc).__name__}: {exc}"}
        if opening.follow_agent or opening.refusal is not None:
            with self._lock:
                self._opening.pop(chat_id, None)
                first = opening.pending[:1]
            opening.planned.set()
            opening.done.set()
            chat.close()
            if opening.follow_agent:
                return acp_chat.dispatch(self.default, first[0]) if first else {"ok": True}
            return dict(opening.refusal)
        opening.planned.set()
        threading.Thread(target=self._open, args=(chat_id, opening),
                         name=f"acp-chat-open-{chat_id[:8]}", daemon=True).start()
        return _accepted(chat_id)

    def _open(self, chat_id: str, opening: _Opening) -> None:
        chat = opening.chat
        error: Optional[BaseException] = None
        try:
            chat.start()
        except BaseException as exc:  # noqa: BLE001 — every failure is a card
            error = exc
        with self._lock:
            self._opening.pop(chat_id, None)
            pending, opening.pending = opening.pending, []
            closed = self._closed
            if error is None and not closed:
                self._chats[chat_id] = chat
                chat.inflight += 1
        try:
            if error is not None or closed:
                if error is not None and not isinstance(error, acp_chat.SessionLoadRefused):
                    logger.error("chat %s did not open: %s: %s", chat_id,
                                 type(error).__name__, error)
                    self._report_open_failed(chat, error, pending)
                chat.close()
                return
            for i, job in enumerate(pending):
                if job.get("op") == "prompt" and i > 0:
                    # Accepted while the chat opened: they wait their turn.
                    job = {**job, "mode": acp_chat.MODE_QUEUE}
                answer = acp_chat.dispatch(chat, job)
                if not answer.get("ok"):
                    logger.warning("chat %s: held %s refused: %s", chat_id,
                                   job.get("op"), answer.get("error"))
        finally:
            if error is None and not closed:
                with self._lock:
                    chat.inflight -= 1
            opening.done.set()

    def _report_open_failed(self, chat: PinnedChatSession, error: BaseException,
                            pending: list[dict]) -> None:
        """A card where the operator looks: in the chat's own copy when MC
        holds its session, else in the agent's main chat (a chat without a
        session has no copy the record could route)."""
        texts = [str(j.get("text") or "") for j in pending if j.get("op") == "prompt"]
        detail = f"{type(error).__name__}: {error}"
        data = {"retryText": texts[0]} if len(texts) == 1 and texts[0] else None
        target: acp_chat.ChatSession = self.default
        if chat._pinned:
            path = chat._copy_of(chat._pinned)
            with chat._lock:
                chat._mapper.set_session_id(chat._pinned)
                chat._sink = acp_chat_events.ChatEventSink.appending(path, chat._pinned)
            target = chat
        target._emit_error(CHAT_OPEN_FAILED, detail, text=_CHAT_OPEN_FAILED_TEXT, data=data)

    def _evict_locked(self) -> bool:
        idle = [c for c in self._chats.values()
                if not c.inflight and not c.state().get("busy")]
        if not idle:
            return False
        victim = min(idle, key=lambda c: c.last_used)
        logger.info("chat %s closed to make room (its session stays on disk)", victim.chat_id)
        del self._chats[victim.chat_id]
        victim.close()
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            chats, self._chats = list(self._chats.values()), {}
        for chat in chats:
            chat.close()


#: Ops that need the chat's session running; the rest only act on an open one.
_STARTING_OPS = ("prompt", "config")


def _accepted(chat_id: str) -> dict:
    """A message for a chat that is opening: it runs once the session is open."""
    return {"ok": True, "chatId": chat_id, "opening": True, "queued": True}


def _not_open(op: Any, chat_id: str, *, opening: bool = False) -> dict:
    """Not open: nothing runs, nothing is held, nothing to answer."""
    if op == "state":
        answer = {"ok": True, "chatId": chat_id, "open": False}
        if opening:
            answer["opening"] = True
        return answer
    if op == "queue_clear":
        return {"ok": True, "texts": [], "cleared": 0}
    if op == "cancel":
        return {"ok": True}
    if op == "new_session":
        return {"ok": False, "error": "chat_pinned"}
    if op == "stop_for_switch":
        # Nothing runs, nothing is held: nothing to stop or hand back.
        return {"ok": True, "chatId": chat_id, "handedOver": [], "stopped": False,
                "idle": not opening}
    return {"ok": False, "error": "chat_opening" if opening else "chat_not_open"}


def dispatch(pool: Optional[ChatPool], session: acp_chat.ChatSession, request: dict) -> dict:
    """Route one control request: no `chatId` = the default session (the op
    table of acp_chat.dispatch, unchanged); `chatId` = that chat."""
    if request.get("op") == "chats":
        return {"ok": True, "chats": pool.open_chats() if pool is not None else []}
    chat_id = request.get("chatId")
    if chat_id is None:
        return acp_chat.dispatch(session, request)
    if pool is None:
        return {"ok": False, "error": "chats_unsupported"}
    return pool.route(chat_id, request)


def build_pool(default: acp_chat.ChatSession, *, mc: Optional[McChats] = None,
               client_factory: Optional[Callable[[], Any]] = None,
               max_open: Optional[int] = None) -> ChatPool:
    """The production wiring: every further chat with the default session's
    cwd, model and child environment, its files under `.mc/chats/<id>/`."""
    mc = mc or McChats()
    sessions_dir = default._sessions_dir
    factory_of_client = client_factory or default._client_factory
    if max_open is None:
        try:
            max_open = int(os.environ.get("OMP_ACP_CHAT_MAX_OPEN") or DEFAULT_MAX_OPEN)
        except ValueError:
            max_open = DEFAULT_MAX_OPEN

    def factory(chat_id: str) -> PinnedChatSession:
        return PinnedChatSession(
            chat_id, mc,
            client_factory=factory_of_client,
            cwd=default._cwd,
            state_dir=sessions_dir / acp_chat_events.MC_SINK_DIRNAME / CHATS_DIRNAME / chat_id,
            sessions_dir=sessions_dir,
            driver=default._driver,
            permission_policy=default._permission_policy,
            prompt_timeout=default._prompt_timeout,
            model=default._model,
            model_resolver=default._model_resolver,
            elicitation=default._elicitation_enabled,
        )

    return ChatPool(default, factory, max_open=max_open)
