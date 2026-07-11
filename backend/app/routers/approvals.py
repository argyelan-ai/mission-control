import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import require_user
from app.config import settings
from app.database import get_session
from app.models.approval import Approval
from app.redis_client import RedisKeys
from app.services.activity import emit_event
from app.services.install_executor import InstallExecutor
from app.services.sse import make_sse_response
from app.services.telegram_bot import consume_action_token, peek_action_token, telegram_bot
from app.utils import utcnow

logger = logging.getLogger("mc.approvals")

router = APIRouter(prefix="/api/v1", tags=["approvals"])


class ApprovalResolve(BaseModel):
    status: str  # 'approved' | 'rejected'
    resolver_note: str | None = None


async def _post_install_callback(
    session: AsyncSession,
    approval: Approval,
    install_result,
    install_exception: Exception | None,
) -> None:
    """Callback to the requester after the Install-Executor — mirrors subtask_completed.

    Creates a TaskComment (install_completed / install_failed) on the
    requester's task if task_id was captured on request. Always emits an
    activity_event so the UI + poll-listeners see it.

    Flow parallels `_post_subtask_completion_comment` in agent_scoped.py:
    comment → next poll-cycle of requester picks it up → context-aware.
    """
    from app.models.task import Task, TaskComment

    payload = approval.payload or {}
    requester_id_str = payload.get("requester_agent_id")
    requester_task_id_str = payload.get("requester_task_id")
    install_type = approval.action_type.split("_", 1)[-1]  # skill|plugin|mcp
    operation = approval.action_type.split("_", 1)[0]       # install|uninstall
    item_name = payload.get("name", "?")
    target_name = payload.get("target_agent_name", "?")

    # Classify the outcome
    if install_exception is not None:
        outcome = "failed"
        error_text = f"Executor-Crash: {install_exception}"
    elif install_result is None:
        outcome = "failed"
        error_text = "Executor lieferte kein Result-Objekt (internal)"
    elif install_result.result == "success":
        outcome = "success"
        error_text = None
    elif install_result.result == "rolled_back":
        outcome = "rolled_back"
        error_text = install_result.error or "unknown"
    else:
        outcome = "failed"
        error_text = install_result.error or "unknown"

    # activity_event for UI + observability — always, even without task_id
    await emit_event(
        session,
        event_type=f"install.{outcome}",
        title=(
            f"{operation} {install_type} '{item_name}' auf {target_name} "
            f"→ {outcome}"
        ),
        severity="info" if outcome == "success" else "warning",
        board_id=approval.board_id,
        agent_id=uuid.UUID(requester_id_str) if requester_id_str else None,
        detail={
            "approval_id": str(approval.id),
            "requester_agent_id": requester_id_str,
            "requester_task_id": requester_task_id_str,
            "target_agent_id": payload.get("target_agent_id"),
            "install_type": install_type,
            "operation": operation,
            "name": item_name,
            "outcome": outcome,
            "error": error_text,
        },
    )

    # No auto-comment without task_id (requester had no task coupling)
    if not requester_task_id_str or not requester_id_str:
        return

    try:
        task_uuid = uuid.UUID(requester_task_id_str)
        requester_uuid = uuid.UUID(requester_id_str)
    except ValueError:
        logger.warning("Invalid UUID in install-callback payload: %s", payload)
        return

    task = await session.get(Task, task_uuid)
    if task is None:
        logger.info(
            "Install-Callback: requester_task %s nicht mehr vorhanden (evtl. geloescht)",
            requester_task_id_str,
        )
        return

    if outcome == "success":
        content = (
            f"**Install-Request abgeschlossen:** `{operation} {install_type}` "
            f"{item_name!r}\n"
            f"**Target:** {target_name}\n"
            f"**Approval:** `{approval.id}` (vom Operator genehmigt)\n"
        )
        if install_result and install_result.installed_version:
            content += f"**Version:** {install_result.installed_version}\n"
        content += (
            "\nDie Faehigkeit ist jetzt verfuegbar. "
            "Wenn der Target-Worker laufend ist und die Plugins/Skills sofort "
            "wirken sollen, trigger `mc worker-restart <agent>` (Kontext weg) "
            "oder warte bis zum naechsten natuerlichen Worker-Restart."
        )
        comment_type = "install_completed"
    else:
        content = (
            f"**Install-Request fehlgeschlagen:** `{operation} {install_type}` "
            f"{item_name!r}\n"
            f"**Target:** {target_name}\n"
            f"**Approval:** `{approval.id}`\n"
            f"**Outcome:** `{outcome}`\n"
            f"**Fehler:** {error_text}\n"
            "\nBitte pruefen ob Source korrekt ist, Alternative waehlen, "
            "oder den Operator direkt fragen."
        )
        comment_type = "install_failed"

    comment = TaskComment(
        task_id=task.id,
        author_type="system",
        author_agent_id=None,
        comment_type=comment_type,
        content=content,
    )
    session.add(comment)
    await session.commit()
    logger.info(
        "Install-Callback posted comment on task %s for requester %s (outcome=%s)",
        task.id, requester_uuid, outcome,
    )


