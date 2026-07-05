"""Defense-in-depth: periodic check + auto-privatization for MC GitHub repos.

Background: agents sometimes call `gh repo create` directly without
`--private`. GitHub's default is public → code leaks. This monitor scans
the operator's repos every 5 minutes and sets MC task repos (prefix
`mc-task-`, `mc-`, `t2-`, etc. — see MC_OWNED_REPO_PREFIXES) that are
public to private — unless they are a fork.

Fallback for when SOUL rules don't apply or the agent doesn't follow them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from app.services.github_config import get_github_owner

logger = logging.getLogger("mc.github_visibility_monitor")

# Repo names that count as MC-owned and should be private.
# Fork repos are NEVER touched (GitHub blocks private forks of public repos).
# Extensible via MC_OWNED_REPO_PREFIXES (comma-separated) in .env.
MC_OWNED_PREFIXES = tuple(
    p.strip()
    for p in os.environ.get("MC_OWNED_REPO_PREFIXES", "mc-,mc-task-,t2-").split(",")
    if p.strip()
)

CHECK_INTERVAL_SECONDS = 300  # 5 minutes


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


async def _list_public_mc_repos(owner: str) -> list[dict]:
    """Lists all public non-fork repos that match an MC-owned prefix."""
    rc, out, err = await _run(
        "gh", "repo", "list", owner,
        "--limit", "200",
        "--visibility", "public",
        "--json", "name,visibility,isFork,createdAt",
    )
    if rc != 0:
        logger.warning("gh repo list failed: %s", err)
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logger.warning("gh repo list: JSON parse failed")
        return []
    return [
        r for r in data
        if not r.get("isFork", False)
        and any(r["name"].startswith(p) for p in MC_OWNED_PREFIXES)
    ]


async def _set_private(owner: str, repo_name: str) -> bool:
    full = f"{owner}/{repo_name}"
    rc, _, err = await _run(
        "gh", "repo", "edit", full,
        "--visibility", "private",
        "--accept-visibility-change-consequences",
    )
    if rc == 0:
        logger.warning(
            "Repo %s wurde AUTO-PRIVATISIERT (Defense-in-Depth, Agent hat --private vergessen)",
            full,
        )
        return True
    logger.error("Auto-privatize failed for %s: %s", full, err)
    return False


async def check_once() -> int:
    """One-off check. Returns: number of auto-privatized repos."""
    owner = await get_github_owner()
    if not owner:
        return 0
    public = await _list_public_mc_repos(owner)
    if not public:
        return 0
    count = 0
    for repo in public:
        ok = await _set_private(owner, repo["name"])
        if ok:
            count += 1
            # Optional: send a Telegram alert so the operator notices
            try:
                from app.services.telegram_reports import telegram_reports
                if telegram_reports.configured:
                    await telegram_reports.send(
                        f"⚠️ <b>Security-Alert</b> · Repo auto-privatisiert\n\n"
                        f"<code>{owner}/{repo['name']}</code> war PUBLIC — Defense-in-Depth-Monitor hat "
                        f"es auf private gesetzt.\n\n"
                        f"Ursache wahrscheinlich: Agent hat <code>gh repo create</code> ohne "
                        f"<code>--private</code> aufgerufen. SOUL-Regel greift jetzt."
                    )
            except Exception as e:
                logger.debug("Telegram-alert skipped: %s", e)
    return count


async def run_forever() -> None:
    """Background loop that checks periodically. Started from app/main.py lifespan.

    Keeps looping even while no owner is configured (warns once): since
    ADR-055 the owner can be set live via Settings → GitHub, and the
    monitor must pick that up without a backend restart.
    """
    logger.info(
        "github_visibility_monitor: Starte Loop (Intervall %ds)",
        CHECK_INTERVAL_SECONDS,
    )
    warned_unconfigured = False
    while True:
        try:
            owner = await get_github_owner()
            if not owner:
                if not warned_unconfigured:
                    logger.warning(
                        "github_visibility_monitor: kein GitHub-Owner konfiguriert — "
                        "Monitor wartet (Defense-in-Depth fuer public Repos inaktiv)."
                    )
                    warned_unconfigured = True
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue
            warned_unconfigured = False
            n = await check_once()
            if n > 0:
                logger.warning("Auto-privatisiert: %d Repo(s)", n)
        except Exception as e:
            logger.error("github_visibility_monitor cycle failed: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
