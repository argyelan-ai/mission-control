"""Per-chat Claude Code process (chats slice S3, item A).

``services/claude_chat_runner.py`` computes the claude command line of a
further chat and opens it in its own tmux window with its own config dir;
``docker/mc-claude-agent/start-claude.sh --chat`` sets the environment and
execs claude. The end-to-end tests below run the REAL start-claude.sh with a
fake ``claude`` on PATH that writes session files the way Claude Code does
(measured 19.11.2026, plan s3-handoff §2.3/§8):

* ``--session-id S`` on a file that exists → error; ``--resume S`` on a
  missing file → error; both together → error; neither → random id.
* a start with a system-prompt file records a ``prompt_snapshot`` attachment
  and an ``api-request-shape`` line; a resume with ``--system-prompt-snapshot
  off`` records only a new ``api-request-shape`` with the new text; a plain
  resume records neither.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services import claude_chat_runner as runner
from app.services.claude_chat_runner import ClaudeChatRunnerError, build_argv
from tests.conftest import test_engine

REPO = Path(__file__).resolve().parents[2]
START_SCRIPT = REPO / "docker" / "mc-claude-agent" / "start-claude.sh"
SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
FILE = "/home/agent/.claude/chats/x/system-prompt.md"


# ══════════════════════════════════════════════════════════════════════════
# (1) F13 — the command line
# ══════════════════════════════════════════════════════════════════════════


def test_argv_rules():
    """Rule F13: start pins the id and passes the prompt as a FILE; resume
    without a handoff passes only the id; resume with a handoff turns the
    snapshot off and passes the file; never --session-id with --resume."""
    start = build_argv(action="start", session_id=SID, prompt_file=FILE)
    assert start == ["--dangerously-skip-permissions", "--session-id", SID,
                     "--append-system-prompt-file", FILE]

    plain = build_argv(action="resume", session_id=SID, prompt_file=FILE)
    assert plain == ["--dangerously-skip-permissions", "--resume", SID]
    assert "--system-prompt-snapshot" not in plain

    handoff = build_argv(action="resume", session_id=SID, prompt_file=FILE, snapshot_off=True)
    assert handoff == ["--dangerously-skip-permissions", "--resume", SID,
                       "--system-prompt-snapshot", "off", "--append-system-prompt-file", FILE]

    for argv in (start, plain, handoff):
        assert not ("--session-id" in argv and "--resume" in argv)
        # Never the text itself as an argument (Linux: 131 072 bytes per argument).
        assert "--append-system-prompt" not in argv

    with pytest.raises(ClaudeChatRunnerError) as e:
        build_argv(action="resume", session_id=SID, prompt_file=None, snapshot_off=True)
    assert e.value.code == "snapshot_off_without_prompt"
    with pytest.raises(ClaudeChatRunnerError):
        build_argv(action="start", session_id="not-a-uuid; rm -rf /", prompt_file=None)
    with pytest.raises(ClaudeChatRunnerError):
        build_argv(action="follow_agent", session_id=SID, prompt_file=None)


def test_prompt_cap_counts_bytes_not_chars():
    """Umlauts are two bytes: the cap is in bytes."""
    chars = runner.MAX_PROMPT_BYTES // 2 + 10
    assert len("ä" * chars) < runner.MAX_PROMPT_BYTES  # fits by characters …
    with pytest.raises(ClaudeChatRunnerError) as e:
        runner.compose_system_prompt("card", "ä" * chars)  # … not by bytes
    assert e.value.code == "prompt_too_large"
    assert runner.compose_system_prompt("CARD", "PACKAGE") == "CARD\n\nPACKAGE"
    assert runner.compose_system_prompt(None, None) == ""


# ══════════════════════════════════════════════════════════════════════════
# end-to-end: real start-claude.sh + fake claude
# ══════════════════════════════════════════════════════════════════════════

_FAKE_CLAUDE = r'''#!__PY__
import json, os, sys, uuid
args = sys.argv[1:]
home = os.environ["MC_CLAUDE_HOME"]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
def to_host(p):
    return p.replace("/home/agent/.claude", home, 1) if p else p
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args, "config": os.environ.get("CLAUDE_CONFIG_DIR"),
                            "securestorage": os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR", "<unset>")}) + "\n")
if os.environ.get("FAKE_ONLY_LOG"):
    sys.exit(0)
sid, resume = opt("--session-id"), opt("--resume")
if sid and resume:
    sys.exit("Error: --session-id can only be used with --continue or --resume if --fork-session is also specified.")
sid = sid or resume or str(uuid.uuid4())
folder = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-home-agent")
os.makedirs(folder, exist_ok=True)
path = os.path.join(folder, sid + ".jsonl")
if opt("--session-id") and os.path.exists(path):
    sys.exit("Error: Session ID " + sid + " is already in use.")
if resume and not os.path.exists(path):
    sys.exit("No conversation found with session ID: " + sid)
prompt_file = to_host(opt("--append-system-prompt-file"))
prompt = open(prompt_file).read() if prompt_file else ""
lines = []
if not resume:
    lines.append({"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": [prompt]}})
    lines.append({"type": "api-request-shape", "shape": {"system": prompt}})
elif opt("--system-prompt-snapshot") == "off":
    lines.append({"type": "api-request-shape", "shape": {"system": prompt}})
lines.append({"type": "user", "message": {"content": os.environ.get("FAKE_TEXT", "")}})
with open(path, "a") as f:
    for line in lines:
        f.write(json.dumps(line) + "\n")
'''


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A host home with an agent's claude-config, and a PATH with the fake
    claude. The container path /home/agent/.claude maps onto the same folder
    (MC_CLAUDE_HOME), as the bind mount does."""
    monkeypatch.setattr(runner, "_host_home", lambda: tmp_path)
    home = tmp_path / ".mc" / "agents" / "rex" / "claude-config"
    home.mkdir(parents=True)
    (home / "SOUL.md").write_text("You are Rex. CARD-TEXT", encoding="utf-8")
    (home / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
    (home / ".claude.json").write_text('{"theme": "dark"}', encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(_FAKE_CLAUDE.replace("__PY__", sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    return {"home": home, "bin": bindir, "tmp": tmp_path}


def _run_window(box, chat_id: str, launch: runner.Launch, text: str, **env) -> subprocess.CompletedProcess:
    """What the chat's tmux window runs: start-claude.sh --chat <id> <argv>."""
    full_env = {
        **os.environ,
        "PATH": f"{box['bin']}{os.pathsep}{os.environ['PATH']}",
        "MC_CLAUDE_HOME": str(box["home"]),
        "FAKE_TEXT": text,
        **env,
    }
    return subprocess.run(
        ["bash", str(START_SCRIPT), "--chat", chat_id, *launch.argv],
        env=full_env, capture_output=True, text=True, timeout=30,
    )


def _say(box, chat_id: str, native: str, text: str, handoff: str | None = None) -> runner.Launch:
    launch = runner.plan_launch("rex", chat_id, native, handoff=handoff)
    result = _run_window(box, chat_id, launch, text)
    assert result.returncode == 0, result.stderr
    return launch


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _texts(path: Path) -> list[str]:
    return [x["message"]["content"] for x in _lines(path) if x["type"] == "user"]


def test_two_chats_alternate_each_file_grows_only_with_its_own(box):
    """(2) Two further chats on one Claude agent, messages alternating: each
    chat's session file holds only its own messages, in its own config dir,
    under its pinned id — nothing in the agent's own projects folder."""
    chat_a, chat_b = str(uuid.uuid4()), str(uuid.uuid4())
    native_a, native_b = str(uuid.uuid4()), str(uuid.uuid4())

    first_a = _say(box, chat_a, native_a, "A1")
    first_b = _say(box, chat_b, native_b, "B1")
    assert first_a.action == first_b.action == "start"
    second_a = _say(box, chat_a, native_a, "A2")
    second_b = _say(box, chat_b, native_b, "B2")
    assert second_a.action == second_b.action == "resume"

    file_a = runner.pinned_transcript("rex", chat_a, native_a)
    file_b = runner.pinned_transcript("rex", chat_b, native_b)
    assert file_a is not None and file_b is not None
    assert file_a == box["home"] / "chats" / chat_a / "projects" / "-home-agent" / f"{native_a}.jsonl"
    assert _texts(file_a) == ["A1", "A2"]
    assert _texts(file_b) == ["B1", "B2"]
    # The default chat follows the newest file in the agent's own folder —
    # a further chat must never write there.
    assert not (box["home"] / "projects").exists()
    # The system prompt went in as the agent's card, by file.
    snapshot = next(x for x in _lines(file_a) if x["type"] == "attachment")
    assert snapshot["attachment"]["systemPrompt"] == ["You are Rex. CARD-TEXT"]
    # The agent's settings as the chat's OWN copy (never a link — the hooks in
    # it are rewritten per chat), own .claude.json with trust + onboarding.
    chat_dir = box["home"] / "chats" / chat_a
    assert not (chat_dir / "settings.json").is_symlink()
    assert json.loads((chat_dir / "settings.json").read_text()) == {"model": "opus"}
    cfg = json.loads((chat_dir / ".claude.json").read_text())
    assert cfg["theme"] == "dark"  # started from the agent's own answers
    assert cfg["hasCompletedOnboarding"] is True
    assert cfg["bypassPermissionsModeAccepted"] is True
    assert cfg["projects"]["/home/agent"]["hasTrustDialogAccepted"] is True


def test_restart_resumes_both_chats_with_history_intact(box):
    """(3) Container restart: the windows are gone, the files stay. The next
    message of each chat starts its process with --resume (never a second
    --session-id, which the CLI refuses) and the history continues."""
    chats = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(2)]
    for n, (chat, native) in enumerate(chats):
        _say(box, chat, native, f"before-{n}")
    # — restart: nothing of the processes survives; only the disk —
    for n, (chat, native) in enumerate(chats):
        launch = _say(box, chat, native, f"after-{n}")
        assert launch.action == "resume"
        assert launch.argv[:3] == ["--dangerously-skip-permissions", "--resume", native]
        assert "--session-id" not in launch.argv
        path = runner.pinned_transcript("rex", chat, native)
        assert _texts(path) == [f"before-{n}", f"after-{n}"]


def test_pin_check_finds_the_pinned_file(box):
    """(4) The chat's file must land under its pinned id. Sabotage: strip
    --session-id from the start argv → the CLI picks a random id, the pin
    check finds nothing and this test goes red."""
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())
    launch = runner.plan_launch("rex", chat, native)
    assert launch.action == "start"
    result = _run_window(box, chat, launch, "hello")
    assert result.returncode == 0, result.stderr
    assert runner.pinned_transcript("rex", chat, native) is not None
    files = list((box["home"] / "chats" / chat / "projects" / "-home-agent").glob("*.jsonl"))
    assert [f.stem for f in files] == [native]


def test_pin_check_catches_a_start_without_session_id(box):
    """The check itself: a claude started without --session-id writes under
    a random id — the pin check must say "not pinned"."""
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())
    launch = runner.plan_launch("rex", chat, native)
    stripped = [a for a in launch.argv if a not in ("--session-id", native)]
    result = _run_window(box, chat, runner.Launch("start", stripped, launch.prompt_file, False), "x")
    assert result.returncode == 0, result.stderr
    assert runner.pinned_transcript("rex", chat, native) is None


def test_handoff_on_resume_reaches_the_request_not_a_second_snapshot(box):
    """(5) A handoff passed on resume: the next request's system prompt
    (``api-request-shape``) carries card + package; no second
    ``prompt_snapshot`` is written (measured 19.11.2026, plan §8). A later
    plain resume keeps ``off`` + the file — without it claude falls back to
    the first snapshot (measured, control step 3)."""
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())
    _say(box, chat, native, "first")
    launch = _say(box, chat, native, "after switch", handoff="HANDOFF-PACKAGE-GAMMA")
    assert launch.action == "resume" and launch.snapshot_off
    assert launch.argv[3:] == ["--system-prompt-snapshot", "off",
                               "--append-system-prompt-file",
                               f"/home/agent/.claude/chats/{chat}/system-prompt.md"]
    lines = _lines(runner.pinned_transcript("rex", chat, native))
    snapshots = [x for x in lines if x["type"] == "attachment"]
    shapes = [x["shape"]["system"] for x in lines if x["type"] == "api-request-shape"]
    assert len(snapshots) == 1
    assert "GAMMA" not in snapshots[0]["attachment"]["systemPrompt"][0]
    assert shapes[-1] == "You are Rex. CARD-TEXT\n\nHANDOFF-PACKAGE-GAMMA"

    # Cold restart afterwards, no new handoff: still off + the same file.
    later = _say(box, chat, native, "next day")
    assert later.snapshot_off
    assert "--system-prompt-snapshot" in later.argv
    shapes = [x["shape"]["system"] for x in _lines(runner.pinned_transcript("rex", chat, native))
              if x["type"] == "api-request-shape"]
    assert shapes[-1].endswith("HANDOFF-PACKAGE-GAMMA")


