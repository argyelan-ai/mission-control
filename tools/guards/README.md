# Guards: lessons as code

A lesson that lives only in a note gets repeated. Each guard here turns one
lesson that already came back at least once into a check that fails loudly.

Every guard ships with fixtures:

- `fixtures/<guard>.bad` (and `<guard>.*.bad`) — a real shape of the mistake.
  The guard **must** exit 1.
- `fixtures/<guard>.good` (and `<guard>.*.good`) — the correct shape. The guard
  **must** exit 0.

`run-all.sh` runs every pair. `sabotage-probe.sh` comments out each guard's
`# >>> guard-logic` block in a copy and checks that at least one `.bad` then
turns green — so the fixtures test the logic, not the file name.
Both run in CI (`.github/workflows/guards.yml`).

Exit codes for every guard: `0` green, `1` red, `2` usage or internal error.

## The guards

| Guard | Lesson it encodes | Use it |
|---|---|---|
| `bash-hook.sh` | All of the below happen in live Bash commands, not in files. A replay of 60 days of agent commands (9,422 that push, test or split) found 713 pushes with cut output, 372 without `git -C`, 6 force-pushes without lease, 195 test runs with `run_in_background`, 9 `set -- $var` in zsh — and 0 findings in 1,500 random other commands. | Claude Code `PreToolUse` hook on `Bash` (see **The hook** below). Runs push-gate, gate-exit, zsh-set-split, background-tests and tmp-previews on the command, runs the privacy scan itself before a push, and reads `git status` before `checkout`/`switch`/`restore`/`reset --hard`/`clean -f`/`stash drop` (a subagent's checkout once overwrote uncommitted work). Fixtures carry the tree state as `<fixture>.status`. |
| `gate-exit.sh` | `preflight.sh \| tail -1; docker compose up --build` rebuilt while an agent worked (2026-09-04, same on 07-08); `privacy-scan.py 2>&1 \| tail -2; git push` is the #804 shape. Replay: 271 of 1,611 gate calls in 60 days dropped the gate's exit code — often with `echo "EXIT:$?"` afterwards, which prints the exit code of `tail`, not of the gate. | Lint and hook. Red when a gate (preflight, privacy scan, run-all, ...) is piped and the line goes on, followed by `;` and a real command, or by `\|\|` without a stop. Fine: a pipe ending in `grep -q <verdict>`, a gate only read as a file. |
| `run-gates.sh` | A gate chained with `;` or piped into `tail` reported FAILED and the next step ran anyway. | `run-gates.sh deploy.gates` — one command per line; stops at the first red gate and refuses `;`, `\|\|` and a lone `&` outside quotes. Pipes are fine (pipefail). |
| `zsh-set-split.sh` | The agent shell is zsh: `set -- $var` does not split, so `git worktree add "$1" -b "$2" <ref>` created a branch named after the ref. | Lint scripts and briefs. Fix: `read -r a b <<< "$var"`. |
| `push-gate.sh` | PRs went red in CI on the privacy scan; a rejected force-push was hidden by `\| tail -1`; pushes without `git -C` hit the wrong checkout. | Lint: every push is `privacy-scan && git -C <worktree> push` (the scan must be a real call joined by `&&` only), output neither cut nor redirected, no `--no-verify`, no force without lease, no push through a variable. Text in quotes is skipped unless it goes to `bash -c`/`eval`/`ssh`. |
| `proof-push.sh` | A PR text described a state that never reached the remote, and the old branch was merged. | Run after every push: `proof-push.sh -C <worktree>`. Red unless the remote branch SHA equals `HEAD`. |
| `post-push-hook.sh` | Same incident: the agent said "pushed", nothing compared the remote with `HEAD`. | Claude Code `PostToolUse` hook on `Bash`: after every command that pushed, runs `proof-push.sh` for the pushed tree. Skips dry runs, deletes, tags and pushes of another branch than `HEAD`'s. Fixtures carry the captured evidence as `<fixture>.proof`. |
| `background-tests.sh` | Agents sent the test suite to the background and ended without commit or report. | Lint briefs and scripts; `--worktree <dir>` is red while the tree has uncommitted changes. |
| `tmp-previews.sh` | Previews and deploy folders under `/tmp` vanished after a few days and handoffs pointed to nothing. | Lint scripts and briefs. Opt out per line with `guard-allow: tmp-previews (<reason>)`. |
| `post-deploy-probe.sh` | "Image built, container running" proved nothing; an empty proof was printed and ignored. | After a deploy: `post-deploy-probe.sh --deploy-wt <dir> [--range A..B]`. Read-only. Red on any failed or empty proof. |

`lint.sh <paths>` runs the five lint guards together. In Markdown only fenced
code blocks are checked. CI runs it on `AGENTS.md`, `CLAUDE.md`, `docs`,
`scripts`, `tools` and `.github`, so a new script or doc that teaches a bad
shape goes red.

## The hooks

`bash-hook.sh` (PreToolUse) reads the hook JSON on stdin. Heredoc bodies
(commit messages, PR texts) are skipped. For a push into a checkout that has
`scripts/privacy-scan.py` it runs the scan there and does not trust the text.
`post-push-hook.sh` (PostToolUse) checks after a push that the remote branch
is `HEAD`; it cannot undo the push, but in `block` mode the agent gets the
reason instead of a silent "pushed".

Modes, from `GUARD_HOOK_MODE` or the first word of `~/.mc/guards/mode`:

- `shadow` (default): never blocks; findings go to `~/.mc/guards/hook-log.jsonl`
  (command excerpt, long tokens redacted). Use it to see the real rate first.
- `block`: a finding blocks the call (exit 2); Claude gets the reason.

The hooks never block on their own failure. Each one runs the version on
`origin/main` of a local checkout, cached by tree hash, and is a no-op until
its script is merged. Install both into every settings file a session can load
(the user-wide one covers worktrees and sessions started outside the repo);
identical entries are de-duplicated by Claude Code:

```bash
python3 tools/guards/install-hooks.py --repo <mc-checkout> \
  --settings ~/.claude/settings.json            # dry run: prints what would change
python3 tools/guards/install-hooks.py --repo <mc-checkout> \
  --settings ~/.claude/settings.json --apply    # writes, backup first
```

It never removes other hooks and replaces only its own entries. `--check`
exits 1 when an entry is missing (for audits).

Replay old commands without the scan: `GUARD_HOOK_NO_SCAN=1 bash-hook.sh <json>`.

`proof-push.sh` and `post-deploy-probe.sh` work in two steps: a read-only
capture of the evidence, and an evaluation that decides. The fixtures are
captured evidence, so CI tests the decision without a remote or a running stack.
`--capture` prints the evidence; passing a file evaluates it.

## Adding a guard

1. Write `fixtures/<name>.bad` from the real incident first (the command from
   the transcript, paths shortened), and a `.good`. Add the bypasses a reviewer
   finds as further `.bad` fixtures (`push-gate.echo-scan.bad` and friends).
   Run `run-all.sh` — it must fail because the guard is missing.
2. Write `<name>.sh`. Put the deciding lines between `# >>> guard-logic` and
   `# <<< guard-logic`; the script must still parse when that block is commented out.
3. `run-all.sh` and `sabotage-probe.sh` must both pass.
4. Keep fixtures neutral: no real host, agent, person or account names.
