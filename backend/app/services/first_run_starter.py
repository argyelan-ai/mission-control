"""First run: a real lead agent, a practice repo and a first job (onboarding W3, PR 3-1).

What a fresh install gets after the setup wizard — one call, nothing foreign:

1. **Lead agent** from the built-in ``Lead`` template (ADR-089, the hands-on
   lead), bound to the runtime the operator chose or to the registry's
   ``lead`` runtime, provisioned like a one-click agent.
2. **Practice repo** — a small bare git repository created on this computer
   (``~/.mc/repos/mc-practice.git``) from ``backend/templates/practice-repo``
   and registered as a NORMAL entry in the repo registry (``source="local"``,
   ``file://`` URL). Dispatch clones it like any registry repo (task.repo_id
   path, ADR-052) — no special case downstream. A later "pick a project"
   wizard step registers a different repo the same way.
3. **First job** on that repo, assigned to the lead: "add a test, ask me
   before you commit" — the commit approval on the phone is the aha moment.

Guards (operator decisions 2026-10-16):

* No foreign seed: an install that already has boards or agents is refused
  (``not_fresh``) — starter content is never written into live data.
* No dead end: without a runtime the agent can start on NOW — Claude with a
  connected account, or a model runtime that has its key and is serving —
  nothing is created (``no_brain``; an explicit choice that is not ready is
  ``runtime_not_ready`` with a ``reason``). The wizard says "your agent needs
  an AI" instead. All shipped runtimes are enabled, so "enabled" alone is
  never enough.
* Retry-safe: every finished part is recorded in ``app_settings``
  (``first_run_starter``); a second call finishes what is missing and creates
  nothing twice. A retry does not provision again — the agent page's
  Provision button is the existing way back for a failed provisioning.

The wizard's done-marker (``services/first_run.py``) stays with the wizard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.models.app_setting import AppSetting
from app.models.board import Board
from app.models.repo import Repo
from app.models.runtime import Runtime
from app.models.task import Task
from app.services.first_run import FIRST_RUN_STARTER_KEY

logger = logging.getLogger("mc.first_run_starter")

STATE_KEY = FIRST_RUN_STARTER_KEY

LEAD_TEMPLATE_NAME = "Lead"
LEAD_RUNTIME_TAG = "lead"

BOARD_SLUG = "getting-started"

PRACTICE_REPO_SLUG = "mc-practice"
PRACTICE_REPO_FULL_NAME = f"local/{PRACTICE_REPO_SLUG}"
PRACTICE_REPO_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "practice-repo"


@dataclass(frozen=True)
class StarterTexts:
    """What the operator reads in the UI as it is stored: board, repo, card.

    Written in the operator's language (the one the agent speaks too), so a
    German install never shows an English card next to a German UI. Commands,
    paths and the commit message stay as they are — they are code.
    """

    board_name: str
    board_description: str
    repo_description: str
    job_title: str
    job_description: str


_TEXTS: dict[str, StarterTexts] = {
    "en": StarterTexts(
        board_name="Getting started",
        board_description="Created by the first run — your lead agent works here.",
        repo_description="Practice repo for your first agent job — lives on this computer.",
        job_title="Add a test for parse()",
        job_description="""\
A first job on a small practice repo — nothing of the operator's own code is in it.

1. Read `practice/parse.py`.
2. Add a test for `parse()` in `tests/test_parse.py`, in the style of
   `tests/test_slug.py`: normal pairs, spaces around keys and values, an empty
   pair, and the `ValueError` case.
3. Run `python3 -m unittest` until everything passes.
4. **Ask me before you commit.** Do not commit yet — ask:
   `mc question "Ready to commit: add a test for parse() (tests/test_parse.py). Commit?" --options "Commit,Show diff,Change something"`
5. On "Commit": commit on your task branch (`test: add a test for parse()`).
   On "Show diff": post `git diff` as a comment, then ask again.
   On anything else: make the change, then ask again.
6. Close the card with `mc finish "<reflection>"`. Do not push — this
   repository lives only on this computer and has no remote.
