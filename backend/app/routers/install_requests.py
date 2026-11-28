"""
Agent-scoped endpoint: POST /api/v1/agent/install-requests

Creates install/uninstall approval requests for skills and plugins.
The actual execution happens in InstallExecutor AFTER approval.

Auth:   Agent PBKDF2 token
Scope:  Any agent can request for themselves; agents:manage required for other targets
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_agent
from app.database import get_session
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.chat_record import Chat, ChatSession
from app.redis_client import RedisKeys, get_redis
from app.scopes import get_agent_effective_scopes
from app.services import chat_record, install_executor
from app.services.install_allowlist import (
    AllowlistError,
    list_allowed_sources,
    validate_skill_name,
    validate_source,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent/install-requests", tags=["install-requests"])


# ── Schemas ───────────────────────────────────────────────────────────────


class InstallRequestCreate(BaseModel):
    type: Literal["skill", "plugin", "mcp"]
    operation: Literal["install", "uninstall"] = "install"
    source: str | None = None
    name: str
    target_agent_id: uuid.UUID
    reason: str = Field(..., min_length=5, max_length=2000)
    # Accepted for old callers, never trusted: an install request always waits
    # for the operator (stored as "L2"), whatever the requester sends.
    autonomy_level: Literal["L1", "L2", "L3"] | None = "L2"
    # Skill from GitHub: the version to install — a commit SHA, tag or branch.
    # Resolved to an exact commit SHA at request time; the operator approves
    # that commit and the executor installs exactly it. Empty = default branch
    # as of now (still pinned to the SHA it resolves to today).
    ref: str | None = Field(default=None, max_length=200)
    proposed_config: dict | None = None
    # Callback coupling: if the requester makes the request in the context of
    # a running task, we post an install_completed comment on that task after
    # successful installation (mirrors the subtask_completed pattern).
    # Without task_id there's no auto-callback — requester must poll manually.
    task_id: uuid.UUID | None = None
    # Chat coupling (agent without a task): the requester's own chat in MC's
    # chat record. On rejected / installed / failed, one system line is
    # written into that chat (notify_requester_chat). Only the requester's
    # own chats are accepted.
    chat_id: uuid.UUID | None = None


class InstallRequestResponse(BaseModel):
    approval_id: uuid.UUID
    status: str
    existing: bool


# ── Helpers ───────────────────────────────────────────────────────────────


def _resource_field(install_type: str) -> str:
    """Map install type to the Agent field that holds installed items."""
    return {"skill": "cli_skills", "plugin": "cli_plugins", "mcp": "mcp_servers"}[install_type]


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.post("", response_model=InstallRequestResponse)
async def create_install_request(
    body: InstallRequestCreate,
    response: Response,
    requester: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> InstallRequestResponse:
    """
    Create an install/uninstall approval request.

    Returns 201 on new request, 200 if an identical pending request already exists.
    """
    # 1. Allowlist check (install only — uninstall doesn't need a source)
    if body.operation == "install":
        if not body.source:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "source is required for install operations")
        try:
            validate_source(body.type, body.source)
        except AllowlistError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Source not in allowlist: {e}")
    if body.type == "skill":
        try:
            validate_skill_name(body.name)
        except AllowlistError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    # 2. Target agent must exist
    target = await session.get(Agent, body.target_agent_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Target agent {body.target_agent_id} not found")

    # 3. Scope check: agents:manage required when targeting another agent
    is_self = body.target_agent_id == requester.id
    effective_scopes = get_agent_effective_scopes(requester)
    has_manage = "agents:manage" in effective_scopes
    if not (is_self or has_manage):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Requester needs 'agents:manage' scope to target other agents",
        )

    # 3b. Chat coupling: only a chat of the requester itself.
    if body.chat_id is not None:
        chat = await session.get(Chat, body.chat_id)
        if chat is None or chat.legacy_agent_id != requester.id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Chat {body.chat_id} not found among your own chats",
            )

    # 4. Already-installed conflict (install only)
    if body.operation == "install":
        field = _resource_field(body.type)
        current: list = getattr(target, field, None) or []
        if body.name in current:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{body.name!r} is already installed on agent {target.name!r}",
            )

    # 5. Idempotency: return existing pending request if identical
    action_type = f"{body.operation}_{body.type}"
    existing_rows = (
        await session.exec(
            select(Approval).where(
                Approval.action_type == action_type,
                Approval.status == "pending",
            )
        )
    ).all()

    for row in existing_rows:
        payload = row.payload or {}
        if (
            payload.get("name") == body.name
            and payload.get("target_agent_id") == str(body.target_agent_id)
        ):
            response.status_code = status.HTTP_200_OK
            return InstallRequestResponse(
                approval_id=row.id,
                status=row.status,
                existing=True,
            )

    # 6. GitHub skill: refuse a name the shared library already has, and pin
    #    the version to an exact commit before the operator sees the request.
    commit: str | None = None
    files: list[str] | None = None
    if (
        body.operation == "install"
        and body.type == "skill"
        and body.source
        and body.source.startswith("github:")
    ):
        if (install_executor.skills_root() / body.name).exists():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A skill named {body.name!r} already exists in the skills "
                "library; it is never overwritten. To give it to this agent, "
                f"request source '~/.mc/skills/{body.name}' instead",
            )
        try:
            commit = await install_executor.resolve_skill_commit(body.source, body.ref)
        except Exception as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Could not pin {body.source} to a commit: {e}",
            )
        # The commit is fixed, so what is read here is exactly what the
        # executor installs later. Only plain text passes this round.
        try:
            files = await inspect_skill_commit(body.source, body.name, commit)
        except SkillContentError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        except Exception as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Could not read {body.source}@{commit[:12]}: {e}",
            )

    # 7. Create Approval row. No board needed: approvals.board_id is optional
    #    and the global pending list shows board-less approvals.
    # The operator approves one fixed version: name the source and the
    # pinned commit in the text they read.
    origin = f" from {body.source}" if body.source else ""
    if commit:
        origin += f"@{commit[:12]}"
    description = (
        f"{requester.name} requests to "
        f"{body.operation} {body.type} "
        f"{body.name!r}{origin} on agent {target.name!r}: "
        f"{body.reason[:200]}"
    )
    payload_data = {
        "name": body.name,
        "source": body.source,
        "target_agent_id": str(body.target_agent_id),
        "target_agent_name": target.name,
        "requester_agent_id": str(requester.id),
        "requester_agent_name": requester.name,
        "reason": body.reason,
        "proposed_config": body.proposed_config,
        "requester_task_id": str(body.task_id) if body.task_id else None,
        "ref": body.ref,
        "commit": commit,
        "requester_chat_id": str(body.chat_id) if body.chat_id else None,
        "files": files,
    }
    approval = Approval(
        board_id=target.board_id,
        agent_id=requester.id,
        action_type=action_type,
        description=description,
        payload=payload_data,
        autonomy_level="L2",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="pending",
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    # 8. Publish SSE event (non-critical — don't fail the request if Redis is unavailable)
    try:
        redis = await get_redis()
        await redis.publish(
            RedisKeys.approvals_events(),
            (
                f'{{"type":"approval.created","approval_id":"{approval.id}",'
                f'"action_type":"{action_type}"}}'
            ),
        )
    except Exception:
        pass  # SSE is best-effort; approval is already persisted

    response.status_code = status.HTTP_201_CREATED
    return InstallRequestResponse(
        approval_id=approval.id,
        status=approval.status,
        existing=False,
    )


# ── Content check: plain text only (extensions concept, section 6) ─────────
#
# A skill is plain text the agent reads. Anything that can run (scripts,
# binaries, executable bits, symlinks) is refused in this round — before the
# operator sees the request. The commit is pinned, so the files read here are
# exactly the files the executor installs after approval.

#: File types that count as plain text in a skill folder.
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
#: Plain-text files that usually carry no suffix.
TEXT_NAMES = frozenset({"LICENSE", "LICENCE", "COPYING", "NOTICE"})
#: Files named in the approval payload at most (the card shows the list).
MAX_LISTED_FILES = 50


class SkillContentError(ValueError):
    """The skill holds something other than plain text."""


def _skill_folder(clone, name: str):
    """The requested skill inside the clone — the same three layouts the
    executor accepts (``install_executor._call_skill_install``)."""
    for candidate in (clone, clone / "skills" / name, clone / name):
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise SkillContentError(
        f"No skill layout: none of /SKILL.md, /skills/{name}/SKILL.md, /{name}/SKILL.md"
    )


def text_only_files(folder) -> list[str]:
    """Relative paths of every file in ``folder`` (``.git`` left out), or
    ``SkillContentError`` naming the files that are not plain text."""
    files: list[str] = []
    bad: list[str] = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if rel.parts and rel.parts[0] == ".git":
            continue
        if path.is_symlink():
            bad.append(f"{rel.as_posix()} (symlink)")
            continue
        if path.is_dir():
            continue
        name = rel.as_posix()
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            bad.append(name)
            continue
        if path.stat().st_mode & 0o111:
            bad.append(f"{name} (executable)")
            continue
        data = path.read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{name} (not UTF-8 text)")
            continue
        if b"\x00" in data:
            bad.append(f"{name} (binary)")
            continue
        files.append(name)
    if bad:
        shown = ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
        raise SkillContentError(
            "Only plain-text skills (SKILL.md plus .md/.txt files) can be "
            f"requested; this one also contains: {shown}"
        )
    return files


async def inspect_skill_commit(source: str, name: str, commit: str) -> list[str]:
    """Fetch ``source`` at ``commit`` into a temp folder and return the
    skill's files — plain text only, else ``SkillContentError``."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone"
        await install_executor._fetch_commit(
            install_executor.github_url(source), commit, clone
        )
        files = text_only_files(_skill_folder(clone, name))
    return files[:MAX_LISTED_FILES]


