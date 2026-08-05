"""Local model registry — seed, refresh from remote sources, "new model" notify.

The local counterpart to ``services/model_catalog.py`` + ``model_catalog_check.py``.
Those two answer "what does my cloud provider offer, and did something new
appear?"; this module answers the same two questions for models that run on the
operator's OWN hardware (DGX Spark today, further boxes later).

Three jobs, deliberately in one module because they share the upsert rules:

1. **Seed** (``seed_local_recipes``) — ``config/local-recipes.json`` into the DB
   on startup, idempotent, insert-only. Same contract as
   ``services/runtime_seeder.py``: once a slug exists, the file is never
   consulted for it again, so a hand-edit in the DB cannot be undone by a
   deploy.
2. **Refresh** (``refresh_from_sources``) — pull curated registries over HTTP
   and upsert them. Operator-triggered from the UI, plus a slow background loop.
3. **Notify** — a slug that was never seen before raises
   ``local_model.new_available``, deduped in Redis exactly like
   ``model_catalog_check`` does it.

The rules that keep a refresh safe
----------------------------------
* **Never delete.** An entry that vanishes from a source stays in the table (with
  a log line). Registries go offline, get renamed, get truncated by a bad build —
  none of that may silently empty an operator's model list.
* **Never re-enable.** ``enabled = False`` is an operator decision ("hide this
  from my list"). A refresh may rewrite every other field, but the incoming
  ``enabled`` is ignored on update. Otherwise every refresh would undo the
  hiding, which is the single most annoying way for a sync to be wrong.
* **Never raise.** A broken source is skipped with a reason in the result; the
  other sources still land. Same reasoning as ``build_catalog``'s per-provider
  isolation: one unreachable host may not abort the pass.
* **Only announce what came from a live fetch.** The seed is not news — it
  ships with the deploy, and announcing eight models on every fresh install is
  noise. Only slugs added by a SUCCESSFUL source fetch notify.

``local_registry_check_interval = 0`` disables the background loop entirely; the
refresh endpoint keeps working.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field as PydanticField, ValidationError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.local_recipe import ARCHS, ENGINES, LocalRecipe
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.local_registry")

EVENT_NEW_LOCAL_MODEL = "local_model.new_available"

_SEED_PATH = Path(__file__).parent.parent.parent / "config" / "local-recipes.json"

# Same 180 days as the provider catalog: a local recipe the operator has
# consciously NOT deployed stays undeployed forever, so a monthly re-nudge
# would be pure noise.
_NOTIFIED_TTL = 60 * 60 * 24 * 180

# Above this many new recipes in one refresh, emit a single summary event
# instead of one per recipe (first refresh against a new registry).
_MAX_INDIVIDUAL_EVENTS = 3

_FETCH_TIMEOUT = 10.0


class RecipeSpec(BaseModel):
    """Wire/seed schema for one recipe. The same shape in both directions:
    what ``config/local-recipes.json`` contains is exactly what a remote
    registry must serve, so a source can be produced by exporting a seed file.

    Unknown keys are ignored rather than rejected — a newer registry that adds
    a field must not break an older MC.
    """

    # `model_identifier` would otherwise collide with pydantic's `model_` namespace.
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    slug: str = PydanticField(min_length=1, max_length=64)
    display_name: str = PydanticField(min_length=1, max_length=128)
    description: str | None = None
    engine: str = PydanticField(max_length=32)
    model_identifier: str = PydanticField(min_length=1, max_length=256)
    quant: str | None = PydanticField(default=None, max_length=32)
    est_weights_gb: float | None = PydanticField(default=None, ge=0)
    min_vram_gb: float | None = PydanticField(default=None, ge=0)
    context_len: int | None = PydanticField(default=None, ge=0)
    arch: str = PydanticField(default="any", max_length=16)
    gb10_validated: bool = False
    recipe_ref: str | None = PydanticField(default=None, max_length=256)
    launch_template: str | None = None
    source_registry: str = PydanticField(default="builtin", max_length=64)
    source_url: str | None = PydanticField(default=None, max_length=512)
    tags: list[str] = PydanticField(default_factory=list)
    notes: str | None = None
    enabled: bool = True

    def validate_vocabulary(self) -> str | None:
        """Return a reason string when engine/arch are outside the vocabulary.

        Kept out of pydantic validators on purpose: a source with one unknown
        engine should skip THAT entry with a readable reason, not fail the
        whole payload — and the reason is what the operator sees in the UI.
        """
        if self.engine not in ENGINES:
            return f"unknown engine {self.engine!r} (expected one of {', '.join(ENGINES)})"
        if self.arch not in ARCHS:
            return f"unknown arch {self.arch!r} (expected one of {', '.join(ARCHS)})"
        return None


@dataclass
class RefreshResult:
    """What one refresh pass did. Returned verbatim by POST /local-registry/refresh."""

    fetched: int = 0  # sources answered with a usable payload
    added: int = 0
    updated: int = 0
    failed: int = 0  # sources that could not be used at all
    reasons: list[str] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "added": self.added,
            "updated": self.updated,
            "failed": self.failed,
            "reasons": self.reasons,
            "notified": self.notified,
        }


def registry_sources() -> list[str]:
    """Configured remote registries, empty by default.

    ``settings.local_registry_sources`` is a comma-separated list of URLs. Each
    URL must serve a JSON ARRAY of recipe objects in the seed schema (see
    ``config/local-recipes.json`` — that file is a valid source payload).
    Empty (the default) means: builtin seed only, no outbound HTTP ever.
    """
    raw = (settings.local_registry_sources or "").strip()
    if not raw:
        return []
    return [url.strip() for url in raw.split(",") if url.strip()]


# ── Seeding ──────────────────────────────────────────────────────────────────


def _load_seed() -> list[RecipeSpec]:
    if not _SEED_PATH.exists():
        logger.info("local-recipes.json not found at %s — skipping seed", _SEED_PATH)
        return []
    try:
        entries = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("local registry seed unreadable (%s)", exc)
        return []
    return _parse_entries(entries, origin=str(_SEED_PATH), reasons=[])


def _parse_entries(entries, origin: str, reasons: list[str]) -> list[RecipeSpec]:
    """Validate a payload entry by entry. One bad entry is skipped, not fatal."""
    if not isinstance(entries, list):
        reasons.append(f"{origin}: payload is not a JSON array")
        return []
    specs: list[RecipeSpec] = []
    for raw in entries:
        try:
            spec = RecipeSpec.model_validate(raw)
        except ValidationError as exc:
            slug = raw.get("slug") if isinstance(raw, dict) else "?"
            reasons.append(f"{origin}: invalid entry {slug!r} ({exc.error_count()} error(s))")
            continue
        problem = spec.validate_vocabulary()
        if problem:
            reasons.append(f"{origin}: skipped {spec.slug!r} — {problem}")
            continue
        specs.append(spec)
    return specs


def _row_from_spec(spec: RecipeSpec) -> LocalRecipe:
    return LocalRecipe(
        slug=spec.slug,
        display_name=spec.display_name,
        description=spec.description,
        engine=spec.engine,
        model_identifier=spec.model_identifier,
        quant=spec.quant,
        est_weights_gb=spec.est_weights_gb,
        min_vram_gb=spec.min_vram_gb,
        context_len=spec.context_len,
        arch=spec.arch,
        gb10_validated=spec.gb10_validated,
        recipe_ref=spec.recipe_ref,
        launch_template=spec.launch_template,
        source_registry=spec.source_registry,
        source_url=spec.source_url,
        tags=list(spec.tags or []),
        notes=spec.notes,
        enabled=spec.enabled,
        first_seen_at=utcnow(),
        updated_at=utcnow(),
    )


def _apply_update(row: LocalRecipe, spec: RecipeSpec) -> bool:
    """Copy changed fields onto an existing row. Returns True if anything moved.

    ``enabled`` and ``first_seen_at`` are NOT in the list: the first is the
    operator's decision (a refresh must never un-hide a recipe), the second is
    the definition of "when did MC first learn about this".
    """
    changed = False
    for attr, value in (
        ("display_name", spec.display_name),
        ("description", spec.description),
        ("engine", spec.engine),
        ("model_identifier", spec.model_identifier),
        ("quant", spec.quant),
        ("est_weights_gb", spec.est_weights_gb),
        ("min_vram_gb", spec.min_vram_gb),
        ("context_len", spec.context_len),
        ("arch", spec.arch),
        ("gb10_validated", spec.gb10_validated),
        ("recipe_ref", spec.recipe_ref),
        ("launch_template", spec.launch_template),
        ("source_registry", spec.source_registry),
        ("source_url", spec.source_url),
        ("tags", list(spec.tags or [])),
        ("notes", spec.notes),
    ):
        if getattr(row, attr) != value:
            setattr(row, attr, value)
            changed = True
    if changed:
        row.updated_at = utcnow()
    return changed


async def seed_local_recipes(session: AsyncSession) -> tuple[int, int]:
    """Import config/local-recipes.json into the DB. Idempotent, insert-only.

    Returns ``(inserted, skipped)``. Mirrors ``runtime_seeder.seed_runtimes``:
    existing slugs are left completely untouched, so a curated edit in the DB
    survives every deploy.
    """
    specs = _load_seed()
    if not specs:
        return (0, 0)

    existing = set(
        (await session.exec(select(LocalRecipe.slug))).all()
    )

    inserted = 0
    skipped = 0
    for spec in specs:
        if spec.slug in existing:
            skipped += 1
            continue
        session.add(_row_from_spec(spec))
        inserted += 1

    if inserted:
        await session.commit()
        logger.info("local registry seed: inserted=%d skipped=%d", inserted, skipped)
    else:
        logger.debug("local registry seed: inserted=0 skipped=%d (already seeded)", skipped)
    return (inserted, skipped)


# ── Refresh ──────────────────────────────────────────────────────────────────


async def _fetch_source(client: httpx.AsyncClient, url: str, reasons: list[str]):
    """GET one registry. Every failure mode ends as a reason string, never a raise."""
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        reasons.append(f"{url}: unreachable ({type(exc).__name__})")
        return None
    if response.status_code >= 400:
        reasons.append(f"{url}: HTTP {response.status_code}")
        return None
    try:
        return response.json()
    except ValueError:
        reasons.append(f"{url}: response is not JSON")
        return None


async def _claim_unnotified(redis, slugs: list[str]) -> list[str]:
    """Mark slugs as announced and return only the ones that were not already.

    ``SET nx`` both tests and records in one round trip, so two workers racing
    the same tick cannot announce the same recipe twice. Claiming happens
    BEFORE the event — a lost event beats the same event on every tick.

    Redis unreachable → ``[]``: without the dedup store "new" and "announced an
    hour ago" are indistinguishable, and repeating is the worse failure.
    """
    fresh: list[str] = []
    for slug in slugs:
        try:
            claimed = await redis.set(
                RedisKeys.local_registry_notified(slug), "1", nx=True, ex=_NOTIFIED_TTL
            )
        except Exception as exc:  # noqa: BLE001 — Redis down must not spam
            logger.warning("local registry: dedup unavailable (%s)", exc)
            return []
        if claimed:
            fresh.append(slug)
    return fresh


async def _notify_new(session: AsyncSession, recipes: list[LocalRecipe]) -> None:
    detail = {
        "slugs": [r.slug for r in recipes],
        "count": len(recipes),
        "engines": sorted({r.engine for r in recipes}),
    }
    # severity=info on purpose: emit_event pushes warning+ to Discord, and
    # "a registry added a recipe" is cockpit information, not an alert.
    if len(recipes) <= _MAX_INDIVIDUAL_EVENTS:
        for recipe in recipes:
            await emit_event(
                session,
                EVENT_NEW_LOCAL_MODEL,
                f"New local model available: {recipe.display_name}",
                severity="info",
                detail={
                    **detail,
                    "slugs": [recipe.slug],
                    "count": 1,
                    "engines": [recipe.engine],
                    "slug": recipe.slug,
                    "source_registry": recipe.source_registry,
                },
            )
        return
    await emit_event(
        session,
        EVENT_NEW_LOCAL_MODEL,
        f"{len(recipes)} new local models available",
        severity="info",
        detail=detail,
    )


async def refresh_from_sources(session: AsyncSession) -> RefreshResult:
    """Pull every configured registry and upsert it. Never raises.

    No sources configured is a normal, successful no-op: the builtin seed is
    the whole registry for an operator who does not want MC talking to the
    internet.
    """
    result = RefreshResult()
    sources = registry_sources()
    if not sources:
        result.reasons.append("no sources configured (settings.local_registry_sources)")
        return result

    payloads: list[tuple[str, object]] = []
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            for url in sources:
                payload = await _fetch_source(client, url, result.reasons)
                if payload is None:
                    result.failed += 1
                    continue
                payloads.append((url, payload))
    except Exception as exc:  # noqa: BLE001 — client construction must not kill a tick
        logger.warning("local registry refresh: HTTP client failed (%s)", exc)
        result.reasons.append(f"http client error ({type(exc).__name__})")
        return result

    seen_slugs: set[str] = set()
    new_rows: list[LocalRecipe] = []

    for url, payload in payloads:
        specs = _parse_entries(payload, origin=url, reasons=result.reasons)
        if not specs:
            # A reachable source that yields nothing usable is a failure too —
            # otherwise a registry serving `[]` after a bad build looks healthy.
            result.failed += 1
            result.reasons.append(f"{url}: no usable entries")
            continue
        result.fetched += 1

        for spec in specs:
            seen_slugs.add(spec.slug)
            existing = (
                await session.exec(select(LocalRecipe).where(LocalRecipe.slug == spec.slug))
            ).first()
            if existing is None:
                row = _row_from_spec(spec)
                session.add(row)
                new_rows.append(row)
                result.added += 1
            elif _apply_update(existing, spec):
                session.add(existing)
                result.updated += 1

    if result.added or result.updated:
        await session.commit()

    # No deletion, by design — log what a source dropped so it is visible.
    # Only rows that CAME from a registry can vanish from one; the builtin seed
    # is never in a source payload and would otherwise be listed every tick.
    if result.fetched:
        imported = set(
            (
                await session.exec(
                    select(LocalRecipe.slug).where(LocalRecipe.source_registry != "builtin")
                )
            ).all()
        )
        vanished = sorted(imported - seen_slugs)
        if vanished:
            logger.info(
                "local registry refresh: %d entrie(s) not in any source, kept: %s",
                len(vanished),
                ", ".join(vanished),
            )

    # Only a successful fetch may announce anything (see module docstring).
    if new_rows:
        try:
            redis = await get_redis()
        except Exception as exc:  # noqa: BLE001
            logger.warning("local registry: redis unavailable, no notifications (%s)", exc)
            return result
        fresh_slugs = await _claim_unnotified(redis, [r.slug for r in new_rows])
        if fresh_slugs:
            fresh_rows = [r for r in new_rows if r.slug in set(fresh_slugs)]
            try:
                await _notify_new(session, fresh_rows)
                result.notified = [r.slug for r in fresh_rows]
            except Exception:  # noqa: BLE001 — a failed event must not fail the refresh
                logger.exception("local registry: emitting new-model event failed")

    logger.info(
        "local registry refresh: fetched=%d added=%d updated=%d failed=%d",
        result.fetched, result.added, result.updated, result.failed,
    )
    return result


# ── Background loop ──────────────────────────────────────────────────────────


class LocalRegistryChecker:
    """Same lifecycle contract as ``ModelCatalogChecker`` — start/stop from the
    app lifespan, one Redis-locked tick per interval.

    Six hours by default: a curated registry gains an entry a handful of times
    per year, and the operator can always press "Jetzt prüfen" in the UI.
    """

    def __init__(self, interval: int | None = None) -> None:
        self._interval = (
            interval if interval is not None else settings.local_registry_check_interval
        )
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if not self._interval:
            logger.info("local registry checker disabled (interval=0)")
            return
        if not registry_sources():
            logger.info("local registry checker idle (no sources configured)")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("local registry checker started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if await self._acquire_lock():
                    await self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("local registry checker tick failed")
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """One worker per tick. Redis down → run anyway (single-worker default);
        the notification dedup has its own Redis guard and stays silent then."""
        try:
            redis = await get_redis()
            return bool(
                await redis.set(
                    RedisKeys.local_registry_check_lock(), "1",
                    nx=True, ex=max(self._interval - 5, 10),
                )
            )
        except Exception:  # noqa: BLE001
            return True

    async def tick(self, session: AsyncSession | None = None) -> None:
        if session is not None:
            await refresh_from_sources(session)
            return
        from app.services.runtime_model_resolver import session_scope

        async with session_scope() as own_session:
            await refresh_from_sources(own_session)


local_registry_checker = LocalRegistryChecker()