""",
    ),
    "de": StarterTexts(
        board_name="Erste Schritte",
        board_description="Vom ersten Start angelegt — hier arbeitet dein Lead-Agent.",
        repo_description="Übungs-Repo für den ersten Auftrag deines Agenten — liegt nur auf diesem Computer.",
        job_title="Einen Test für parse() schreiben",
        job_description="""\
Ein erster Auftrag in einem kleinen Übungs-Repo — von deinem eigenen Code steckt nichts darin.

1. Lies `practice/parse.py`.
2. Schreib einen Test für `parse()` in `tests/test_parse.py`, im Stil von
   `tests/test_slug.py`: normale Paare, Leerzeichen um Schlüssel und Werte, ein
   leeres Paar und den Fall `ValueError`.
3. Führ `python3 -m unittest` aus, bis alles grün ist.
4. **Frag mich, bevor du committest.** Noch nicht committen — frag:
   `mc question "Bereit zum Commit: Test für parse() (tests/test_parse.py). Committen?" --options "Committen,Diff zeigen,Etwas ändern"`
5. Bei „Committen“: auf deinem Task-Branch committen (`test: add a test for parse()`).
   Bei „Diff zeigen“: `git diff` als Kommentar posten, dann erneut fragen.
   Bei allem anderen: die Änderung machen, dann erneut fragen.
6. Schliess die Karte mit `mc finish "<Reflexion>"`. Nicht pushen — dieses
   Repository liegt nur auf diesem Computer und hat kein Remote.
