"""One Claude Code process per further chat (chats slice S3, item A).

A Claude agent (cli-bridge container) runs its own conversation in tmux
window 0, started by ``docker/mc-claude-agent/start-claude.sh`` — that is the
agent's default chat and nothing here touches it. A *further* chat of the
same agent (``chats.agent_default = false``, created by
``chat_record.create_chat`` with a pinned ``native_session_id``) gets:

* its own tmux window ``chat-<chat id>`` in a SEPARATE tmux session
  ``chats-<slug>`` — never in the agent's own session: the container
  watchdog (entrypoint.sh) knows windows 0/1/2 there by index only, poll.sh
  types tasks into :0 and the recycler respawns :0, so a chat window there
  could take a free index 0/1/2 (``new-window`` picks the lowest free one);
* its own ``CLAUDE_CONFIG_DIR`` = ``<claude-config>/chats/<chat id>/`` — so
  its session file lands in ``…/chats/<chat id>/projects/<cwd>/<native>.jsonl``
  and never next to the agent's own files (the default chat follows the
  newest file there; a further chat's file in the same folder would steal
  it);
* a claude command line computed here (:func:`build_argv`), from the chat's
  :func:`chat_record.resume_plan` and what is on disk.

Rules for the command line (plan s3-handoff §2.3, measured 19.11.2026 with
Claude Code 2.1.323 in ``-p`` mode and in a real terminal window):

* ``start``  → ``--session-id <id>`` and the system prompt as a FILE
  (``--append-system-prompt-file``). Linux allows at most 131 072 bytes in one
  argument; a handoff package plus the agent's card can be larger, so the
  text never travels as an argument.
* ``resume`` without a handoff → ``--resume <id>`` only: the session file
  holds a snapshot of the system prompt from the first start and claude uses
  it; a new ``--append-system-prompt`` would be ignored anyway.
* ``resume`` WITH a handoff → ``--resume <id> --system-prompt-snapshot off
  --append-system-prompt-file <file>``: only with the snapshot off does the
  new text reach the model. Measured as well: a later resume WITHOUT ``off``
  falls back to the first snapshot. So once a handoff was applied that way,
  every later resume of that session passes ``off`` + the file again (state
  file ``runner.json`` in the chat dir).
* never ``--session-id`` together with ``--resume`` (the CLI refuses it).

Hardening (item A2): a chat window without a turn for
:data:`IDLE_CLOSE_SECONDS` is closed by a sweep and resumed on the next
prompt; at most :data:`MAX_WARM_CHATS` chat processes warm and
:data:`MAX_WORKING_CHATS` mid-turn over all agents; :func:`http_answer` maps
every error code to 404 / 409 / 502; :func:`session_switch_command` names
the commands a further chat must not receive.

Everything here is behind ``settings.chat_record_enabled`` — the caller
(``agent_chat_input.send_text``) only comes here for a ``chat_id``, and only
with the flag on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.token_harvester import _host_home, encode_cwd

logger = logging.getLogger(__name__)

#: The agent's claude config dir inside its container (CLAUDE_CONFIG_DIR of
#: window 0; host side ``~/.mc/agents/<slug>/claude-config``).
CONTAINER_CLAUDE_HOME = "/home/agent/.claude"
#: Working directory of every claude in the container (window 0 too) — the
#: session file's project folder is derived from it.
CONTAINER_CWD = "/home/agent"
START_SCRIPT = "/home/agent/start-claude.sh"
CHATS_DIRNAME = "chats"
PROMPT_FILENAME = "system-prompt.md"
#: The package of the last handoff applied with the snapshot off: a later
#: resume recomposes the prompt file from the CURRENT card plus this.
HANDOFF_FILENAME = "handoff.md"
STATE_FILENAME = "runner.json"
WINDOW_PREFIX = "chat-"
#: The tmux session that holds every further chat's window of one agent.
#: A prefix, not a suffix: tmux resolves ``-t <slug>`` by name prefix too,
#: so ``<slug>-chats`` would answer the watchdog's ``has-session -t <slug>``
#: once the agent's own session is gone (and it would never restart it).
CHATS_SESSION_PREFIX = "chats-"
#: Upper bound for the system-prompt file in BYTES (umlauts are two). The
#: handoff budget is at most 32 000 tokens ≈ 128 000 chars (plan §2.0); with
#: two-byte characters and the agent's card that stays far below this.
MAX_PROMPT_BYTES = 512 * 1024
#: The same flag start-claude.sh passes to window 0.
SKIP_PERMISSIONS = "--dangerously-skip-permissions"


class ClaudeChatRunnerError(Exception):
    """A further chat could not be put in front of a claude process.
    ``code`` is stable (router / switch card), ``detail`` is for the log."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ── hardening (S3 item A2): limits, refused commands, HTTP answers ─────────