async def _handle_x_post_resolution(
    session: AsyncSession,
    approval: Approval,
    resolution_status: str,
) -> None:
    """Post-resolve hook for action_type == "x_post".

    On approve: calls x_publisher.post_media() when the payload carries media_paths, else post_text() —
    persists the outcome — tweet URL in approval.resolver_note (operator-visible) + activity_event
    (detail carries the structured result for the frontend/API). If the
    draft came from a ContentPipeline row (content_pipeline_id in payload),
    that row's published_url/published_platform/published_at/status are
    updated too — reusing the existing content lifecycle instead of adding
    a parallel one.

    On reject: no API call, event only. Never raises — API-side failures
    (rate-limit/403/duplicate/missing secrets) become a clean failed result,
    not a crash of the approval-resolve endpoint.
    """
    payload = approval.payload or {}
    text = payload.get("text", "")

    if resolution_status != "approved":
        await emit_event(
            session,
            event_type="x_post.rejected",
            title=f"X-Post abgelehnt: {text[:80]}",
            severity="info",
            board_id=approval.board_id,
            agent_id=approval.agent_id,
            task_id=approval.task_id,
            detail={"approval_id": str(approval.id)},
        )
        from app.verticals import hooks as vertical_hooks
        await vertical_hooks.run_x_post_resolved_hooks(
            session, approval, resolution_status, None
        )
        return

    from app.services import x_publisher

    media_paths = payload.get("media_paths") or []
    if media_paths:
        result = await x_publisher.post_media(session, text, media_paths)
    else:
        result = await x_publisher.post_text(session, text)

    note_suffix = (
        f"\n[X-Post] {result['url']}" if result.get("ok")
        else f"\n[X-Post FAILED, {result.get('error_type')}] {result.get('error')}"
    )
    approval.resolver_note = ((approval.resolver_note or "") + note_suffix)[:2000]
    session.add(approval)
    await session.commit()

    content_pipeline_id = payload.get("content_pipeline_id")
    if result.get("ok") and content_pipeline_id:
        from app.models.content import ContentPipeline

        pipeline = await session.get(ContentPipeline, uuid.UUID(content_pipeline_id))
        if pipeline is not None:
            pipeline.published_url = result["url"]
            pipeline.published_platform = "twitter"
            pipeline.published_at = datetime.utcnow()
            pipeline.status = "published"
            session.add(pipeline)
            await session.commit()

    await emit_event(
        session,
        event_type="x_post.published" if result.get("ok") else "x_post.failed",
        title=(
            f"X-Post veroeffentlicht: {result.get('url')}" if result.get("ok")
            else f"X-Post fehlgeschlagen ({result.get('error_type')}): {result.get('error')}"
        ),
        severity="info" if result.get("ok") else "warning",
        board_id=approval.board_id,
        agent_id=approval.agent_id,
        task_id=approval.task_id,
        detail={"approval_id": str(approval.id), **result},
    )

    # Callback comment on the requester's task, mirrors _post_install_callback
    requester_task_id_str = payload.get("requester_task_id")
    if requester_task_id_str:
        from app.models.task import Task, TaskComment

        try:
            task = await session.get(Task, uuid.UUID(requester_task_id_str))
        except ValueError:
            task = None
        if task is not None:
            if result.get("ok"):
                content = (
                    f"**X-Post veroeffentlicht** (Approval `{approval.id}`):\n"
                    f"{result['url']}"
                )
            else:
                content = (
                    f"**X-Post fehlgeschlagen** (Approval `{approval.id}`):\n"
                    f"`{result.get('error_type')}` — {result.get('error')}"
                )
            session.add(TaskComment(
                task_id=task.id,
                author_type="system",
                comment_type="x_post_completed" if result.get("ok") else "x_post_failed",
                content=content,
            ))
            await session.commit()

    # Vertical hooks (ADR-044): e.g. bench_studio flips its challenge to
    # `published` on a successful post. No-op when no vertical registered.
    from app.verticals import hooks as vertical_hooks
    await vertical_hooks.run_x_post_resolved_hooks(
        session, approval, resolution_status, result
    )