""",
    ),
}


def texts_for(language: str | None) -> StarterTexts:
    """The starter texts in ``language`` (``de``, ``de-CH`` …); English otherwise."""
    base = (language or "en").split("-")[0].split("_")[0].lower()
    return _TEXTS.get(base, _TEXTS["en"])


class StarterError(Exception):
    """A refusal with a stable code the wizard can word in its language."""

    def __init__(self, status: int, code: str, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        #: Finer cause for ``runtime_not_ready`` (see ``NOT_READY_MESSAGES``).
        self.reason = reason


@dataclass
class StarterResult:
    board: Board
    repo: Repo
    agent: Agent
    task: Task
    runtime: Runtime | None
    created: list[str] = field(default_factory=list)
    #: The new agent's token, only when the agent was created in THIS call —
    #: the caller hands it to the one-click provisioning (never stored).
    new_agent_token: str | None = None


# ── state ──────────────────────────────────────────────────────────────────

_lock = asyncio.Lock()


async def _state_row(session: AsyncSession) -> AppSetting | None:
    return (
        await session.exec(select(AppSetting).where(AppSetting.key == STATE_KEY))
    ).one_or_none()


async def _read_state(session: AsyncSession) -> dict[str, str]:
    row = await _state_row(session)
    if row is None:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


async def _write_state(session: AsyncSession, state: dict[str, str]) -> None:
    row = await _state_row(session)
    value = json.dumps(state, sort_keys=True)
    if row is None:
        session.add(AppSetting(key=STATE_KEY, value=value))
    else:
        row.value = value
        session.add(row)


async def _get(session: AsyncSession, model, raw_id: str | None):
    if not raw_id:
        return None
    try:
        return await session.get(model, uuid.UUID(raw_id))
    except ValueError:
        return None


# ── the brain ──────────────────────────────────────────────────────────────


def _runnable(runtime: Runtime) -> bool:
    """An agent can be built on it: enabled, and a known harness drives it."""
    from app.services.harness_compat import HARNESSES, derive_harness

    return bool(runtime.enabled) and derive_harness(runtime) in HARNESSES


#: Why a runnable runtime still cannot carry the first agent. Stable codes
#: (``detail.reason``) the wizard words in the operator's language.
NOT_READY_MESSAGES: dict[str, str] = {
    "claude_not_connected": "Claude is not connected yet — connect your Claude account first.",
    "no_api_key": "This AI has no API key in MC yet — add its key first.",
    "not_serving": "This AI is not answering right now — start it first.",
    "needs_sign_in": "A new agent on this AI has to sign in by hand first — pick another AI.",
}


async def _not_ready(session: AsyncSession, runtime: Runtime) -> str | None:
    """Why the lead agent could not start on ``runtime`` right now (None = it can).

    No second rulebook — the checks MC already has decide:

    * **Claude** (``uses_claude_account``): the shared Claude account must be
      connected — ``claude_account.is_connected`` (vault token not rejected by
      its registry check, or the ``.env`` fallback). Never calls Claude.
    * **Model runtimes** (OpenAI protocol): a hosted ``cloud`` endpoint answers
      ``/models`` without a key, so it first needs the key the bootstrap would
      hand the agent (``resolve_provider_credentials``); then the runtime must
      be serving (``runtime_readiness.is_runtime_ready`` — cached probe).
    * **Kimi** signs each new agent in by hand (device login per agent), so a
      fresh agent cannot start on it without the operator.
    """
    from app.services import claude_account, runtime_readiness
    from app.services.harness_compat import (
        resolve_provider_credentials,
        runtime_protocol,
        uses_claude_account,
    )

    if uses_claude_account(runtime):
        return None if await claude_account.is_connected(session) else "claude_not_connected"
    if runtime_protocol(runtime) != "openai":
        return "needs_sign_in"
    if (runtime.runtime_type or "").strip() == "cloud" and not await resolve_provider_credentials(
        session, None, runtime
    ):
        return "no_api_key"
    try:
        serving = await runtime_readiness.is_runtime_ready(runtime)
    except Exception as exc:  # noqa: BLE001 — a probe error means "not answering"
        logger.warning("first run: readiness probe for %s failed: %s", runtime.slug, exc)
        serving = False
    return None if serving else "not_serving"


async def resolve_brain(session: AsyncSession, runtime_ref: str | None) -> Runtime:
    """The runtime the lead agent runs on — one it can start on NOW.

    An explicit choice (the wizard's "Which AI?" step; UUID or slug) wins, but
    only when it is ready (``runtime_not_ready`` + ``reason`` otherwise).
    Otherwise the registry decides among the ready runtimes: the first one
    tagged ``lead`` (``role_tags``), else the first by ``ui_order``. None ready
    → ``no_brain``: a fresh install without a connected account ends here
    instead of building a lead that can never start.
    """
    if runtime_ref:
        runtime = None
        try:
            runtime = await session.get(Runtime, uuid.UUID(runtime_ref))
        except ValueError:
            runtime = (
                await session.exec(select(Runtime).where(Runtime.slug == runtime_ref))
            ).first()
        if runtime is None:
            raise StarterError(404, "runtime_not_found", f"Runtime '{runtime_ref}' not found")
        if not _runnable(runtime):
            raise StarterError(
                409, "runtime_not_runnable",
                f"No agent can run on '{runtime.display_name}' — pick another AI.",
            )
        reason = await _not_ready(session, runtime)
        if reason:
            raise StarterError(409, "runtime_not_ready", NOT_READY_MESSAGES[reason], reason=reason)
        return runtime

    runtimes = (await session.exec(select(Runtime))).all()
    candidates = sorted((r for r in runtimes if _runnable(r)), key=lambda r: (r.ui_order, r.slug))
    # Lead-tagged first, then the rest — each checked only until one is ready,
    # so a ready lead runtime never waits on the probes of the others.
    ordered = [r for r in candidates if LEAD_RUNTIME_TAG in (r.role_tags or [])] + [
        r for r in candidates if LEAD_RUNTIME_TAG not in (r.role_tags or [])
    ]
    for runtime in ordered:
        if await _not_ready(session, runtime) is None:
            return runtime
    raise StarterError(
        409, "no_brain",
        "Your agent needs an AI. Connect Claude Code or add a model runtime first.",
    )


# ── the practice repo on disk ──────────────────────────────────────────────


def practice_repo_dir() -> Path:
    return Path(settings.home_host) / ".mc" / "repos" / f"{PRACTICE_REPO_SLUG}.git"


def practice_repo_url() -> str:
    """Registry URL: ``file://`` + path without ``.git`` (clone_url_for adds it)."""
    return "file://" + str(practice_repo_dir()).removesuffix(".git")


async def _git(*args: str, cwd: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {err.decode().strip()}")
    return out.decode().strip()


async def ensure_practice_repo_on_disk() -> Path:
    """Create the bare practice repository once (one commit on ``main``).

    Built in a work dir next to the target and cloned bare into place, so a
    crash never leaves a half-written repository under the final name. An
    existing repository is kept as it is (a retry, or a reinstall)."""
    target = practice_repo_dir()
    if (target / "HEAD").is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".practice-", dir=target.parent))
    try:
        shutil.copytree(PRACTICE_REPO_TEMPLATE, work, dirs_exist_ok=True)
        await _git("init", "-q", "-b", "main", cwd=str(work))
        await _git("add", ".", cwd=str(work))
        await _git(
            "-c", "user.name=Mission Control", "-c", "user.email=mc@mc.local",
            "commit", "-q", "-m", "Practice repo for your first agent job",
            cwd=str(work),
        )
        await _git("clone", "-q", "--bare", str(work), str(target))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return target


