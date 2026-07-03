"""Claude Code CLI Runner — spawns Claude Code as a subprocess for tasks.

Claude Code (Opus 4.6) works directly in the filesystem instead of
via the OpenClaw Gateway. Ideal for complex coding tasks that need
full filesystem access.
"""

import asyncio
import logging
import uuid
from pathlib import Path

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.task import Task
from app.services.activity import emit_event
from app.utils import utcnow, create_tracked_task

logger = logging.getLogger("mc.claude_code")


async def dispatch_to_claude_code(
    agent: Agent,
    task: Task,
    message: str,
    session: AsyncSession,
) -> bool:
    """Spawns the Claude Code CLI as a subprocess for a task.

    Returns True if the process was started successfully.
    """
    prompt = _build_claude_code_prompt(task, agent, message)
    workspace = agent.workspace_path or str(Path(settings.home_host) / "Workspace")

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--print",
            "-p", prompt,
            "--allowedTools", "Edit,Write,Bash(read-only:false),Read,Glob,Grep",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("claude CLI not found — is Claude Code installed?")
        return False
    except Exception as e:
        logger.error("Failed to spawn claude CLI: %s", e)
        return False

    logger.info(
        "Claude Code spawned for task '%s' (pid=%s, workspace=%s)",
        task.title, proc.pid, workspace,
    )

    # Persist workspace path in DB
    task.workspace_path = workspace
    session.add(task)
    await session.commit()

    await emit_event(
        session, "task.claude_code_started",
        f"Claude Code gestartet fuer '{task.title}' (PID {proc.pid})",
        board_id=task.board_id, task_id=task.id, agent_id=agent.id,
    )

    # Monitoring in the background
    create_tracked_task(_monitor_claude_code(proc, task, agent))
    return True


def _build_claude_code_prompt(task: Task, agent: Agent, dispatch_message: str) -> str:
    """Builds the self-contained prompt for Claude Code.

    Contains task details, API curl commands for status updates, and context.
    """
    board_id = task.board_id
    task_id = task.id

    # Extract API token from agent.tools_md (stored there as a Bearer token)
    api_token = _extract_token_from_tools_md(agent.tools_md or "")

    return f"""# Task: {task.title}

## Beschreibung
{task.description or "Keine Beschreibung"}

## Kontext aus Mission Control
{dispatch_message}

## Status-Updates (PFLICHT)

Melde deinen Fortschritt an Mission Control zurueck:

### Task ACK (sofort am Anfang):
```bash
curl -s -X PATCH "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{task_id}" \\
  -H "Authorization: Bearer {api_token}" \\
  -H "Content-Type: application/json" \\
  -d '{{"status": "in_progress"}}'
```

### Progress-Kommentar (regelmaessig):
```bash
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments" \\
  -H "Authorization: Bearer {api_token}" \\
  -H "Content-Type: application/json" \\
  -d '{{"content": "**Update** — Was getan\\n**Evidence** — Dateipfade, Outputs\\n**Next** — Naechste Schritte", "comment_type": "progress"}}'
```

### Fertig — Resolution:
```bash
curl -s -X PATCH "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{task_id}" \\
  -H "Authorization: Bearer {api_token}" \\
  -H "Content-Type: application/json" \\
  -d '{{"status": "review"}}'
```
Danach Resolution-Kommentar:
```bash
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments" \\
  -H "Authorization: Bearer {api_token}" \\
  -H "Content-Type: application/json" \\
  -d '{{"content": "**Update** — Fertig: Was umgesetzt\\n**Evidence** — Dateipfade, Tests", "comment_type": "resolution"}}'
```

## Regeln
- Arbeite im Workspace: {agent.workspace_path or str(Path(settings.home_host) / "Workspace")}
- Git: Feature-Branches, nie direkt auf main
- Melde dich SOFORT mit ACK, dann arbeite selbststaendig
- Bei Blockierung: Status auf "blocked" setzen + Blocker-Kommentar
"""


def _extract_token_from_tools_md(tools_md: str) -> str:
    """Extracts the Bearer token from TOOLS.md."""
    import re
    match = re.search(r'Bearer\s+([A-Za-z0-9_-]+)', tools_md)
    return match.group(1) if match else "TOKEN-NICHT-GEFUNDEN"


async def _monitor_claude_code(
    proc: asyncio.subprocess.Process,
    task: Task,
    agent: Agent,
) -> None:
    """Monitors the Claude Code subprocess until it finishes."""
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=1800,  # 30 minutes max
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Claude Code timeout after 30min for task '%s' — killing",
            task.title,
        )
        proc.kill()
        await proc.wait()
        return

    rc = proc.returncode
    output = (stdout or b"").decode("utf-8", errors="replace")
    errors = (stderr or b"").decode("utf-8", errors="replace")

    if rc == 0:
        logger.info(
            "Claude Code finished successfully for task '%s' (output: %d chars)",
            task.title, len(output),
        )
    else:
        logger.warning(
            "Claude Code exited with code %d for task '%s': %s",
            rc, task.title, errors[:500],
        )