#: A further chat's window closes after this long without a turn; the next
#: prompt opens it again with ``--resume`` (the session file is on disk).
IDLE_CLOSE_SECONDS = 15 * 60
#: Upper bounds over ALL agents' further-chat processes (window 0 of each
#: agent is not counted — it is the agent itself). From the process
#: measurement of 19.11.2026: a quiet claude about 0.13–0.37 GB, a working
#: one up to about 1 GB; about 6.2 GiB free in the Docker VM, 2 GiB of it
#: kept back for backend peaks and builds → "provisionally 4 warm, at most 2
#: working at the same time". Constants, not settings: changing them is a
#: measured decision, not a knob.
MAX_WARM_CHATS = 4
MAX_WORKING_CHATS = 2
#: A chat touched this recently is never closed to make room: a send that
#: just got its window's target must still find the window when it types.
EVICT_GRACE_SECONDS = 60
#: How often the idle sweep looks.
SWEEP_INTERVAL_SECONDS = 60
#: A turn MC just sent counts as working this long before claude's own
#: ``submit`` hook has written anything (window start + resume).
TURN_START_GRACE_SECONDS = 30
#: The chat's own turn signal (start-claude.sh --chat rewrites the agent's
#: hooks to ``<chat dir>/.turn-signal``): ``<epoch> submit`` / ``<epoch> stop``.
TURN_SIGNAL_FILENAME = ".turn-signal"

#: Slash commands that would move a further chat's claude OFF its pinned
#: session, with every alias the CLI accepts (live: the command table in the
#: Claude Code 2.1.328 binary — guarded by a test against the installed
#: binary): ``/clear`` (``/reset``, ``/new``) starts a new one under a random
#: id; ``/resume`` (``/continue``) switches to some other one; ``/branch``
#: creates a branch of the conversation; ``/teleport`` (``/tp``) sends it to
#: or resumes one from claude.ai; ``/background`` (``/bg``) hands it to a
#: background session and frees the window; ``/login`` / ``/logout`` swap
#: the shared login for every chat and the agent. The chat's pin would keep
#: naming the old file: the next restart would resume it, and everything
#: after the switch would be silently gone. Matched on the first word —
#: ``/resume <id>`` takes arguments, and a refused harmless message is
#: cheaper than a lost one. ``/exit`` is fine: the next prompt resumes.
SESSION_SWITCH_ALIASES: dict[str, tuple[str, ...]] = {
    "clear": ("reset", "new"),
    "resume": ("continue",),
    "branch": (),
    "teleport": ("tp",),
    "background": ("bg",),
    "login": (),
    "logout": (),
}
SESSION_SWITCH_COMMANDS = frozenset(
    {name for name in SESSION_SWITCH_ALIASES}
    | {alias for aliases in SESSION_SWITCH_ALIASES.values() for alias in aliases}
)


