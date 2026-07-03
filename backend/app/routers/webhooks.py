"""
Webhook endpoints for external events (GitHub push, etc.).

No user auth needed — the webhook secret is checked instead.
Creates activity events for the dashboard + optional RPC to the agent.
"""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.webhook import Webhook, WebhookPayload
from app.services.activity import emit_event

logger = logging.getLogger("mc.webhooks")

router = APIRouter(prefix="/api/v1", tags=["webhooks"])


# ── Pydantic Models ─────────────────────────────────────────────────────


class GitHubPushEvent(BaseModel):
    """Minimal GitHub push event — only what we need."""

    ref: str | None = None
    repository: dict | None = None
    commits: list[dict] | None = None
    head_commit: dict | None = None
    pusher: dict | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _verify_github_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Verifies GitHub HMAC-SHA256 signature."""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_push_summary(data: dict) -> dict:
    """Extracts relevant info from a GitHub push event."""
    repo = data.get("repository", {})
    head = data.get("head_commit", {})
    commits = data.get("commits", [])
    ref = data.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref else "unknown"

    return {
        "repo": repo.get("full_name", "unknown"),
        "branch": branch,
        "commit_count": len(commits),
        "head_sha": head.get("id", "")[:8] if head else "",
        "head_message": head.get("message", "").split("\n")[0] if head else "",
        "author": head.get("author", {}).get("name", "unknown") if head else "unknown",
        "timestamp": head.get("timestamp") if head else None,
    }


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/webhooks/github/{webhook_id}")
async def receive_github_webhook(
    webhook_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Receives GitHub webhook events (push, PR, etc.).

    URL format: POST /api/v1/webhooks/github/{webhook_id}
    GitHub is configured with this URL as the webhook URL.
    """
    # Load webhook from DB
    webhook = await session.get(Webhook, webhook_id)
    if not webhook or not webhook.is_enabled:
        raise HTTPException(status_code=404, detail="Webhook not found or disabled")

    # Read body
    body = await request.body()
    headers = dict(request.headers)
    source_ip = request.client.host if request.client else None

    # Check HMAC signature (if secret configured)
    if webhook.secret:
        signature = headers.get("x-hub-signature-256")
        if not _verify_github_signature(body, signature, webhook.secret):
            logger.warning("Invalid webhook signature from %s for webhook %s", source_ip, webhook_id)
            raise HTTPException(status_code=403, detail="Invalid signature")

    # Parse payload
    try:
        payload_data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Save payload to DB
    wh_payload = WebhookPayload(
        webhook_id=webhook.id,
        payload=payload_data,
        headers={k: v for k, v in headers.items() if k.startswith("x-github")},
        source_ip=source_ip,
        processed=False,
    )
    session.add(wh_payload)
    await session.commit()
    await session.refresh(wh_payload)

    # Event type from GitHub header
    gh_event = headers.get("x-github-event", "unknown")

    # Push-specific processing
    if gh_event == "push":
        summary = _extract_push_summary(payload_data)
        title = f"Push: {summary['author']} → {summary['branch']} ({summary['head_sha']})"

        await emit_event(
            session,
            event_type="webhook.github.push",
            title=title,
            severity="info",
            board_id=webhook.board_id,
            detail={
                **summary,
                "webhook_payload_id": str(wh_payload.id),
            },
        )

        # Mark payload as processed
        wh_payload.processed = True
        session.add(wh_payload)
        await session.commit()

        logger.info(
            "GitHub push: %s → %s (%d commits)",
            summary["repo"], summary["branch"], summary["commit_count"],
        )
    else:
        # Just log other events
        await emit_event(
            session,
            event_type=f"webhook.github.{gh_event}",
            title=f"GitHub {gh_event} event received",
            severity="info",
            board_id=webhook.board_id,
            detail={
                "event_type": gh_event,
                "webhook_payload_id": str(wh_payload.id),
            },
        )

    return {"status": "received", "payload_id": str(wh_payload.id)}


@router.post("/webhooks/local/push")
async def receive_local_push(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Lightweight endpoint for local git post-commit hooks.

    Needs no webhook setup — accepts directly:
    { "repo", "branch", "commit_sha", "commit_message", "author" }

    No auth needed since it's local-only (Docker network + loopback).
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    repo = data.get("repo", "unknown")
    branch = data.get("branch", "unknown")
    sha = data.get("commit_sha", "")[:8]
    message = data.get("commit_message", "").split("\n")[0]
    author = data.get("author", "unknown")

    # Find board (mc-dev for mission-control)
    board_id = None
    if "mission-control" in repo.lower():
        from app.models.board import Board
        result = await session.exec(
            select(Board).where(Board.slug == "mc-dev")
        )
        board = result.first()
        if board:
            board_id = board.id

    title = f"Local push: {author} → {branch} ({sha})"

    await emit_event(
        session,
        event_type="webhook.local.push",
        title=title,
        severity="info",
        board_id=board_id,
        detail={
            "repo": repo,
            "branch": branch,
            "commit_sha": data.get("commit_sha", ""),
            "commit_message": message,
            "author": author,
        },
    )

    logger.info("Local push event: %s → %s", repo, branch)

    return {"status": "received"}
