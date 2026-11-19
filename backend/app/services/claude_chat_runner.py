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

Everything here is behind ``settings.chat_record_enabled`` — the caller
(``agent_chat_input.send_text``) only comes here for a ``chat_id``, and only
with the flag on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    """Plan + window in one: what ``send_text`` calls for a ``chat_id``."""
    plan = await load_chat_plan(agent, chat_id)
    return await ensure_window(
        agent, chat_id, plan["nativeSessionId"],
        planned_action=plan.get("action"), handoff=handoff,
    )
