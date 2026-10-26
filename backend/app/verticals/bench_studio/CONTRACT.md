# Bench W1 contract — "Testen in 2 Taps"

Frozen 24.10.2026 on branch `feat/bench-w1` (lead commit). Four packages
build in parallel against it: **A** backend + renderer · **B** test sheet +
live run · **C** result page + X-Kit · **D** entries + list. Sources of
truth for behaviour: `~/.mc/bench-neu/30-konzept.md` (§3, §5, §6, §7, W1
table), `33-entscheide.md` (D1–D5 = B, C, B, B, A), the approved mockups
`~/.mc/bench-neu/mockups/*.html`, and the lead plan
`~/.mc/bench-neu/w1/plan.md`.

## 1. What is frozen (change only with the lead)

| Piece | Files | Rule |
|---|---|---|
| API shapes | `backend/app/verticals/bench_studio/w1_schemas.py` ⇄ `frontend-v2/src/verticals/bench_studio/w1/types.ts` | Change both + rebuild fixtures in one commit, lead OK first |
| Routes | `w1_routes.py` (paths, methods, status codes, query params, response models) | A fills the bodies, never the signatures |
| Fixtures | `w1_fixtures/*.json` + generated `w1/fixtures.ts` | `cd backend && python -m app.verticals.bench_studio.w1_fixtures.build` — never edit by hand |
| API client | `frontend-v2/src/verticals/bench_studio/w1/api.ts` | B/C/D call `benchW1Api` + `benchW1Keys`, no second fetch path |
| Core entry helpers | `frontend-v2/src/lib/benchLinks.ts` | URL shape `/bench?test=1&runtime=&preset=&template=&rerun=&from=` and `/bench/r/<id>`; core reaches the bench only through this file |
| Component signatures | `w1/sheet/TestSheetHost.tsx`, `w1/live/LiveRun.tsx`, `w1/result/ResultPage.tsx`, `w1/kit/XKitSheet.tsx`, `w1/list/TestList.tsx` | Props are frozen; the owner fills the body |
| Schema | `backend/app/models/bench.py` + `alembic/versions/0224_bench_w1_schema.py` | One head. A may add 0225 only with the lead (no second schema owner) |
| Shared words | `messages/{en,de}.json` → `benchShared` | Lead only; packages use them for every state/section/wait/abort/blocker/harness/level/cost/gate word |

Guards (must stay green in every package):
`backend/tests/test_bench_w1_contract.py` (fixtures = schema dump, every
route answers with its model, migration backfill) and
`frontend-v2/src/verticals/bench_studio/w1/__tests__/contractFixtures.test.ts`
(TS mirror = backend JSON, link round-trip, 409 cost parse, benchShared
covers every code in EN + DE).

## 2. Routes (all `require_user`, prefix `/api/v1/bench`)

| Method + path | Model | Notes |
|---|---|---|
| `GET /plan?runtime_id&preset&template_id&rerun_of` | `BenchPlan` | Pre-filled sheet. No runtime → most recently started live runtime. Read-only: never probes, never a completion |
| `GET /contestants` | `ContestantList` | local (one row per engine, `pairs._dedupe_local`) + cloud |
| `POST /tests` | 201 `BenchTestDetail` | Body `BenchTestCreate` (1–2 picks, never the same pick twice → 422 `bench_pick_duplicate`). Paid run: 409 `{detail: CostConfirmation}` step 1, over $1 step 2 (`confirm_over_cap`). Cloud API with no known price: 409 `CostConfirmation{usd: null, price_unknown: true}`, confirmed with `confirm_unknown_cost` |
| `GET /tests?section&model&device&harness&run_class&include_archived` | `BenchTestList` | Human states; GET never writes |
| `GET /tests/{id}` | `BenchTestDetail` | Result + live (poll `LIVE_POLL_MS` while waiting/running) |
| `POST /tests/{id}/rerun` | 201 `BenchTestDetail` | `{budget: "same" \| "larger", cost_confirmed_usd?, confirm_over_cap?}` — "Nochmal" / truncation re-run. Paid model: 409 CostConfirmation like `POST /tests`; the page confirms on this route, the budget stays (integration 24.10.) |
| `POST /tests/{id}/seen` | `SeenResponse` | The result page of a FINISHED test was opened: resolves its Eingang item `bench_result` (no side effect, idempotent; a live test resolves nothing) |
| `POST /tests/{id}/stop` | `BenchTestDetail` | |
| `POST /tests/{id}/harvest` | `BenchTestDetail` | "Zahlen nachladen" → new receipt version + recompose |
| `GET /tests/{id}/kit` | `XKit` | English texts; red = preview only |
| `POST /tests/{id}/kit/download-token` | `DownloadToken` | Resource-scoped, short-lived |
| `GET /tests/{id}/kit/files/{video.mp4\|card.png\|kit.zip}?token=` | file | `Content-Disposition: attachment`; red gate → 409 |
| `GET /runtime-summary` | `RuntimeTestSummary[]` | Runtimes cards: testable, primary (K11), last test, `planned_test` (an auto test after a switch that has no runs yet) |
| `POST /auto-tests` | 201 `AutoTestResponse` | From the switch dialog; waiting test, superseded by a later switch |

