"""Defense-in-Depth: Periodischer Check + Auto-Privat-Setzung fuer MC-GitHub-Repos.

Hintergrund: Agents rufen manchmal `gh repo create` direkt auf ohne `--private`.
GitHub-Default ist public → Code leakt. Dieser Monitor scanned alle 5 Minuten
die Repos des Operators und setzt MC-Tasks-Repos (Prefix `mc-task-`, `mc-`,
`t2-`, etc. — siehe MC_OWNED_REPO_PREFIXES) die public sind auf private —
ausser sie sind ein Fork.

Fallback wenn SOUL-Regeln nicht greifen oder Agent sich nicht dran haelt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from app.services.git_service import GITHUB_OWNER

logger = logging.getLogger("mc.github_visibility_monitor")

# Repo-Namen die als MC-owned gelten und privat sein sollten.
# Fork-Repos werden NIE angefasst (GitHub blockiert private forks of public repos).
# Erweiterbar via MC_OWNED_REPO_PREFIXES (comma-separated) in .env.
MC_OWNED_PREFIXES = tuple(
    p.strip()
    for p in os.environ.get("MC_OWNED_REPO_PREFIXES", "mc-,mc-task-,t2-").split(",")
    if p.strip()
)

CHECK_INTERVAL_SECONDS = 300  # 5 Minuten


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


async def _list_public_mc_repos(owner: str = GITHUB_OWNER) -> list[dict]:
    """Listet alle public non-fork Repos die MC-owned-Prefix matchen."""
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
    """Einmaliger Check. Returns: Anzahl auto-privatisierter Repos."""
    public = await _list_public_mc_repos()
    if not public:
        return 0
    count = 0
    for repo in public:
        ok = await _set_private(GITHUB_OWNER, repo["name"])
        if ok:
            count += 1
            # Optional: Telegram-Alert senden damit der Operator es mitbekommt
            try:
                from app.services.telegram_reports import telegram_reports
                if telegram_reports.configured:
                    await telegram_reports.send(
                        f"⚠️ <b>Security-Alert</b> · Repo auto-privatisiert\n\n"
                        f"<code>{GITHUB_OWNER}/{repo['name']}</code> war PUBLIC — Defense-in-Depth-Monitor hat "
                        f"es auf private gesetzt.\n\n"
                        f"Ursache wahrscheinlich: Agent hat <code>gh repo create</code> ohne "
                        f"<code>--private</code> aufgerufen. SOUL-Regel greift jetzt."
                    )
            except Exception as e:
                logger.debug("Telegram-alert skipped: %s", e)
    return count


async def run_forever() -> None:
    """Background-Loop der periodisch prueft. Gestartet aus app/main.py lifespan."""
    if not GITHUB_OWNER:
        # Security-Monitor kann ohne Owner nichts pruefen — einmalig sichtbar
        # machen statt alle 5 Minuten still zu failen (gh repo list "").
        logger.warning(
            "github_visibility_monitor: GITHUB_OWNER nicht gesetzt — "
            "Monitor pausiert (Defense-in-Depth fuer public Repos inaktiv)."
        )
        return
    logger.info(
        "github_visibility_monitor: Starte Loop (Intervall %ds)",
        CHECK_INTERVAL_SECONDS,
    )
    while True:
        try:
            n = await check_once()
            if n > 0:
                logger.warning("Auto-privatisiert: %d Repo(s)", n)
        except Exception as e:
            logger.error("github_visibility_monitor cycle failed: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
