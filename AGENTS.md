# AGENTS.md — rules for coding agents in this repository

Read this first; it points to the rest. Human contributors: see
[CONTRIBUTING.md](CONTRIBUTING.md) — the same rules apply to you.

## Sources of truth (look here, not in memory)

| Question | Where |
|---|---|
| Why MC exists, build/evidence/decision/language rules | [docs/PRINCIPLES.md](docs/PRINCIPLES.md) |
| Current stage, what is next, what is stopped | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Decisions and their reasons (ADRs, index + template) | [docs/decisions/README.md](docs/decisions/README.md) — direction since [ADR-085](docs/decisions/085-head-per-job.md) + [ADR-086](docs/decisions/086-harness-runtime-cross-switch.md) |
| Architecture, "where do I change what" | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Product and design | [PRODUCT.md](PRODUCT.md) · [DESIGN.md](DESIGN.md) + [docs/design/ui-craft.md](docs/design/ui-craft.md) |
| Product map, rule register, known gaps (checked by `kz check`) | [docs/produkt/README.md](docs/produkt/README.md) |

- **Where decisions live:** a decision that changes direction or replaces an
  older one is a new ADR in `docs/decisions/` (the old one gets marked, never
  rewritten). Rules and stages go into PRINCIPLES / ROADMAP in the same PR.
  Chat, memory and run records are not decisions until they land here.
- **Private overlay:** if `CLAUDE.local.md` exists next to this file, read it
  as well — it holds the operator's private operating details (never in git).
- **Operator memory** (Claude Code auto-memory under `~/.claude/projects/…/memory/`)
  is **private, not in this repo** and not a source of truth. Notes there are
  the past; check the repo on current `origin/main` before relying on them.
  New product or rule knowledge goes into the repo by PR, not into memory.

## Before you build: context brief (max. 60 lines)

Write it before the first code change — in your plan or run record, not in
the PR. Short answers are fine; missing answers are not.

1. **Base:** `git fetch origin` — sha of `origin/main`, how far your branch is behind.
2. **Does it already exist?** `git grep` for the endpoint, command, table,
   setting or component you are about to add. Name what you found (path).
3. **Which decision applies?** Matching ADRs (`grep -il <keyword> docs/decisions/*.md`),
   PRINCIPLES / ROADMAP lines, and ideas that were rejected or retired.
   Building against a decision needs the operator, not a workaround.
4. **User flow:** which thing the operator wants to get done gets better, and
   what comes right before and after your change.
5. **Scope:** the paths you will touch. Deleting outside the job is not allowed.

## The three reviewer questions

Every review (self-review, fresh reviewer, PR review) answers these, with evidence:

1. **Does it already exist?** Is something built twice, or is existing behaviour lost?
2. **Which decision applies?** Does the change contradict an ADR, a PRINCIPLES
   rule or a rejected/retired idea?
3. **What comes before and after in the user's flow?** Does every affected flow
   still reach its end — is a button missing, or is something useless (nobody
   calls or sees it)?

## Merging — no bypass, ever

- `main` is protected by a ruleset with a merge queue and required checks.
  Agents never use `gh pr merge --admin`, never disable or edit rules or
  checks, never push around the queue — not even for an urgent fix.
- Heads and unattended agents never merge at all; merges follow the
  operator's merge rule.
- **Every bypass is counted and reported:** if a merge or push went past the
  queue or a required check by any means, write `bypass: <n> — <why>` in the
  run record and tell the operator. An unreported bypass is a failed job.

## Always

- Work on a branch (`feat/…`, `fix/…`, `docs/…`), never on `main`. Conventional commits.
- A failing test first, then the change; show the test red before and green after.
- Tests: backend `cd backend && pytest -q` · frontend `cd frontend-v2 && npx tsc --noEmit && npm run test:run`.
- **Public repository:** no secrets, tokens, personal names, host names, private
  addresses or home-directory paths in code, fixtures, docs, commits or PR bodies.
  Before pushing: `python3 scripts/privacy-scan.py` must exit 0.
- Principles and language rules: [docs/PRINCIPLES.md](docs/PRINCIPLES.md) ·
  architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · UI strings only through i18n ([docs/i18n.md](docs/i18n.md)).

## Rules that bite (from past incidents)

- **Migrations:** one Alembic head only (`cd backend && alembic heads`, guarded by
  `backend/tests/test_alembic_chain_integrity.py`); check for a colliding revision first.
- **Templates are the source** for agent files (ADR-006): edit `backend/templates/`,
  never patch rendered output or DB copies.
- **Runtime switches** go through `backend/app/services/agent_runtime_switch.py`; never set
  `agent.runtime_id` directly. Hosts resolve via `backend/app/services/host_resolver.py`.
- **Secrets vs credentials** (ADR-033): system tokens in `secrets`, task logins in
  `credentials` — referenced by id, never inline.
- **FastAPI routes:** static segments (`/tasks/reorder`) before parameterised ones (`/tasks/{id}`).
- **Deploys and live writes** are not agent work: no `docker compose up`, no DB
  writes, no restarts of live services from a coding session.

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
