"""Henry-Sunset Migration: Reassign tasks -> Boss, delete Henry, promote Boss.

Phase 28 of v0.9 OpenClaw Gateway Sunset. Single operator-facing
wrapper around Alembic migration 0122_henry_sunset_boss_promotion.

Usage (im backend container):
    docker compose exec backend python -m scripts.mc_henry_sunset
    docker compose exec backend python -m scripts.mc_henry_sunset --dry-run
    docker compose exec backend python -m scripts.mc_henry_sunset --commit

--dry-run (Default): Inventory Henry's footprint + render Markdown
                     report + post to DISCORD_WEBHOOK_OPS. ZERO DB
                     changes. The operator reviews the report in Discord
                     before approving commit.

--commit:            Run `alembic upgrade head` (which executes
                     migrations 0121 + 0122) + post Discord
                     confirmation. Atomic transaction inside the
                     migration; this wrapper just kicks Alembic and
                     reports the result.

Voraussetzung: `./backup.sh` MANUAL Pre-Step before --commit
(CONTEXT.md D-04). Script does NOT auto-run backup to avoid masking
backup failures.

Schema-Note: agents table uses `name` (NOT `slug`); operational health
lives on `status` (NOT `is_active`). See Plan 28-02 SUMMARY for the
rationale — same substitution applies in this script's footprint queries.

Implements CONTEXT.md D-01, D-02, D-03, D-04.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from dataclasses import dataclass

from app.services.discord import send_discord_notification


@dataclass
class HenryFootprint:
    """Snapshot of every DB row that points at Henry."""

    henry_id: str | None
    boss_id: str | None
    tasks_assigned: list[tuple[str, str, str]]      # (id, title, status)
    tasks_callback: list[tuple[str, str, str]]
    tasks_owner: list[tuple[str, str, str]]
    comments_to_null: int
    events_to_null: int
    current_task_holders: list[tuple[str, str]]     # (agent_name, task_id)
    boss_state: dict | None                          # provision_status, status, scopes_count
    henry_discord_channel_id: str | None
    non_henry_discord_channels: int


async def _gather_footprint() -> HenryFootprint:
    """Inventory every Henry-pointer for the dry-run report.

    Uses agents.name (NOT slug) per Wave 2 schema discovery — the agents
    table has no `slug` column. Uses agents.status (NOT is_active) for
    operational health.
    """
    from app.database import engine
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlalchemy import text as sa_text

    async with AsyncSession(engine, expire_on_commit=False) as s:
        henry_row = (await s.exec(sa_text(
            "SELECT id FROM agents WHERE name = 'Henry'"
        ))).first()
        henry_id = str(henry_row[0]) if henry_row else None

        boss_row = (await s.exec(sa_text(
            "SELECT id, provision_status, status, scopes, discord_channel_id "
            "FROM agents WHERE name = 'Boss'"
        ))).first()
        if boss_row is None:
            return HenryFootprint(
                henry_id=henry_id, boss_id=None,
                tasks_assigned=[], tasks_callback=[], tasks_owner=[],
                comments_to_null=0, events_to_null=0,
                current_task_holders=[], boss_state=None,
                henry_discord_channel_id=None,
                non_henry_discord_channels=0,
            )

        boss_id = str(boss_row[0])
        scopes_raw = boss_row[3] or []
        # Cross-engine: Postgres JSONB returns list, SQLite TEXT returns string.
        if isinstance(scopes_raw, str):
            import json as _json
            try:
                scopes = _json.loads(scopes_raw)
            except (ValueError, TypeError):
                scopes = []
        else:
            scopes = scopes_raw

        boss_state = {
            "provision_status": boss_row[1],
            "status": boss_row[2],
            "scopes_count": len(scopes) if scopes else 0,
            "scopes_meaning": (
                "ALL_SCOPES (NULL/empty == 16)" if not scopes
                else f"{len(scopes)}/16"
            ),
        }

        if not henry_id:
            # No Henry — migration is a no-op for everything except
            # confirming Boss state.
            non_henry_channels = (await s.exec(sa_text(
                "SELECT count(*) FROM agents "
                "WHERE discord_channel_id IS NOT NULL AND name != 'Henry'"
            ))).scalar_one()
            return HenryFootprint(
                henry_id=None, boss_id=boss_id,
                tasks_assigned=[], tasks_callback=[], tasks_owner=[],
                comments_to_null=0, events_to_null=0,
                current_task_holders=[], boss_state=boss_state,
                henry_discord_channel_id=None,
                non_henry_discord_channels=non_henry_channels,
            )

        async def _inventory_tasks(column: str) -> list[tuple[str, str, str]]:
            # `column` is a hard-coded literal from a 3-element list below.
            # NEVER user input — safe to f-string. STRIDE T-28-03-02.
            rows = (await s.exec(sa_text(
                f"SELECT id, title, status FROM tasks "  # noqa: S608 - column is hard-coded
                f"WHERE {column} = CAST(:hid AS uuid)"
            ).bindparams(hid=henry_id))).all()
            return [(str(r[0]), r[1] or "", r[2] or "") for r in rows]

        tasks_assigned = await _inventory_tasks("assigned_agent_id")
        tasks_callback = await _inventory_tasks("callback_agent_id")
        tasks_owner = await _inventory_tasks("owner_agent_id")

        comments_to_null = (await s.exec(sa_text(
            "SELECT count(*) FROM task_comments "
            "WHERE author_agent_id = CAST(:hid AS uuid)"
        ).bindparams(hid=henry_id))).scalar_one()

        events_to_null = (await s.exec(sa_text(
            "SELECT count(*) FROM activity_events "
            "WHERE agent_id = CAST(:hid AS uuid)"
        ).bindparams(hid=henry_id))).scalar_one()

        current_holders = (await s.exec(sa_text(
            "SELECT name, current_task_id FROM agents "
            "WHERE current_task_id IN ("
            "  SELECT id FROM tasks WHERE assigned_agent_id = CAST(:hid AS uuid)"
            ") AND name != 'Henry'"
        ).bindparams(hid=henry_id))).all()

        henry_discord_channel = (await s.exec(sa_text(
            "SELECT discord_channel_id FROM agents WHERE name = 'Henry'"
        ))).first()
        henry_dc = (
            str(henry_discord_channel[0])
            if henry_discord_channel and henry_discord_channel[0]
            else None
        )
        non_henry_channels = (await s.exec(sa_text(
            "SELECT count(*) FROM agents "
            "WHERE discord_channel_id IS NOT NULL AND name != 'Henry'"
        ))).scalar_one()

        return HenryFootprint(
            henry_id=henry_id, boss_id=boss_id,
            tasks_assigned=tasks_assigned,
            tasks_callback=tasks_callback,
            tasks_owner=tasks_owner,
            comments_to_null=comments_to_null,
            events_to_null=events_to_null,
            current_task_holders=[(str(r[0]), str(r[1])) for r in current_holders],
            boss_state=boss_state,
            henry_discord_channel_id=henry_dc,
            non_henry_discord_channels=non_henry_channels,
        )


def _render_dry_run_md(fp: HenryFootprint) -> str:
    """Markdown report per CONTEXT.md D-02.

    Pre-Flight verdict is informational only — the authoritative gates run
    inside migration 0122. This block tells the operator what the migration WILL
    check at commit time.
    """
    lines: list[str] = [
        "## Henry-Sunset - Dry-Run Report",
        "",
        f"**Phase 28** - Henry (`{fp.henry_id or 'NOT FOUND'}`) -> Boss (`{fp.boss_id or 'NOT FOUND'}`)",
        "",
        "### Pre-Flight Check",
    ]
    if fp.boss_state is None:
        lines.append("- ABORT Boss agent not found - cannot promote a non-existent agent.")
        lines.append("- **MIGRATION WILL ABORT** on Pre-Flight Check.")
    else:
        b = fp.boss_state
        ok_status = "OK" if b["provision_status"] == "provisioned" else "FAIL"
        ok_active = "OK" if b["status"] != "error" else "FAIL"
        ok_scopes = "OK" if b["scopes_count"] == 0 or b["scopes_count"] == 16 else "WARN"
        lines.append(f"- {ok_status} Boss provision_status: `{b['provision_status']}`")
        lines.append(f"- {ok_active} Boss status: `{b['status']}`")
        lines.append(f"- {ok_scopes} Boss scopes: {b['scopes_meaning']}")

    total_tasks = len(fp.tasks_assigned) + len(fp.tasks_callback) + len(fp.tasks_owner)
    lines += [
        "",
        f"### Tasks to reassign to Boss ({len(fp.tasks_assigned)} assigned, "
        f"{len(fp.tasks_callback)} callback, {len(fp.tasks_owner)} owner = "
        f"{total_tasks} pointer-updates)",
    ]

    for label, tasks in [
        ("Assigned", fp.tasks_assigned),
        ("Callback", fp.tasks_callback),
        ("Owner", fp.tasks_owner),
    ]:
        if not tasks:
            continue
        lines += [
            "",
            f"#### {label} ({len(tasks)})",
            "| Task ID | Title | Status |",
            "|---------|-------|--------|",
        ]
        for tid, title, status in tasks[:20]:  # Cap at 20 per table for Discord
            title_short = (title or "<no title>")[:50]
            lines.append(f"| `{tid[:8]}` | {title_short} | {status} |")
        if len(tasks) > 20:
            lines.append(f"| ... | ({len(tasks) - 20} more rows truncated) | |")

    lines += [
        "",
        "### Historical data preserved",
        f"- task_comments to be SET NULL: **{fp.comments_to_null}**",
        f"- activity_events to be SET NULL: **{fp.events_to_null}**",
        "",
        "### Discord channels",
        f"- Henry discord_channel_id: `{fp.henry_discord_channel_id or '(none)'}` "
        f"(will be **unbound** when Henry row is deleted)",
        f"- Other agents with discord_channel_id: **{fp.non_henry_discord_channels}** "
        f"(unaffected - D-16)",
    ]

    if fp.current_task_holders:
        lines += [
            "",
            f"### WARNING Cross-agent current_task_id pointers ({len(fp.current_task_holders)})",
            "These agents have current_task_id pointing at a Henry-assigned task:",
        ]
        for name, tid in fp.current_task_holders:
            lines.append(f"- `{name}` -> task `{tid[:8]}`")
        lines.append("(Their `current_task_id` is left intact by migration 0122; only Henry's current_task_id is cleared via reassignment.)")

    lines += [
        "",
        "### Next step",
        "1. The operator reviews this report.",
        "2. `./backup.sh` (manual Pre-Step, CONTEXT.md D-04).",
        "3. `docker compose exec backend python -m scripts.mc_henry_sunset --commit`",
        "",
        "_This is a DRY RUN - no database changes have been made._",
    ]

    return "\n".join(lines)


async def _dry_run() -> int:
    """Inventory + Discord report, ZERO DB mutations (D-02)."""
    fp = await _gather_footprint()
    report_md = _render_dry_run_md(fp)
    print(report_md)
    print("")
    print("=" * 70)
    print("Posting to Discord OPS webhook...")

    # Discord embed description is capped at ~4096 chars. Truncate safely
    # if the report grows past the limit on a busy DB.
    description = report_md
    if len(description) > 3800:
        description = description[:3800] + "\n\n_...(truncated; full report above)..._"

    await send_discord_notification(
        title="Henry-Sunset - Dry-Run Report",
        description=description,
        severity="warning",   # warning=amber color stands out in OPS channel
        fields=[
            {
                "name": "Tasks to reassign",
                "value": str(
                    len(fp.tasks_assigned)
                    + len(fp.tasks_callback)
                    + len(fp.tasks_owner)
                ),
                "inline": True,
            },
            {
                "name": "Comments -> NULL",
                "value": str(fp.comments_to_null),
                "inline": True,
            },
            {
                "name": "Events -> NULL",
                "value": str(fp.events_to_null),
                "inline": True,
            },
            {
                "name": "Mode",
                "value": "--dry-run (no changes made)",
                "inline": False,
            },
        ],
    )
    print("Posted. Awaiting the operator's go-ahead for --commit.")
    return 0


async def _commit() -> int:
    """Run alembic upgrade head + post Discord confirmation (D-03/D-04).

    Pre-Flight runs INSIDE migration 0122 (raises RuntimeError before any
    mutation). This wrapper just kicks Alembic via subprocess and reports
    the result back to Discord OPS.
    """
    # argv-list form (not shell=True) prevents injection. STRIDE T-28-03-01.
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd="/app",   # backend Docker WORKDIR
    )

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-1500:]
        await send_discord_notification(
            title="Henry-Sunset - COMMIT FAILED",
            description=(
                "```\n" + stderr_tail + "\n```\n\n"
                "Migration aborted. DB state is unchanged "
                "(transaction rolled back). Investigate the error "
                "above, then re-run --commit when fixed."
            ),
            severity="critical",
        )
        print(result.stderr, file=sys.stderr)
        return 1

    # Success - gather post-state for the confirmation embed.
    fp = await _gather_footprint()
    await send_discord_notification(
        title="Henry-Sunset - Migration applied",
        description=(
            "`alembic upgrade head` ran cleanly. Boss is now Board Lead. "
            "Henry has been removed from the DB."
        ),
        severity="warning",   # warning=amber stands out in OPS
        fields=[
            {
                "name": "Henry rows remaining",
                "value": "0" if fp.henry_id is None else "NON-ZERO (CHECK MANUALLY)",
                "inline": True,
            },
            {
                "name": "Boss provisioned",
                "value": (
                    "OK"
                    if fp.boss_state and fp.boss_state["provision_status"] == "provisioned"
                    else "WARN (check manually)"
                ),
                "inline": True,
            },
            {
                "name": "Non-Henry Discord channels intact",
                "value": str(fp.non_henry_discord_channels),
                "inline": True,
            },
        ],
    )
    print("Migration applied successfully. Discord OPS notified.")
    return 0


async def main(*, dry_run: bool) -> int:
    if dry_run:
        return await _dry_run()
    return await _commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Henry-Sunset migration wrapper (Phase 28 of v0.9)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Inventory + Discord report; no DB changes. (Default.)",
    )
    mode.add_argument(
        "--commit", action="store_true",
        help="Run `alembic upgrade head` + Discord confirmation. "
             "Run `./backup.sh` first.",
    )
    args = parser.parse_args()
    # --commit overrides default --dry-run via the explicit boolean.
    dry_run = not args.commit

    try:
        exit_code = asyncio.run(main(dry_run=dry_run))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        sys.exit(130)
