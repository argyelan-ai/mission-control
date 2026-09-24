"""Write a run folder + spool request (docs/specs/head-launcher.md §6.1, §6.3).

The backend never starts a process on the host. It writes
``spec.json`` (no secrets), ``job.md``, ``procedure.md`` and ``head.env``
(0600, provider env only) and drops ``spool/<run_id>.<action>.json`` with
``action`` + run ids. The host watcher validates everything again.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.repo import Repo
from app.models.runtime import Runtime
from app.models.task import Task
from app.services.heads import paths, scratch
from app.services.runtime_protocols import engine_root

TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "heads" / "head-AGENTS.md"

# A secret-carrying field name (…_key, token, secret, password). "box_keys"
# (host ids) is plural and does not match.
SECRETISH = re.compile(r"(^|_)(api_?key|key|token|secret|password|passwd)$", re.I)
#: Keys head.env may carry (mirrors HEAD_ENV_KEYS in scripts/head/mc-head).
HEAD_ENV_KEYS = frozenset({
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY",
})
#: Placeholder key for Claude Code on a local engine — under --bare only
#: ANTHROPIC_API_KEY (or apiKeyHelper) authenticates; the engine ignores it.
LOCAL_PLACEHOLDER_KEY = "local-engine-no-key"


class SpoolUnavailable(Exception):
    pass


def _slug(text: str, limit: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:limit].strip("-") or "job")


def branch_name(title: str, run_id: str, when: datetime) -> str:
    # "mc-head/" (not "head/"): refs/remotes/origin/head/… would collide with
    # origin/HEAD on a case-insensitive disk (see scripts/head/mc-head).
    return f"mc-head/{when:%Y-%m-%d}-{_slug(title)}-{run_id[:4]}"


async def head_env(session: AsyncSession, harness: str, runtime: Runtime) -> tuple[dict[str, str], str, str]:
    """(head.env, base_url, model) for a pair.

    omp reuses the fleet's ``build_runtime_env`` with a transient, never
    saved agent — one source for URL + model. Claude Code on a local engine
    gets the Anthropic variables pointed at the engine root (spec §4) — built
    here so the fleet's claude branch stays untouched.
    """
    from app.routers.internal import build_runtime_env

    transient = Agent(name=f"head-{harness}", harness="omp")  # never added to the session
    fleet_env = await build_runtime_env(runtime, session, transient)
    base_url = fleet_env.get("OPENAI_BASE_URL") or runtime.endpoint
    model = fleet_env.get("OPENAI_MODEL") or runtime.model_identifier or ""
    env: dict[str, str] = {}
    if harness == "claude":
        env = {
            "ANTHROPIC_BASE_URL": engine_root(base_url),
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "ANTHROPIC_API_KEY": LOCAL_PLACEHOLDER_KEY,
        }
    return env, base_url, model


def render_procedure(values: dict) -> str:
    text = TEMPLATE.read_text()
    # Drop the maintainer comment block — it is for humans reading the repo.
    text = re.sub(r"<!--.*?-->\n*", "", text, count=1, flags=re.S)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


#: Job-text block for a scratch repo whose origin is a local bare repo.
SCRATCH_LOCAL_ORIGIN_NOTE = (
    "## Scratch repo — push only\n\n"
    "This is a scratch repo whose origin is a local bare repo: no pull request is possible here.\n"
    "In step 6 run `git push -u origin {branch}` and skip `gh pr create`.\n"
    "The pushed branch plus the run record with `Status: passed` is the result — "
    "do not mark the run failed because there is no PR. In the run record write "
    "`PR: none (scratch repo, branch pushed)`."
)


def render_job(
    task: Task, *, answer: str | None, previous: dict | None,
    scratch_local_origin: bool = False, branch: str | None = None,
) -> str:
    parts = [f"# {task.title}", ""]
    if task.description:
        parts += [task.description.strip(), ""]
    if scratch_local_origin:
        parts += [SCRATCH_LOCAL_ORIGIN_NOTE.format(branch=branch or "<branch>"), ""]
    if previous:
        parts += [
            "## Previous run",
            f"- Pair: {previous.get('pair')}",
            f"- Result: {previous.get('state')} {('(' + previous['reason'] + ')') if previous.get('reason') else ''}".rstrip(),
            f"- Branch: `{previous.get('branch')}` — run `git log origin/{previous.get('base')}..HEAD`"
            " to see what is already done. Git and the run record are the memory; chat history does not carry over.",
            "",
        ]
        if previous.get("run_record"):
            parts += ["### Run record so far", "", previous["run_record"].strip(), ""]
        if previous.get("question"):
            parts += ["### Open question", "", previous["question"].strip(), ""]
    if answer:
        parts += ["## Operator answer", "", answer.strip(), ""]
    return "\n".join(parts).rstrip() + "\n"


def _atomic(path: Path, text: str, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    tmp.replace(path)


def _format_env(env: dict[str, str]) -> str:
    lines = []
    for key in sorted(env):
        if key not in HEAD_ENV_KEYS:
            raise ValueError(f"head.env key not allowed: {key}")
        value = env[key]
        if "\n" in value or "'" in value:
            raise ValueError(f"head.env value for {key} is not single-line")
        lines.append(f"{key}='{value}'")
    return "\n".join(lines) + ("\n" if lines else "")


class TaskStartBusy(Exception):
    """Another start/restart for the same task is in flight."""


#: A start lock older than this belongs to a crashed request and is taken over.
TASK_LOCK_STALE_S = 120


def acquire_task_lock(task_id: str) -> Path:
    """One start/restart per task at a time (two tabs, two API calls).

    O_EXCL marker in heads_root/task-locks/: the check for an active run and
    writing the new run folder form one critical section. A marker older
    than TASK_LOCK_STALE_S is left over from a crash and is taken over.
    Raises TaskStartBusy; release with release_task_lock().
    """
    ldir = paths.heads_root() / "task-locks"
    ldir.mkdir(parents=True, exist_ok=True)
    marker = ldir / str(uuid.UUID(str(task_id)))
    for _ in range(2):
        try:
            os.close(os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return marker
        except FileExistsError:
            try:
                age = time.time() - marker.stat().st_mtime
            except OSError:
                continue
            if age < TASK_LOCK_STALE_S:
                raise TaskStartBusy(task_id)
            marker.unlink(missing_ok=True)
    raise TaskStartBusy(task_id)


def release_task_lock(marker: Path) -> None:
    marker.unlink(missing_ok=True)


def discard_run(run_id: str) -> None:
    """Remove a run folder whose spool request never went out."""
    shutil.rmtree(paths.run_dir(run_id), ignore_errors=True)


def spool(action: str, run_id: str, from_run_id: str | None = None) -> Path:
    sdir = paths.spool_dir()
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        payload = {"action": action, "run_id": run_id}
        if from_run_id:
            payload["from_run_id"] = from_run_id
        target = sdir / f"{run_id}.{action}.json"
        _atomic(target, json.dumps(payload))
    except OSError as exc:
        raise SpoolUnavailable(str(exc)) from exc
    return target


async def write_run(
    session: AsyncSession,
    *,
    task: Task,
    repo: Repo,
    harness: str,
    runtime: Runtime,
    box_keys: list[str],
    user_id: str | None,
    answer: str | None = None,
    restarted_from: dict | None = None,
    mode: str = "fresh",
) -> dict:
    """Create the run folder. ``restarted_from`` = previous run's spec +
    derived state (for continue mode and the previous-run block)."""
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    env, base_url, model = await head_env(session, harness, runtime)
    local = runtime.host_id is not None
    limit = settings.heads_time_limit_local_s if local else settings.heads_time_limit_cloud_s
    if restarted_from and mode == "continue":
        branch = restarted_from["spec"]["branch"]
    else:
        branch = branch_name(task.title, run_id, now)
    spec = {
        "run_id": run_id,
        "task_id": str(task.id),
        "title": task.title[:200],
        "repo_full_name": repo.full_name,
        "base_branch": repo.default_branch or "main",
        "branch": branch,
        "harness": harness,
        "runtime_slug": runtime.slug,
        "model": model,
        "base_url": base_url,
        "box_keys": box_keys,
        "recipe_slug": None,
        "time_limit_s": int(limit),
        "restarted_from": restarted_from["spec"]["run_id"] if restarted_from else None,
        "mode": mode,
        "job_folder": None,  # set below
        "created_by": user_id,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    bad = [k for k in spec if SECRETISH.search(k)]
    if bad:  # pragma: no cover — structural guard
        raise ValueError(f"secret-looking field in spec.json: {bad}")

    folder = paths.run_dir(run_id)
    folder.mkdir(parents=True, exist_ok=False)
    short = _slug(task.title, 24)
    job_dir = paths.vault_jobs_dir() / f"{now:%Y-%m-%d}-{short}-{run_id[:4]}"
    spec["job_folder"] = job_dir.name
    previous = None
    if restarted_from:
        prev_spec = restarted_from["spec"]
        previous = {
            "pair": f"{prev_spec.get('harness')} × {prev_spec.get('runtime_slug')}",
            "state": restarted_from.get("state"),
            "reason": restarted_from.get("reason"),
            "branch": prev_spec.get("branch"),
            "base": prev_spec.get("base_branch"),
            "run_record": restarted_from.get("run_record") if mode == "continue" else None,
            "question": restarted_from.get("question"),
        }
    procedure = render_procedure({
        "run_dir": folder,
        "worktree": folder / "wt",
        "branch": branch,
        "base_branch": spec["base_branch"],
        "harness": harness,
        "runtime": f"{runtime.display_name} ({model})",
        "time_limit": f"{limit // 60} min",
        "vault_job_dir": job_dir,
        "test_command": "see the repo's AGENTS.md / CLAUDE.md / CONTRIBUTING (use the documented test command)",
        "lint_command": "see the repo's contributor docs (skip if none)",
        "privacy_command": "python3 scripts/privacy-scan.py (only if the repo has it)",
        "date": f"{now:%Y-%m-%d}",
        "short_name": short,
        "run_id": run_id,
        "task_id": str(task.id),
        "repo": repo.full_name,
    })
    _atomic(folder / "spec.json", json.dumps(spec, indent=1))
    job = render_job(
        task, answer=answer, previous=previous, branch=branch,
        # git subprocess — off the event loop
        scratch_local_origin=await asyncio.to_thread(scratch.local_origin, repo.full_name) is not None,
    )
    _atomic(folder / "job.md", job)
    _atomic(folder / "procedure.md", procedure)
    _atomic(folder / "head.env", _format_env(env), mode=0o600)
    return spec