# ── the run ────────────────────────────────────────────────────────────────


async def _refuse_if_not_fresh(session: AsyncSession, state: dict[str, str]) -> None:
    """Starter content only on a fresh install (no foreign seed into live data).
    The starter's own board/agent from an earlier, interrupted call do not
    count — that is a retry."""
    own_board, own_agent = state.get("board_id"), state.get("agent_id")
    boards = [str(b) for b in (await session.exec(select(Board.id))).all()]
    agents = [str(a) for a in (await session.exec(select(Agent.id))).all()]
    if any(b != own_board for b in boards) or any(a != own_agent for a in agents):
        raise StarterError(
            409, "not_fresh",
            "This install already has boards or agents — the first run only "
            "sets up a fresh install. Create agents from the Agents page.",
        )


async def start(
    session: AsyncSession,
    *,
    runtime_ref: str | None = None,
    language: str = "en",
) -> StarterResult:
    """Create (or finish creating) board, practice repo, lead agent and first job."""
    async with _lock:
        return await _start(session, runtime_ref=runtime_ref, language=language)


async def _start(session: AsyncSession, *, runtime_ref: str | None, language: str) -> StarterResult:
    from app.services.fs_service import agent_slug
    from app.services.harness_compat import derive_harness
    from app.services.repo_registry import LOCAL_SOURCE, upsert_repo

    state = await _read_state(session)
    texts = texts_for(language)
    created: list[str] = []
    new_token: str | None = None

    existing_agent = await _get(session, Agent, state.get("agent_id"))
    # The brain only matters while the agent does not exist yet; a retry
    # keeps the agent's binding.
    runtime = (
        await session.get(Runtime, existing_agent.runtime_id)
        if existing_agent is not None and existing_agent.runtime_id
        else None
    )
    if existing_agent is None:
        runtime = await resolve_brain(session, runtime_ref)
    await _refuse_if_not_fresh(session, state)

    # 1. board — the lead agent's home; auto-dispatch so the job starts itself
    board = await _get(session, Board, state.get("board_id"))
    if board is None:
        board = Board(
            name=texts.board_name,
            slug=BOARD_SLUG,
            description=texts.board_description,
            auto_dispatch_enabled=True,
        )
        session.add(board)
        await session.flush()
        state["board_id"] = str(board.id)
        await _write_state(session, state)
        await session.commit()
        created.append("board")

    # 2. practice repo — on disk, then a normal registry entry
    repo = await _get(session, Repo, state.get("repo_id"))
    if repo is None:
        await ensure_practice_repo_on_disk()
        repo = await upsert_repo(
            session,
            full_name=PRACTICE_REPO_FULL_NAME,
            url=practice_repo_url(),
            default_branch="main",
            description=texts.repo_description,
            visibility="private",
            source=LOCAL_SOURCE,
        )
        repo.is_active = True
        await session.flush()
        state["repo_id"] = str(repo.id)
        await _write_state(session, state)
        await session.commit()
        created.append("repo")

    # 3. lead agent — built-in Lead template, one-click provisioning
    agent = existing_agent
    if agent is None:
        from app.routers.agent_templates import _do_instantiate

        template = (
            await session.exec(
                select(AgentTemplate).where(
                    AgentTemplate.name == LEAD_TEMPLATE_NAME,
                    AgentTemplate.is_builtin == True,  # noqa: E712
                )
            )
        ).first()
        if template is None:
            raise StarterError(
                500, "template_missing",
                "The built-in Lead template is missing — restart the backend to seed it.",
            )
        agent, new_token = await _do_instantiate(
            template=template,
            board_id=board.id,
            name=None,
            model=runtime.model_identifier if runtime else None,
            session=session,
            is_board_lead=True,
            runtime_id=str(runtime.id) if runtime else None,
            harness=derive_harness(runtime),
        )
        agent.is_board_lead = True
        agent.agent_runtime = "cli-bridge"
        agent.operator_language = language
        # The rename-safe slug every other workspace path uses (models/agent.py).
        agent.workspace_path = f"{settings.home_host}/.mc/workspaces/{agent_slug(agent)}"
        session.add(agent)
        state["agent_id"] = str(agent.id)
        await _write_state(session, state)
        await session.commit()
        await session.refresh(agent)
        created.append("agent")

    # 4. first job — assigned, on the practice repo, dispatched like any card
    task = await _get(session, Task, state.get("task_id"))
    if task is None:
        from app.services.task_create import create_task_internal

        task = await create_task_internal(
            session,
            board_id=board.id,
            title=texts.job_title,
            description=texts.job_description,
            priority="high",
            assigned_agent_id=agent.id,
            skip_review=True,
            extra_fields={"repo_id": repo.id},
        )
        state["task_id"] = str(task.id)
        await _write_state(session, state)
        await session.commit()
        created.append("task")

    if created:
        logger.info("first run: created %s", ", ".join(created))
    return StarterResult(
        board=board, repo=repo, agent=agent, task=task, runtime=runtime,
        created=created, new_agent_token=new_token,
    )


