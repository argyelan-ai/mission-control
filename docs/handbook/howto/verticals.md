# How to: build a vertical module

A **vertical** is an optional feature bundle that lives outside Mission
Control's core. It is loaded when its package is present and skipped when the
directory is gone — deleting it removes the feature without touching the core
boot path.

That property is what makes verticals useful in two directions: private or
experimental features can be stripped from a public release, and MC can grow
new feature areas without the core accumulating imports of them
([ADR-044](../../decisions/044-vertical-modules.md)).

The hands-on tutorial that builds a small `rss_digest` vertical end to end is
[docs/setup/build-a-vertical.md](../../setup/build-a-vertical.md). This page is
the shape of the contract and the decisions behind it.

## The contract in five rules

1. **Backend package.** A vertical is `backend/app/verticals/<name>/`
   (routers and services) exposing a `register(app)` entrypoint.
   `backend/app/verticals/__init__.py` discovers subpackages with `pkgutil` and
   calls `register(app)` on each during startup. Missing packages are ignored,
   not errors.
2. **Coupling only through hooks.** `backend/app/verticals/hooks.py` is core
   code that is never stripped. It holds the registries — `task_done_hooks`
   and `tools_md_sections` — that core code iterates over and verticals fill in
   their `register()`. **Core never imports from a vertical package.** Tests
   may, conditionally, via `try` / `ImportError`.
3. **Models and migrations stay in core.** The database schema is identical
   whether the vertical is installed or stripped; a stripped install simply has
   unused tables. This keeps the Alembic chain linear and makes upgrades work
   in both directions — the deliberate cost is a few idle tables.
4. **Frontend package plus a flag.** Vertical UI lives in
   `frontend-v2/src/verticals/<name>/` with its own `types.ts` and `api.ts`.
   Navigation is gated by a flag in `frontend-v2/src/lib/verticals.ts`. Core UI
   may read the flag; it must not import vertical components.
5. **Release stripping.** `release/internal-paths.txt` lists the directories
   removed for a public build, and `scripts/release-public.sh` flips the
   frontend flags off.

## The two hook signatures

The hook *names* are ordinary list attributes, but their signatures are the
actual contract:

- `task_done_hooks` — async callables taking `(session, task)` and returning
  `None`. Core callers log errors from a hook and continue the task flow, so a
  broken vertical never wedges a task.
- `tools_md_sections` — `(scope_string, builder)` pairs, where `builder(ctx)`
  returns Markdown. `tools_md_builder` renders the section only for agents that
  hold the matching scope, which is how a vertical adds instructions to an
  agent's `TOOLS.md` without editing a core template.

If the vertical is removed, both lists stay empty and the app boots unchanged.

## Building one

The tutorial walks these steps with working code:

1. Create the backend package with a router under its own API prefix
   (`/api/v1/verticals/<name>/…`) — a distinct prefix makes it obvious which
   API surface disappears when the vertical is stripped.
2. Expose `register(app)` in the package `__init__.py`: include the router,
   append to the hook registries.
3. Add tests at the boundary — a router smoke test, a `register(app)` test
   proving routes and hooks land, and a core test that patches
   `app.verticals.hooks.task_done_hooks` rather than importing the vertical.
4. Add the frontend flag and guard the navigation entry with it.
5. Keep stripping boring (below).

Read the tutorial for the code:
[docs/setup/build-a-vertical.md](../../setup/build-a-vertical.md).

## The strip test

A vertical is safe to strip when all four hold:

- Deleting `backend/app/verticals/<name>/` leaves backend startup intact.
- Deleting `frontend-v2/src/verticals/<name>/` leaves the production build
  intact.
- The flag in `frontend-v2/src/lib/verticals.ts` can be set to `false`.
- No core backend, frontend, migration or template imports the vertical
  package directly.

Verify both shapes when you touch release stripping: once with the vertical
present, once with the directory removed and the flag off.

```bash
cd backend && pytest -v
cd frontend-v2 && npm run test:run
```

## What this is not

- **Not a plugin system.** Verticals are compiled into the same build; there is
  no dynamic loading and no separate versioning. A plugin repo per feature —
  with its own migration chain and version matrix — was considered and rejected
  as overkill for a self-hosted single-operator deployment. It stays open as a
  later step.
- **Not a way to keep schema out of core.** If your feature needs tables, they
  go into core models and the core Alembic chain, by design.
- **Not free of indirection.** The hook registry is a deliberately indirect
  code path; debugging it requires knowing that `register()` runs during app
  startup and that hook errors are logged and swallowed rather than raised.

The reference vertical is `news_studio` — a full-size example with seven
routers, ten services and its own frontend area. Internal checkouts can read
it; public checkouts strip that package, which is exactly the mechanism this
page describes.
