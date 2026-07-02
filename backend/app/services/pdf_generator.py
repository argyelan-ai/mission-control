"""PDF Generator Client — Backend → mc-playwright Container /pdf Endpoint.

Orchestriert Markdown/HTML → PDF via mc-playwright Service + registriert
das PDF automatisch als TaskDeliverable. Backend-Flow spart Agents den
lokalen puppeteer/chromium-Download-Dance der im ARM-Container via Rosetta
x86 crasht (Incident 2026-04-23: FreeCode hing 2+h in Download-Kaskade).
"""

from __future__ import annotations

import logging
import os
import uuid

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.deliverable import TaskDeliverable

logger = logging.getLogger("mc.pdf_generator")

PLAYWRIGHT_BASE = os.environ.get("MC_PLAYWRIGHT_URL", "http://mc-playwright:8790")


async def generate_pdf(
    *,
    markdown: str | None = None,
    html: str | None = None,
    title: str,
    task_id: uuid.UUID,
    filename_prefix: str = "report",
    custom_css: str | None = None,
    format: str = "A4",
    header_html: str | None = None,
    footer_html: str | None = None,
) -> dict:
    """Ruft mc-playwright /pdf Endpoint. Returns raw response dict.

    Entweder `markdown` ODER `html` angeben (nicht beide).
    Gibt zurueck: {path, bytes, title, task_id, pages}
    """
    if not markdown and not html:
        raise ValueError("Entweder 'markdown' ODER 'html' muss gesetzt sein.")
    if markdown and html:
        raise ValueError("'markdown' und 'html' schliessen sich aus.")

    payload: dict = {
        "task_id": str(task_id),
        "title": title,
        "filename_prefix": filename_prefix,
        "format": format,
    }
    if markdown:
        payload["markdown"] = markdown
    if html:
        payload["html"] = html
    if custom_css:
        payload["custom_css"] = custom_css
    if header_html:
        payload["header_html"] = header_html
    if footer_html:
        payload["footer_html"] = footer_html

    # Timeout generous — grosse Dokumente mit externen Fonts brauchen bis 90s
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{PLAYWRIGHT_BASE}/pdf", json=payload)
        resp.raise_for_status()
        return resp.json()


async def generate_and_register_pdf(
    session: AsyncSession,
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    markdown: str | None = None,
    html: str | None = None,
    title: str,
    filename_prefix: str = "report",
    custom_css: str | None = None,
    description: str | None = None,
) -> TaskDeliverable:
    """High-Level API: PDF generieren + als TaskDeliverable registrieren.

    Deliverable-Flow: Backend-zu-Backend, umgeht die Path-Prefix-Validation
    aus dem Agent-Endpoint (der nur `/deliverables/<task_id>/` + `/shared-mcp/`
    erlaubt). mc-playwright schreibt nach `/shared-deliverables/<task_id>/`
    (named Volume `mc_shared_deliverables`) — analog zum visual_verifier flow.
    """
    pdf_result = await generate_pdf(
        markdown=markdown,
        html=html,
        title=title,
        task_id=task_id,
        filename_prefix=filename_prefix,
        custom_css=custom_css,
    )

    deliverable = TaskDeliverable(
        id=uuid.uuid4(),
        task_id=task_id,
        agent_id=agent_id,
        deliverable_type="file",
        title=title,
        path=pdf_result["path"],
        description=description,
    )
    session.add(deliverable)
    await session.commit()
    await session.refresh(deliverable)

    logger.info(
        "PDF deliverable registered: task=%s agent=%s deliverable=%s bytes=%d pages~%d",
        task_id, agent_id, deliverable.id, pdf_result["bytes"], pdf_result.get("pages", 0),
    )
    return deliverable