def test_start_with_handoff_keeps_the_snapshot_on(box):
    """omp → Claude into a fresh session: card + package in the file, snapshot
    on (the recorded snapshot then carries the package)."""
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())
    launch = _say(box, chat, native, "hi", handoff="PKG")
    assert launch.action == "start" and not launch.snapshot_off
    assert "--system-prompt-snapshot" not in launch.argv
    snap = next(x for x in _lines(runner.pinned_transcript("rex", chat, native)) if x["type"] == "attachment")
    assert snap["attachment"]["systemPrompt"] == ["You are Rex. CARD-TEXT\n\nPKG"]


_TURN_SIGNAL_SETTINGS = {
    "model": "opus",
    # Exactly what backend/templates/cli_agent_settings.json.j2 renders.
    "hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 5,
            "command": "printf '%s submit\\n' $(date +%s) >> /home/agent/.turn-signal"}]}],
        "Stop": [{"hooks": [{"type": "command", "timeout": 5,
            "command": "printf '%s stop\\n' $(date +%s) >> /home/agent/.turn-signal"}]}],
    },
    "statusLine": {"type": "command", "command": "bash /home/agent/.claude/statusline-mc.sh"},
}


def _hook_commands(settings_data: dict) -> list[str]:
    return [h["command"] for groups in settings_data.get("hooks", {}).values()
            for g in groups for h in g["hooks"]]


