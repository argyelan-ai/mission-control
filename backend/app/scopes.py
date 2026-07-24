"""
Scope-based permission system for agent skills.

Each agent has a list of scopes that determine which API
endpoints it may use and which sections are generated in
its TOOLS.md.
"""
from enum import StrEnum

from fastapi import Depends, HTTPException, status

from app.auth import require_agent


class Scope(StrEnum):
    TASKS_READ = "tasks:read"
    TASKS_WRITE = "tasks:write"
    TASKS_CREATE = "tasks:create"
    TASKS_MANAGE = "tasks:manage"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    APPROVALS_CREATE = "approvals:create"
    CHAT_WRITE = "chat:write"
    AGENTS_MANAGE = "agents:manage"
    CONTENT_SUBMIT = "content:submit"
    HEARTBEAT = "heartbeat"
    DEPLOY_EXECUTE = "deploy:execute"
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    TASKS_HELP = "tasks:help"
    CREDENTIALS_READ = "credentials:read"
    VAULT_READ = "vault:read"
    VAULT_WRITE = "vault:write"
    # Phase C vault-as-brain: lets the Voice-Agent push a vault file (PDF,
    # screenshot, document wrapper) to the operator's Telegram via the existing
    # telegram_reports.send_document() pipeline. Granted to LEAD + a future
    # voice template; kept narrow because it can shovel any file from the
    # vault onto the operator's phone.
    TELEGRAM_SEND = "telegram:send"


class AgentRole(StrEnum):
    LEAD = "lead"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    TESTER = "tester"
    PLANNER = "planner"
    RESEARCHER = "researcher"
    DEPLOYER = "deployer"
    WRITER = "writer"
    ORCHESTRATOR = "orchestrator"
    RELAY = "relay"        # legacy relay agent — OpenClaw Gateway / Telegram (retired, ADR-039)


# Role groups for dispatch logic
# RELAY intentionally absent: gateway/relay runtime never receives dispatched tasks
# and has no session to watch — watchdog skips it on both sides.
WORKER_ROLES: frozenset[AgentRole] = frozenset({AgentRole.DEVELOPER, AgentRole.DEPLOYER})
NON_WORKER_ROLES: frozenset[AgentRole] = frozenset({
    AgentRole.PLANNER, AgentRole.RESEARCHER, AgentRole.WRITER, AgentRole.ORCHESTRATOR,
})


ALL_SCOPES: list[str] = [s.value for s in Scope]