@router.get("/approvals")
async def list_approvals(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    result = await session.exec(
        select(Approval).where(Approval.status == "pending").order_by(Approval.created_at.desc())  # type: ignore[attr-defined]
    )
    return result.all()


@router.get("/approvals/stream")
async def stream_approvals(current_user = Depends(require_user)):
    return make_sse_response([RedisKeys.approvals_events()])


@router.get("/boards/{board_id}/approvals")
async def list_board_approvals(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    result = await session.exec(
        select(Approval)
        .where(Approval.board_id == board_id, Approval.status == "pending")
        .order_by(Approval.created_at.desc())  # type: ignore[attr-defined]
    )
    return result.all()


@router.patch("/approvals/{approval_id}")
async def resolve_approval(
    approval_id: uuid.UUID,
    payload: ApprovalResolve,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="Status must be 'approved' or 'rejected'")

    approval = await session.get(Approval, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=400, detail="Approval already resolved")

    approval.status = payload.status
    approval.resolved_at = utcnow()
    approval.resolver_note = payload.resolver_note
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    # ── Blocker Decision: Operator resolved → unblock/fail the task ──
    if approval.action_type == "blocker_decision" and approval.task_id:
        from app.models.task import Task as TaskModel
        from app.models.agent import Agent

        task = await session.get(TaskModel, approval.task_id)
        if task and task.status == "blocked":
            if payload.status == "approved":
                # The operator has decided → unblock the task via re-dispatch.
                # blocked is session-terminal: the old task session was
                # deleted when it went blocked. Instead of writing into a dead
                # session, we set the task to inbox and let the normal
                # dispatch mechanism create a fresh session.
                note = payload.resolver_note or "Blocker wurde vom Operator geloest."

                # Resolution comment WITH the answer text — analogous to clarification_question.
                # build_recovery_context() reads comment_type="resolution" and builds
                # the text into the re-dispatch message (via build_agent_task_prompt).
                # Without this comment, HOST_POLL agents (e.g. Hermes) only see the
                # original prompt — the operator's answer gets lost (live bug 2026-05-01).
                from app.models.task import TaskComment
                block_reason = approval.payload.get("reason", "") if approval.payload else ""
                session.add(TaskComment(
                    task_id=task.id,
                    author_type="user",
                    content=(
                        f"**Blocker-Antwort vom Operator:** {note}\n\n"
                        + (f"[Urspruenglicher Blocker: {block_reason}]\n" if block_reason else "")
                        + "Task wird neu dispatcht — ACK und weitermachen."
                    ),
                    comment_type="resolution",
                ))

                task.status = "inbox"
                task.dispatched_at = None
                task.ack_at = None
                from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
                await clear_dispatch_attempt_id(
                    session, task,
                    caller="approval", reason="blocker_unblock_redispatch",
                )
                task.spawn_session_key = None
                task.spawn_run_id = None
                task.updated_at = utcnow()
                session.add(task)
                from app.services.task_lifecycle import record_task_event
                await record_task_event(
                    session, task.id, "blocked", "inbox",
                    changed_by="user", reason="blocker_approval_approved",
                )
                await session.commit()
                logger.info("Task %s unblocked → inbox for re-dispatch (note: %s)", task.id, note[:50])

                # Trigger re-dispatch immediately (same as the initial dispatch)
                from app.services.dispatch import auto_dispatch_task
                from app.utils import create_tracked_task
                create_tracked_task(
                    auto_dispatch_task(str(task.id), str(task.board_id))
                )
            elif payload.status == "rejected":
                # The operator wants to cancel the task
                task.status = "failed"
                task.updated_at = utcnow()
                # Auto-unassign — otherwise a failed task in agent_poll triggers
                # a cancel loop. The operator explicitly cancelled the task.
                from app.services.task_lifecycle import apply_terminal_unassign, record_task_event
                await apply_terminal_unassign(session, task, "failed")
                session.add(task)
                await record_task_event(
                    session, task.id, "blocked", "failed",
                    changed_by="user", reason="blocker_approval_rejected",
                )
                await session.commit()

    # ── Promote Approval: approved → promote + dispatch ──
    if approval.action_type == "promote_approval" and approval.task_id:
        from app.models.task import Task as TaskModel
        task = await session.get(TaskModel, approval.task_id)
        if task and task.dispatch_phase == "planning":
            if payload.status == "approved":
                # Policy approval granted → promote independent of agent liveness
                from app.services.dispatch_gating import promote_task_to_ready
                try:
                    await promote_task_to_ready(task, session)
                    # Trigger dispatch (agent liveness is a separate step)
                    from app.services.dispatch import auto_dispatch_task
                    from app.utils import create_tracked_task
                    create_tracked_task(auto_dispatch_task(task.id, task.board_id))
                    logger.info("Promote-Approval approved → task %s promoted + dispatch triggered", task.id)
                except Exception as e:
                    logger.warning("Promote after approval failed for %s: %s", task.id, e)
            elif payload.status == "rejected":
                await emit_event(
                    session, "task.promote_rejected",
                    f"Freigabe abgelehnt fuer '{task.title}'",
                    severity="info", task_id=task.id, board_id=task.board_id,
                )

    # ── Visual Review: auto-create revision on reject ──
    if approval.action_type == "visual_review" and payload.status == "rejected" and approval.task_id:
        from app.models.task import Task as TaskModel
        original_task = await session.get(TaskModel, approval.task_id)
        if original_task:
            revision = TaskModel(
                board_id=approval.board_id,
                project_id=original_task.project_id,
                title=f"Revision: {original_task.title}",
                description=payload.resolver_note or "Revision nach Visual Review",
                task_type="revision",
                priority=original_task.priority,
                status="inbox",
                is_auto_created=True,
                auto_reason="visual_review_rejected",
            )
            session.add(revision)
            # Set original task back to in_progress
            original_task.status = "in_progress"
            original_task.completed_at = None
            session.add(original_task)
            await session.commit()

    # ── Clarification Question: send answer to agent, unblock task ──
    # Only for status=approved: the operator has answered + the task should continue.
    # For rejected: task stays blocked (the operator didn't answer, only declined).
    if (
        approval.action_type == "clarification_question"
        and approval.task_id
        and payload.status == "approved"
    ):
        from app.models.task import Task as TaskModel, TaskComment
        from app.models.agent import Agent

        task = await session.get(TaskModel, approval.task_id)
        if task and task.status == "blocked":
            task.status = "in_progress"
            session.add(task)

            answer_text = payload.resolver_note or "(Keine Antwort — nur bestaetigt)"
            agent = await session.get(Agent, approval.agent_id)

            # Phase 29: TaskComment is the only (runtime-agnostic) callback
            # path. The gateway-side chat_send_isolated call is gone after the
            # gateway sunset. There used to be an optional live-delivery
            # attempt — that path no longer exists; TaskComment + poll.sh
            # deliver_comments covers all active runtimes (host, cli-bridge,
            # claude-code).
            question_text = approval.payload.get("question", "") if approval.payload else ""
            session.add(TaskComment(
                task_id=task.id,
                author_type="user",
                content=(
                    f"**Antwort auf deine Klaerungsfrage** (vom Operator):\n\n"
                    f"> {question_text}\n\n"
                    f"**Antwort:** {answer_text}\n\n"
                    f"Task ist wieder in_progress — mach weiter."
                ),
                comment_type="resolution",
            ))

            await session.commit()

            await emit_event(
                session,
                event_type="clarification.resolved",
                title=f"Frage von {agent.name if agent else 'Agent'} beantwortet",
                severity="info",
                board_id=approval.board_id,
                task_id=task.id,
                agent_id=approval.agent_id,
            )

    # ── Loop-Gate (ADR-051): geteilter Pfad mit Telegram-Quick-Resolve ──
    if approval.action_type == "loop_gate":
        from app.services.loop_runner import apply_loop_gate_decision
        await apply_loop_gate_decision(session, approval, payload.status)

    # ── Boss Spawn-Approval: approved → create + provision agent ──
    if approval.action_type == "spawn_agent":
        import re as _re
        from app.models.agent import Agent as AgentModel
        from app.models.agent_template import AgentTemplate

        spawn_payload = approval.payload or {}

        # Defense-in-depth: Whitelist validation of name + role (I7+I8)
        _NAME_RE = _re.compile(r"^[A-Za-z0-9 _\-]{2,40}$")
        _ROLE_RE = _re.compile(r"^[a-z_]{2,30}$")
        if payload.status == "approved":
            _name = spawn_payload.get("name")
            _role = spawn_payload.get("role")
            if not _name or not _NAME_RE.match(_name):
                raise HTTPException(
                    status_code=400,
                    detail="Spawn-Payload ungueltig: name fehlt oder passt nicht auf ^[A-Za-z0-9 _-]{2,40}$",
                )
            if not _role or not _ROLE_RE.match(_role):
                raise HTTPException(
                    status_code=400,
                    detail="Spawn-Payload ungueltig: role fehlt oder passt nicht auf ^[a-z_]{2,30}$",
                )

            try:
                # 2 paths: via template_id (from a template) or custom (soul_md directly)
                template_id = spawn_payload.get("template_id")
                raw_token = None

                if template_id:
                    from app.routers.agent_templates import _do_instantiate
                    template = await session.get(AgentTemplate, uuid.UUID(template_id))
                    if not template:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Spawn-Template {template_id} nicht gefunden",
                        )
                    new_agent, raw_token = await _do_instantiate(
                        template=template,
                        board_id=approval.board_id,
                        name=spawn_payload.get("name"),
                        model=spawn_payload.get("model"),
                        session=session,
                    )
                    # Apply optional overrides from the payload
                    if spawn_payload.get("scopes") is not None:
                        new_agent.scopes = spawn_payload["scopes"]
                    if spawn_payload.get("skill_filter") is not None:
                        new_agent.skill_filter = spawn_payload["skill_filter"]
                    if spawn_payload.get("cli_plugins") is not None:
                        new_agent.cli_plugins = spawn_payload["cli_plugins"]
                else:
                    # Custom agent without a template
                    from app.auth import generate_agent_token
                    from app.routers.agents import _generate_tools_md
                    from app.scopes import get_default_scopes

                    scopes = spawn_payload.get("scopes") or get_default_scopes(
                        spawn_payload.get("role", "developer"),
                    )
                    raw_token, token_hash = generate_agent_token()
                    tools_md = _generate_tools_md(
                        spawn_payload["name"],
                        spawn_payload.get("emoji", "🤖"),
                        raw_token,
                        str(approval.board_id) if approval.board_id else None,
                        is_board_lead=False,
                        scopes=scopes,
                    )
                    new_agent = AgentModel(
                        board_id=approval.board_id,
                        name=spawn_payload["name"],
                        emoji=spawn_payload.get("emoji", "🤖"),
                        role=spawn_payload.get("role", "developer"),
                        model=spawn_payload.get("model") or "glm-5.1:cloud",
                        soul_md=spawn_payload.get("soul_md") or "",
                        skills=[],
                        skill_filter=spawn_payload.get("skill_filter"),
                        scopes=scopes,
                        tools_md=tools_md,
                        agent_token_hash=token_hash,
                        cli_plugins=spawn_payload.get("cli_plugins"),
                        provision_status="local",
                    )
                    session.add(new_agent)
                    await session.commit()
                    await session.refresh(new_agent)

                    # Vault write mc_token_{slug} for /internal/bootstrap
                    # (the template path runs via _do_instantiate, which writes itself).
                    from app.services.secrets_helper import upsert_agent_token_secret
                    await upsert_agent_token_secret(session, new_agent.name, raw_token)

                # Ephemeral flag: as a tag in the skills array (no schema change)
                if spawn_payload.get("ephemeral", True):
                    current_skills = list(new_agent.skills or [])
                    if "ephemeral" not in current_skills:
                        current_skills.append("ephemeral")
                        new_agent.skills = current_skills
                        session.add(new_agent)

                # Spawner tag for audit
                spawner_tag = f"spawned-by:{approval.agent_id}"
                _skills = list(new_agent.skills or [])
                if spawner_tag not in _skills:
                    _skills.append(spawner_tag)
                    new_agent.skills = _skills
                    session.add(new_agent)

                # Explicitly set provision_status BEFORE firing the background
                # task — prevents race conditions with parallel spawns.
                new_agent.provision_status = "provisioning"
                session.add(new_agent)
                await session.commit()

                # Provisioning in the background
                from app.services.provisioning import provision_agent_background as _prov
                import asyncio as _aio
                _aio.create_task(_prov(new_agent.id))

                await emit_event(
                    session,
                    event_type="agent.spawned_by_boss",
                    title=f"Agent '{new_agent.name}' gespawnt (approved vom Operator)",
                    severity="info",
                    board_id=approval.board_id,
                    agent_id=approval.agent_id,
                    detail={
                        "new_agent_id": str(new_agent.id),
                        "ephemeral": spawn_payload.get("ephemeral", True),
                        "requested_by": str(approval.agent_id),
                        "approval_id": str(approval.id),
                    },
                )
                logger.info(
                    "Spawn-Approval %s approved → Agent %s (%s) created + provisioning",
                    approval.id, new_agent.id, new_agent.name,
                )
            except Exception as e:
                logger.exception("Spawn failed for approval %s: %s", approval.id, e)
                # Rollback: delete the dangling Agent row if it was already committed
                if new_agent is not None and getattr(new_agent, "id", None):
                    try:
                        _dangling_id = new_agent.id
                        await session.delete(new_agent)
                        await session.commit()
                        logger.info(
                            "Spawn-Rollback: dangling Agent %s geloescht nach Fehler",
                            _dangling_id,
                        )
                    except Exception as _rollback_err:
                        await session.rollback()
                        logger.error(
                            "Spawn-Rollback ebenfalls fehlgeschlagen: %s",
                            _rollback_err,
                        )
                # Enrich the approval note with the error so the operator knows what went wrong
                try:
                    approval.resolver_note = (
                        (approval.resolver_note or "") + f"\n[SPAWN FAILED] {e}"
                    )[:2000]
                    session.add(approval)
                    await session.commit()
                except Exception:
                    await session.rollback()
                await emit_event(
                    session,
                    event_type="agent.spawn_failed",
                    title=f"Spawn fehlgeschlagen: {spawn_payload.get('name', '?')} — {e}",
                    severity="warning",
                    board_id=approval.board_id,
                    agent_id=approval.agent_id,
                    detail={"error": str(e), "approval_id": str(approval.id)},
                )
        else:
            # rejected — event only, no agent
            await emit_event(
                session,
                event_type="agent.spawn_rejected",
                title=f"Spawn abgelehnt: {spawn_payload.get('name', '?')}",
                severity="info",
                board_id=approval.board_id,
                agent_id=approval.agent_id,
                detail={
                    "requested_by": str(approval.agent_id),
                    "approval_id": str(approval.id),
                    "reason": payload.resolver_note or "",
                },
            )

    # ── X (Twitter) Post: approved → tweepy post via XPublisher ──
    if approval.action_type == "x_post":
        await _handle_x_post_resolution(session, approval, payload.status)

    # ── Install-Executor Hook + Requester-Callback ──
    if (
        approval.action_type in {
            "install_skill", "uninstall_skill",
            "install_plugin", "uninstall_plugin",
            "install_mcp", "uninstall_mcp",
        }
        and approval.status == "approved"
    ):
        executor = InstallExecutor(session)
        install_result = None
        install_exception: Exception | None = None
        try:
            install_result = await executor.execute(approval)
            if install_result.result in {"failed", "rolled_back"}:
                approval.failure_reason = install_result.error
                session.add(approval)
                await session.commit()
        except Exception as e:
            install_exception = e
            approval.failure_reason = f"Executor crashed: {e}"
            session.add(approval)
            await session.commit()

        # Callback to the requester: comment on their task (mirrors subtask_completed)
        # If no requester_task_id is set → emit_event only, no comment.
        await _post_install_callback(
            session,
            approval=approval,
            install_result=install_result,
            install_exception=install_exception,
        )

    # ── Update Telegram message (remove buttons) ──
    try:
        await telegram_bot.update_resolved_telegram(
            approval_id, payload.status, payload.resolver_note,
        )
    except Exception as e:
        logger.warning("Telegram update failed: %s", e)

    await emit_event(
        session,
        "approval.resolved",
        f"Approval {payload.status}: {approval.description}",
        board_id=approval.board_id,
        agent_id=approval.agent_id,
        detail={"status": payload.status, "note": payload.resolver_note},
    )
    return approval


# ── Quick-Resolve (Telegram URL Buttons) ────────────────────────────────
# Unauthenticated — the token IS the authorization.


def _quick_html(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """Minimal HTML page for quick-resolve responses."""
    base_url = settings.mc_base_url.rstrip("/")
    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Mission Control</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0A0A0A; color: #E5E5E5;
         display: flex; justify-content: center; align-items: center; min-height: 100dvh; margin: 0; padding: 1rem; }}
  .card {{ background: #1A1A1A; border-radius: 12px; padding: 2rem; max-width: 420px; width: 100%;
           border: 1px solid #2A2A2A; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 1rem; }}
  p {{ color: #999; line-height: 1.5; margin: 0.5rem 0; }}
  .info {{ background: #111; border-radius: 8px; padding: 1rem; margin: 1rem 0; }}
  .label {{ color: #666; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }}
  .value {{ color: #E5E5E5; margin-top: 0.25rem; }}
  button {{ background: #7C3AED; color: white; border: none; border-radius: 8px; padding: 0.75rem 1.5rem;
            font-size: 1rem; cursor: pointer; width: 100%; margin-top: 1rem; }}
  button:hover {{ background: #6D28D9; }}
  button.danger {{ background: #DC2626; }}
  button.danger:hover {{ background: #B91C1C; }}
  a {{ color: #7C3AED; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .footer {{ text-align: center; margin-top: 1.5rem; font-size: 0.85rem; }}
</style>
</head>
<body>
<div class="card">
{body}
<div class="footer"><a href="{base_url}">Mission Control Dashboard</a></div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=status_code)


@router.get("/approvals/{approval_id}/quick-resolve")
async def quick_resolve_page(
    approval_id: uuid.UUID,
    token: str,
    session: AsyncSession = Depends(get_session),
):
    """GET: Validate token (peek, don't consume), render confirmation page."""
    payload = await peek_action_token(token)
    if not payload:
        return _quick_html("Link ungueltig", "<h1>Link ungueltig oder abgelaufen</h1>"
            "<p>Dieser Link wurde bereits benutzt oder ist abgelaufen.</p>", 410)

    if str(payload["approval_id"]) != str(approval_id):
        return _quick_html("Ungueltig", "<h1>Ungueltiger Link</h1>"
            "<p>Token passt nicht zur Approval-ID.</p>", 400)

    approval = await session.get(Approval, approval_id)
    if not approval:
        return _quick_html("Nicht gefunden", "<h1>Approval nicht gefunden</h1>", 404)
    if approval.status != "pending":
        return _quick_html("Bereits erledigt",
            f"<h1>Bereits erledigt</h1><p>Dieser Approval wurde bereits als <b>{approval.status}</b> markiert.</p>")

    action = payload["action"]
    action_label = "Entblocken" if action == "approve" else "Abbrechen"
    btn_class = "" if action == "approve" else "danger"

    # Build info from approval payload
    agent_name = (approval.payload or {}).get("blocked_agent_name", "—")
    task_title = (approval.payload or {}).get("task_title", approval.description or "—")
    blocker = (approval.payload or {}).get("blocker_comment", "—")

    body = f"""<h1>Approval — {action_label}?</h1>
<div class="info">
  <div class="label">Agent</div><div class="value">{_html_escape(agent_name)}</div>
</div>
<div class="info">
  <div class="label">Task</div><div class="value">{_html_escape(task_title)}</div>
</div>
<div class="info">
  <div class="label">Blocker</div><div class="value">{_html_escape(blocker[:500])}</div>
</div>
<form method="POST" action="/api/v1/approvals/{approval_id}/quick-resolve/confirm">
  <input type="hidden" name="token" value="{token}">
  <button type="submit" class="{btn_class}">{action_label}</button>
</form>"""

    return _quick_html(f"Approval — {action_label}", body)


@router.post("/approvals/{approval_id}/quick-resolve/confirm")
async def quick_resolve_confirm(
    approval_id: uuid.UUID,
    token: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """POST: Consume token, resolve approval."""
    payload = await consume_action_token(token)
    if not payload:
        return _quick_html("Link ungueltig", "<h1>Link ungueltig oder abgelaufen</h1>"
            "<p>Dieser Link wurde bereits benutzt oder ist abgelaufen.</p>", 410)

    if str(payload["approval_id"]) != str(approval_id):
        return _quick_html("Ungueltig", "<h1>Ungueltiger Link</h1>", 400)

    action = payload["action"]
    status = "approved" if action == "approve" else "rejected"

    approval = await session.get(Approval, approval_id)
    if not approval:
        return _quick_html("Nicht gefunden", "<h1>Approval nicht gefunden</h1>", 404)
    if approval.status != "pending":
        return _quick_html("Bereits erledigt",
            f"<h1>Bereits erledigt</h1><p>Status: <b>{approval.status}</b></p>")

    # Resolve approval
    approval.status = status
    approval.resolved_at = utcnow()
    approval.resolver_note = "Via Telegram link"
    session.add(approval)
    await session.commit()

    # Blocker Decision: unblock/fail task
    if approval.action_type == "blocker_decision" and approval.task_id:
        from app.models.task import Task as TaskModel
        from app.models.agent import Agent

        task = await session.get(TaskModel, approval.task_id)
        if task and task.status == "blocked":
            if status == "approved":
                # Generic audit comment WITHOUT the original text (leak prevention).
                from app.models.task import TaskComment
                session.add(TaskComment(
                    task_id=task.id,
                    author_type="user",
                    content="**Blocker geloest** — Operator hat via Telegram entschieden. Details im Approval-Datensatz.",
                    comment_type="resolution",
                ))

                # Re-dispatch instead of session wakeup (blocked is session-terminal)
                task.status = "inbox"
                task.dispatched_at = None
                task.ack_at = None
                from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
                await clear_dispatch_attempt_id(
                    session, task,
                    caller="approval", reason="telegram_unblock_redispatch",
                )
                task.spawn_session_key = None
                task.spawn_run_id = None
                task.updated_at = utcnow()
                session.add(task)
                from app.services.task_lifecycle import record_task_event
                await record_task_event(
                    session, task.id, "blocked", "inbox",
                    changed_by="user", reason="telegram_unblock_redispatch",
                )
                await session.commit()
                logger.info("Task %s unblocked via Telegram → inbox for re-dispatch", task.id)

                from app.services.dispatch import auto_dispatch_task
                from app.utils import create_tracked_task
                create_tracked_task(
                    auto_dispatch_task(str(task.id), str(task.board_id))
                )
            elif status == "rejected":
                task.status = "failed"
                task.updated_at = utcnow()
                # Auto-unassign — see above (cancel-loop protection).
                from app.services.task_lifecycle import apply_terminal_unassign, record_task_event
                await apply_terminal_unassign(session, task, "failed")
                session.add(task)
                await record_task_event(
                    session, task.id, "blocked", "failed",
                    changed_by="user", reason="telegram_blocker_rejected",
                )
                await session.commit()

    # Loop-Gate (ADR-051): geteilter Pfad mit resolve_approval — ohne diesen
    # Aufruf wäre ein Telegram-Approve fürs Loop-Gate wirkungslos.
    if approval.action_type == "loop_gate":
        from app.services.loop_runner import apply_loop_gate_decision
        await apply_loop_gate_decision(session, approval, status)

    # Update Telegram message
    try:
        await telegram_bot.update_resolved_telegram(approval_id, status)
    except Exception as e:
        logger.warning("Telegram update failed: %s", e)

    await emit_event(
        session,
        "approval.resolved",
        f"Approval {status} via Telegram link: {approval.description}",
        board_id=approval.board_id,
        agent_id=approval.agent_id,
        detail={"status": status, "source": "telegram_link"},
    )

    emoji = "✅" if status == "approved" else "❌"
    result_text = "Entblockt" if status == "approved" else "Abgebrochen"
    return _quick_html(f"Approval {result_text}",
        f"<h1>{emoji} Approval {result_text}</h1>"
        f"<p>Task wird {'fortgesetzt' if status == 'approved' else 'abgebrochen'}.</p>")


def _html_escape(text: str) -> str:
    """Escape HTML for safe rendering in quick-resolve pages."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