def test_chat_never_writes_the_agents_turn_signal(box):
    """poll.sh and the backend read /home/agent/.turn-signal as the turn state
    of window 0 (heartbeat, stagnation, the new-task /clear, dispatch guard).
    A chat's claude runs the agent's hooks — so in the chat's settings every
    turn-signal hook writes the chat's OWN file, never the agent's."""
    (box["home"] / "settings.json").write_text(json.dumps(_TURN_SIGNAL_SETTINGS), encoding="utf-8")
    (box["home"] / "settings.local.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
            "command": "echo x >> $HOME/.turn-signal"}]}]}}), encoding="utf-8")
    chat = str(uuid.uuid4())
    chat_dir = box["home"] / "chats" / chat
    # A chat dir from an earlier build still has the link to the agent's file.
    chat_dir.mkdir(parents=True)
    (chat_dir / "settings.json").symlink_to("../../settings.json")

    _script(box, "--chat", chat, "--resume", SID)

    raw = (chat_dir / "settings.json").read_text()
    assert not (chat_dir / "settings.json").is_symlink()
    assert not (chat_dir / "settings.local.json").is_symlink()
    assert "/home/agent/.turn-signal" not in raw
    assert "/.turn-signal" not in (chat_dir / "settings.local.json").read_text().replace(
        f"{chat_dir}/.turn-signal", "")
    data = json.loads(raw)
    assert data["model"] == "opus" and data["statusLine"] == _TURN_SIGNAL_SETTINGS["statusLine"]
    # The agent's own file is untouched (the link was not written through).
    assert json.loads((box["home"] / "settings.json").read_text()) == _TURN_SIGNAL_SETTINGS
    # Running the chat's hooks writes the chat's file only.
    commands = _hook_commands(data)
    assert len(commands) == 2
    for command in commands:
        subprocess.run(["sh", "-c", command], check=True, timeout=10)
    lines = (chat_dir / ".turn-signal").read_text().split()
    assert lines[1::2] == ["submit", "stop"]

    # A later change by the agent's sync reaches the chat on its next start.
    (box["home"] / "settings.json").write_text(
        json.dumps({**_TURN_SIGNAL_SETTINGS, "model": "sonnet"}), encoding="utf-8")
    _script(box, "--chat", chat, "--resume", SID)
    assert json.loads((chat_dir / "settings.json").read_text())["model"] == "sonnet"


