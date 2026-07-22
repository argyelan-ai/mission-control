# Runtime Adapter Contract

How Mission Control drives a CLI agent runtime (claude, openclaude, omp, grok, …)
inside a tmux window, and how to onboard a new or updated CLI safely.

The fleet runs many CLIs behind one dispatch loop (`docker/shared/poll.sh`). Each
CLI is a *runtime adapter*: a thin layer that lets poll.sh answer four questions
about a tmux pane. claude-cli 2.1 broke every scraping heuristic at once — NBSP
prompt, new spinners, collapse chips — and cost 6 production bugs fixed live on
the fleet. This contract plus the golden-fixture TCK
(`backend/tests/test_adapter_tck.py`) exist so the next CLI update is caught by a
red test, not a broken fleet.

## The four-function contract

Every adapter must answer these, in poll.sh terms:

| # | Concern | Function(s) today | Location |
|---|---------|-------------------|----------|
| 1 | **Is the agent idle / working / crashed?** | `detect_turn_state` (+ `extract_turn_error`, `turn_activity_hash`) | `lib/turn-state.sh` |
| 2 | **Deliver a message and submit it** | `paste_and_submit` | `lib/paste-verify.sh` |
| 3 | **Verify the message actually landed** | `verify_paste` | `lib/paste-verify.sh` |
| 4 | **Which runtime is this pane?** (routes 2/3) | `detect_pane_ui` | `lib/ui-detect.sh` |

`reset_session` (session restart / clear) is handled by poll.sh + the container
entrypoint per runtime; it is not a separate lib function today.

These libs live **byte-identical** in two hand-maintained copies —
`docker/mc-agent-base/lib/` (openclaude/omp base) and
`docker/mc-claude-agent/lib/` (claude image). `build-agent-images.sh` syncs only
`shared/poll.sh`, not `lib/`, so the copies are kept in sync by hand. The TCK
byte-compares them (`test_lib_copies_byte_identical`) — a fix that lands in one
image and not the other is a silent split-brain.

## Signal hierarchy — prefer the least fragile source

When answering "is the agent idle or working?", use signals in this order. Each
tier down is more fragile; scraping is the last resort precisely because it is
what breaks on CLI updates.

1. **Native signal / hook (best).** A first-class event the CLI emits.
   claude-code fires `UserPromptSubmit` / `Stop` hooks (W2.1 Phase A) that append
   `<epoch> submit|stop` to `~/.turn-signal`. `detect_turn_state` reads this file
   first (`TURN_SIGNAL_MODE=auto`). Deterministic, no pane parsing.
2. **Structured output.** JSON / machine-readable state the CLI can print
   (e.g. `--output-format json`, a status socket). None of the current CLIs
   expose a usable turn-state stream, but prefer this over scraping if a new CLI
   does.
3. **Pane scraping + fixtures (fallback, PFLICHT).** Parse `tmux capture-pane`.
   Fragile — this is the layer that broke 6× on claude 2.1. It stays mandatory
   because hooks don't cover every case: `Stop` does NOT fire on user-interrupt
   (Esc) or a mid-turn API/network crash, so scraping remains authoritative for
   `crashed` and for the staleness fallback. The golden fixtures under
   `backend/tests/fixtures/panes/<cli>/` pin this layer per CLI.

`PANE_UI_OVERRIDE` (baked into each image) short-circuits tier 3 for
`detect_pane_ui`: claude-cli 2.1.x dropped its box glyphs and is now visually
identical to openclaude in a bare pane, so the heuristic alone cannot tell them
apart. The override is the reliable production path; the heuristic stays as a
fallback for host agents without the ENV.

## Golden fixtures & the TCK

`backend/tests/fixtures/panes/<cli>/` holds real pane snapshots per CLI:

- `<state>.txt` — plain `tmux capture-pane -p` (what the libs scrape).
- `<state>.esc.txt` — capture with escape codes (`-e`), secondary/debug.
- `meta.json` — `cli_version`, `container_image`, `recorded_at`, `states`,
  `true_runtime`, and the blessed `expected_ui` heuristic output.

`test_adapter_tck.py` parametrizes over every CLI directory, so **onboarding a
new CLI needs no test edits** — record the fixtures and the suite runs against
them. It asserts, in scrape mode (`TURN_SIGNAL_MODE=scrape`, because the fixtures
test the scraping layer, not the hook path):

- `idle.txt ⇒ idle`, `working.txt ⇒ working`, `crashed.txt ⇒ crashed`
- `detect_pane_ui` (no override) matches `meta.json:expected_ui` (regression guard)
- `detect_pane_ui` with `PANE_UI_OVERRIDE=<true_runtime>` resolves the true runtime
- both lib copies byte-identical

Known scraping gaps are encoded as `xfail(strict=True)` in `_KNOWN_SCRAPE_BUGS`
(currently `claude/working`, see below) — they assert the *correct* expectation,
so when turn-state.sh is fixed the xfail flips to XPASS and forces removal of the
entry. The suite never papers over a real bug by blessing wrong output.

### Known gap: claude 2.1.x working turn scrapes as idle

claude-cli 2.1.x renders its input box with a bare `❯` at all times, including
mid-turn. `detect_turn_state`'s idle check (`tail -5 | ^❯ *$`) runs before the
working check, so a signal-less active turn scrapes as `idle`. In production this
is masked by the Phase-A hook signal (`submit ⇒ working`); it only bites in
scrape mode. Fix belongs in `turn-state.sh` (reorder: crashed → working → idle,
or gate the bare-`❯` idle check on absence of `esc to interrupt`). Tracked as the
`claude/working` xfail.

## Onboarding checklist — a new or updated CLI

1. **Look for a native signal first.** Does the CLI have hooks / events /
   structured status output (tier 1–2)? Wire that before touching scraping. For
   claude-code that's the `UserPromptSubmit`/`Stop` hooks → `~/.turn-signal`.
2. **Record golden fixtures** from a running container, for each turn state:
   ```
   tools/record-pane-fixtures.sh <container> <tmux-target> <cli-name> idle
   tools/record-pane-fixtures.sh <container> <tmux-target> <cli-name> working
   ```
   Record `working` by submitting a short prompt and capturing during the
   spinner (an in-container tight capture loop avoids per-frame docker latency).
   `crashed` may be synthetic (hand-write an `API Error: fetch failed` pane).
3. **Run the TCK** — it auto-discovers the new directory:
   ```
   cd backend && .venv/bin/python -m pytest tests/test_adapter_tck.py -v
   ```
4. **If the heuristic can't classify the pane** (looks like another runtime),
   bake `PANE_UI_OVERRIDE=<cli>` into the image and confirm the override test.
5. **If scraping misclassifies a state**, fix the heuristic in **both** lib
   copies (keep them byte-identical) or, if the fix is out of scope, encode the
   gap as an `xfail` with a reason and escalate.
6. **Pin the CLI version** in `docker/cli-versions.json` and commit the fixtures.

## Key files

- `docker/mc-agent-base/lib/`, `docker/mc-claude-agent/lib/` — the adapter libs (byte-identical copies)
- `docker/shared/poll.sh` — the dispatch loop that calls them
- `docker/cli-versions.json` — pinned CLI versions
- `tools/record-pane-fixtures.sh` — fixture recorder
- `backend/tests/test_adapter_tck.py` + `backend/tests/fixtures/panes/` — the TCK
- ADR-071 — W2.1 Delivery Foundation (native signals, pull delivery, this TCK)