The legacy routes (`/challenges*`, `/entries*`, `/effort-levels*`,
prompt templates) keep working until A's kill-list step; the result page
may still call `rerender`/`recompose`/`archive`/`delete`/entry `view-token`
through the old `benchApi` (C), mapped by `BenchTestDetail.allowed`.

Stub marker: until A builds a route it returns its fixture with header
`X-Bench-Contract-Stub: 1` (kit files: 501). Integration gate:
`grep -c "_stub(response" w1_routes.py` = 0, no 501 left.

### Settled at integration (24.10.2026)

- **`bench_blocked` 409** (`POST /tests`, rerun): `{detail: {code: "bench_blocked", message, blockers: [{code, blocking}]}}` when a pick has a hard blocker found after the plan was read. The sheet words the blockers through `benchShared.blocker` (`parseBenchBlocked`), never the raw body.
- **Extra reason codes** `agent_paused` (harness option of a paused agent) and `files_not_ready` (kit `blocked_reason`) live in `benchShared.reason`. Codes with their own path, not a sentence: `bench_blocked`, `bench_cost_confirmation`, `gate_red`, `harness_default` (no note), `omp_session_default` (`benchSheet.ompSession`), `price_unknown` (`cost.unknown`). Guard: `contractFixtures.test.ts` scans `w1_*.py` for every `"code"` and fails on a code without a word.
- **`allowed` `"kit"`** = the kit can be downloaded (green/amber, finished). A finished red test shows the kit as a preview from the ⋯ menu; its files answer 409.
- **Paid re-run:** `POST /tests/{id}/rerun` takes the cost confirmation itself (see §2); the result page and the X-Kit sheet ask on the same button (`useRerunLarger`).
- **Review fixes (24.10., PR #982 reviews):**
  - A run that thinks until `max_tokens` and writes no page keeps `finish_reason`/`truncated`/`max_tokens` on the failed entry, is not retried, and counts in `truncation` (also on an aborted test: `runs_without_page`, `rerun_larger` allowed). `video_run_cut` = run 1 (the video) was cut.
  - Budget (operator decision 25.10.2026 — time, not tokens): a local run sends `max_tokens` = context − 4096 (`max_tokens_rule` "context"), or none when the context limit is unknown ("engine"); every other run keeps a cap ("cap": 32k × effort factor, at most 64k, "default" counted as the level the probe saw). Every direct run ends after `time_limit_min` (120) with error code `time_limit`, not retried. `truncation.larger_budget` is false when only uncapped runs were cut — then `rerun_larger` is not allowed. Receipts of class A carry `max_tokens_rule` and `time_limit_min`.
  - Card, alt text and settings text count the MEASURED runs behind the median ("median of 2 of 3", foot "· 1 run failed"); the card prints the full chips (device/provider), `chips_short` only for the 790 px duo row of the 16:9 receipt card.
  - D4 fail-closed: a run with real or unknown cost is attempted once (no spend past the OK); a paid run never probes (stored probe only); `cost_of` counts every attempt that reported usage. Ranking api_price > unknown > subscription > local on both sides.
  - Every direct attempt writes one `model_usage_events` row (harness `vanilla`, `$` only on `api_price`).
  - `EffortRecord.default_level` = the level the server default maps to ("hoch (Standard)" on sheet, live card, result page; "effort high (default)" on the card).
  - Run error codes are words in `benchShared.reason` (guard scans `RunError("…")`, `"error_code": "…"`, `code = "…"` too).
- **Removed with the kill list:** `/effort-levels*` (Spark-only; the plan probe replaces it), besides `/spark-models`, legacy `POST /challenges` and the X draft route.

### Gate rule R6 — the page needed the internet (live finding 25.10.2026)

- The recorder blocks every request (W0.9). `/record` now also answers `network_blocked_scripts` = how many blocked requests were scripts/modules (Playwright `resource_type == "script"`: `<script src>`, an ES-module import resolved by an import map, a worker). It travels into `entry.metrics` (`_RECORD_REPORT_KEYS`) and the receipt (only when reported, so old receipts keep their checksum).
- **Red `R6` / `needs_network`:** at least one blocked script/module — the page's own code never ran (e.g. `import * as THREE from 'three'` from a CDN), the video shows an empty scene. Also red when attempts > 0 but the sidecar did not report the type (older image): it cannot prove the scene drew. Kit files answer 409 like every red gate.
- **Amber `network_assets_blocked`:** only fonts/images/stylesheets/other requests blocked — the scene still draws; printed in the settings text notes.
- `page_check` is `"unknown"` (not `"loads"`) when a script was blocked: the recorder saw a frame, not the page's work.
- The pinned local three.js (lane `lib:three-local`, below) is the fairness fix: a three.js request on a known CDN is no longer blocked, so it never counts for R6.

### Lane `lib:three-local` — MC serves one pinned three.js (25.10.2026)

- **What:** `docker/mc-playwright/vendor/three/` holds ONE pinned three.js (r170: `three.module.min.js` verbatim, `three.global.min.js` = classic `window.THREE` built with esbuild, 17 addons from `examples/jsm`, MIT `LICENSE`, `MANIFEST.json` with sha256; rebuild with its `build.sh`). `record_three.py` answers requests for it on known CDNs (unpkg, jsDelivr, cdnjs, esm.sh, Skypack, jspm, threejs.org): module, classic global, the vendored addons (`three/addons/…` = `examples/jsm/…`) and classic `examples/js/…` stubs. A bare `import … from 'three'` without an import map gets MC's import map injected into the page. **Everything else stays blocked and counted** (other libraries, addons MC does not ship, the WebGPU build, any other host).
- **Automatic, not opt-in:** the lane is set when the page asked for three.js — no flag, no extra tap in the two-tap flow. It is labelled everywhere instead: `/record` answers `lib_track` (`three-local` | `none`), `libraries_provided` (`["three.js r170"]`), `three_requested` (`["r160"]`), `three_version_mismatch`, `three_import_map_injected`; they travel into `entry.metrics` (`LIB_LANE_KEYS`) and the receipt (only when reported — old receipts keep their checksum). `ContestantResult.lib_track` mirrors it.
- **Never compared silently:** a block with contestants in different lanes gets amber `lib_tracks_mixed` (printed), a page that asked for another version gets amber `three_version_differs`. The video/card chips and the result page chip say `three.js r170`; the settings text says `three.js r170 served offline by the recorder (page asked for r160) · page loads three.js from a CDN`.
- **Served copy ≠ pasted copy:** `libraries_inlined` scans only the page's own HTML, so the served file never counts as pasted.
- **Prompt variants:** a "with three.js" prompt should load exactly the pinned version (no mismatch): import map `"three": "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js"`, `"three/addons/": "https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/"`, and name the allowed addons. The system prompt is unchanged (`system_prompt_sha` stays).
- **Render cost (measured 25.10.2026, Mac mini M4, Docker, SwiftShader software GL, 2880×1620):** simple lit cube 56 ms/frame (20 s video ≈ 34 s); 1 600 instanced shadowed boxes + bloom 871 ms/frame (20 s video ≈ 8.7 min, the capture cap is 900 s — a heavy scene longer than ~20 s times out).

## 3. Behaviour rules every package keeps

- **Names:** `display_name` = served model id verbatim (gate R2); alias only next to it. Never the agent name on artefacts.
- **Money (D3/D4):** local = "lokal" + small hypothetical API price; subscription = "über dein Abo" (no $ on the button); `api_price` = "Start · $X — OK?", over $1 a second confirmation. Never a bare `$` on local/subscription, never "$0.00" from the `*` fallback.
- **Classes:** A before B, never at the same time on one box, never one table (`blocks[]`, divider). Class B: no tempo row, n=1, effort display-only (D5).
- **Missing headline number:** "nicht gemessen: Grund" (K15, `Stat.measured=false`); other empty values are omitted (K3).
- **Truncation (operator decision 24.10.):** NOT on any public artefact. The result page and the X-Kit sheet warn the operator privately (`truncation`) and offer "re-run with a larger budget" (`rerun` budget `larger`) before the download.
- **Public artefacts English**, footer = config `MC_BENCH_FOOTER` (the operator's handle lives in his `.env`, never in the repo); the episode site only when an episode is published (`texts.link`). No X posting, no X API.
- **UI:** every string through i18n EN + DE; DESIGN.md K1–K19 + `docs/design/ui-craft.md`; impeccable skill; chips ≥ 44 px (K11); one primary per surface; numbers/durations in sans tabular (mono only for sha/ids, K7); phone 390 px and desktop 1280 px.

## 4. Ownership (disjoint — touch only your files)

| Pkg | Owns (create/edit/delete) | i18n namespace |
|---|---|---|
| **A** | `backend/app/verticals/bench_studio/**` except `w1_schemas.py`/`w1_fixtures/**`/`CONTRACT.md` (contract), `backend/app/models/bench.py` only via the lead, `backend/tests/test_bench_*` (new: `test_bench_w1_*`), `docker/mc-playwright/**`, `docs/decisions/070-*` amendment, `docs/produkt/landkarte.yaml` bench status, backend Inbox item creation (`Approval(action_type="bench_result")`) | — (English artefact texts live in backend) |
| **B** | `frontend-v2/src/verticals/bench_studio/w1/sheet/**`, `w1/live/**`, their `__tests__` | `benchSheet` |
| **C** | `frontend-v2/src/app/bench/r/**`, `w1/result/**`, `w1/kit/**`, their `__tests__` | `benchResult` |
| **D** | `frontend-v2/src/verticals/bench_studio/BenchStudioPage.tsx`, `ChallengesTab.tsx`, `PromptLibraryTab.tsx`, `w1/list/**`, legacy UI removal (`NewChallengeDialog`, `EffortChips`, `ChallengeDetail`, `DraftDialog`, `EntryEffort`, `EntryTaskState` + their tests, legacy `api.ts`/`types.ts` trim), core entries: `src/app/runtimes/**` (card "Testen", cloud rows), `src/components/shared/HostRecipeSwitcher.tsx` (toggle), `src/components/layout/QuickSheet.tsx` (row), `src/components/inbox/**` (bench_result item) | `benchHome` |

- B and C **never import legacy files** (`NewChallengeDialog`, `EffortChips`, `ChallengeDetail`, `EntryEffort`, …): copy what you need into `w1/`. D deletes them.
- i18n: add keys **only inside your namespace, directly below its `_owner` line** (keeps merges conflict-free). Shared words → ask the lead.
- New shared helpers (formatters) go into your own folder; if two packages need the same one, the lead moves it into `w1/` after integration.

## 5. Done per package (before handing back)

Red test first → implementation → sabotage probe (break it, see red,
restore) → green: backend in the throwaway container
(`docker run --rm --network none -v <wt>/backend:/w -w /w --entrypoint python <backend image> -m pytest -q -p no:cacheprovider <tests>`),
frontend `npx tsc --noEmit` + `npx vitest run --maxWorkers=4` (design
ratchet included). Screenshots at 390 px and 1280 px from fixtures
(no live stack, no DB writes, no deploy, no real bench run). Privacy scan
before push. Never `git stash`.