def test_chat_claude_json_follows_the_agents_mcp_servers(box):
    """The chat's .claude.json is its own (trust, history), but MCP servers
    and per-folder trust the agent's sync adds later reach existing chats."""
    chat = str(uuid.uuid4())
    _script(box, "--chat", chat, "--resume", SID)
    chat_json = box["home"] / "chats" / chat / ".claude.json"
    own = json.loads(chat_json.read_text())
    own["theme"] = "light"  # the chat's own answer stays
    chat_json.write_text(json.dumps(own))

    (box["home"] / ".claude.json").write_text(json.dumps({
        "theme": "dark",
        "mcpServers": {"mc": {"command": "mc-mcp"}},
        "projects": {"/workspace/repo": {"hasTrustDialogAccepted": True,
                                         "mcpServers": {"x": {"command": "x"}}}},
    }))
    _script(box, "--chat", chat, "--resume", SID)
    merged = json.loads(chat_json.read_text())
    assert merged["theme"] == "light"
    assert merged["mcpServers"] == {"mc": {"command": "mc-mcp"}}
    assert merged["projects"]["/workspace/repo"]["hasTrustDialogAccepted"] is True
    assert merged["projects"]["/workspace/repo"]["mcpServers"] == {"x": {"command": "x"}}
    assert merged["projects"]["/home/agent"]["hasTrustDialogAccepted"] is True


def test_sticky_resume_carries_the_current_card_and_the_old_package(box):
    """After a handoff the session runs with the snapshot off — every request
    reads the prompt file. A later card change by the agent's sync must show
    up there; the package of the handoff stays."""
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())
    _say(box, chat, native, "first")
    _say(box, chat, native, "switch", handoff="PKG-OLD")
    (box["home"] / "SOUL.md").write_text("You are Rex. NEW-CARD", encoding="utf-8")
    later = _say(box, chat, native, "next day")
    assert later.snapshot_off
    shapes = [x["shape"]["system"] for x in _lines(runner.pinned_transcript("rex", chat, native))
              if x["type"] == "api-request-shape"]
    assert shapes[-1] == "You are Rex. NEW-CARD\n\nPKG-OLD"


# ══════════════════════════════════════════════════════════════════════════
# start-claude.sh — default chat unchanged, macOS login
# ══════════════════════════════════════════════════════════════════════════