async def current(session: AsyncSession) -> StarterResult | None:
    """The parts of an earlier run, or None when it never ran (or is incomplete)."""
    state = await _read_state(session)
    parts = {
        "board": await _get(session, Board, state.get("board_id")),
        "repo": await _get(session, Repo, state.get("repo_id")),
        "agent": await _get(session, Agent, state.get("agent_id")),
        "task": await _get(session, Task, state.get("task_id")),
    }
    if any(v is None for v in parts.values()):
        return None
    agent = parts["agent"]
    runtime = await session.get(Runtime, agent.runtime_id) if agent.runtime_id else None
    return StarterResult(runtime=runtime, **parts)


def describe(result: StarterResult) -> dict:
    """The JSON the wizard's progress chain reads (no token, no paths)."""
    agent, runtime = result.agent, result.runtime
    return {
        "board": {"id": str(result.board.id), "name": result.board.name},
        "repo": {
            "id": str(result.repo.id),
            "full_name": result.repo.full_name,
            "source": result.repo.source,
        },
        "agent": {
            "id": str(agent.id),
            "name": agent.name,
            "emoji": agent.emoji,
            "harness": agent.harness,
            "provision_status": agent.provision_status,
            "status": agent.status,
        },
        "runtime": (
            {"id": str(runtime.id), "display_name": runtime.display_name}
            if runtime is not None else None
        ),
        "task": {
            "id": str(result.task.id),
            "title": result.task.title,
            "status": result.task.status,
        },
        "created": list(result.created),
    }


__all__ = [
    "STATE_KEY",
    "StarterError",
    "StarterResult",
    "current",
    "describe",
    "ensure_practice_repo_on_disk",
    "practice_repo_dir",
    "resolve_brain",
    "start",
]