# ── Feedback line in the requester's chat ──────────────────────────────────

#: ``teammate`` / ``source.title`` of the line — lets a later chat card
#: (concept PR 2) find and localise it from the structured ``install`` field.
CHAT_LINE_TITLE = "install-request"


def _outcome_of(status_: str, install_result, install_exception) -> tuple[str, str | None]:
    """(outcome, error) — outcome is rejected | installed | failed."""
    if status_ == "rejected":
        return "rejected", None
    if install_exception is not None:
        return "failed", f"executor crashed: {install_exception}"
    if install_result is None:
        return "failed", "executor returned no result"
    if install_result.result == "success":
        return "installed", None
    return "failed", install_result.error or install_result.result or "unknown"


def _chat_line_text(payload: dict, action_type: str, outcome: str, error: str | None) -> str:
    operation, _, kind = action_type.partition("_")
    what = f"{operation} {kind} '{payload.get('name', '?')}'"
    target = payload.get("target_agent_name") or "?"
    if outcome == "rejected":
        return f"MC: the operator rejected your request to {what} on {target}."
    if outcome == "installed":
        verb = "removed" if operation == "uninstall" else "installed"
        return (
            f"MC: approved — {kind} '{payload.get('name', '?')}' is {verb} on "
            f"{target}. It takes effect from the next chat start."
        )
    return f"MC: approved, but {what} on {target} failed: {error}"


