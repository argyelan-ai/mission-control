# E0 baseline — token usage, page usage, heads (September 2026)

Measured on 2026-09-24 (read-only). Scope: the three open E0 items in
[`ROADMAP.md`](../ROADMAP.md) — token baseline per source and week, page usage
from existing logs, and the head numbers that feed stop 1
([ADR-085](../decisions/085-head-per-job.md),
[ADR-086](../decisions/086-harness-runtime-cross-switch.md),
[head launcher spec](../specs/head-launcher.md)).

Every number below carries its source. "Measured" means a query or file read
on 2026-09-24; nothing here is carried over from notes.

## Summary

| Question | Answer |
|---|---|
| Token baseline, 6 weeks (2026-08-17 → 2026-09-24 18:02 UTC) | 142,974 usage events · **28.83 B tokens** in total, of which 97.3 % are cache reads · **791.6 M** non-cache tokens (input + output + cache write) · **86.6 M** output tokens |
| USD list-price equivalent, same window | **$15,066.07** — cloud (Anthropic list prices) $14,993.31, local notional price $72.56, other cloud $0.20. Stored `cost_usd` already equals a fresh recompute (max. difference per row 1e-11 USD), so the recompute after #650 has run |
| Local share | 6.5 % of all tokens, 8.7 % of output tokens (6 weeks); peak week 11.8 % of output |
| Biggest source | Operator's interactive sessions: 59.7 % of the USD equivalent ($8,986.79), then the lead agent (17.2 %), then two cloud-heavy container agents (11.9 % and 6.7 %) |
| Quiet mode (since 2026-09-22) | Persistent agents from 2026-09-22 12:00 UTC until the measurement: 20 events, $0.50. Lead agent: no usage event after 2026-09-21 19:38 UTC. Operator sessions in the same window: 6,489 events, $625.24 |
| Page usage from existing logs | **Not measurable.** The proxy writes no access log; the backend log holds API calls only, without referrer, and only since the last container start (~3 h). A proxy signal exists (see §2) but is biased toward pages with live streams. Minimal proposal in §2.4 |
| Head numbers | 2 head runs, 1 job, both on the local pair (omp × local GPU runtime), **0 passed, 2 failed**, 0 cloud runs. Head token usage is **not recorded anywhere** |

## 1. Token baseline

### 1.1 Method

- Source: `model_usage_events`, `ts >= 2026-08-17 00:00 UTC`, grouped by
  week (Monday 00:00 UTC), source, harness and model. Read-only `SELECT`.
- The last week (starting 2026-09-21) is partial: Monday to Thursday
  18:02 UTC (latest `harvested_at` 2026-09-24 18:02 UTC).
- **USD** = recompute with the current `model_prices` table and the same
  matching rule as `match_price()` in `backend/app/services/token_harvester.py`
  (glob on `model_pattern`, `valid_from <= ts`, highest `priority` wins). All
  price rows used were valid for the whole window.
- Recompute check: the recomputed value equals the stored `cost_usd` on every
  (week, source, model) row, maximum difference 1.0e-11 USD. Sabotage probe:
  raising the Opus 5 output price from 25 to 26 USD/MTok in a copy of the price
  table moves the 6-week total from $15,066.07 to $15,097.80 (+31.7 M output
  tokens × $1), so the comparison is not trivially equal.
- Anthropic usage runs on subscriptions; the USD figure is a **list-price
  equivalent**, not an invoice.

**Source buckets** (how the harvester attributes rows, verified against
`source_file`):

| Bucket | What it is | How identified |
|---|---|---|
| Operator sessions | Interactive coding-CLI sessions on the host, including their subagents and workflow agents | Host CLI project directories other than the lead's checkout. The harvester attributes these to the lead agent's id (its host-session attribution rule in `token_harvester.py`), so `agent_id` alone mixes both |
| Lead agent | The persistent lead agent on the host (coding CLI, cloud) | Project directory of the MC checkout the lead process runs in (verified: working directory of the running lead process) |
| Persistent agents A–K | Container and host agents with their own config directory | `agent_id` + per-agent config directory |
| Heads | Short-lived heads from the head launcher | **No rows** (see §3.3) |
| Other | Benchmark entries, one probe agent | 4 rows, 0.1 M tokens |

**Local vs. cloud** (per model, checked against the `runtimes` table):