def session_switch_command(text: str | None) -> str | None:
    """``"/resume"`` etc. when ``text`` starts with a session-switching
    command (case and surrounding space aside), else None."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    word = stripped.split(None, 1)[0][1:].lower()
    return f"/{word}" if word in SESSION_SWITCH_COMMANDS else None


#: Codes the caller answers with 404: the chat is not one this agent has.
NOT_FOUND_CODES = frozenset({"chat_not_found"})
#: Codes that are a refusal by content (409): asking again unchanged does not help.
REFUSED_CODES = frozenset({
    "not_a_container_agent", "not_a_claude_chat", "default_chat",
    "bad_session_id", "bad_action", "snapshot_off_without_prompt",
    "prompt_too_large",
})
#: A temporary state (503 + ``Retry-After``): the same message a little
#: later goes through — every chat slot is taken right now.
TEMPORARY_CODES = frozenset({"chats_busy"})
#: What ``Retry-After`` says for ``chats_busy``: half the sweep interval —
#: a turn ending or the sweep closing a chat frees a slot.
CHATS_BUSY_RETRY_SECONDS = 30
#: Codes where nobody answered (502): container down, docker/tmux failed,
#: the prompt file could not be written. Same split as the ACP path
#: ("the agent refuses" vs "nobody is there").
UNREACHABLE_CODES = frozenset({
    "agent_unreachable", "docker_timeout", "process_start_failed",
    "process_stop_failed", "prompt_write_failed",
})


def http_answer(err: ClaudeChatRunnerError) -> tuple[int, dict[str, Any], dict[str, str]]:
    """``(status, body, headers)`` for a router. ``detail`` never goes out
    (it can hold paths and tmux text) — the router logs it. An unknown code
    is a 502: never a 204 for something that did not arrive."""
    if err.code in NOT_FOUND_CODES:
        return 404, {"reason": "chat_not_found"}, {}
    if err.code in REFUSED_CODES:
        return 409, {"reason": "chat_process_refused", "error": err.code}, {}
    if err.code in TEMPORARY_CODES:
        return 503, {"reason": err.code, "error": err.code,
                     "retryAfter": CHATS_BUSY_RETRY_SECONDS}, \
            {"Retry-After": str(CHATS_BUSY_RETRY_SECONDS)}
    return 502, {"reason": "chat_process_unreachable", "error": err.code}, {}


# ── pure parts ─────────────────────────────────────────────────────────────


def _valid_uuid(value: str | None) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ClaudeChatRunnerError("bad_session_id", repr(value)) from None


def build_argv(
    *,
    action: str,
    session_id: str,
    prompt_file: str | None,
    snapshot_off: bool = False,
) -> list[str]:
    """The claude arguments for one chat process (rule F13, see the module
    docstring). ``prompt_file`` is the container path of the system-prompt
    file; ``snapshot_off`` only on ``resume`` and only with a file."""
    sid = _valid_uuid(session_id)
    if action == "start":
        argv = [SKIP_PERMISSIONS, "--session-id", sid]
        if prompt_file:
            argv += ["--append-system-prompt-file", prompt_file]
        return argv
    if action == "resume":
        argv = [SKIP_PERMISSIONS, "--resume", sid]
        if snapshot_off:
            if not prompt_file:
                raise ClaudeChatRunnerError("snapshot_off_without_prompt")
            argv += ["--system-prompt-snapshot", "off", "--append-system-prompt-file", prompt_file]
        return argv
    raise ClaudeChatRunnerError("bad_action", action)


def compose_system_prompt(card_text: str | None, handoff: str | None) -> str:
    """The agent's card (CARD.md, else SOUL.md — the same choice
    start-claude.sh makes for window 0), then the handoff package."""
    parts = [p.strip("\n") for p in (card_text, handoff) if p and p.strip()]
    text = "\n\n".join(parts)
    size = len(text.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise ClaudeChatRunnerError("prompt_too_large", f"{size} bytes > {MAX_PROMPT_BYTES}")
    return text


def window_name(chat_id: str) -> str:
    return f"{WINDOW_PREFIX}{_valid_uuid(chat_id)}"


def container_chat_dir(chat_id: str) -> str:
    return f"{CONTAINER_CLAUDE_HOME}/{CHATS_DIRNAME}/{_valid_uuid(chat_id)}"


def host_claude_home(slug: str) -> Path:
    """Host side of the agent's claude config — the same folder
    ``transcript_chat.resolve_transcript_dir`` reads for window 0."""
    return _host_home() / ".mc" / "agents" / slug / "claude-config"


def host_chat_dir(slug: str, chat_id: str) -> Path:
    return host_claude_home(slug) / CHATS_DIRNAME / _valid_uuid(chat_id)


def transcript_path(slug: str, chat_id: str, native_session_id: str) -> Path:
    """Where this chat's own session file lives (host path) — the file the
    chat's tailer follows: ``chat.native_session_id``, no newest-file guess."""
    return (
        host_chat_dir(slug, chat_id)
        / "projects"
        / encode_cwd(CONTAINER_CWD)
        / f"{_valid_uuid(native_session_id)}.jsonl"
    )


def pinned_transcript(slug: str, chat_id: str, native_session_id: str) -> Path | None:
    """The chat's session file if it is where the pin says, else None. A
    claude started without ``--session-id`` writes under a random id — this
    check is what catches that (and nothing else would: the file would look
    like a normal session)."""
    path = transcript_path(slug, chat_id, native_session_id)
    return path if path.is_file() else None