def _script(box, *args: str) -> list[dict]:
    log = box["tmp"] / "fake.log"
    log.unlink(missing_ok=True)
    env = {**os.environ, "PATH": f"{box['bin']}{os.pathsep}{os.environ['PATH']}",
           "MC_CLAUDE_HOME": str(box["home"]), "FAKE_LOG": str(log), "FAKE_ONLY_LOG": "1"}
    env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    result = subprocess.run(["bash", str(START_SCRIPT), *args], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_default_window_starts_claude_exactly_as_before(box):
    """Without --chat: the agent's own window 0 — same flags, the card as an
    argument, the agent's own config dir, no chat dir created."""
    (calls,) = _script(box)
    assert calls["argv"] == ["--dangerously-skip-permissions", "--append-system-prompt",
                             "You are Rex. CARD-TEXT"]
    assert calls["config"] is None
    assert calls["securestorage"] == "<unset>"
    assert not (box["home"] / "chats").exists()


def test_chat_mode_login_env(box):
    """Chat mode: own CLAUDE_CONFIG_DIR. On macOS the login lives in the
    keychain under a name derived from that dir — CLAUDE_SECURESTORAGE_CONFIG_DIR
    empty keeps the normal entry (measured 19.11.2026). On Linux it stays
    unset (the container logs in with CLAUDE_CODE_OAUTH_TOKEN)."""
    chat = str(uuid.uuid4())
    (calls,) = _script(box, "--chat", chat, "--resume", SID)
    assert calls["argv"] == ["--resume", SID]
    assert calls["config"] == str(box["home"] / "chats" / chat)
    expected = "" if platform.system() == "Darwin" else "<unset>"
    assert calls["securestorage"] == expected


def test_chat_mode_refuses_a_bad_chat_id(box):
    env = {**os.environ, "PATH": f"{box['bin']}{os.pathsep}{os.environ['PATH']}",
           "MC_CLAUDE_HOME": str(box["home"])}
    result = subprocess.run(["bash", str(START_SCRIPT), "--chat", "../../etc"], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert not (box["home"] / "chats").exists()


# ══════════════════════════════════════════════════════════════════════════
# container side: windows, send_text routing, flag
# ══════════════════════════════════════════════════════════════════════════


class _Docker:
    """A fake ``docker exec … tmux`` with sessions: records argv, answers
    ``list-windows -a`` and behaves like tmux 3.3a for ``new-session`` (a
    duplicate name fails), ``new-window`` (``-S`` selects an existing window
    of that name instead of opening a second one) and ``kill-window``.
    ``delay`` stretches every call, as a real docker exec does (~50 ms)."""

    def __init__(self, windows=(("rex", "0"), ("rex", "1"), ("rex", "2")), delay: float = 0.0):
        self.calls: list[list[str]] = []
        self.windows = [tuple(w) for w in windows]
        self.delay = delay

    def opens(self) -> list[list[str]]:
        return [c for c in self.calls if "new-window" in c or "new-session" in c]

    async def __call__(self, argv):
        import asyncio

        self.calls.append(argv)
        if self.delay:
            await asyncio.sleep(self.delay)
        tmux = argv[argv.index("tmux"):]

        def opt(name):
            return tmux[tmux.index(name) + 1] if name in tmux else None

        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(argv, rc, out, err)

        if "list-windows" in tmux:
            fmt = opt("-F")
            return done(out="".join(
                fmt.replace("#{session_name}", s).replace("#{window_name}", n) + "\n"
                for s, n in self.windows))
        if "new-session" in tmux:
            session = opt("-s")
            if any(s == session for s, _ in self.windows):
                return done(1, err=f"duplicate session: {session}")
            self.windows.append((session, opt("-n")))
            return done()
        if "new-window" in tmux:
            session, name = opt("-t").rstrip(":"), opt("-n")
            if not any(s == session for s, _ in self.windows):
                return done(1, err=f"can't find session: {session}")
            if "-S" in tmux and (session, name) in self.windows:
                return done()
            self.windows.append((session, name))
            return done()
        if "kill-window" in tmux:
            session, name = opt("-t").split(":", 1)
            if (session, name) not in self.windows:
                return done(1, err=f"can't find window: {name}")
            self.windows.remove((session, name))
            return done()
        return done()


class _Agent:
    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4())
        self.slug = kw.get("slug", "rex")
        self.agent_runtime = kw.get("agent_runtime", "cli-bridge")
        self.harness = kw.get("harness", "claude")
        self.name = "Rex"


async def test_ensure_window_opens_once_and_restarts_only_for_a_handoff(box, monkeypatch):
    docker = _Docker()
    monkeypatch.setattr(runner, "_docker", docker)
    agent, chat, native = _Agent(), str(uuid.uuid4()), str(uuid.uuid4())

    target = await runner.ensure_window(agent, chat, native)
    assert target == f"chats-rex:chat-{chat}"
    (new,) = docker.opens()
    assert new[:7] == ["docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", "mc-agent-rex"]
    tail = new[new.index("/home/agent/start-claude.sh"):]
    assert tail[:3] == ["/home/agent/start-claude.sh", "--chat", chat]
    assert tail[3:6] == ["--dangerously-skip-permissions", "--session-id", native]
    assert new[new.index("-c") + 1] == "/home/agent"

    # Running window, no handoff: used as it is.
    await runner.ensure_window(agent, chat, native)
    assert len(docker.opens()) == 1
    assert not [c for c in docker.calls if "kill-window" in c]

    # A switch with a handoff: the old process goes, a new one comes.
    await runner.ensure_window(agent, chat, native, handoff="PKG")
    assert [c for c in docker.calls if "kill-window" in c][0][-1] == f"chats-rex:chat-{chat}"
    assert len(docker.opens()) == 2
    assert docker.windows.count(("chats-rex", f"chat-{chat}")) == 1


async def test_chat_windows_never_touch_the_agents_own_session(box, monkeypatch):
    """The container watchdog knows windows 0/1/2 of the agent's session by
    INDEX only, poll.sh types tasks into :0, the recycler respawns :0. A
    chat window in that session would take the lowest free index — hiding a
    dead poll.sh (1) or recycler (2), or becoming :0 itself. So chat windows
    live in their own session ``chats-<slug>`` and every tmux call of the
    runner names that session, never the agent's."""
    # poll.sh's window 1 just died: index 1 is free in the agent's session.
    docker = _Docker(windows=(("rex", "0"), ("rex", "2")))
    monkeypatch.setattr(runner, "_docker", docker)
    agent = _Agent()
    chats = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(3)]
    for chat, native in chats:
        target = await runner.ensure_window(agent, chat, native)
        assert target.startswith("chats-rex:")
    # Nothing new in the agent's session: index 1 stays free for the watchdog.
    assert [w for w in docker.windows if w[0] == "rex"] == [("rex", "0"), ("rex", "2")]
    assert sorted(n for s, n in docker.windows if s == "chats-rex") == sorted(
        f"chat-{c}" for c, _ in chats)
    for call in docker.opens():
        tmux = call[call.index("tmux"):]
        session = tmux[tmux.index("-s") + 1] if "-s" in tmux else tmux[tmux.index("-t") + 1]
        assert session.rstrip(":") == "chats-rex", call
    # The first open made the session; the later ones added windows to it.
    assert "new-session" in docker.opens()[0] and all("new-window" in c for c in docker.opens()[1:])
    # The agent's send-keys/capture target is a chats-session window too.
    assert runner.chat_target("rex", chats[0][0]) == f"chats-rex:chat-{chats[0][0]}"