| Class | Models |
|---|---|
| local | `GLM-5.3-Flash-EXL3`, `qwen38-*`, `qwen3.8-*`, `deepseek-v4-flash-vision-exp` |
| cloud, flat rate | `deepseek-v4.1-flash`, `deepseek-v4-flash`, `glm-5.1`, `glm-5.2` (hosted flat-rate runtimes; `deepseek-v4-flash` also exists as a disabled local runtime — 95 rows, ambiguous, counted as cloud) |
| cloud, Anthropic | `claude-*` |
| cloud, other | `grok-*`, `gpt-*`, `codex-mini-latest` |

### 1.2 Per week — all sources

| Week from | Events | Tokens total (M) | Non-cache (M) | Output (M) | USD equiv. | Local share of output |
|---|---:|---:|---:|---:|---:|---:|
| 2026-08-17 | 16,361 | 4,690.4 | 169.6 | 9.9 | 3,595.08 | 1.1 % |
| 2026-08-24 | 1,901 | 553.0 | 16.9 | 1.4 | 339.85 | 0.0 % |
| 2026-08-31 | 17,771 | 4,513.9 | 135.5 | 7.1 | 2,716.12 | 2.6 % |
| 2026-09-07 | 56,547 | 10,286.0 | 228.8 | 31.8 | 4,414.24 | 10.1 % |
| 2026-09-14 | 42,363 | 6,927.2 | 166.4 | 31.6 | 3,148.97 | 11.8 % |
| 2026-09-21 (partial) | 8,031 | 1,863.2 | 74.4 | 4.7 | 851.81 | 6.2 % |
| **Total** | **142,974** | **28,833.8** | **791.6** | **86.6** | **15,066.07** | **8.7 %** |

The low week from 2026-08-24 shows low counts on every day (74–288 events per
day from 08-24 to 08-29); it looks like low activity, not a harvester gap, but
that is not proven.

### 1.3 Per week — by source group (USD equivalent / tokens total in M)

| Week from | Operator sessions | Lead agent | Persistent agents | Heads |
|---|---|---|---|---|
| 2026-08-17 | 3,534.4 / 4,566.5 | 15.3 / 12.1 | 45.4 / 111.9 | — |
| 2026-08-24 | 283.0 / 426.4 | 24.2 / 27.1 | 32.7 / 99.5 | — |
| 2026-08-31 | 2,547.7 / 4,118.9 | 85.4 / 85.4 | 83.0 / 309.5 | — |
| 2026-09-07 | 1,371.4 / 2,754.7 | 672.8 / 1,044.7 | 2,370.1 / 6,486.5 | — |
| 2026-09-14 | 430.8 / 810.1 | 1,776.7 / 2,865.3 | 941.5 / 3,251.8 | — |
| 2026-09-21 (partial) | 819.7 / 1,806.7 | 19.1 / 11.4 | 13.0 / 45.1 | 0 rows |
| **6 weeks** | **8,986.79 / 14,483.3** | **2,593.50 / 4,046.0** | **3,485.78 / 10,304.4** | **not recorded** |

### 1.4 Per source — 6 weeks

Agents are named by role and harness only.

