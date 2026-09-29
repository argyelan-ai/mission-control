# Journey tests (E6)

A journey is something the operator wants to get done, start to finish
(`docs/produkt/landkarte.yaml`, `journeys:`). A journey test clicks through
it in a real browser against a real MC backend and ends on the **effect**,
not on the click: "the agent received the instruction", "Insights shows
exactly the recorded cost". Unit tests prove the parts; journey tests prove
that the parts still reach each other.

They run **nightly on the operator's machine** against a throw-away stack —
not in GitHub CI and not on a self-hosted runner (the repository is public,
fork code must never run on that machine).

## What is there

| Path | What |
|---|---|
| `e2e/journeys/J-<id>.spec.ts` | One file per journey, named as in `landkarte.yaml` (`test:`). `kz check` counts a missing file as a gap. |
| `e2e/journeys/lib/mc.ts` | Operator API calls, fixture SQL, sign-in. Refuses the ports a normal install uses. |
| `e2e/journeys/lib/fakeHarness.ts` | The fake harness (below). |
| `e2e/stack/` | The test stack: `docker-compose.journeys.yml` + a small `Caddyfile`. |
| `e2e/run-journeys.sh` | Build → start → test → tear down, with preflight and time box. |
| `e2e/install-journeys.sh` + `com.mc.journeys.plist.template` | Nightly launchd job (installed copy, never a file in a checkout). |

| Journey | Covers | Proves |
|---|---|---|
| `J-phone-needs-you` | Home → Inbox → task, at 390 px | A blocked task is unblocked from the phone and the agent receives the instruction and the task again; afterwards nothing is blocked. A second test pins a **known gap** (below) and checks, as a control, that the resuming answer path reaches the agent. |
| `J-usage` | Insights, at 390 px | Recorded usage rows show up as exactly that cost, local share (this week, last week), day and source. |
| `J-vault`, `J-setup`, `J-runtime-switch`, `J-job-to-merge` | — | Not written yet; listed in `docs/produkt/luecken-basis.json`. One per round, cheapest first. |

## The test stack

`docker compose -p mc-journeys -f e2e/stack/docker-compose.journeys.yml`,
started only by `run-journeys.sh`:

- **Own project and own image names** (`mc-journeys-backend:nightly`,
  `mc-journeys-frontend:nightly`). `docker-compose.yml` builds into the
  published image tag — building from it would overwrite the images a live
  install on the same machine runs.
- **Nothing shared with a live install:** no `~/.mc` mount (the backend's
  home is an empty scratch folder), no Docker socket, no SSH keys, no channel
  or provider tokens, database on tmpfs, fresh random secrets per run.
- **Background services off** (`ENABLE_BACKGROUND_SERVICES=false`): no
  scheduler, no Slack/Telegram, no runtime watcher, no heads. The API answers
  requests; agent delivery is the poll path, which lives in the API.
- **One port**, `127.0.0.1:18080` (`MC_JOURNEYS_PORT`), behind a small Caddy
  with the same `/api` routing as the product `Caddyfile`.
- About 0.8 GB of RAM while it runs; a cached build plus the run takes about
  a minute.

## The fake harness

A stand-in for an agent runtime that can do only the minimum: take a task,
ask, block, and record everything MC delivers. It speaks the same HTTP as the
real bridges — `GET /api/v1/agent/me/poll` with the agent token, plus the
agent-scoped endpoints — so it needs no container, tmux or model. The agent
row is created through `POST /agents` with `agent_runtime: "manual"` (no
provisioning); the `comm_v2` pilot flag has no API setter, so the fixture
sets it in the test database.

## Running it

```bash
cd e2e && npm ci                          # once
e2e/run-journeys.sh --src .               # this checkout, as it is
e2e/run-journeys.sh --src . --keep        # leave the stack up for debugging
e2e/run-journeys.sh --src . --grep usage  # one journey
```

With a stack left up by `--keep`, the specs also run directly (the env file
holds that run's operator password):
`cd e2e && set -a && . ~/.mc/journeys/stack.env && set +a && npx playwright test`
(base `http://localhost:18080`).

Result: `~/.mc/journeys/last.json` (`status` green / red / skipped / error,
one entry per test, `known_gaps`) and `~/.mc/journeys/runs/<stamp>/` with the
log and the Playwright HTML report (traces and screenshots on failure). The
last 14 runs are kept. Exit code 0 green or skipped, 1 red, 2 error.

**Preflight — the run skips (status `skipped`, exit 0) when:** a head is
working (a request in `heads/spool/processing`, or a head heartbeat in the
last 3 minutes), a deploy is going on (a running `docker compose build/up`
of the live project; a deploy script can also drop `~/.mc/locks/deploy.lock`
— nothing creates that file yet), less than 20 GB of disk are free, or port
18080 is taken. A step that dies (clone, fetch, npm) is reported as
`error`, so `last.json` never keeps showing an older run. The whole run,
build included, is limited to 30 minutes; the stack is always torn down with
`down -v`, also on failure or timeout. Afterwards only dangling images labelled `mc-journeys=1` are
pruned — never a global prune, the build cache is shared with the live
install.

## Nightly rollout (operator step)

```bash
e2e/install-journeys.sh                    # copies the runner to ~/.mc/bin/mc-journeys,
                                           # renders ~/Library/LaunchAgents/com.mc.journeys.plist
~/.mc/bin/mc-journeys                      # one run by hand (clones the repo into ~/.mc/journeys/src)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mc.journeys.plist
```

The job runs at 05:15, after the night-shift window, and tests `origin/main`
from the runner's own clone. Uninstall: `launchctl bootout
gui/$(id -u)/com.mc.journeys`, then remove the runner and the plist.

**On Home** one quiet line under the metrics card shows the last result
(`GET /api/v1/system/journeys`, read from `last.json` through the `~/.mc`
mount): "Journey tests: 3 of 3 passed, 1 known gap · 5 h ago", or which
journeys failed, or why the run was skipped. Nothing shows before the first
run. Next step: on-demand runs for a PR head with the result as a GitHub
commit status.

## Known gaps pinned by a test

A journey that finds a product gap does not stay red forever and does not
hide the gap either: the test runs every step up to the gap and then asserts
that the gap **still exists**, with `[known gap: …]` in its title
(`last.json` lists them under `known_gaps`). When someone fixes the gap the
test turns red, and the fix PR flips the last assertion into the real effect
check.

| Test | Gap |
|---|---|
| `J-phone-needs-you` › a waiting question is answered from the phone | A blocking question parks the task in `waiting`. "Reply" posts a plain comment; comments are not delivered while a task is `waiting`, so the agent never gets the answer and the task stays parked. The resuming answer path (`POST /tasks/{id}/thread/messages` with `reply_to`) has no UI caller. The task card also does not show the question itself. |

## Writing a new journey

1. The file name is the journey id from `landkarte.yaml`; the test ends on
   an effect a user would care about (agent got it, number is right, record
   is back), never on "the button was clicked".
2. Create data through the real API (`lib/mc.ts`, `FakeAgent`); use SQL only
   for rows that have no API (say so in a comment).
3. Unique titles per run (`Date.now().toString(36)`), own board per test,
   `signIn(page, token, board.id)` so Home shows that board.
4. Prefer roles and visible labels; where a list has no stable handle, add a
   `data-testid` / `data-*` hook in the component.
5. Sabotage once before merging: break the element or the call the journey
   needs and watch it turn red.
6. `kz check --update-baseline` removes the journey's `orphans:…:test` key.