async def _chat_session_row(session: AsyncSession, chat: Chat) -> ChatSession | None:
    """The session a chat runs on: the pinned one, else the newest open one,
    else the newest (same choice as a chat switch)."""
    rows = (
        await session.exec(
            select(ChatSession)
            .where(ChatSession.chat_id == chat.id)
            .order_by(ChatSession.created_at.desc())
        )
    ).all()
    if chat.native_session_id:
        for row in rows:
            if row.native_session_id == chat.native_session_id:
                return row
    for row in rows:
        if row.ended_at is None:
            return row
    return rows[0] if rows else None


async def notify_requester_chat(
    session: AsyncSession,
    approval: Approval,
    status_: str,
    install_result=None,
    install_exception: Exception | None = None,
) -> bool:
    """Write one system line into the requester's chat (``requester_chat_id``
    in the payload) after the request was rejected, installed or failed.

    Never raises: the approval is already decided, a missing chat record
    (flag off, chat gone, no session yet) only means no line. Returns True
    when a line was written."""
    try:
        payload = approval.payload or {}
        chat_id_str = payload.get("requester_chat_id")
        if not chat_id_str or not chat_record.enabled():
            return False
        chat = await session.get(Chat, uuid.UUID(chat_id_str))
        if chat is None:
            return False
        row = await _chat_session_row(session, chat)
        if row is None:
            logger.info("install feedback: chat %s has no session yet — no line", chat.id)
            return False
        outcome, error = _outcome_of(status_, install_result, install_exception)
        key = f"{CHAT_LINE_TITLE}:{approval.id}:{outcome}"
        event = {
            "kind": "message",
            "uuid": key,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "role": "teammate",
            "teammate": CHAT_LINE_TITLE,
            "source": {"kind": "system", "title": CHAT_LINE_TITLE},
            "text": _chat_line_text(payload, approval.action_type, outcome, error),
            "model": None,
            "sidechain": False,
            "install": {
                "approval_id": str(approval.id),
                "action_type": approval.action_type,
                "name": payload.get("name"),
                "target_agent_name": payload.get("target_agent_name"),
                "commit": payload.get("commit"),
                "outcome": outcome,
                "error": error,
            },
        }
        await chat_record.upsert_events(session, chat.id, row.id, [(key, event)])
        await session.commit()
        return True
    except Exception as e:  # noqa: BLE001 — feedback must never break the resolve
        logger.warning("install feedback into chat failed: %s", e)
        try:
            await session.rollback()
        except Exception:
            pass
        return False