| Source | Main models | Tokens total (M) | Non-cache (M) | Output (M) | USD equiv. |
|---|---|---:|---:|---:|---:|
| Operator sessions (host coding CLI + subagents) | Opus 5, Fable 5.1, Sonnet 5, Opus 5.5, Fable 5 | 14,483.3 | 417.8 | 25.5 | 8,986.79 |
| Lead agent (host coding CLI) | Opus 5 | 4,046.0 | 72.8 | 9.6 | 2,593.50 |
| Persistent agent A (container, coding CLI, review/coding) | Opus 5; local GLM for 7 % of its tokens | 2,845.6 | 47.9 | 13.3 | 1,794.48 |
| Persistent agent B (container, coding CLI, coding) | Sonnet 5, Opus 5; flat-rate cloud for a small part | 3,743.1 | 68.8 | 20.3 | 1,013.15 |
| Persistent agent C (container, coding CLI, deploy) | Sonnet 5 | 1,313.1 | 23.6 | 5.6 | 358.78 |
| Persistent agent D (container, coding CLI, research) | Sonnet 5 | 502.1 | 27.6 | 3.2 | 187.80 |
| Persistent local agent E (container, omp harness) | local GLM | 1,577.5 | 21.1 | 4.3 | 51.33 (notional) |
| Persistent agent F (container, coding CLI, downloads) | Sonnet 5, Opus 5 | 77.2 | 2.1 | 0.6 | 39.92 |
| Persistent agent G (container, coding CLI, tests) | Sonnet 5 | 69.9 | 1.9 | 0.3 | 20.39 |
| Persistent local agent H (host, third-party agent harness) | local GLM, some local Qwen and flat-rate cloud | 103.3 | 94.5 | 2.0 | 13.84 (notional) |
| Persistent agent I (container, design) | Sonnet 5, flat-rate cloud | 16.4 | 3.8 | 1.5 | 5.89 |
| Persistent agent J (host, second vendor's CLI) | grok-4.6 (unpriced) | 51.7 | 8.7 | 0.3 | 0.20 |
| Persistent agent K (container, writing) | local Qwen, flat-rate cloud | 4.5 | 1.0 | 0.2 | 0.00 |
| Other (benchmark, probe) | local, flat-rate cloud | 0.1 | 0.1 | 0.0 | 0.00 |

### 1.5 Per model class — 6 weeks

| Class | Events | Tokens total (M) | Share of tokens | Output (M) | Share of output | USD equiv. |
|---|---:|---:|---:|---:|---:|---:|
| cloud, Anthropic | 109,140 | 26,855.9 | 93.1 % | 69.0 | 79.6 % | 14,993.31 |
| local | 24,708 | 1,883.9 | 6.5 % | 7.6 | 8.7 % | 72.56 (notional) |
| cloud, flat rate | 8,503 | 42.3 | 0.1 % | 9.8 | 11.3 % | 0.00 |
| cloud, other | 623 | 51.7 | 0.2 % | 0.3 | 0.4 % | 0.20 |

By model (USD equivalent): Opus 5 $8,426.22 · Fable 5.1 $2,685.62 ·
Sonnet 5 $2,128.20 · Fable 5 $1,158.95 · Opus 5.5 $552.59 · GLM-5.3-Flash-EXL3
$72.56 (notional) · Opus 4.8 $40.76 · Haiku 4.5 $0.96 · grok-4.5 $0.20.

### 1.6 Caveats

1. **Operator sessions are under-counted.** The harvester skips host CLI lines
   whose working directory contains neither `mission-control` nor `/.mc/` and
   whose branch does not start with `task/` (private sessions). Work started
   elsewhere is not in the table.
2. **The lead/operator split is by working directory.** An operator session
   started inside the lead's checkout would count as lead.
3. **Local "USD" is notional.** `GLM-5.3-Flash-EXL3` has its own price row
   (0.15 / 0.50 USD per MTok, note "local"); every other local model falls to
   the `*` row at 0. The cloud figure without it is $14,993.51.
4. **Unpriced cloud models** fall to the `*` fallback at 0 USD:
   `grok-4.6` (51.6 M tokens), `gpt-*`/`codex-mini-latest` (small), and the
   flat-rate models. The `grok-4.5*` row does not match `grok-4.6`.
5. **Cache reads dominate** (97.3 % of all tokens). Compare sources by
   non-cache or output tokens, not by the total.

## 2. Page usage

### 2.1 What the logs contain (checked 2026-09-24)

| Log | Content | Retention | Usable for pages? |
|---|---|---|---|
| Proxy (Caddy) | No `log` directive in the `Caddyfile` → **no access log**. Only warnings/errors: "aborting with incomplete response" (4,455) and upstream "connection refused" / DNS errors (~11,500), each with full request headers incl. `Referer` | since 2026-08-21 20:06 UTC (json-file, 3 × 10 MB) | Only indirectly (§2.2) |
| Backend (uvicorn) | Access lines `METHOD /api/v1/... status`, no timestamp, no referrer; mixed with agent and worker callers | only since the last container start, 2026-09-24 15:12 UTC (~3 h, 24,889 requests) | No — API path ≠ page |
| Frontend (Next.js) | Start-up lines only | — | No |

### 2.2 Proxy signal: referrer of aborted or failed requests

An error or stream-abort line carries the `Referer` of the page that made the
request. Deduplicated (same page within 5 s = one signal), 2026-08-21 →
2026-09-24: **2,419 signals, 11 of 21 page routes seen**.

| Route | Signals | Share |
|---|---:|---:|
| `/sessions` | 1,629 | 67.3 % |
| `/` | 315 | 13.0 % |
| `/agents` | 226 | 9.3 % |
| `/schedule` | 115 | 4.8 % |
| `/runtimes` | 59 | 2.4 % |
| `/agents/:id` | 46 | 1.9 % |
| `/inbox` | 23 | 1.0 % |
| `/tasks` | 3 | 0.1 % |
| `/memory`, `/insights`, `/bench` | 1 each | < 0.1 % |

Never seen: `/settings`, `/repos`, `/office`, `/setup`, `/files`, `/loops`,
`/skills`, `/login`, `/schedule/:jobId`, `/memory/graph`.
Device mix: 69.5 % desktop, 30.5 % mobile (user agent).

**Why this is not page usage:** a line is written only when a stream is cut
(tab closed, navigation, backend restart) or the backend is unreachable. Pages
with long-lived streams (`/sessions`, `/agents`, `/`) are over-represented;
pages that only fetch once almost never appear. "Never seen" does not mean
"never opened". The signal is good enough to say that `/sessions` is open far
more than any other page while backend restarts happen, and nothing more.

### 2.3 Answer to the roadmap question

"Do existing logs contain page views?" — **No.** Measuring page usage needs a
small change (§2.4).

### 2.4 Minimal proposal

Preferred: **route beacon** (about 30 lines, no new table).

- Frontend: one effect on pathname change in the root layout sends
  `navigator.sendBeacon('/api/v1/metrics/page-view', {route})`, with the route
  normalised to its pattern (`/agents/:id`) — no ids, no query, no user.
- Backend: the endpoint writes one structured log line
  (`page_view route=/agents/:id`) or increments a per-day counter in Redis;
  the daily metrics digest (#641) reads it.
- Counts client-side navigations, which a proxy log cannot see.

Zero-code fallback: enable a Caddy access log for the frontend site only, with
a `filter` encoder that deletes request headers and the query string, and count
requests with `Sec-Fetch-Dest: document`. This counts full page loads only
(reloads, new tabs, links from chat), not client-side navigation. The filter is
required, not optional: stream endpoints carry their credential in the query
string, and today's error lines already log it in full.

## 3. Head numbers (for stop 1)

### 3.1 Sources

`~/.mc/heads/<run>/` (`spec.json`, `.wrapper/status.json`,
`.backend/mirror.json`, `head.log`, `run-record.md`), `~/.mc/heads/spool/`,
`~/.mc/vault/jobs/`. There is no head table in the database. Read-only.

### 3.2 Runs

| Run | Start (UTC) | Duration | Pair | Wrapper exit | Final state | Run record | PR |
|---|---|---:|---|---|---|---|---|
| 1 | 2026-09-24 15:17:34 | 22 s | omp × local GPU runtime (GLM-5.3-Flash-EXL3) | exit 1, reason `exit_1` | superseded (restarted) | none | none |
| 2 (restart of 1) | 2026-09-24 17:51:02 | 5 min 17 s | same | exit 0 | **failed** | yes | none |

Both runs work the same probe job (fix `add()` in a scratch repo, failing test
first).

- **Run 1:** the harness printed only "Was there a typo in the url or port?"
  and exited after 22 s — the engine was not reachable. The recorded reason is
  `exit_1`, not "runtime unreachable" as E1b's acceptance criterion expects.
- **Run 2:** the work itself was complete — failing test, fix, green suite,
  sabotage probe, fresh reviewer ("correct", 0.98), two commits. `git push` to
  the scratch origin was denied by the head sandbox (no write access to the
  origin's object directory), so no PR; the head ended with "failed" instead
  of working around it, as the procedure requires. Operator minutes
  (self-reported by the head): ~5.
- **Run-record times are wrong:** the record says "start 17:16 · end 17:20";
  the wrapper says 17:51:02 → 17:56:19 UTC. Run-record times cannot be used as
  the duration source; the wrapper's `status.json` can.

### 3.3 Totals

| Metric | Value |
|---|---|
| Jobs / runs | 1 / 2 |
| Passed / failed | 0 / 2 (1 engine unreachable, 1 push blocked by sandbox) |
| Local / cloud runs | 2 / 0 |
| Run durations | 22 s and 5 min 17 s (n = 2) |
| Restarts ("restart with …") | 1 |
| PRs opened | 0 |
| Head tokens | **not recorded**: the launcher runs omp with `--no-session`, the head's omp database has empty usage tables, and the harvester does not read `~/.mc/heads/` (0 rows in `model_usage_events`) |

### 3.4 What this means for stop 1

- There is **no head baseline yet**: n = 2 on a single probe job, no pass, no
  cloud comparison. Stop 1's "share of jobs that finish on a local runtime"
  cannot be answered from this data.
- Three gaps block the stop-1 numbers and should be closed before the E1 trial
  jobs count:
  1. head token usage is not captured (heads are invisible in §1),
  2. an unreachable engine is recorded as `exit_1`, not as "runtime
     unreachable",
  3. run-record times disagree with the wrapper; durations must come from
     `status.json`.
- The push block in run 2 is a sandbox permission on the local scratch origin,
  not a head failure; the run still counts as failed ("partial does not
  exist").