# Default scopes per role (AgentRole key)
DEFAULT_SCOPES: dict[AgentRole, list[str]] = {
    AgentRole.LEAD: ALL_SCOPES,  # includes CREDENTIALS_READ via ALL_SCOPES
    AgentRole.DEVELOPER: [
        Scope.TASKS_READ,
        Scope.TASKS_WRITE,
        Scope.KNOWLEDGE_READ,
        Scope.KNOWLEDGE_WRITE,
        Scope.MEMORY_READ,
        Scope.MEMORY_WRITE,
        Scope.APPROVALS_CREATE,
        Scope.CHAT_WRITE,
        Scope.HEARTBEAT,
        Scope.PROJECT_READ,
        Scope.PROJECT_WRITE,
        Scope.TASKS_HELP,
        Scope.CREDENTIALS_READ,
        Scope.VAULT_READ,
        Scope.VAULT_WRITE,
    ],
    AgentRole.REVIEWER: [
        Scope.TASKS_READ,
        Scope.TASKS_WRITE,
        Scope.KNOWLEDGE_READ,
        Scope.KNOWLEDGE_WRITE,
        Scope.MEMORY_READ,
        Scope.APPROVALS_CREATE,
        Scope.CHAT_WRITE,
        Scope.HEARTBEAT,
        Scope.TASKS_HELP,
        Scope.VAULT_READ,
    ],
    AgentRole.PLANNER: [
        Scope.TASKS_READ,
        Scope.KNOWLEDGE_READ,
        Scope.KNOWLEDGE_WRITE,
        Scope.MEMORY_WRITE,
        Scope.APPROVALS_CREATE,
        Scope.CHAT_WRITE,
        Scope.HEARTBEAT,
        Scope.PROJECT_READ,
        Scope.PROJECT_WRITE,
        Scope.TASKS_HELP,
        Scope.VAULT_READ,
        Scope.VAULT_WRITE,
    ],
    AgentRole.RESEARCHER: [
        Scope.TASKS_READ,
        Scope.TASKS_WRITE,
        Scope.KNOWLEDGE_READ,
        Scope.KNOWLEDGE_WRITE,
        # MEMORY_READ + MEMORY_WRITE: the researcher writes lessons (reflection
        # flow) AND must be able to retrieve its own earlier lessons again
        # (`mc memory search`). Before 2026-04-23 the researcher only had WRITE —
        # `mc memory search` failed with 403, the lesson loop was broken.
        Scope.MEMORY_READ,
        Scope.MEMORY_WRITE,
        Scope.CHAT_WRITE,
        Scope.CONTENT_SUBMIT,
        Scope.HEARTBEAT,
        Scope.PROJECT_READ,
        Scope.PROJECT_WRITE,
        Scope.TASKS_HELP,
        Scope.VAULT_READ,
        Scope.VAULT_WRITE,
    ],
    AgentRole.WRITER: [
        Scope.TASKS_READ,
        Scope.TASKS_WRITE,
        Scope.KNOWLEDGE_READ,
        Scope.MEMORY_READ,
        Scope.CHAT_WRITE,
        Scope.CONTENT_SUBMIT,
        Scope.HEARTBEAT,
        Scope.TASKS_HELP,
        Scope.VAULT_READ,
        Scope.VAULT_WRITE,
    ],
    AgentRole.TESTER: [
        Scope.TASKS_READ,
        Scope.TASKS_WRITE,
        Scope.KNOWLEDGE_READ,
        Scope.KNOWLEDGE_WRITE,
        Scope.MEMORY_WRITE,
        Scope.CHAT_WRITE,
        Scope.HEARTBEAT,
        Scope.TASKS_HELP,
        Scope.CREDENTIALS_READ,
    ],
    AgentRole.DEPLOYER: [
        Scope.TASKS_READ,
        Scope.TASKS_WRITE,
        Scope.KNOWLEDGE_READ,
        Scope.KNOWLEDGE_WRITE,
        Scope.MEMORY_WRITE,
        Scope.CHAT_WRITE,
        Scope.HEARTBEAT,
        Scope.DEPLOY_EXECUTE,
        Scope.TASKS_HELP,
        Scope.CREDENTIALS_READ,
        Scope.VAULT_READ,
        Scope.VAULT_WRITE,
    ],
    AgentRole.ORCHESTRATOR: ALL_SCOPES,
    AgentRole.RELAY: ALL_SCOPES,  # Gateway/relay runtime — full access like lead
}


def get_default_scopes(template_name: str | AgentRole) -> list[str]:
    """Look up default scopes for a role or template name. Fallback: ALL_SCOPES."""
    if isinstance(template_name, AgentRole):
        return list(DEFAULT_SCOPES.get(template_name, ALL_SCOPES))
    # Legacy: string-based lookup
    try:
        role = AgentRole(template_name.lower())
        return list(DEFAULT_SCOPES.get(role, ALL_SCOPES))
    except ValueError:
        return list(ALL_SCOPES)


def get_agent_effective_scopes(agent) -> list[str]:
    """Effective scopes of an agent. Empty list = ALL_SCOPES (backward compat)."""
    if agent.scopes:
        return list(agent.scopes)
    return list(ALL_SCOPES)


def require_scope(*scopes: Scope):
    """
    FastAPI dependency factory — checks whether the agent has the required scopes.
    Returns 403 if a scope is missing.

    Usage:
        @router.post("/tasks")
        async def create_task(agent=Depends(require_scope(Scope.TASKS_CREATE))):
            ...
    """
    async def _check(agent=Depends(require_agent)):
        effective = get_agent_effective_scopes(agent)
        missing = [s.value for s in scopes if s.value not in effective]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing scopes: {', '.join(missing)}",
            )
        return agent
    return _check
