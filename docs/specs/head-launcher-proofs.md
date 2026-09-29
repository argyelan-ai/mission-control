# Head launcher — answer proofs

Record of live runs per harness × runtime pair (spec §4, §12). A pair moves
from `experimental` to `ok` only with a full acceptance proof here: an open
pull request on GitHub, a run record in the vault with `Status: passed`, and
the task card in Review. Config inspection does not count.

## 2026-09-23 — scratch repo, attended (build of Part A)

Setup: a throwaway local git repo (bare `origin` on disk, listed in
`heads/scratch-repos`), a scratch `MC_HOME`, the real backend code
(`services/heads/launcher.write_run`, `pairs.list_pairs`, `sync.sync_once`)
against a throwaway SQLite database, and `mc-head watch` run with the macOS
system python as launchd would. Runtime values (endpoint, model) were read
with a `SELECT` from the live runtimes table; nothing was written to the live
database. Engine: GLM-5.3-Flash (EXL3) on the local GPU box. The engine's
`/v1/messages` route answers 400 on an empty body → `claude × local` is
offered (open point 12 answered for this engine).

| # | Pair | Mode | Result | Duration |
|---|---|---|---|---|
| 1 | omp × GLM local | fresh | failed at once — inside tmux, omp could not reach the engine's LAN address ("Was there a typo in the url or port?"); curl in the same tmux session could → heads now run as a detached process group, tmux is opt-in | 3 s |
| 2 | omp × GLM local | fresh | head did the job: failing test → `sub()` → green → sabotage probe → self-review → branch pushed; run record written, but into the run folder instead of the vault path → wrapper now files `<run>/run-record.md` into the vault | 3 min 40 s |
| 3 | omp × GLM local | fresh | `prepare_failed`: `refs/remotes/origin/head/…` collides with `origin/HEAD` on a case-insensitive disk → branch prefix is now `mc-head/` | – |
| 4 | omp × GLM local | fresh | exit 0, run record filed into vault `jobs/`, branch pushed (2 commits), `main` untouched; derived state `failed / no_pr` (no GitHub in a scratch repo — expected), task mirrored to `failed` with the hold kept | 3 min 29 s |
| 5 | **claude × GLM local** (crosswise restart of #4) | continue | Claude Code started on the same moved worktree but exited: `--add-dir` is variadic and swallowed the prompt → the job now goes in on stdin | – |
| 6 | claude × GLM local (restart of #5) | continue | Claude Code on the local engine read the previous run, added `mul()` with a test and committed; the allow list refused the test run and the push (`prefix:*` rules did not match) → rules rewritten in wildcard form, probed: test + head-branch push run, push to `main` and `docker` denied | 5 min 4 s |
| 7 | claude × GLM local (restart of #6) | continue | exit 0: failing test proven, green (3 tests), sabotage probe, branch pushed (`46030db`), run record `Status: passed` filed into the vault; derived state `failed / no_pr` (no GitHub) — the earlier runs of the task are marked `superseded`, only the latest is mirrored | 4 min 36 s |

**Proven:** start path end to end (backend → spool → watcher → worktree →
harness → status files → derived state → task mirror); both v1 pairs on
the same local engine with zero Claude quota; crosswise restart
omp → Claude Code on one branch (continue mode moves the worktree); the
pre-push guard and the Claude allow/deny list refuse a push to `main`.

**Not yet proven (acceptance still open):** a real pull request — needs the
heads' own GitHub identity (spec §9 layer 4, operator action) and a scratch
repo on GitHub; the sandbox with a real harness (open point 2); the launchd
watcher itself (installing it is an operator step — and whether a
launchd-started head can reach the engine's LAN address has to be checked
right after install, see run #1).
