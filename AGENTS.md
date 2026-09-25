# AGENTS.md — rules for coding agents in this repository

Read this first; it points to the rest. Human contributors: see
[CONTRIBUTING.md](CONTRIBUTING.md) — the same rules apply to you.

## Always

- Work on a branch (`feat/…`, `fix/…`, `docs/…`), never on `main`. Conventional commits.
- A failing test first, then the change; show the test red before and green after.
- Tests: backend `cd backend && pytest -q` · frontend `cd frontend-v2 && npx tsc --noEmit && npm run test:run`.
- **Public repository:** no secrets, tokens, personal names, host names, private
  addresses or home-directory paths in code, fixtures, docs, commits or PR bodies.
- Principles and language rules: [docs/PRINCIPLES.md](docs/PRINCIPLES.md) ·
  architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · UI strings only through i18n ([docs/i18n.md](docs/i18n.md)).

## Anything visible in `frontend-v2/`

1. [DESIGN.md](DESIGN.md) is the only design rule source — read its
   **Komposition** table (K1–K14) before touching a screen. No other taste skill
   or style guide overrides it.
2. Follow [docs/design/ui-craft.md](docs/design/ui-craft.md): fix vs. structure,
   steps, rubric, anti-slop list.
3. `npm run test:run` includes the design ratchet — no new free pixel values,
   off-scale spacing or inline colours.
4. Measure the region with `npm run design:budget` on your branch's dev server
   (port 3001) when you can; otherwise say so in the run record.
5. Put a phone picture (390 px, before/after) in the run record — **not** in the
   public PR. A **structure** change (new block, new page, rebuilt header) is not
   merged before the operator has seen that picture.
