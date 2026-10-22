"""Re-render ONE host agent's agent.env from its runtime binding — now.

Why this exists
---------------
MC re-renders a host agent's ``agent.env`` only when the agent is flagged for
a model sync (runtime switch, model/window drift, ``pending_runtime_sync``).
A deploy that adds a NEW key to the rendered env — e.g.
``MC_THINKING_TEMPLATE_KWARG`` for Hermes' thinking-off switch — therefore
never reaches a host agent that keeps running on the same runtime: nothing
flags it, so its agent.env (and everything rendered from it, like Hermes'
``~/.hermes/config.yaml``) stays on the old shape until the next runtime
change. This script runs the very same sync (``sync_host_agent_model``) for
one agent on demand.

What it touches
---------------
* Only the agent's ``agent.env`` (and, for a host omp agent, its
  ``models.yml``), and only when the bytes change. ``MC_AGENT_TOKEN`` and every
  other key are kept — this is the model-drift sync, not a re-provision.
* No database write.
* A changed ``agent.env`` restarts a bridge that watches it (launchd
  ``WatchPaths``), which cuts off a running turn. The script therefore
  refuses while the agent has a task in progress (``--force`` overrides).
* Prints key NAMES only, never values (they carry the agent token).

Usage (backend container)::

    docker compose -p mission-control exec -u mcuser backend python -m scripts.sync_host_agent_env hermes --dry-run
    docker compose -p mission-control exec -u mcuser backend python -m scripts.sync_host_agent_env hermes

Exit codes: 0 done (or nothing to change), 2 refused (unknown agent, not a
host agent, no runtime, task in progress).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.models.task import Task


class Refused(Exception):
    """The sync must not run for this agent (exit code 2)."""


async def sync_one(
    session: AsyncSession, slug: str, *, dry_run: bool, force: bool = False
) -> list[str]:
    """Sync ``slug``'s agent.env; return the changed key names."""
    from app.services.harness_compat import derive_harness
    from app.services.host_harness_adapter import get_adapter, sync_host_agent_model

    agent = (await session.exec(select(Agent).where(Agent.slug == slug))).first()
    if agent is None:
        raise Refused(f"no agent with slug {slug!r}")
    if agent.agent_runtime != "host":
        raise Refused(f"{slug}: agent_runtime is {agent.agent_runtime!r}, not 'host'")
    if agent.runtime_id is None:
        raise Refused(f"{slug}: no runtime binding — nothing to render")
    runtime = await session.get(Runtime, agent.runtime_id)
    if runtime is None:
        raise Refused(f"{slug}: runtime {agent.runtime_id} not found")
    if get_adapter(agent.harness or derive_harness(runtime)) is None:
        raise Refused(f"{slug}: no host adapter for harness {agent.harness!r}")

    if not dry_run and not force:
        busy = (
            await session.exec(
                select(Task.id).where(
                    Task.assigned_agent_id == agent.id, Task.status == "in_progress"
                )
            )
        ).first()
        if busy is not None:
            raise Refused(
                f"{slug}: task {busy} is in progress — a changed agent.env restarts "
                "the bridge and cuts it off (wait, or pass --force)"
            )

    return await sync_host_agent_model(agent, runtime, session=session, write=not dry_run)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slug", help="agent slug, e.g. hermes")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument("--force", action="store_true", help="sync even while a task is in progress")
    args = parser.parse_args(argv)

    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            changed = await sync_one(session, args.slug, dry_run=args.dry_run, force=args.force)
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2

    verb = "would change" if args.dry_run else "changed"
    if changed:
        print(f"{args.slug}: agent.env {verb}: {', '.join(changed)}")
    else:
        print(f"{args.slug}: agent.env already current — nothing to write")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
