# Journey tests (E6)

A journey is something the operator wants to get done, start to finish
(`docs/produkt/landkarte.yaml`, `journeys:`). A journey test clicks through
it in a real browser against a real MC backend and ends on the **effect**,
not on the click: "the agent received the instruction", "Insights shows
exactly the recorded cost". Unit tests prove the parts; journey tests prove
that the parts still reach each other.

They run against a throw-away stack in two places: **nightly on the
operator's machine** (launchd, below) and **nightly on GitHub-hosted runners**
(`.github/workflows/journeys.yml`, also on demand and on PRs that change
`e2e/`, the workflow or `install.sh`). Never on a self-hosted runner — the
repository is public, fork code must never run on the operator's machine.
They are not part of the per-PR CI: a stack build plus the run takes
10–15 minutes.

## What is there

| Path | What |
|---|---|
| `e2e/journeys/J-<id>.spec.ts` | One file per journey, named as in `landkarte.yaml` (`test:`). `kz check` counts a missing file as a gap. |
| `e2e/journeys/lib/mc.ts` | Operator API calls, fixture SQL, sign-in. Refuses the ports a normal install uses. |
| `e2e/journeys/lib/fakeHarness.ts` | The fake harness (below). |
| `e2e/journeys/lib/stack.ts` | Stop/start one stack service for a sabotage step (`withServiceDown`). |
| `e2e/stub-model/server.py` | The stub model (below). |
| `e2e/stack/` | The test stack: `docker-compose.journeys.yml` + a small `Caddyfile`. |
| `.github/workflows/journeys.yml` | Nightly/on-demand run on GitHub-hosted runners, plus the installer's "port busy" check (S0). |
| `e2e/run-journeys.sh` | Build → start → test → tear down, with preflight and time box. |
| `e2e/install-journeys.sh` + `com.mc.journeys.plist.template` | Nightly launchd job (installed copy, never a file in a checkout). |

| Journey | Covers | Proves |
|---|---|---|
| `J-phone-needs-you` | Home → Inbox (tab bar) → task, at 390 px | A blocked task is unblocked from the phone and the agent receives the instruction and the task again; afterwards nothing is blocked. A waiting question (`mc ask --blocking`) is answered in the task card: the agent receives the answer once and the task runs again. |
| `J-usage` | Insights, at 390 px | Recorded usage rows show up as exactly that cost, local share (this week, last week), day and source. |
| `J-first-run` | Login → setup → Runtimes → Home → Inbox, at 390 px | The onboarding flow S0–S10 on a fresh stack: the first admin is created in the browser, the operator's own model (the stub) is added by address and answers the probe, the first agent asks before it commits, the answer from the phone reaches it once and it carries on to review. Sabotage "model gone": the add dialog and the runtime check say so in words, and it recovers. |
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
- **The only AI is the stub model** (`stub-model` service), reachable inside
  the stack as `http://stub-model:8080/v1`; no provider keys, no network.
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

## The stub model

`e2e/stub-model/server.py` stands in for a local model server (Ollama, LM
Studio, vLLM): `GET /v1/models` lists one model, `stub-model`, and
`POST /v1/chat/completions` answers one fixed sentence, plain or streamed.
Standard library only, so it runs in a bare `python:3.12-alpine`
(`backend/tests/test_e2e_stub_model.py` tests it, including that MC's probe
picks its model). A journey stops its container for "the model is gone"
(`withServiceDown("stub-model", …)`) and always starts it again.

## First run (S0–S10)

`J-first-run` follows the onboarding plan's screens on today's product and
names the PR that replaces a step, so that PR swaps one step instead of
writing a second journey:

| Step | Today | Replaced by |
|---|---|---|
| S0 install | the stack itself; the installer's port line is the workflow's `install-preflight` job (another program on 8000 → `Port 8000 is already in use…`, before any build) and `backend/tests/test_install_port_preflight.py` | — |
| S1 account | the login page creates the first admin, lands in `/setup` | wizard rebuild (3-2) |
| S2 AI | cloud key skipped; the stub is added under Runtimes → "Connect by address", then "Re-probe model" (the stored check turns `ok`) | wizard AI step (3-2) |
| S3–S4 first agent and job | API fixture: a lead on the fake harness, bound to the stub runtime | first-run call with lead template and practice repo as a registered repo row (3-1) |
| S5 the agent works | the job in the running lane on Home | the chat hand-over (3-2) |
| S6–S8 phone pairing | not built; the test already runs at 390 px | device pairing (Welle 4) |
| S9 the question | `mc question` with options → Inbox card, option + Reply | chat question card, one tap (3-3) |
| S10 effect | the agent receives "Commit" once, carries on, posts the commit as progress, moves to review; only the review waits | — |

Each step is a `test.step`; `result.json` keeps every step's wall-clock time
(`journeys[].steps`), so a slower step shows up run over run.

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
one entry per test with its step times, `known_gaps`) and `~/.mc/journeys/runs/<stamp>/` with the
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
run. A red line names the failed journeys by their short human name
(`home.journeys.names.<id>` in the i18n files, one per journey in
`landkarte.yaml` — a test enforces that; an id without a name shows as the
id). Next step: on-demand runs for a PR head with the result as a GitHub
commit status.

## Known gaps pinned by a test

A journey that finds a product gap does not stay red forever and does not
hide the gap either: the test runs every step up to the gap and then asserts
that the gap **still exists**, with `[known gap: …]` in its title
(`last.json` lists them under `known_gaps`). When someone fixes the gap the
test turns red, and the fix PR flips the last assertion into the real effect
check.

None open. Closed so far:

| Test | Gap | Closed by |
|---|---|---|
| `J-phone-needs-you` › a waiting question is answered from the phone | "Reply" on a `waiting` task posted a plain comment, which is not delivered while the task waits; the task stayed parked. | #712 — the card shows the open question with an answer field; Reply is a thread reply to it (`reply_to`) and resumes the task. |
| `J-first-run` › model gone | With the model server down, "Re-probe model" kept the last model id and said "Model unchanged: stub-model"; the add dialog showed the HTTP client's raw English error as the only line. | The PR that added this journey — the probe answer carries its stored check and the panel words a failure from its reason ("No chat model answers at this endpoint."); the dialog says "Endpoint unreachable." with the raw text as a detail. |
| install-preflight (S0) | With another program on port 8000, the installer failed only at `docker compose up` with Docker's raw error — on the prebuilt path as "did not come up in time". | Same PR — `mc_port_preflight` checks every published port before pull or build and names the busy one. |

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