async def test_concurrent_sends_open_one_window(box, monkeypatch):
    """Two sends to the same not-yet-open chat at the same time: exactly ONE
    claude process. Two windows of one name would run two claudes on one
    session id, and ``send-keys -t <name>`` then fails (ambiguous name)."""
    import asyncio

    docker = _Docker(delay=0.05)
    monkeypatch.setattr(runner, "_docker", docker)
    agent, chat, native = _Agent(), str(uuid.uuid4()), str(uuid.uuid4())
    targets = await asyncio.gather(runner.ensure_window(agent, chat, native),
                                   runner.ensure_window(agent, chat, native))
    assert targets[0] == targets[1]
    assert len(docker.opens()) == 1, f"{len(docker.opens())} windows named chat-{chat} opened"
    assert docker.windows.count(("chats-rex", f"chat-{chat}")) == 1

    # Two DIFFERENT chats at once while the chats session does not exist yet:
    # both open (one makes the session, the other joins it), none fails.
    docker2 = _Docker(delay=0.05)
    monkeypatch.setattr(runner, "_docker", docker2)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await asyncio.gather(runner.ensure_window(agent, a, native),
                         runner.ensure_window(agent, b, str(uuid.uuid4())))
    assert {n for s, n in docker2.windows if s == "chats-rex"} == {f"chat-{a}", f"chat-{b}"}


async def test_window_opened_elsewhere_after_the_check_is_not_doubled(box, monkeypatch):
    """The lock is per backend process. A window of the same name that
    appears after the check (a second process, a hand start) is selected by
    ``new-window -S`` — never a second claude on the same session id."""
    chat, native = str(uuid.uuid4()), str(uuid.uuid4())

    class _Stale(_Docker):
        async def __call__(self, argv):
            if "list-windows" in argv:
                answer = await super().__call__(argv)
                # Someone else opens it right after our check.
                self.windows.append(("chats-rex", f"chat-{chat}"))
                return answer
            return await super().__call__(argv)

    docker = _Stale(windows=(("rex", "0"), ("chats-rex", "chat-other")))
    monkeypatch.setattr(runner, "_docker", docker)
    await runner.ensure_window(_Agent(), chat, native)
    assert docker.windows.count(("chats-rex", f"chat-{chat}")) == 1