def _card_text(slug: str) -> str | None:
    home = host_claude_home(slug)
    for name in ("CARD.md", "SOUL.md"):
        path = home / name
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def _read_state(chat_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((chat_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Launch:
    """What :func:`plan_launch` decided for one chat process."""

    action: str
    argv: list[str]
    prompt_file: str | None
    snapshot_off: bool


def plan_launch(
    slug: str,
    chat_id: str,
    native_session_id: str,
    *,
    planned_action: str | None = None,
    handoff: str | None = None,
) -> Launch:
    """Decides start vs resume and writes the chat's system-prompt file.

    The session file on disk decides, not only the record: ``resume_plan``
    says ``start`` until the importer saw the file, and ``--session-id`` on
    an id whose file exists is a CLI error; a file that is gone cannot be
    resumed. ``planned_action`` (from ``resume_plan``) only goes into the log
    when it disagrees."""
    sid = _valid_uuid(native_session_id)
    chat_dir = host_chat_dir(slug, chat_id)
    action = "resume" if pinned_transcript(slug, chat_id, sid) else "start"
    if planned_action and planned_action != action:
        logger.info("claude chat runner: plan said %s, disk says %s (slug=%s chat=%s)",
                    planned_action, action, slug, chat_id)

    state = _read_state(chat_dir)
    sticky_off = state.get("snapshotOffSession") == sid
    snapshot_off = action == "resume" and (bool(handoff) or sticky_off)

    prompt_file: str | None = None
    prompt_host = chat_dir / PROMPT_FILENAME
    handoff_host = chat_dir / HANDOFF_FILENAME
    try:
        if action == "start" or snapshot_off:
            package: str | None = handoff
            keep_file = False
            if not handoff and sticky_off:
                # A later resume of a session that runs with the snapshot
                # off: the CURRENT card (the agent's sync may have changed
                # it) plus the package of its handoff. A chat dir from before
                # handoff.md existed keeps its prompt file as it is.
                if handoff_host.is_file():
                    package = handoff_host.read_text(encoding="utf-8")
                else:
                    keep_file = prompt_host.is_file()
            if not keep_file:
                text = compose_system_prompt(_card_text(slug), package)
                chat_dir.mkdir(parents=True, exist_ok=True)
                if text:
                    prompt_host.write_text(text, encoding="utf-8")
                else:
                    prompt_host.unlink(missing_ok=True)
            if handoff and snapshot_off:
                handoff_host.write_text(handoff, encoding="utf-8")
            if prompt_host.is_file():
                prompt_file = f"{container_chat_dir(chat_id)}/{PROMPT_FILENAME}"
        if snapshot_off and prompt_file is None:
            raise ClaudeChatRunnerError("snapshot_off_without_prompt", slug)
        if snapshot_off and not sticky_off:
            chat_dir.mkdir(parents=True, exist_ok=True)
            (chat_dir / STATE_FILENAME).write_text(
                json.dumps({"snapshotOffSession": sid}), encoding="utf-8"
            )
    except OSError as e:
        # The chat dir is not writable (owner, disk): a stable code, so the
        # caller can tell it apart — and nothing has been closed yet.
        raise ClaudeChatRunnerError("prompt_write_failed", str(e)[:200]) from e

    argv = build_argv(action=action, session_id=sid, prompt_file=prompt_file,
                      snapshot_off=snapshot_off)
    return Launch(action=action, argv=argv, prompt_file=prompt_file, snapshot_off=snapshot_off)


def chats_session(slug: str) -> str:
    """The agent's tmux session for further chats (never ``<slug>`` itself)."""
    return f"{CHATS_SESSION_PREFIX}{slug}"


def _chat_command(chat_id: str, launch: Launch) -> list[str]:
    return [START_SCRIPT, "--chat", _valid_uuid(chat_id), *launch.argv]


def new_window_argv(slug: str, chat_id: str, launch: Launch) -> list[str]:
    """``docker exec`` that opens the chat's window in the chats session.
    ``-S``: a window of that name that exists already is selected, never
    opened a second time. tmux runs the command directly (several arguments,
    no shell), so nothing is quoted."""
    return [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", f"mc-agent-{slug}",
        "tmux", "new-window", "-d", "-S", "-t", f"{chats_session(slug)}:",
        "-n", window_name(chat_id), "-c", CONTAINER_CWD,
        *_chat_command(chat_id, launch),
    ]


def new_session_argv(slug: str, chat_id: str, launch: Launch) -> list[str]:
    """``docker exec`` that makes the chats session with this chat as its
    first window (the session ends with its last window)."""
    return [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", f"mc-agent-{slug}",
        "tmux", "new-session", "-d", "-s", chats_session(slug),
        "-n", window_name(chat_id), "-c", CONTAINER_CWD,
        *_chat_command(chat_id, launch),
    ]


def chat_target(slug: str, chat_id: str) -> str:
    """tmux target of the chat's window (``send-keys -t`` / ``capture-pane -t``)."""
    return f"{chats_session(slug)}:{window_name(chat_id)}"


# ── container side ─────────────────────────────────────────────────────────


async def _docker(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeChatRunnerError("docker_timeout", " ".join(argv[:6])) from e


#: One lock per agent around "is the window there? → open it": two sends to a
#: chat that is not open yet would otherwise both open a window of the same
#: name (two claudes on one session id; ``send-keys -t <name>`` then fails as
#: ambiguous), and two chats would race to make the chats session. In-process
#: is enough: the backend runs one worker (see ``agent_chat_input._LAST_SENT``).
#: Kept per event loop (a lock belongs to the loop it was first used in).
_OPEN_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _open_lock(slug: str) -> asyncio.Lock:
    locks = _OPEN_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.get(slug)
    if lock is None:
        lock = locks[slug] = asyncio.Lock()
    return lock


#: One lock over ALL agents around the working bound's check-and-note.
_WORKING_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _working_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _WORKING_LOCKS.get(loop)
    if lock is None:
        lock = _WORKING_LOCKS[loop] = asyncio.Lock()
    return lock


async def _chat_windows(slug: str) -> tuple[bool, set[str]]:
    """``(chats session exists, its window names)`` — one ``list-windows -a``
    (a missing chats session is no error; a dead tmux server is)."""
    result = await _docker([
        "docker", "exec", "-u", "agent", f"mc-agent-{slug}",
        # ":" as the separator: tmux never allows it in a session name (a tab
        # would not do — tmux 3.3a, the image's, prints it as "_").
        "tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_name}",
    ])
    if result.returncode != 0:
        raise ClaudeChatRunnerError("agent_unreachable", (result.stderr or "").strip()[:200])
    prefix = f"{chats_session(slug)}:"
    names = {
        line[len(prefix):]
        for line in (result.stdout or "").splitlines()
        if line.startswith(prefix)
    }
    return bool(names), names


async def window_exists(slug: str, chat_id: str) -> bool:
    _session, names = await _chat_windows(slug)
    return window_name(chat_id) in names


async def capture_chat_pane(slug: str, chat_id: str) -> str | None:
    """The chat window's pane text, or None (window gone, container down)."""
    try:
        result = await _docker([
            "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", f"mc-agent-{slug}",
            "tmux", "capture-pane", "-p", "-t", chat_target(slug, chat_id),
        ])
    except ClaudeChatRunnerError:
        return None
    return result.stdout if result.returncode == 0 else None


async def _open_window(slug: str, chat_id: str, launch: Launch, session_up: bool) -> None:
    """New window in the chats session, or the session itself when there is
    none. Each way falls back to the other once: the session can end (its
    last window closed) or appear (opened outside this process) in between."""
    order = [new_window_argv, new_session_argv] if session_up else [new_session_argv, new_window_argv]
    errors: list[str] = []
    for make in order:
        result = await _docker(make(slug, chat_id, launch))
        if result.returncode == 0:
            return
        errors.append((result.stderr or "").strip()[:200])
    raise ClaudeChatRunnerError("process_start_failed", " | ".join(errors))


async def ensure_window(
    agent: Any,
    chat_id: str,
    native_session_id: str,
    *,
    planned_action: str | None = None,
    handoff: str | None = None,
) -> str:
    """The chat's window runs claude on the chat's own session; returns its
    tmux target. An existing window is used as it is — except with a
    ``handoff`` (a switch), which needs a new process: the new launch is
    planned first (so a refused package leaves the running process alone),
    then the old window is closed. A window that is gone (container
    restart, ``/exit``) is opened again and resumes the session from its
    file."""
    slug = getattr(agent, "slug", None)
    if not slug or getattr(agent, "agent_runtime", None) != "cli-bridge":
        raise ClaudeChatRunnerError("not_a_container_agent", str(slug))
    target = chat_target(slug, chat_id)
    async with _open_lock(slug):
        session_up, names = await _chat_windows(slug)
        exists = window_name(chat_id) in names
        if exists and not handoff:
            return target
        launch = plan_launch(slug, chat_id, native_session_id,
                             planned_action=planned_action, handoff=handoff)
        if exists:
            result = await _docker([
                "docker", "exec", "-u", "agent", f"mc-agent-{slug}",
                "tmux", "kill-window", "-t", target,
            ])
            if result.returncode != 0:
                # ``new-window -S`` would then select the OLD process and the
                # switch would silently not happen.
                raise ClaudeChatRunnerError("process_stop_failed",
                                            (result.stderr or "").strip()[:200])
            # The last window closed ends the chats session.
            session_up = len(names) > 1
        else:
            # A process more: keep within MAX_WARM_CHATS (closes the least
            # recently used quiet chat, or refuses ``chats_busy``).
            names = await _make_room(slug, chat_id, names)
            session_up = bool(names)
        # A new process: whatever turn the folder still names (a ``submit``
        # from before a container restart or a handoff kill) is over.
        mark_closed(slug, chat_id)
        await _open_window(slug, chat_id, launch, session_up)
    logger.info("claude chat runner: opened %s (%s, snapshot_off=%s)",
                target, launch.action, launch.snapshot_off)
    return target


async def load_chat_plan(agent: Any, chat_id: str) -> dict[str, Any]:
    """The chat's ``resume_plan`` — after checking the chat is a further
    Claude chat of THIS agent. Read only."""
    from app.database import async_session_maker
    from app.models.chat_record import Chat
    from app.services import chat_record

    try:
        cid = uuid.UUID(str(chat_id))
    except ValueError:
        raise ClaudeChatRunnerError("chat_not_found", str(chat_id)) from None
    async with async_session_maker() as session:
        chat = await session.get(Chat, cid)
        if (
            chat is None
            or chat.legacy_agent_id != getattr(agent, "id", None)
            or chat.archived_at is not None
        ):
            raise ClaudeChatRunnerError("chat_not_found", str(chat_id))
        if (chat.harness or "claude") != "claude":
            raise ClaudeChatRunnerError("not_a_claude_chat", chat.harness or "")
        plan = await chat_record.resume_plan(session, chat)
    if plan.get("agentDefault") or not plan.get("nativeSessionId"):
        # The default chat is window 0 (start-claude.sh), never a chat window.
        raise ClaudeChatRunnerError("default_chat")
    return plan


async def open_chat(agent: Any, chat_id: str, *, handoff: str | None = None) -> str:
    """Plan + window in one: what ``send_text`` calls for a ``chat_id``.
    Every call is a turn: it counts against :data:`MAX_WORKING_CHATS`
    (refused ``chats_busy`` when that many OTHER chats are mid-turn) and
    resets the chat's idle clock."""
    plan = await load_chat_plan(agent, chat_id)
    slug = getattr(agent, "slug", None)
    if slug:
        # Check and note under ONE lock over all agents: two sends that
        # arrive together must not both see "one slot free".
        async with _working_lock():
            own = (slug, _valid_uuid(chat_id))
            busy = await working_chats()
            if own not in busy:
                others = [c for c in busy if c != own]
                if len(others) >= MAX_WORKING_CHATS:
                    raise ClaudeChatRunnerError("chats_busy", f"{len(others)} chats working")
            note_turn(slug, chat_id)
    target = await ensure_window(
        agent, chat_id, plan["nativeSessionId"],
        planned_action=plan.get("action"), handoff=handoff,
    )
    ensure_idle_sweeper()
    return target


# ── idle close and the warm / working bounds ──────────────────────────────

#: Last turn MC sent per ``(slug, chat id)`` (wall clock). Only a floor for
#: the activity below: the files on disk are the truth, and they survive a
#: backend restart; this covers the seconds between a send and claude's
#: first write.
_LAST_TURN: dict[tuple[str, str], float] = {}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def note_turn(slug: str, chat_id: str, now: float | None = None) -> None:
    _LAST_TURN[(slug, _valid_uuid(chat_id))] = time.time() if now is None else now


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def last_activity(slug: str, chat_id: str) -> float:
    """Wall-clock time of the chat's last sign of life: the turn MC sent,
    the chat's own turn signal, or any transcript under its ``projects``
    growing — the session file AND the nested ones (a subagent's turn
    writes ``<session>/subagents/agent-*.jsonl``, not the main file). A
    single long tool call writes nothing; that is what the pane check in
    :func:`_pane_mid_turn` is for."""
    chat_dir = host_chat_dir(slug, chat_id)
    times = [_LAST_TURN.get((slug, _valid_uuid(chat_id)), 0.0),
             _mtime(chat_dir / TURN_SIGNAL_FILENAME)]
    times += [_mtime(p) for p in chat_dir.glob("projects/**/*.jsonl")]
    return max(times)


def _last_signal(slug: str, chat_id: str) -> str | None:
    path = host_chat_dir(slug, chat_id) / TURN_SIGNAL_FILENAME
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [x.split() for x in tail.splitlines() if x.strip()]
    return lines[-1][-1] if lines and lines[-1] else None


def chat_is_working(slug: str, chat_id: str, now: float | None = None) -> bool:
    """Mid-turn: the chat's last turn signal is ``submit`` (no ``stop`` yet)
    — and it showed life within :data:`IDLE_CLOSE_SECONDS`. An interrupted
    turn (Esc) writes no ``stop``; without the second condition it would
    count as working for ever."""
    if _last_signal(slug, chat_id) != "submit":
        return False
    now = time.time() if now is None else now
    return now - last_activity(slug, chat_id) < IDLE_CLOSE_SECONDS


def _chat_dirs() -> list[tuple[str, str]]:
    """Every further chat that has a folder on the host, as ``(slug, chat id)``."""
    found: list[tuple[str, str]] = []
    for chats in (_host_home() / ".mc" / "agents").glob(f"*/claude-config/{CHATS_DIRNAME}"):
        slug = chats.parent.parent.name
        if not _SLUG_RE.match(slug):
            continue
        for entry in chats.iterdir() if chats.is_dir() else ():
            try:
                found.append((slug, _valid_uuid(entry.name)))
            except ClaudeChatRunnerError:
                continue
    return found


def known_slugs() -> set[str]:
    """Agents that may have chat windows: what this process opened, plus
    every agent with a chats folder on disk (covers a backend restart)."""
    return {s for s, _ in _LAST_TURN} | {s for s, _ in _chat_dirs()}


async def _running_slugs(slugs: set[str]) -> set[str]:
    """The ones among ``slugs`` whose container runs — one ``docker ps``
    instead of one ``docker exec`` per agent that ever had a chat (archived
    chats keep their folder; a stopped container makes an exec wait). When
    docker cannot be asked, all of them (the old way: each exec then
    answers for itself)."""
    if not slugs:
        return set()
    try:
        result = await _docker(["docker", "ps", "--format", "{{.Names}}"])
    except ClaudeChatRunnerError:
        return set(slugs)
    if result.returncode != 0:
        return set(slugs)
    running = {line.strip() for line in (result.stdout or "").splitlines()}
    return {s for s in slugs if f"mc-agent-{s}" in running}


async def _open_chat_windows(slugs: set[str]) -> set[tuple[str, str]]:
    """``(slug, chat id)`` of every chat window open on these agents."""
    found: set[tuple[str, str]] = set()
    for slug in sorted(await _running_slugs(slugs)):
        try:
            _up, names = await _chat_windows(slug)
        except ClaudeChatRunnerError:
            continue  # container down: its windows are gone with it
        found |= {(slug, c) for c in _chat_ids(names)}
    return found


def _just_sent(key: tuple[str, str], now: float) -> bool:
    """MC sent this chat a turn within :data:`TURN_START_GRACE_SECONDS` and
    claude has not finished it: it counts as working before its own
    ``submit`` hook has fired (a chat that was just opened has written
    nothing yet)."""
    sent = _LAST_TURN.get(key)
    if sent is None or now - sent >= TURN_START_GRACE_SECONDS:
        return False
    sig = host_chat_dir(*key) / TURN_SIGNAL_FILENAME
    return not (_last_signal(*key) == "stop" and _mtime(sig) >= sent)


async def working_chats(now: float | None = None) -> list[tuple[str, str]]:
    """Chats mid-turn over all agents: a turn MC just sent, or a ``submit``
    without ``stop`` in a chat whose window is OPEN. A folder whose process
    is gone (container restart, handoff kill, ``/exit``, an Esc interrupt —
    the Stop hook does not fire for it) is no turn."""
    now = time.time() if now is None else now
    fresh = {k for k in _LAST_TURN if _just_sent(k, now)}
    signalled = {k for k in set(_chat_dirs()) | set(_LAST_TURN)
                 if k not in fresh and chat_is_working(k[0], k[1], now)}
    if signalled:
        signalled &= await _open_chat_windows({s for s, _ in signalled})
    return sorted(fresh | signalled)


def mark_closed(slug: str, chat_id: str) -> None:
    """Appends ``<epoch> closed`` to the chat's turn signal: its process is
    gone (or about to be replaced), so a ``submit`` in there is over. A
    folder that is not there yet needs nothing; a write that fails is only
    logged — the window check in :func:`working_chats` still holds."""
    sig = host_chat_dir(slug, chat_id) / TURN_SIGNAL_FILENAME
    if not sig.parent.is_dir():
        return
    try:
        with sig.open("a", encoding="utf-8") as f:
            f.write(f"{int(time.time())} closed\n")
    except OSError as e:
        logger.warning("claude chat runner: could not mark %s closed: %s",
                       chat_target(slug, chat_id), e)


async def _pane_mid_turn(slug: str, chat_id: str) -> bool | None:
    """What the chat's pane says: True mid-turn (spinner, or a question /
    permission prompt waiting for the operator), False at the idle prompt,
    None when it cannot be read or recognised (then the time rule
    decides)."""
    from app.services.pane_state import parse_pane_state

    pane = await capture_chat_pane(slug, chat_id)
    if pane is None:
        return None
    status = parse_pane_state(pane, False)["status"]
    if status in ("working", "permission_prompt"):
        return True
    if status == "idle":
        return False
    return None


def _chat_ids(names: set[str]) -> list[str]:
    out = []
    for name in names:
        if name.startswith(WINDOW_PREFIX):
            try:
                out.append(_valid_uuid(name[len(WINDOW_PREFIX):]))
            except ClaudeChatRunnerError:
                continue
    return out


async def _kill_chat_window(slug: str, chat_id: str) -> bool:
    result = await _docker([
        "docker", "exec", "-u", "agent", f"mc-agent-{slug}",
        "tmux", "kill-window", "-t", chat_target(slug, chat_id),
    ])
    _LAST_TURN.pop((slug, _valid_uuid(chat_id)), None)
    if result.returncode != 0:
        return False
    mark_closed(slug, chat_id)
    return True


async def _make_room(slug: str, chat_id: str, names: set[str]) -> set[str]:
    """Before a new chat process: at most :data:`MAX_WARM_CHATS` open over
    all agents. Closes the least recently active chat that is neither
    working nor touched within :data:`EVICT_GRACE_SECONDS` (its session file
    stays; the next prompt resumes it). Refuses ``chats_busy`` when none
    qualifies. Returns this agent's window names after closing. Runs under
    THIS agent's open lock; another agent's window is closed without its
    lock — the grace time keeps a send in flight there safe."""
    own = (slug, _valid_uuid(chat_id))
    open_chats: list[tuple[str, str]] = [(slug, c) for c in _chat_ids(names)]
    open_chats += sorted(await _open_chat_windows(known_slugs() - {slug}))
    open_chats = [c for c in open_chats if c != own]
    names = set(names)
    now = time.time()
    spared: set[tuple[str, str]] = set()
    while len(open_chats) >= MAX_WARM_CHATS:
        quiet = sorted((c for c in open_chats
                        if c not in spared
                        and not chat_is_working(*c, now=now)
                        and now - last_activity(*c) >= EVICT_GRACE_SECONDS),
                       key=lambda c: last_activity(*c))
        victim = None
        for candidate in quiet:
            # Quiet on disk is not enough: one long tool call writes nothing
            # for minutes. Mid-turn by its pane → never the one closed.
            if _last_signal(*candidate) == "submit" and await _pane_mid_turn(*candidate):
                spared.add(candidate)
                continue
            victim = candidate
            break
        if victim is None:
            raise ClaudeChatRunnerError("chats_busy", f"{len(open_chats)} chats warm")
        if not await _kill_chat_window(*victim):
            raise ClaudeChatRunnerError("process_stop_failed", f"{victim[0]}:{victim[1]}")
        logger.info("claude chat runner: closed %s to stay within %d warm chats",
                    chat_target(*victim), MAX_WARM_CHATS)
        open_chats.remove(victim)
        if victim[0] == slug:
            names.discard(window_name(victim[1]))
    return names


async def close_idle_chats(now: float | None = None) -> list[str]:
    """Closes every further-chat window without a turn for
    :data:`IDLE_CLOSE_SECONDS`; returns the closed targets. Nothing while
    ``chat_record_enabled`` is off. Each close re-checks under the agent's
    open lock, so a send that came in meanwhile keeps its window."""
    if not getattr(settings, "chat_record_enabled", False):
        return []
    closed: list[str] = []
    by_slug: dict[str, list[str]] = {}
    for slug, cid in sorted(await _open_chat_windows(known_slugs())):
        by_slug.setdefault(slug, []).append(cid)
    for slug, cids in by_slug.items():
        for cid in cids:
            when = time.time() if now is None else now
            if when - last_activity(slug, cid) < IDLE_CLOSE_SECONDS:
                continue
            if _last_signal(slug, cid) == "submit" and await _pane_mid_turn(slug, cid):
                # ``submit`` without ``stop`` and a pane that still works
                # (one long tool call, a question waiting): never killed —
                # the turn and its answer would be lost without a word.
                continue
            async with _open_lock(slug):
                when = time.time() if now is None else now
                if when - last_activity(slug, cid) < IDLE_CLOSE_SECONDS:
                    continue
                if await _kill_chat_window(slug, cid):
                    closed.append(chat_target(slug, cid))
                    logger.info("claude chat runner: closed idle %s", chat_target(slug, cid))
    return closed


_SWEEPERS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Task]" = (
    weakref.WeakKeyDictionary()
)


async def _sweep_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await close_idle_chats()
        except Exception:  # noqa: BLE001 — the sweep must keep running
            logger.warning("claude chat runner: idle sweep failed", exc_info=True)


def ensure_idle_sweeper() -> asyncio.Task | None:
    """Starts the idle sweep on this event loop once (flag on only). Called
    from the backend's lifespan (``app.main``) and again on every chat send
    (restarts it if it died)."""
    if not getattr(settings, "chat_record_enabled", False):
        return None
    loop = asyncio.get_running_loop()
    task = _SWEEPERS.get(loop)
    if task is None or task.done():
        task = _SWEEPERS[loop] = loop.create_task(_sweep_forever())
    return task
