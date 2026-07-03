# Contributing to Mission Control

Thanks for your interest! A few ground rules keep this codebase healthy.

## Getting started

1. Fork + clone, then follow the Quickstart in [README.md](README.md).
2. Backend tests: `cd backend && source .venv/bin/activate && pytest -v`
3. Frontend tests: `cd frontend-v2 && npm run test:run`
4. Building an optional feature bundle? Start with
   [Build a vertical](docs/setup/build-a-vertical.md).

## Workflow

- **Branches**: never commit to `main`. Use `feat/…`, `fix/…`, `docs/…`.
- **Commits**: conventional prefixes (`feat:`, `fix:`, `docs:`, `chore:`).
- **Tests**: changes to backend logic need pytest coverage; frontend
  components need vitest coverage. The suite must be green before review.
- **Migrations**: any model change requires an Alembic migration
  (`docker compose exec backend alembic revision --autogenerate -m "..."`).
  Never edit an already-merged migration.
- **Architecture changes** (new service, new runtime, new dispatch flow, new
  table): update `docs/ARCHITECTURE.md` and add an ADR in `docs/decisions/`
  (template: `_template.md`). Superseded decisions get a new ADR that marks
  the old one — originals are never deleted.

## Code conventions

- Backend: Python 3.12, SQLModel `AsyncSession` everywhere, routers stay
  thin — logic lives in `backend/app/services/`.
- Frontend: TypeScript strict; colors ONLY via the token maps in
  `frontend-v2/src/lib/colors.ts` (see `DESIGN.md` — one teal accent,
  dark-mode only, no purple).
- FastAPI route ordering: static path segments before parameterized routes.
- No secrets, tokens, personal data, or machine-specific absolute paths in
  code, configs, or fixtures — parameterize via env vars with portable
  defaults.

## Reporting bugs

Open an issue with: what you did, what you expected, what happened, relevant
logs (`docker compose logs backend --tail=100`). For security issues see
[SECURITY.md](SECURITY.md) — do not open public issues for vulnerabilities.

## How development works here

**This repository is the upstream.** Development happens directly on
`main` via feature branches and pull requests — what you see is what the
maintainers run. (Early releases were published as squashed snapshots from a
private repo; since 2026-07-03 this repo is the single source of truth. The
pre-launch history remains private because it contained credentials.)

- New code comments and docs are written in **English** (existing German
  comments are being migrated gradually — PRs welcome, see the language note
  in the README).
- CI runs a **leak gate** (gitleaks + forbidden-file check) on every push and
  PR in addition to tests.
- Maintainer-private modules (e.g. a personal news pipeline) live in private
  overlay repositories synced via `scripts/dev-overlay.sh` — their paths are
  gitignored here. You can use the same mechanism for your own private
  verticals.