async def test_failed_handoff_plan_does_not_kill_running_window(box, monkeypatch):
    """A switch whose launch cannot be planned (package too large, prompt
    file not writable) must leave the running process alone."""
    docker = _Docker()
    monkeypatch.setattr(runner, "_docker", docker)
    agent, chat, native = _Agent(), str(uuid.uuid4()), str(uuid.uuid4())
    await runner.ensure_window(agent, chat, native)

    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.ensure_window(agent, chat, native, handoff="x" * (600 * 1024))
    assert e.value.code == "prompt_too_large"
    assert not [c for c in docker.calls if "kill-window" in c]
    assert ("chats-rex", f"chat-{chat}") in docker.windows

    def _no_write(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", _no_write)
    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.ensure_window(agent, chat, native, handoff="PKG")
    assert e.value.code == "prompt_write_failed"
    assert not [c for c in docker.calls if "kill-window" in c]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs a real tmux")
async def test_real_tmux_watchdog_indexes_stay_free(box, monkeypatch, tmp_path):
    """The same with a REAL tmux server (private socket): the agent's session
    has windows 0 and 2 (poll.sh died); three chats open; the watchdog's
    ``new-window -t <slug>:1`` still works, and two concurrent opens of one
    chat give one window."""
    import asyncio

    sock = f"mc-test-{uuid.uuid4().hex[:8]}"

    def local(argv):
        tmux = argv[argv.index("tmux") + 1:]
        if "/home/agent/start-claude.sh" in tmux:
            tmux = tmux[:tmux.index("/home/agent/start-claude.sh")] + ["sleep", "60"]
        if "-c" in tmux:
            tmux[tmux.index("-c") + 1] = str(tmp_path)
        return ["tmux", "-L", sock, *tmux]

    async def _real(argv):
        await asyncio.sleep(0.02)
        return await asyncio.to_thread(subprocess.run, local(argv), capture_output=True,
                                       text=True, timeout=10)

    run = lambda *a: subprocess.run(["tmux", "-L", sock, *a], capture_output=True, text=True)
    run("new-session", "-d", "-s", "rex", "sleep 60")
    run("new-window", "-t", "rex:1", "sleep 60")
    run("new-window", "-t", "rex:2", "sleep 60")
    run("kill-window", "-t", "rex:1")
    try:
        monkeypatch.setattr(runner, "_docker", _real)
        agent, native = _Agent(), str(uuid.uuid4())
        chats = [str(uuid.uuid4()) for _ in range(3)]
        await asyncio.gather(*(runner.ensure_window(agent, c, str(uuid.uuid4())) for c in chats))
        await asyncio.gather(runner.ensure_window(agent, chats[0], native),
                             runner.ensure_window(agent, chats[0], native))
        rex = run("list-windows", "-t", "rex", "-F", "#{window_index}").stdout.split()
        assert rex == ["0", "2"]
        # The watchdog's restart of poll.sh (entrypoint.sh) still succeeds.
        assert run("new-window", "-t", "rex:1", "sleep 60").returncode == 0
        names = run("list-windows", "-t", "chats-rex", "-F", "#{window_name}").stdout.split()
        assert sorted(names) == sorted(f"chat-{c}" for c in chats)
        # send-keys by name finds exactly one window.
        assert run("send-keys", "-t", runner.chat_target("rex", chats[0]), "-l", "--", "x").returncode == 0
        # The agent's session is never matched by the chats session's name.
        run("kill-session", "-t", "rex")
        assert run("has-session", "-t", "rex").returncode != 0
    finally:
        run("kill-server")


async def test_ensure_window_refuses_non_container_agents(box, monkeypatch):
    monkeypatch.setattr(runner, "_docker", _Docker())
    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.ensure_window(_Agent(agent_runtime="host", slug="boss"), str(uuid.uuid4()), SID)
    assert e.value.code == "not_a_container_agent"


def _fake_send(monkeypatch, agent_chat_input):
    calls: list[list[str]] = []
    panes: list[str] = []

    async def _fake_run(argv):
        calls.append(argv)

    async def _pane0(agent):
        panes.append("0")
        return "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"

    async def _chat_pane(slug, chat_id):
        panes.append(chat_id)
        return "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run)
    monkeypatch.setattr(agent_chat_input, "capture_pane", _pane0)
    monkeypatch.setattr(runner, "capture_chat_pane", _chat_pane)
    return calls, panes


async def test_flag_off_default_chat_unchanged_and_chat_id_refused(monkeypatch):
    """Without CHAT_RECORD_ENABLED: the default chat types into window 0 as
    today; a chat id is refused before anything runs."""
    from app.services import agent_chat_input

    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)
    calls, panes = _fake_send(monkeypatch, agent_chat_input)

    async def _boom(*a, **k):
        raise AssertionError("runner must not run")

    monkeypatch.setattr(runner, "open_chat", _boom)
    agent = _Agent()
    await agent_chat_input.send_text(agent, "hello")
    assert calls == [
        ["docker", "exec", "-u", "agent", "mc-agent-rex", "touch", "/home/agent/.claude/last-task.marker"],
        ["docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", "mc-agent-rex",
         "tmux", "send-keys", "-t", "rex:0", "-l", "--", "hello"],
        ["docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", "mc-agent-rex",
         "tmux", "send-keys", "-t", "rex:0", "Enter"],
    ]
    assert panes == ["0"]

    calls.clear()
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.send_text(agent, "hello", chat_id=str(uuid.uuid4()))
    assert calls == []


async def test_flag_on_without_chat_id_is_unchanged_too(monkeypatch):
    from app.services import agent_chat_input

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    calls, _ = _fake_send(monkeypatch, agent_chat_input)
    await agent_chat_input.send_text(_Agent(), "hello")
    assert [c[c.index("-t") + 1] for c in calls if "send-keys" in c] == ["rex:0", "rex:0"]