# ── Status + search (read-only, for `mc ext status` / `mc ext search`) ────


def _request_out(a: Approval) -> dict:
    p = a.payload or {}
    return {
        "approval_id": str(a.id),
        "status": a.status,
        "action_type": a.action_type,
        "name": p.get("name"),
        "source": p.get("source"),
        "commit": p.get("commit"),
        "files": p.get("files"),
        "target_agent_name": p.get("target_agent_name"),
        "chat_id": p.get("requester_chat_id"),
        "task_id": p.get("requester_task_id"),
        "failure_reason": a.failure_reason,
        "resolver_note": a.resolver_note,
        "created_at": a.created_at.isoformat() if getattr(a, "created_at", None) else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        "expires_at": a.expires_at.isoformat() if getattr(a, "expires_at", None) else None,
    }


_INSTALL_ACTIONS = (
    "install_skill", "uninstall_skill",
    "install_plugin", "uninstall_plugin",
    "install_mcp", "uninstall_mcp",
)


def _concerns(a: Approval, agent: Agent) -> bool:
    """An agent sees a request it filed or one that targets it."""
    p = a.payload or {}
    me = str(agent.id)
    return p.get("requester_agent_id") == me or p.get("target_agent_id") == me


@router.get("")
async def list_install_requests(
    limit: int = Query(20, ge=1, le=100),
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The calling agent's own install requests (filed by it or for it), newest first."""
    rows = (
        await session.exec(
            select(Approval)
            .where(Approval.action_type.in_(_INSTALL_ACTIONS))
            .order_by(Approval.created_at.desc())
            .limit(500)
        )
    ).all()
    mine = [r for r in rows if _concerns(r, agent)][:limit]
    return {"requests": [_request_out(r) for r in mine]}


def _frontmatter(text: str) -> dict[str, str]:
    """name / description from a SKILL.md front matter (plain line parser,
    same reading as routers/skills.py)."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:40]:
        stripped = line.strip()
        if stripped == "---":
            break
        for key in ("name", "description"):
            if stripped.startswith(f"{key}:"):
                out[key] = stripped[len(key) + 1:].strip().strip('"').strip("'")
    return out


@router.get("/search")
async def search_extensions(
    q: str = Query(..., min_length=1, max_length=200),
    agent: Agent = Depends(require_agent),
) -> dict:
    """Search the local skills library by name and description, and name
    the sources a request may use. Every word of ``q`` must match."""
    words = [w for w in q.lower().split() if w]
    root = install_executor.skills_root()
    found = []
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            meta: dict[str, str] = {}
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                try:
                    meta = _frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    meta = {}
            hay = f"{entry.name} {meta.get('name', '')} {meta.get('description', '')}".lower()
            if all(w in hay for w in words):
                found.append({
                    "name": entry.name,
                    "description": meta.get("description"),
                    "source": f"~/.mc/skills/{entry.name}",
                })
    return {
        "library": found[:50],
        "allowed_sources": list_allowed_sources("skill"),
    }


@router.get("/{approval_id}")
async def get_install_request(
    approval_id: uuid.UUID,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One install request — only for the agent that filed it or that it targets."""
    a = await session.get(Approval, approval_id)
    if a is None or a.action_type not in _INSTALL_ACTIONS or not _concerns(a, agent):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Install request not found")
    return _request_out(a)
