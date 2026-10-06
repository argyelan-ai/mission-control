---
id: job-2026-10-01-fixture-claude-run-filled
type: run-record
agent: head
date: 2026-10-01T13:55:00
title: 'Run record: fixture-claude-run-filled'
head_run: 11111111-1111-4111-8111-111111111111
task: 33333333-3333-4333-8333-333333333333
---

<!--
A full, real head-AGENTS.md-shaped run record (test_heads_summary.py review
finding on PR #756: the parser was written against the OLD personal-skill
labels and never matched a real run on disk). Content is a scrubbed copy of
a real passed Claude Code head run — personal names replaced, IDs/branch
swapped for this fixture's own, everything else (the exact wording and
layout the real template produces, arrows, parentheses, "·" separators)
kept as-is on purpose: that is the shape this test has to survive.
-->

# Run record: fixture-claude-run-filled

Heartbeat: 2026-10-01 14:23 · step 7 done · waiting for: nothing
Status: passed

## Job
- Goal: Tasks whose PR is merged on GitHub move automatically to done (today 12 head cards sit in review forever); open/closed-unmerged PRs leave the card alone.
- Repo / branch: scratch/mission-control · mc-head/2026-10-01-fixture-claude-run-1111
- Base: origin/main f0cb774ab3857e0589aff5f597904b85ace05a5a
- Pair: claude × DGX Spark :8000 (aktuell: GLM-5.3-Flash-EXL3) (GLM-5.3-Flash-EXL3) · start 13:53 · end 14:23

## Context brief (max. 60 lines, written in step 0)
- kz brief: used — named rejected ideas (X-loop-as-workflow-or-schedule: no workflow child / schedule variant for loops; workflows bypass the task pipeline) and ADRs 019/030/055/062/085. No existing feature for the paths.
- Already exists:
  - `Task.pr_url` / `Task.pr_number` columns: `backend/app/models/task.py:193` (migration `backend/alembic/versions/0200_task_pr_reference.py`).
  - **Head result path already stores pr_url**: `backend/app/services/heads/mirror.py` `apply_head_state()` sets `task.pr_url = run.pr_url` when a head passes with a PR — requirement "head result path stores pr_url" already satisfied; no second writer built.
  - GitHub config resolver (ADR-055): `backend/app/services/github_config.py` (`resolve_github_config`, `get_github_owner`, `configured` flag, TTL cache).
  - Existing GitHub access pattern = `gh` CLI subprocess (`backend/app/services/git_service.py`), not a HTTP client; periodic-loop pattern = `backend/app/services/github_visibility_monitor.py` (`check_once` + `run_forever`), started/stopped in `backend/app/background.py`.
  - Status path with events: `record_task_event` (`backend/app/services/task_lifecycle.py:41`), hop-walking `move_task` + `mirror_path` (`backend/app/services/heads/mirror.py:51/82`); transitions review→done and user_test→done are valid (`backend/app/task_status.py` VALID_TRANSITIONS).
  - No merge detection anywhere: `git grep -il merged` shows only `gh pr merge` calls (manual review approval path), no polling of PR merged state.
- Decisions that apply:
  - ADR-085 (head per job): fleet dispatch/healers frozen — NOT touched; heads launcher untouched, mirror only read.
  - ADR-055 (GitHub connection config): owner/token via `github_config` resolver (vault > env) — no direct env reads.
  - ADR-030: worker owns background loops; new loop wired in `background.py` start/stop chain like the other loops.
  - kz rejected idea X-loop-as-workflow-or-schedule: implemented as a plain background service loop (like github_visibility_monitor), not a workflow/schedule.
- User flow: before — head passes, card lands in review with pr_url and stays until the operator closes it manually (12 stale cards); this change — a periodic check sees the PR merged on GitHub and closes the card (done, system actor, resolution comment); after — morning report/metrics show no stale merged cards; unmerged or closed-unmerged PRs stay open for review.
- Scope: new `backend/app/services/pr_merge_monitor.py`; wiring in `backend/app/background.py`; tests `backend/tests/test_pr_merge_monitor.py`; docs map `docs/produkt/landkarte.yaml` if a new setting/endpoint appears (none — module constants only).

## Result
- New background service `backend/app/services/pr_merge_monitor.py` (worker loop, 10 min interval, max 20 GitHub checks/cycle): every review/user_test card with a `pr_url` is checked via `gh pr view --json state,mergedAt`; a merged PR moves the card to done through the normal status path (TaskEvent `changed_by="system"`, `actor_label="system"`, reason `pr_merged`, plus one `resolution` comment). Open/closed-unmerged PRs and gh errors leave the card alone; with no configured GitHub token (ADR-055) zero calls are made. Wired start/stop into `backend/app/background.py` next to the GitHub visibility monitor.
- Head cards already store `pr_url` on the task (`heads/mirror.apply_head_state`) — verified, reused, not rebuilt.
- PR: none (scratch repo, branch pushed)

## Evidence
- Failing test before: `uv run pytest tests/test_pr_merge_monitor.py -q` → "11 failed" (ModuleNotFoundError: app.services.pr_merge_monitor)
- Green after: same command → "11 passed"; full suite `uv run pytest -q -n auto` → "24 failed, 9765 passed, 55 skipped" — the 24 are pre-existing environment failures (nested `sandbox-exec` in test_mc_head_sandbox/test_mc_head_scratch_origin, docker-sync tmp-path, /proc jiffies, compose bootstrap); none of the 12 failing test files reference `pr_merge_monitor` (git grep empty), failures are sandbox/tmp-path permission issues independent of this diff.
- Sabotage check: `_close_task` short-circuited before the status write → `2 failed` (both merged→done tests red) · restored via `git checkout --` → `11 passed` · `git status` clean. (First probe on the MERGED-string comparison alone showed only 1 red — parser path — so the probe was moved to the close path, the actual core.)
- kz check: `kz check --pr-body-file .pr-body.md` → first run 2 new (missing PR sections), after completing the body → "kz check: 0 new, 4 known, 0 fixed, 0 skipped -> OK"; push hook ran kz check again → OK.
- Review: self, no helper available in this harness — (1) already exists: pr_url storage reused from heads/mirror, no duplicate merge detection on main; nothing existing lost (stop chain extended, not altered). (2) Decisions: ADR-055 config resolver used, ADR-085 frozen fleet layer untouched, kz X-loop-as-workflow respected (plain service loop). (3) Flow: head passes → card in review with pr_url → monitor closes on merge → no stale cards; unmerged PRs still reach the reviewer; no button/page missing, no UI change.
- Bypass: 0
- UI: no visible change — not measured (no frontend file touched).

## Questions / decisions
- No operator questions.
- Assumption 1: "existing GitHub client" = the `gh` CLI subprocess pattern (git_service, visibility monitor) — no HTTP GitHub client exists in the repo; `github_config` supplies owner/token per ADR-055.
- Assumption 2: interval 600 s and cap 20 checks/cycle as module constants (no new env setting → nothing to map in landkarte.yaml).
- Blocked: none

## Numbers
- Quota: unknown (no quota file read; single head, no helpers)
- Helpers: 0
- Operator minutes (estimate): 0 (unattended)