async def test_chat_id_types_into_the_chats_own_window(monkeypatch):
    """With the flag on, a chat id opens/resumes that chat's process and the
    text — readiness check included — goes to its window, not window 0."""
    from app.services import agent_chat_input

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    calls, panes = _fake_send(monkeypatch, agent_chat_input)
    opened: list[str] = []

    async def _open(agent, chat_id, *, handoff=None):
        opened.append(chat_id)
        return f"chats-{agent.slug}:chat-{chat_id}"

    monkeypatch.setattr(runner, "open_chat", _open)
    chat_a, chat_b = str(uuid.uuid4()), str(uuid.uuid4())
    agent = _Agent()
    await agent_chat_input.send_text(agent, "to A", chat_id=chat_a)
    await agent_chat_input.send_text(agent, "line1\nline2", chat_id=chat_b)
    targets = [c[c.index("-t") + 1] for c in calls if "send-keys" in c]
    assert targets == [f"chats-rex:chat-{chat_a}"] * 2 + [f"chats-rex:chat-{chat_b}"] * 2
    assert opened == [chat_a, chat_b]
    assert panes == [chat_a, chat_b]


async def test_chat_send_leaves_window_0s_recycler_clock_and_preview_alone(monkeypatch):
    """A further chat's traffic is not window 0's: no touch of the agent's
    recycler idle marker (it decides when window 0 is recycled) and no
    preview anchor for the agent's default chat."""
    from app.services import agent_chat_input

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    calls, _ = _fake_send(monkeypatch, agent_chat_input)

    async def _open(agent, chat_id, *, handoff=None):
        return f"chats-{agent.slug}:chat-{chat_id}"

    monkeypatch.setattr(runner, "open_chat", _open)
    agent = _Agent()
    agent_chat_input.pop_last_sent(str(agent.id))
    await agent_chat_input.send_text(agent, "only for the chat", chat_id=str(uuid.uuid4()))
    assert not [c for c in calls if "touch" in c]
    assert agent_chat_input.pop_last_sent(str(agent.id)) is None
    # The default chat still does both.
    await agent_chat_input.send_text(agent, "default")
    assert [c for c in calls if "touch" in c]
    assert agent_chat_input.pop_last_sent(str(agent.id)) == "default"


@pytest.mark.parametrize("word", ["/clear", "/new", " /CLEAR "])
async def test_clear_in_a_further_chat_is_refused_not_typed(monkeypatch, word):
    """/clear or /new typed into a further chat's claude would start a new
    session under a random id: the chat's pin would still name the old file,
    and the next restart would resume the pre-clear session — everything
    after the /clear silently gone. Refused before anything opens or types."""
    from app.services import agent_chat_input

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    calls, _ = _fake_send(monkeypatch, agent_chat_input)
    opened: list[str] = []

    async def _open(agent, chat_id, *, handoff=None):
        opened.append(chat_id)
        return f"chats-{agent.slug}:chat-{chat_id}"

    monkeypatch.setattr(runner, "open_chat", _open)
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.send_text(_Agent(), word, chat_id=str(uuid.uuid4()))
    assert calls == [] and opened == []
    # ``/clear`` takes an argument in Claude Code (``argumentHint: "[name]"``,
    # 2.1.328): "/clear den Cache" still clears — refused too (item A2).
    with pytest.raises(agent_chat_input.SessionCommandBlockedError):
        await agent_chat_input.send_text(_Agent(), "/clear den Cache", chat_id=str(uuid.uuid4()))
    assert opened == []
    # A message that only mentions the word is just text.
    await agent_chat_input.send_text(_Agent(), "please /clear it", chat_id=str(uuid.uuid4()))
    assert len(opened) == 1


async def test_chat_id_refused_for_other_harnesses(monkeypatch):
    from app.services import agent_chat_input

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    calls, _ = _fake_send(monkeypatch, agent_chat_input)
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.send_text(_Agent(harness="openclaude"), "x", chat_id=str(uuid.uuid4()))
    assert calls == []


# ══════════════════════════════════════════════════════════════════════════
# the record: which chats may get a window
# ══════════════════════════════════════════════════════════════════════════


async def test_load_chat_plan_only_for_further_claude_chats_of_the_agent(monkeypatch, make_agent):
    from app import database
    from app.services import chat_record

    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)
    monkeypatch.setattr(database, "async_session_maker",
                        lambda: AsyncSession(test_engine, expire_on_commit=False))
    agent = await make_agent(name="Rex", slug="rex", harness="claude")
    other = await make_agent(name="Other", slug="other", harness="claude")

    class _User:
        id = None

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, _User(), agent, title="Second")
        await s.commit()
        default = await chat_record.ensure_agent_chat(s, agent)
        await s.commit()

    plan = await runner.load_chat_plan(agent, str(chat.id))
    assert plan["action"] == "start"
    assert plan["nativeSessionId"] == chat.native_session_id

    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.load_chat_plan(other, str(chat.id))
    assert e.value.code == "chat_not_found"
    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.load_chat_plan(agent, str(default.id))
    assert e.value.code == "default_chat"
    with pytest.raises(ClaudeChatRunnerError) as e:
        await runner.load_chat_plan(agent, "nope")
    assert e.value.code == "chat_not_found"
