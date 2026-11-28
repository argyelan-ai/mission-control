"""Share skills and personas between MC installations (``.mcext`` bundles).

GET  /api/v1/extensions/export?skill=<name> | ?template=<id>  -> .mcext download
POST /api/v1/extensions/import  (multipart ``file``, optional ``target_agent_id``)

Admin only. An import never installs: it validates, stages and creates
PENDING approvals (see ``services/extension_bundle.py``). See
``docs/extensions-sharing.md``.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.models.agent_template import AgentTemplate
from app.services import extension_bundle as eb

router = APIRouter(prefix="/api/v1/extensions", tags=["extensions"])

eb.register_hooks()

_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "exists": status.HTTP_409_CONFLICT,
}


def _http(e: eb.BundleError, default: int) -> HTTPException:
    return HTTPException(_STATUS.get(e.reason, default),
                         detail={"reason": e.reason, "message": e.message})


@router.get("/export")
async def export_extension(
    skill: str | None = None,
    template: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_role(Role.ADMIN)),
) -> Response:
    if (skill is None) == (template is None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail={"reason": "bad_request",
                                    "message": "give exactly one of skill or template"})
    try:
        if skill is not None:
            data = eb.build_skill_bundle(skill)
            name = skill
        else:
            tmpl = await session.get(AgentTemplate, template)
            if tmpl is None:
                raise eb.BundleError("not_found", f"template {template} not found")
            data = eb.build_persona_bundle(tmpl)
            name = tmpl.name
    except eb.BundleError as e:
        raise _http(e, status.HTTP_422_UNPROCESSABLE_ENTITY)
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{eb.export_filename(name)}"',
                 "X-Mcext-Sha256": eb.file_sha256(data)},
    )


@router.post("/import")
async def import_extension(
    response: Response,
    file: UploadFile = File(...),
    target_agent_id: uuid.UUID | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    admin=Depends(require_role(Role.ADMIN)),
) -> dict:
    data = await file.read(eb.MAX_TOTAL_BYTES + 1)
    try:
        parsed = eb.parse_bundle(data)
        outcome = await eb.create_import_approvals(
            session, parsed, target_agent_id=target_agent_id,
            user_label=getattr(admin, "preferred_name", None)
            or getattr(admin, "name", None) or "operator",
        )
    except eb.BundleError as e:
        raise _http(e, status.HTTP_400_BAD_REQUEST)
    response.status_code = status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
    return {
        "kind": parsed.kind, "name": parsed.name,
        "bundle_sha256": parsed.bundle_sha256,
        "existing": not outcome.created,
        "approvals": outcome.approvals,
    }
