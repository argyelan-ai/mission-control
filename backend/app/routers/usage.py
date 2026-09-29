"""Page usage beacon (E0) — POST a route on navigation, read weekly counts.

Rules and storage: app/services/usage_pages.py.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.auth import require_user
from app.redis_client import get_redis
from app.services.usage_pages import normalize_route, page_views_by_week, record_page_view

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


class PageView(BaseModel):
    route: str = Field(max_length=256)


@router.post("/page", status_code=204)
async def post_page_view(body: PageView, redis=Depends(get_redis), current_user=Depends(require_user)):
    """Count one view of a route pattern for today. Only the normalised
    pattern is stored — no user, no ids, no query string."""
    route = normalize_route(body.route)
    if route is None:
        raise HTTPException(status_code=422, detail="route must be a plain app path")
    await record_page_view(redis, route)
    return Response(status_code=204)


@router.get("/pages")
async def get_page_views(weeks: int = 6, redis=Depends(get_redis), current_user=Depends(require_user)):
    """Views per ISO week × route pattern (weeks clamped to 1..26)."""
    return await page_views_by_week(redis, weeks=weeks)
