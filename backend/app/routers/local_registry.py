"""Local Model Registry API — the shop window for models that run on own hardware.

- ``GET   /api/v1/local-registry``                    — the catalogue (filterable)
- ``POST  /api/v1/local-registry/refresh``            — pull the registries now
- ``PATCH /api/v1/local-registry/{slug}``             — hide/unhide an entry
- ``POST  /api/v1/local-registry/{slug}/install``     — install the engine on a box
- ``GET   /api/v1/local-registry/{slug}/install/log`` — that install's live log

Router ordering: ``/refresh`` is declared before ``/{slug}`` so FastAPI cannot
parse "refresh" as a slug. They differ in method today, but the ordering rule is
cheap and survives the day someone adds ``GET /{slug}``.

Reminder on the contract (same one as the provider catalog): this endpoint says
which recipes EXIST. ``runtime.model_identifier`` remains the only statement
about what runs — ``running`` below is a derived hint for the UI, computed from
the runtime rows at read time, never stored.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import launch_template, local_registry, recipe_install
from app.services.host_resolver import resolved_host_from_row

router = APIRouter(prefix="/api/v1/local-registry", tags=["local-registry"])


class LocalRecipeOut(BaseModel):
    """Full recipe + the derived ``running`` hint."""

    model_config = ConfigDict(protected_namespaces=())

    slug: str
    display_name: str
    description: str | None
    engine: str
    model_identifier: str
    quant: str | None
    est_weights_gb: float | None
    min_vram_gb: float | None
    context_len: int | None
    arch: str
    gb10_validated: bool
    recipe_ref: str | None
    launch_template: str | None
    install_template: str | None
    stop_template: str | None
    process_name: str | None
    author: str | None
    author_url: str | None
    source_registry: str
    source_url: str | None
    tags: list[str]
    notes: str | None
    enabled: bool
    first_seen_at: str | None
    updated_at: str | None
    running: bool


class LocalRecipePatch(BaseModel):
    """Only ``enabled`` is operator-editable — everything else belongs to the
    source and would be overwritten by the next refresh anyway."""

    enabled: bool


async def _running_matcher(session: AsyncSession):
    """Build a predicate "is some enabled runtime serving this recipe?".

    Two conservative signals, both read-only:
      * an enabled runtime whose ``model_identifier`` equals the recipe's, or
      * an enabled runtime whose ``launch_command`` mentions the recipe_ref
        (that is how a sparkrun recipe shows up on a runtime row).

    Conservative on purpose: a false "running" badge would invite the operator
    to skip a deploy that never happened. Loading the runtimes once and matching
    in Python keeps this one query instead of one per recipe.
    """
    runtimes = (
        await session.exec(select(Runtime).where(Runtime.enabled == True))  # noqa: E712
    ).all()
    identifiers = {
        (rt.model_identifier or "").strip().lower() for rt in runtimes if rt.model_identifier
    }
    launch_commands = [rt.launch_command for rt in runtimes if rt.launch_command]

    def _is_running(recipe: LocalRecipe) -> bool:
        if (recipe.model_identifier or "").strip().lower() in identifiers:
            return True
        ref = (recipe.recipe_ref or "").strip()
        return bool(ref) and any(ref in cmd for cmd in launch_commands)

    return _is_running


def _serialize(recipe: LocalRecipe, running: bool) -> LocalRecipeOut:
    return LocalRecipeOut(
        slug=recipe.slug,
        display_name=recipe.display_name,
        description=recipe.description,
        engine=recipe.engine,
        model_identifier=recipe.model_identifier,
        quant=recipe.quant,
        est_weights_gb=recipe.est_weights_gb,
        min_vram_gb=recipe.min_vram_gb,
        context_len=recipe.context_len,
        arch=recipe.arch,
        gb10_validated=recipe.gb10_validated,
        recipe_ref=recipe.recipe_ref,
        launch_template=recipe.launch_template,
        install_template=recipe.install_template,
        stop_template=recipe.stop_template,
        process_name=recipe.process_name,
        author=recipe.author,
        author_url=recipe.author_url,
        source_registry=recipe.source_registry,
        source_url=recipe.source_url,
        tags=list(recipe.tags or []),
        notes=recipe.notes,
        enabled=recipe.enabled,
        first_seen_at=recipe.first_seen_at.isoformat() if recipe.first_seen_at else None,
        updated_at=recipe.updated_at.isoformat() if recipe.updated_at else None,
        running=running,
    )


@router.get("")
async def list_local_recipes(
    engine: str | None = Query(default=None, description="sparkrun | vllm_docker | llamacpp_docker"),
    arch: str | None = Query(default=None, description="arm64 | x86_64 | any"),
    enabled: bool | None = Query(default=None),
    q: str | None = Query(default=None, description="substring over name, slug, model id, tags"),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """The catalogue, sorted by display name.

    ``arch`` filters inclusively: asking for ``arm64`` also returns ``any``
    entries, because those DO run on an arm64 box. Asking for ``any`` returns
    only the arch-agnostic ones.
    """
    statement = select(LocalRecipe)
    if engine:
        statement = statement.where(LocalRecipe.engine == engine)
    if arch:
        statement = (
            statement.where(LocalRecipe.arch == arch)
            if arch == "any"
            else statement.where(LocalRecipe.arch.in_([arch, "any"]))
        )
    if enabled is not None:
        statement = statement.where(LocalRecipe.enabled == enabled)

    recipes = list((await session.exec(statement)).all())

    if q:
        needle = q.strip().lower()
        recipes = [
            r
            for r in recipes
            if needle in r.display_name.lower()
            or needle in r.slug.lower()
            or needle in (r.model_identifier or "").lower()
            or any(needle in t.lower() for t in (r.tags or []))
        ]

    is_running = await _running_matcher(session)
    recipes.sort(key=lambda r: r.display_name.lower())
    return {
        "recipes": [_serialize(r, is_running(r)) for r in recipes],
        "total": len(recipes),
        "sources": local_registry.registry_sources(),
    }


@router.post("/refresh")
async def refresh_local_registry(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.OPERATOR)),
):
    """Pull every configured registry now (the "Jetzt prüfen" button).

    Never fails on a broken source — the per-source reasons come back in the
    body so the operator sees WHICH registry is down instead of a red toast.
    """
    result = await local_registry.refresh_from_sources(session)
    return result.as_dict()


class InstallBody(BaseModel):
    """Which box, and the values the install command is rendered with."""

    host_id: str
    port: int | None = Field(default=None, ge=1, le=65535)
    ctx: int | None = Field(default=None, ge=0)
    src_dir: str | None = Field(default=None, max_length=512)
    gguf_dir: str | None = Field(default=None, max_length=512)


async def _get_recipe(session: AsyncSession, slug: str) -> LocalRecipe:
    recipe = (await session.exec(select(LocalRecipe).where(LocalRecipe.slug == slug))).first()
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Unknown local recipe '{slug}'")
    return recipe


async def _get_ssh_host(session: AsyncSession, host_id: str) -> Host:
    """Slug-or-UUID lookup, same as routers/hosts._get_host, plus the kind check."""
    host = (await session.exec(select(Host).where(Host.slug == host_id))).first()
    if not host:
        try:
            host = await session.get(Host, uuid.UUID(host_id))
        except ValueError:
            host = None
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")
    if host.kind != "ssh":
        raise HTTPException(
            status_code=400,
            detail=f"Host '{host.slug}' hat kind='{host.kind}' — Installation gibt es nur für SSH-Hosts.",
        )
    return host


@router.post("/{slug}/install", status_code=202)
async def install_recipe(
    slug: str,
    body: InstallBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Install this recipe's engine on a box. Returns immediately.

    Admin-only for the same reason the host endpoints are: this decides which
    box runs a build script from the internet.

    Progress is polled from ``GET /{slug}/install/log``. 409 while a run for
    the same (box, recipe) is still going — two installers sharing one clone
    and one weight directory produce a corrupted checkout, not a faster
    download.
    """
    recipe = await _get_recipe(session, slug)
    host = await _get_ssh_host(session, body.host_id)

    try:
        command = launch_template.build_install_command(
            slug=recipe.slug,
            install_template=recipe.install_template or "",
            port=body.port,
            model_identifier=recipe.model_identifier,
            src_dir=body.src_dir,
            gguf_dir=body.gguf_dir,
            ctx=body.ctx or recipe.context_len,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    current = await recipe_install.get_status(str(host.id), recipe.slug)
    if current and current.get("status") == recipe_install.STATUS_RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"Für '{recipe.display_name}' läuft auf '{host.slug}' bereits eine Installation.",
        )

    await recipe_install.start_install(
        str(host.id),
        recipe.slug,
        resolved_host_from_row(host),
        command=command,
        est_weights_gb=recipe.est_weights_gb,
        display_name=recipe.display_name,
    )
    return {"status": "started", "host_id": str(host.id), "slug": recipe.slug}


@router.get("/{slug}/install/log")
async def install_log(
    slug: str,
    host_id: str = Query(...),
    cursor: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Install progress since ``cursor`` (status + new lines in one read).

    ``status`` is ``idle`` when nothing was ever installed here, or the log
    aged out. That doubles as the UI's "is this engine on the box?" signal —
    a ``done`` is the only positive evidence MC has without probing.
    """
    host = await _get_ssh_host(session, host_id)
    return await recipe_install.read_log(str(host.id), slug, cursor)


@router.patch("/{slug}")
async def patch_local_recipe(
    slug: str,
    body: LocalRecipePatch,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.OPERATOR)),
):
    """Hide or unhide an entry. Nothing else is editable here — see LocalRecipePatch."""
    recipe = (
        await session.exec(select(LocalRecipe).where(LocalRecipe.slug == slug))
    ).first()
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Unknown local recipe '{slug}'")

    recipe.enabled = body.enabled
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)

    is_running = await _running_matcher(session)
    return _serialize(recipe, is_running(recipe))
