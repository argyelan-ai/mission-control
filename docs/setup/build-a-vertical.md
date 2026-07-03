# Build a vertical

Mission Control verticals are optional feature bundles that live outside the
core app. They are loaded when their package exists and skipped when it is
absent, which is how private or experimental features can be removed from a
public release without changing the core boot path.

This tutorial builds a tiny `rss_digest` vertical that exposes one backend
route, registers one task-done hook, and adds one optional frontend flag. The
example is intentionally small enough to finish in about 10 minutes, so you can
compare every step against the vertical contract in
[ADR-044](../decisions/044-vertical-modules.md).
Internal checkouts can also compare against `news_studio`, the full reference
vertical; public checkouts intentionally strip that package.

## Before You Start

Read these files first:

- `backend/app/verticals/__init__.py` discovers vertical packages and calls
  their `register(app)` function.
- `backend/app/verticals/hooks.py` contains the core hook registries a vertical
  may fill.
- `frontend-v2/src/lib/verticals.ts` is the frontend gate used by stripped
  public builds.
- `frontend-v2/src/components/layout/Sidebar.tsx` shows how navigation checks
  a vertical flag before exposing feature routes.

Core code must not import from a vertical package directly. The coupling point
is `register(app)` plus the hook registries in `backend/app/verticals/hooks.py`.

## 1. Create The Backend Package

Add a new package under `backend/app/verticals/`:

```text
backend/app/verticals/rss_digest/
  __init__.py
  routers/
    __init__.py
    digest.py
```

`digest.py` can start with a minimal health-style route:

```python
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/verticals/rss-digest", tags=["verticals:rss-digest"])


@router.get("/status")
async def status() -> dict[str, str]:
    return {"vertical": "rss_digest", "status": "ok"}
```

Keep vertical routers under their own prefix. That makes it obvious which API
surface disappears if the vertical is stripped.

## 2. Register The Vertical

In `backend/app/verticals/rss_digest/__init__.py`, expose `register(app)`.
Discovery in `backend/app/verticals/__init__.py` imports this package and calls
that function during app startup.

```python
from fastapi import FastAPI

from app.verticals import hooks

from .routers.digest import router


async def sync_digest_for_done_task(session, task) -> None:
    if not getattr(task, "pipeline_id", None):
        return
    # Replace this with vertical-specific sync work.


def build_tools_section(ctx: dict) -> str:
    return """
---

## RSS Digest Vertical

- Use the RSS digest tools only for tasks that explicitly request digest work.
"""


def register(app: FastAPI) -> None:
    app.include_router(router)
    hooks.task_done_hooks.append(sync_digest_for_done_task)
    hooks.tools_md_sections.append(("tasks:read", build_tools_section))
```

The hook names are not special, but their signatures are:

- `task_done_hooks`: async callables that accept `(session, task)` and return
  `None`. Core callers log hook errors and continue the task flow.
- `tools_md_sections`: `(scope_string, builder)` pairs where `builder(ctx)`
  returns Markdown. `tools_md_builder` renders a section only when the agent has
  the matching scope.

If the vertical is removed, both hook lists stay empty and the core app keeps
booting.

## 3. Add Tests Near The Boundary

Prefer tests that prove the vertical can be present or absent without changing
core behavior:

- a router smoke test for `/api/v1/verticals/rss-digest/status`
- a `register(app)` test that confirms the route and hooks are registered
- a core test that patches `app.verticals.hooks.task_done_hooks`, rather than
  importing the vertical package from core test fixtures

Avoid hard imports from core modules into `app.verticals.rss_digest`. Those
imports are exactly what make a stripped build fragile.

## 4. Add A Frontend Flag

Frontend verticals are gated from `frontend-v2/src/lib/verticals.ts`:

```ts
export const VERTICALS = {
  newsStudio: false,
  rssDigest: true,
} as const;
```

Then guard any navigation or page entry point:

```tsx
...(VERTICALS.rssDigest
  ? [{ href: "/rss-digest", icon: Newspaper, label: "RSS Digest" }]
  : []),
```

Place larger frontend code under a vertical-owned directory such as
`frontend-v2/src/verticals/rss-digest/`. Core UI may read the flag, but it
should not import vertical components unless the release process keeps those
files.

## 5. Keep Release Stripping Boring

ADR-044 describes the public-release path: internal vertical directories are
removed and frontend flags are flipped off. A vertical is safe to strip when:

- deleting `backend/app/verticals/rss_digest/` leaves backend startup intact
- deleting `frontend-v2/src/verticals/rss-digest/` leaves the production build
  intact
- `frontend-v2/src/lib/verticals.ts` can set the vertical flag to `false`
- no core backend, frontend, migration, or template imports the vertical package
  directly

Models and migrations stay in core. If a stripped install leaves unused tables,
that is expected; it keeps upgrades linear across internal and public builds.

## Validation

For a docs-only walkthrough change, run:

```bash
git diff --check
```

For a real vertical, also run the boundary tests you added and the normal app
suites that apply to the touched area:

```bash
cd backend && pytest -v
cd frontend-v2 && npm run test:run
```

If you change release stripping, verify both shapes: with the vertical present
and with the vertical directory removed and its frontend flag set to `false`.
