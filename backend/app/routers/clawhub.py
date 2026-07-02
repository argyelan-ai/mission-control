"""
ClawHub Integration — Skill Marketplace via clawhub.ai (Convex backend).

Listing/search via Convex API, SKILL.md content via HTTP API.
Install: download zip → extract to ~/.mc/skills/.
"""

import io
import logging
import os
import time
import zipfile

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import require_user

logger = logging.getLogger(__name__)

# ClawHub uses Convex as backend — the public REST API returns empty results
CONVEX_URL = "https://wry-manatee-359.convex.cloud/api"
CLAWHUB_HTTP = "https://clawhub.ai/api/v1"
TIMEOUT = 15.0

router = APIRouter(prefix="/api/v1/clawhub", tags=["clawhub"])

# Simple in-memory cache: sort → (timestamp, items)
_skills_cache: dict[str, tuple[float, list]] = {}
CACHE_TTL = 300.0  # 5 Minuten


# ── Helper ──────────────────────────────────────────────────────────────────────

def _skill_install_dir() -> str:
    """Zielverzeichnis für Skill-Installation (HOST_HOME bevorzugt)."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    import pathlib
    return str(pathlib.Path(home) / ".mc" / "skills")


def _normalize(item: dict) -> dict:
    """Convex listPublicPageV4 record → einheitliches Format.
    Unterstützt sowohl das neue nested Format {skill, owner, latestVersion}
    als auch das alte flache Format (skills:list).
    """
    # Neues Format: {skill: {...}, owner: {...}, latestVersion: {...}}
    if "skill" in item:
        skill = item["skill"]
        owner = item.get("owner") or {}
        latest = item.get("latestVersion") or {}
    else:
        # Altes flaches Format
        skill = item
        owner = {}
        latest = {}

    stats = skill.get("stats") or {}
    return {
        "slug": skill.get("slug", ""),
        "name": skill.get("displayName") or skill.get("slug", ""),
        "description": skill.get("summary", ""),
        "version": latest.get("version"),
        "author": item.get("ownerHandle") or owner.get("handle"),
        "author_image": owner.get("image"),
        "downloads": int(stats.get("downloads") or 0),
        "stars": int(stats.get("stars") or 0),
        "installs": int(stats.get("installsAllTime") or 0),
        "versions": int(stats.get("versions") or 1),
        "tags": list((skill.get("tags") or {}).keys()),
        "created_at": skill.get("createdAt"),
        "updated_at": skill.get("updatedAt"),
    }


async def _convex_query_raw(path: str, args: dict | None = None) -> dict:
    """Convex public query — gibt das rohe value-Objekt zurück."""
    payload = {
        "path": path,
        "format": "convex_encoded_json",
        "args": [args or {}],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            f"{CONVEX_URL}/query",
            json=payload,
            headers={"Convex-Client": "npm-1.34.0", "Content-Type": "application/json"},
        )
        if not r.is_success:
            raise HTTPException(502, f"ClawHub Convex Fehler: {r.status_code}")
        data = r.json()
        if data.get("status") != "success":
            raise HTTPException(502, f"Convex query failed: {data.get('errorMessage', 'unknown')}")
        return data.get("value") or {}


def _matches_query(item: dict, ql: str) -> bool:
    """Prüft ob ein skill-item den Suchbegriff enthält."""
    skill = item.get("skill") or item
    owner = item.get("owner") or {}
    return (
        ql in (skill.get("slug") or "").lower()
        or ql in (skill.get("displayName") or "").lower()
        or ql in (skill.get("summary") or "").lower()
        or ql in (item.get("ownerHandle") or "").lower()
        or ql in (owner.get("handle") or "").lower()
        or any(ql in tag.lower() for tag in (skill.get("tags") or {}).keys())
    )


async def _fetch_all_skills(sort: str = "downloads", max_pages: int = 20) -> list:
    """Alle Skills via cursor-basierter Pagination von skills:listPublicPageV4 laden.
    Ergebnis wird 5 Minuten gecacht (pro sort-Wert).
    """
    now = time.monotonic()
    cached = _skills_cache.get(sort)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]

    sort_map = {
        "downloads": "downloads",
        "stars": "stars",
        "newest": "newest",
        "updated": "updated",
        "installs": "installs",
    }
    convex_sort = sort_map.get(sort, "downloads")

    all_items: list = []
    cursor = None

    for _ in range(max_pages):
        args: dict = {
            "numItems": 50,
            "sort": convex_sort,
            "dir": "desc",
            "highlightedOnly": False,
            "nonSuspiciousOnly": False,
        }
        if cursor is not None:
            args["cursor"] = cursor

        result = await _convex_query_raw("skills:listPublicPageV4", args)
        page = result.get("page") or []
        all_items.extend(page)

        if not result.get("hasMore"):
            break
        cursor = result.get("nextCursor")
        if cursor is None:
            break

    _skills_cache[sort] = (now, all_items)
    logger.info("ClawHub: %d skills geladen (sort=%s)", len(all_items), sort)
    return all_items


# ── Request Models ───────────────────────────────────────────────────────────────

class InstallRequest(BaseModel):
    slug: str
    version: str | None = None


# ── Endpoints ────────────────────────────────────────────────────────────────────

@router.get("/skills")
async def list_skills(
    q: str = Query(default="", description="Suchbegriff"),
    sort: str = Query(default="downloads", description="downloads | stars | newest | updated | installs"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=24, ge=1, le=50),
    current_user=Depends(require_user),
):
    """Skills vom ClawHub Marketplace (via Convex API — skills:listPublicPageV4)."""
    try:
        raw = await _fetch_all_skills(sort=sort)

        # Client-side search filter
        if q.strip():
            ql = q.lower()
            raw = [
                item for item in raw
                if _matches_query(item, ql)
            ]

        total = len(raw)
        offset = (page - 1) * limit
        page_items = raw[offset: offset + limit]

        return {
            "items": [_normalize(s) for s in page_items],
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.warning("ClawHub list failed: %s", e)
        raise HTTPException(502, f"ClawHub nicht erreichbar: {e}")


@router.get("/skills/{slug}")
async def get_skill(
    slug: str,
    current_user=Depends(require_user),
):
    """Skill-Details vom ClawHub."""
    try:
        raw = await _fetch_all_skills()
        for item in raw:
            skill = item.get("skill") or item
            if skill.get("slug") == slug:
                return _normalize(item)
        raise HTTPException(404, f"Skill '{slug}' nicht gefunden")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@router.get("/skills/{slug}/readme")
async def get_skill_readme(
    slug: str,
    current_user=Depends(require_user),
):
    """SKILL.md Inhalt eines ClawHub Skills (via HTTP API)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(
                f"{CLAWHUB_HTTP}/skills/{slug}/file",
                params={"path": "SKILL.md"},
            )
            if r.status_code == 404:
                raise HTTPException(404, "SKILL.md nicht gefunden")
            if not r.is_success:
                raise HTTPException(502, f"ClawHub HTTP Fehler: {r.status_code}")
            content = r.text
            return {"slug": slug, "content": content}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/install")
