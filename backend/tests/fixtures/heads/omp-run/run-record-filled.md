---
id: job-2026-10-04-fixture-omp-run-filled
type: run-record
agent: head
date: 2026-10-04T00:00:00
title: 'Run record: fixture-omp-run-filled'
head_run: 22222222-2222-4222-8222-222222222222
task: 44444444-4444-4444-8444-444444444444
---

<!--
A full, real head-AGENTS.md-shaped run record (test_heads_summary.py review
finding on PR #756), scrubbed copy of a real passed omp head run — IDs and
branch swapped for this fixture's own, wording/layout kept as-is.
-->

# Run record: fixture-omp-run-filled

Heartbeat: 2026-10-04 (end) · step 7 finished · waiting for: nothing
Status: passed

## Job
- Goal: `drift:map` must not fire when only test files changed (`__tests__/`, `*.test.*`, `*.spec.*`, `tests/`); real route/page changes must still fire.
- Repo / branch: scratch/kohaerenz · mc-head/2026-10-04-fixture-omp-run-2222
- Base: origin/main d33976a0b0e16cf07fcbe1ae6a01702552e6e4d0
- Pair: omp × DGX Spark :8000 (aktuell: GLM-5.3-Flash-EXL3) (GLM-5.3-Flash-EXL3) · start ~2026-10-04 · end 2026-10-04

## Context brief (max. 60 lines, written in step 0)
- kz brief: used — named tests/test_checks.py:181 (drift:map test), confirmed no existing handling of `__tests__/` or the two reported paths; no matching ADRs or rejected ideas.
- Already exists: `check_drift` in src/kohaerenz/checks.py:273 — filters `c.diff.files` through `watched_paths` (checks.py:261) only; no test-path exemption anywhere (git grep: `__tests__/`, `*.test.` → no hits in src/). Per-adapter `exclude` globs exist (ADAPTER_KEYS core.py:36-40, used only by inventory `_walk` inventory.py:41) — not applied to drift.
- Decisions that apply: none found — searched docs/decisions, docs/PRINCIPLES.md (not present in repo), AGENTS.md rules (public repo, stdlib+PyYAML, every check positive+negative test).
- User flow: before — user edits only `*/__tests__/*.test.tsx` in a Next.js app dir, pushes; `kz check` red-flags `drift:map` although no route/page/endpoint changed → false alarm, user must write a "map unchanged because ..." excuse or touch the map. After — test-only diffs pass drift:map; changing a real `page.tsx`/endpoint still flags it, and the map path itself still silences the check.
- Scope: src/kohaerenz/checks.py (drift), tests/test_checks.py, CHANGELOG.md, README.md. No version bump.

## Result
- `check_drift` (src/kohaerenz/checks.py) now drops diff paths matching test patterns (`*__tests__/*`, `*.test.*`, `*.spec.*`, `tests/*`, `*/tests/*`) and the per-adapter `exclude` globs, via new `ignored_paths()` helper. `pr_size_reasons` and `check_map` are untouched.
- PR: none (scratch repo, branch pushed) · needs deploy: no

## Evidence
- Failing test before: `.venv/bin/pytest -q tests/test_checks.py::test_drift_ignores_test_only_changes` → `1 failed` — `NEW drift:map UI/API paths changed (frontend-v2/src/app/files/__tests__/FilesPage.test.tsx, ...ReposPage.test.tsx) but not the map` (exact 02.10 case reproduced red).
- Green after: `.venv/bin/pytest -q tests/test_checks.py` → `29 passed`; full suite `71 passed, 1 failed, 2 errors` — the 1 failed + 2 errors are `tests/test_unreadable.py` chmod-000 cases, verified failing identically on clean origin/main (environment: macOS git vs unreadable test trees, unrelated to this change).
- Sabotage check: removed the `and not match(p, ignored_paths(c.repo))` filter from checks.py → `test_drift_ignores_test_only_changes` red again (`NEW drift:map ... FilesPage.test.tsx`); restored via `git checkout -- src/kohaerenz/checks.py` → drift tests `3 passed`; `git status` clean.
- kz check: `kz check: 0 new, 0 known, 0 fixed, 1 skipped -> OK (1 skipped)` (also run by the push hook at push time; push accepted, no bypass).
- Review: fresh helper (reviewer agent) PASSED — confirmed no duplication/existing-behaviour loss, no AGENTS.md contradiction (994/1000 code lines), flow works end to end, new test fails without the change, no fnmatch false positives (`latest/x.tsx`, `app.test-utils.ts`), `pr_size_reasons`/`check_map` unaffected, 29/29 drift-file tests green.
- Bypass: 0
- UI: no visible UI change (CLI check behaviour only)

## Questions / decisions
- no questions
- Decided alone: applied the exemption only to drift (`check_drift`), not to `pr_size_reasons` (new-page/endpoint/table detection) — a new test file in an app dir is not a page (`PAGE_RE` requires `page.*`), so PR-size stays correct; reviewer confirmed. Also decided: `exclude` globs silence drift too (job suggested "ideally configurable via the existing per-adapter exclude globs"); documented in README/CHANGELOG.
- Blocked: none

## Numbers
- Quota (Claude pairs only): n/a (not a Claude pair)
- Helpers: 1 (reviewer)
- Operator minutes (estimate): ~25

## Failed? (only then)
- n/a
