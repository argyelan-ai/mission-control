"""Visual Verifier Client — Backend → mc-playwright container.

Orchestrates screenshots/metrics via the mc-playwright service, registers
each screenshot as a TaskDeliverable, and optionally sends all screenshots
as image attachments to the operator reports adapter (Telegram + Slack).

Addresses Bug 3 (2026-04-22): agents needed their own Playwright setups.
Now: a dedicated container, agents call it via API.
"""

from __future__ import annotations

import logging
import os
import uuid

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.deliverable import TaskDeliverable

logger = logging.getLogger("mc.visual_verifier")

# Intra-compose DNS: mc-playwright:8790 (container name)
PLAYWRIGHT_BASE = os.environ.get("MC_PLAYWRIGHT_URL", "http://mc-playwright:8790")
SHARED_MOUNT = "/shared-deliverables"  # mounted in backend via volume


async def verify_url(
    url: str,
    task_id: uuid.UUID,
    viewports: list[str] | None = None,
    scroll: bool = True,
    metrics: bool = True,
    *,
    auth_token: str | None = None,
    login: dict | None = None,
    interactions: list[dict] | None = None,
    wait_for_selector: str | None = None,
    full_page: bool = True,
) -> dict:
    """Calls the mc-playwright /verify endpoint. Returns the raw response from mc-playwright.

    Optional interaction parameters (since 2026-04-23):
      auth_token        — JWT is set in localStorage before navigate
      login             — form-login dict (LoginSpec schema in mc-playwright)
      interactions      — list of {action, selector, value?, wait_after_ms?}
      wait_for_selector — final wait before screenshot
      full_page         — False: viewport only instead of full-page (for modals)
    """
    if viewports is None:
        viewports = ["desktop", "mobile"]

    payload: dict = {
        "url": url,
        "task_id": str(task_id),
        "viewports": viewports,
        "scroll": scroll,
        "metrics": metrics,
        "full_page": full_page,
    }
    if auth_token:
        payload["auth_token"] = auth_token
    if login:
        payload["login"] = login
    if interactions:
        payload["interactions"] = interactions
    if wait_for_selector:
        payload["wait_for_selector"] = wait_for_selector

    # Generous timeout, because form-login + multiple viewports can take more time.
    timeout_s = 180.0 if (login or interactions) else 120.0
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(f"{PLAYWRIGHT_BASE}/verify", json=payload)
        resp.raise_for_status()
        return resp.json()


async def register_screenshots_as_deliverables(
    session: AsyncSession,
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    verify_result: dict,
) -> list[TaskDeliverable]:
    """Registers all screenshots from the verify response as TaskDeliverable rows."""
    created: list[TaskDeliverable] = []

    def _host_path(shared_path: str) -> str:
        """mc-playwright writes to /shared-deliverables, backend reads the same path.
        For the deliverable path: we keep the /shared-deliverables path as reference.
        """
        return shared_path

    for shot in verify_result.get("screenshots", []):
        d = TaskDeliverable(
            id=uuid.uuid4(),
            task_id=task_id,
            agent_id=agent_id,
            deliverable_type="screenshot",
            title=f"Screenshot ({shot['viewport']})",
            path=_host_path(shot["path"]),
            description=f"Viewport: {shot['viewport']} · Full-Page · {shot['bytes']} bytes",
        )
        session.add(d)
        created.append(d)

    for shot in verify_result.get("scroll_shots", []):
        d = TaskDeliverable(
            id=uuid.uuid4(),
            task_id=task_id,
            agent_id=agent_id,
            deliverable_type="screenshot",
            title=f"Scroll-{shot['position']}",
            path=_host_path(shot["path"]),
            description=f"Scroll-Position: {shot['position']}",
        )
        session.add(d)
        created.append(d)

    if created:
        await session.commit()
    return created


async def send_screenshots_to_operator(
    verify_result: dict,
    caption: str | None = None,
) -> bool | None:
    """Sends all screenshots from the verify response to the operator reports
    adapter (Telegram + Slack #mc-reports).

    The old path was one Telegram media group; the adapter has no group
    concept (Slack uploads are per-file anyway), so each screenshot goes as
    its own photo report and the caption rides only on the first — same
    information, channel-neutral.

    Returns None when there is nothing to send, otherwise True only when
    EVERY image reached at least one backend — mirroring the media group's
    all-or-nothing semantics so the caller's dedup marker never suppresses a
    resend of partially delivered batches.
    """
    from app.services.operator_reports import send_report

    paths = [s["path"] for s in verify_result.get("screenshots", [])]
    paths += [s["path"] for s in verify_result.get("scroll_shots", [])]
    if not paths:
        return None
    all_delivered = True
    for i, path in enumerate(paths):
        delivered, _results = await send_report(
            (caption or "") if i == 0 else "",
            file_path=path,
            as_photo=True,
        )
        all_delivered = all_delivered and delivered
    return all_delivered


def format_metrics_summary(verify_result: dict) -> str:
    """Renders metrics as a compact HTML block for Telegram."""
    m = verify_result.get("metrics")
    if not m:
        return ""
    ttfb = m.get("ttfb_ms")
    fcp = m.get("fcp_ms")
    lcp = m.get("lcp_ms")
    total_bytes = m.get("total_bytes", 0)
    status = m.get("status_code", "?")
    size_kb = total_bytes / 1024 if total_bytes else 0
    lines = ["📊 <b>Performance</b>"]
    lines.append(f"Status: <code>{status}</code>")
    if ttfb is not None:
        lines.append(f"TTFB: <code>{ttfb:.0f}ms</code>")
    if fcp is not None:
        lines.append(f"FCP: <code>{fcp:.0f}ms</code>")
    if lcp is not None:
        lines.append(f"LCP: <code>{lcp:.0f}ms</code>")
    lines.append(f"Size: <code>{size_kb:.1f}kb</code>")
    return "\n".join(lines)