async def install_skill(
    body: InstallRequest,
    current_user=Depends(require_user),
):
    """Skill von ClawHub herunterladen und in ~/.mc/skills/ installieren."""
    import pathlib
    slug = body.slug

    if not slug or ".." in slug or "/" in slug or "\\" in slug:
        raise HTTPException(400, "Ungültiger Skill-Slug")

    install_base = pathlib.Path(_skill_install_dir())
    skill_dir = install_base / slug

    try:
        params: dict = {"slug": slug}
        if body.version:
            params["version"] = body.version

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(f"{CLAWHUB_HTTP}/download", params=params)
            if r.status_code == 404:
                raise HTTPException(404, f"Skill '{slug}' nicht zum Download verfügbar")
            if not r.is_success:
                raise HTTPException(502, f"Download fehlgeschlagen: HTTP {r.status_code}")

            content_type = r.headers.get("content-type", "")

            # ZIP download
            if "zip" in content_type or r.content[:4] == b"PK\x03\x04":
                skill_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                    for member in zf.infolist():
                        mpath = pathlib.Path(member.filename)
                        if mpath.is_absolute() or ".." in mpath.parts:
                            continue
                        parts = mpath.parts
                        if len(parts) > 1 and parts[0] == slug:
                            mpath = pathlib.Path(*parts[1:])
                        target = skill_dir / mpath
                        if member.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(zf.read(member.filename))

            # Nur SKILL.md (plain text/markdown)
            elif "text" in content_type or "markdown" in content_type:
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_bytes(r.content)

            else:
                raise HTTPException(502, f"Unbekannter Content-Type: {content_type}")

        logger.info("ClawHub skill '%s' installiert nach %s", slug, skill_dir)
        return {
            "success": True,
            "slug": slug,
            "path": str(skill_dir),
            "message": f"'{slug}' wurde nach ~/.mc/skills/{slug}/ installiert. Gateway-Neustart aktiviert den Skill.",
        }

    except HTTPException:
        raise
    except zipfile.BadZipFile:
        raise HTTPException(502, "Ungültige ZIP-Datei von ClawHub")
    except OSError as e:
        raise HTTPException(500, f"Dateisystem-Fehler: {e}")
    except Exception as e:
        logger.exception("ClawHub install failed for %s", slug)
        raise HTTPException(500, str(e))
