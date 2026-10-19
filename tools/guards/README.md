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
| `run-gates.sh` | A gate chained with `;` or piped into `tail` reported FAILED and the next step ran anyway. | `run-gates.sh deploy.gates` — one command per line; stops at the first red gate and refuses `;`, `\|\|`, trailing `&`, `\| tail/head/grep`. |
| `zsh-set-split.sh` | The agent shell is zsh: `set -- $var` does not split, so `git worktree add "$1" -b "$2" <ref>` created a branch named after the ref. | Lint scripts and briefs. Fix: `read -r a b <<< "$var"`. |
| `push-gate.sh` | PRs went red in CI on the privacy scan; a rejected force-push was hidden by `\| tail -1`; pushes without `git -C` hit the wrong checkout. | Lint: every push is `privacy-scan && git -C <worktree> push`, output uncut, no `--no-verify`. |
| `proof-push.sh` | A PR text described a state that never reached the remote, and the old branch was merged. | Run after every push: `proof-push.sh -C <worktree>`. Red unless the remote branch SHA equals `HEAD`. |
| `background-tests.sh` | Agents sent the test suite to the background and ended without commit or report. | Lint briefs and scripts; `--worktree <dir>` is red while the tree has uncommitted changes. |
| `tmp-previews.sh` | Previews and deploy folders under `/tmp` vanished after a few days and handoffs pointed to nothing. | Lint scripts and briefs. Opt out per line with `guard-allow: tmp-previews (<reason>)`. |
| `post-deploy-probe.sh` | "Image built, container running" proved nothing; an empty proof was printed and ignored. | After a deploy: `post-deploy-probe.sh --deploy-wt <dir> [--range A..B]`. Read-only. Red on any failed or empty proof. |

`lint.sh <paths>` runs the four lint guards together.

`proof-push.sh` and `post-deploy-probe.sh` work in two steps: a read-only
capture of the evidence, and an evaluation that decides. The fixtures are
captured evidence, so CI tests the decision without a remote or a running stack.
`--capture` prints the evidence; passing a file evaluates it.

## Adding a guard

1. Write `fixtures/<name>.bad` from the real incident first, and a `.good`.
   Run `run-all.sh` — it must fail because the guard is missing.
2. Write `<name>.sh`. Put the deciding lines between `# >>> guard-logic` and
   `# <<< guard-logic`; the script must still parse when that block is commented out.
3. `run-all.sh` and `sabotage-probe.sh` must both pass.
4. Keep fixtures neutral: no real host, agent, person or account names.
