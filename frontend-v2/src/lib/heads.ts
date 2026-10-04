/**
 * Head launcher — frontend model (docs/specs/head-launcher.md §7, §8).
 *
 * Types for `/api/v1/heads*`, the pure rules the UI follows (which pair is
 * pre-selected, which pairs show first, which i18n key explains a reason or
 * an error) and the per-viewer "last pair" convenience in localStorage.
 *
 * Rules that matter:
 *   - The pre-selected pair is ALWAYS local: `default_pair` from the backend
 *     (omp × the best local runtime), even when its engine is not running —
 *     then "Run as head" stays disabled with a link to Runtimes. A cloud pair
 *     is never pre-selected, not even a remembered one.
 *   - Texts never come from the backend: reason and error CODES map to keys
 *     in the `heads` i18n namespace.
 */

export type HeadPairStatus = "ok" | "experimental" | "blocked";
export type HeadLocality = "local" | "cloud";

export interface HeadBusy {
  run_id: string;
  task_id: string | null;
  title: string | null;
  harness: string | null;
  runtime_slug: string | null;
  since: string | null;
}

export interface HeadPair {
  harness: string;
  harness_label: string;
  runtime_slug: string;
  runtime_label: string;
  model: string | null;
  locality: HeadLocality;
  status: HeadPairStatus;
  reason_code: string | null;
  live: boolean;
  box_keys: string[];
  busy_by: HeadBusy | null;
  engine_in_use: boolean;
  startable: boolean;
}

export interface HeadPairsResponse {
  pairs: HeadPair[];
  default_pair: HeadPair | null;
}

export type HeadState = "starting" | "running" | "needs_you" | "passed" | "failed" | "stopped";

export interface HeadRun {
  run_id: string;
  task_id: string | null;
  title: string | null;
  harness: string | null;
  runtime_slug: string | null;
  model: string | null;
  repo_full_name: string | null;
  branch: string | null;
  mode: "fresh" | "continue" | null;
  restarted_from: string | null;
  box_keys: string[];
  created_at: string | null;
  started_at: string | null;
  exited_at: string | null;
  state: HeadState;
  reason: string | null;
  silent_s: number | null;
  heartbeat_stale: boolean;
  step: string | null;
  question: string | null;
  pr_url: string | null;
  tmux: string | null;
  run_record: boolean;
  task_deleted: boolean;
}

export interface HeadStartBody {
  task_id: string;
  harness: string;
  runtime_slug: string;
  answer?: string;
  /** New task created the card only for this head: hold it if the start fails. */
  hold_on_failure?: boolean;
}

export interface HeadRestartBody {
  harness: string;
  runtime_slug: string;
  mode: "fresh" | "continue";
  answer?: string;
}

export const HEAD_ACTIVE_STATES: ReadonlySet<HeadState> = new Set<HeadState>(["starting", "running"]);

/** Task detail polls the runs this often — only while one is active. */
export const HEAD_POLL_MS = 10_000;

/** Output silence after which the card warns (the head still runs). */
export const HEAD_SILENT_WARN_S = 15 * 60;

export function isHeadActive(run: Pick<HeadRun, "state"> | null | undefined): boolean {
  return !!run && HEAD_ACTIVE_STATES.has(run.state);
}

export function pairKey(p: { harness: string | null; runtime_slug: string | null }): string {
  return `${p.harness ?? ""}::${p.runtime_slug ?? ""}`;
}

/** Plain pair name for the UI: "omp · GLM-5.3 Flash". */
export function pairLabel(p: { harness_label?: string | null; harness?: string | null; runtime_label?: string | null; runtime_slug?: string | null }): string {
  const harness = p.harness_label || harnessLabel(p.harness);
  const runtime = p.runtime_label || p.runtime_slug || "—";
  return `${harness} · ${runtime}`;
}

const HARNESS_LABELS: Record<string, string> = {
  omp: "omp",
  claude: "Claude Code",
  openclaude: "OpenClaude",
};

export function harnessLabel(harness: string | null | undefined): string {
  if (!harness) return "—";
  return HARNESS_LABELS[harness] ?? harness;
}

/** Pair label for a run (no runtime label on the run — the picker list, when
 *  loaded, gives the nicer name). */
export function runPairLabel(run: Pick<HeadRun, "harness" | "runtime_slug">, pairs?: HeadPair[] | null): string {
  const match = pairs?.find((p) => p.runtime_slug === run.runtime_slug);
  return pairLabel({ harness: run.harness, runtime_label: match?.runtime_label ?? null, runtime_slug: run.runtime_slug });
}

/**
 * Picker order: startable pairs first (local before cloud, `ok` before
 * `experimental`, backend order otherwise); the blocked rest goes behind
 * "Show more". The selected pair is always visible, even when blocked
 * (the default can be a local pair whose engine is down).
 */
export function splitPairs(pairs: HeadPair[], selectedKey?: string | null): { primary: HeadPair[]; more: HeadPair[] } {
  const rank = (p: HeadPair) => (p.locality === "local" ? 0 : 2) + (p.status === "ok" ? 0 : 1);
  const startable = pairs
    .map((p, i) => ({ p, i }))
    .filter(({ p }) => p.startable)
    .sort((a, b) => rank(a.p) - rank(b.p) || a.i - b.i)
    .map(({ p }) => p);
  const blocked = pairs.filter((p) => !p.startable);
  const selectedBlocked = blocked.find((p) => pairKey(p) === selectedKey);
  return {
    primary: selectedBlocked ? [selectedBlocked, ...startable] : startable,
    more: blocked.filter((p) => p !== selectedBlocked),
  };
}

/**
 * The pair the picker opens with. A remembered choice wins only while it is
 * still startable AND local; otherwise the backend's `default_pair` (always
 * local, possibly not startable). Never a cloud pair.
 */
export function chooseInitialPair(resp: HeadPairsResponse | null | undefined, rememberedKey: string | null): HeadPair | null {
  if (!resp) return null;
  if (rememberedKey) {
    const remembered = resp.pairs.find((p) => pairKey(p) === rememberedKey);
    if (remembered && remembered.startable && remembered.locality === "local") return remembered;
  }
  const def = resp.default_pair;
  if (def && def.locality === "local") {
    // Prefer the live copy from the list (same key) — it carries busy_by etc.
    return resp.pairs.find((p) => pairKey(p) === pairKey(def)) ?? def;
  }
  return null;
}

/** Why a pair cannot start — one i18n key under `heads.reason`. */
const REASON_CODES = new Set([
  "protocol_mismatch",
  "harness_not_supported",
  "needs_operator_decision",
  "unproven",
  "engine_not_ready",
  "box_busy",
  "quota_limit",
  "runtime_not_offered",
]);

export function pairReasonKey(code: string | null | undefined): string {
  return code && REASON_CODES.has(code) ? `reason.${code}` : "reason.other";
}

/** State word for a run — `heads.state.<state>`. */
export function headStateKey(state: HeadState): string {
  return `state.${state}`;
}

/** One sentence for why a run ended as failed/stopped — `heads.failReason.<code>`. */
const FAIL_REASONS = new Set([
  "stopped",
  "time_limit", // runs from before the progress watchdog
  "no_progress",
  "hard_limit",
  "no_pr",
  "run_record_missing",
  "not_picked_up",
  "process_vanished",
  "box_busy",
  "prepare_failed",
  "previous_run_still_active",
  "spec_invalid",
  "wrapper_error",
  "gh_identity_missing",
  "gh_identity_unsafe",
  "sandbox_required",
  "hook_bypassed",
  "local_network_blocked",
]);

export function failReasonKey(reason: string | null | undefined): { key: string; values?: Record<string, string> } {
  if (!reason) return { key: "failReason.unknown" };
  if (FAIL_REASONS.has(reason)) return { key: `failReason.${reason}` };
  const exit = /^exit_(-?\d+)$/.exec(reason);
  if (exit) return { key: "failReason.exit", values: { code: exit[1] } };
  return { key: "failReason.unknown" };
}

// ── Errors ───────────────────────────────────────────────────────────────────

/** lib/api.ts throws `Error("API <status>: <body>")`; heads errors carry
 *  `{detail: {code, ...}}`. Returns the code and the extra fields. */
export function parseHeadError(err: unknown): { status: number | null; code: string | null; detail: Record<string, unknown> } {
  if (!(err instanceof Error)) return { status: null, code: null, detail: {} };
  const m = /^API (\d+): ([\s\S]*)$/.exec(err.message);
  if (!m) return { status: null, code: null, detail: {} };
  const status = Number(m[1]);
  try {
    const body = JSON.parse(m[2]) as { detail?: unknown };
    const d = body?.detail;
    if (d && typeof d === "object" && typeof (d as { code?: unknown }).code === "string") {
      return { status, code: (d as { code: string }).code, detail: d as Record<string, unknown> };
    }
  } catch {
    // not JSON — no code
  }
  return { status, code: null, detail: {} };
}

const ERROR_CODES = new Set([
  "pair_blocked",
  "engine_not_ready",
  "box_busy",
  "head_active",
  "task_move_failed",
  "repo_required",
  "spool_unavailable",
  "heads_disabled",
  "task_not_found",
  "run_not_found",
  "run_record_missing",
  "head_on_box",
  "engine_busy",
  "head_not_active",
]);

// ── Restart mode ─────────────────────────────────────────────────────────────

/** Runs that ended before mc-head created the worktree/branch: a "continue"
 *  restart would run `git worktree add` on a branch that does not exist. */
const NO_WORKTREE_REASONS = new Set([
  "not_picked_up",
  "gh_identity_missing",
  "gh_identity_unsafe",
  "sandbox_required",
  "prepare_failed",
  "spec_invalid",
  "box_busy",
  "previous_run_still_active",
]);

/** Whether "Continue on this branch" can work for this run. */
export function canContinueRun(run: Pick<HeadRun, "reason" | "started_at">): boolean {
  if (run.reason && NO_WORKTREE_REASONS.has(run.reason)) return false;
  // stopped before the host picked it up — nothing was ever prepared
  if (run.reason === "stopped" && !run.started_at) return false;
  return true;
}

/** Pre-selected restart mode: continue where there is work, else fresh. */
export function defaultRestartMode(run: Pick<HeadRun, "reason" | "started_at">): "continue" | "fresh" {
  return canContinueRun(run) ? "continue" : "fresh";
}

/** i18n key under `heads.errors` for any error thrown by a heads call. */
export function headErrorKey(err: unknown): string {
  const { code } = parseHeadError(err);
  return code && ERROR_CODES.has(code) ? `errors.${code}` : "errors.unknown";
}

/** 409 `head_on_box` from a recipe switch / runtime stop / restart. */
export interface HeadOnBox {
  run_id: string;
  task_id: string | null;
  title: string | null;
}

export function parseHeadOnBox(err: unknown): HeadOnBox | null {
  const { status, code, detail } = parseHeadError(err);
  if (status !== 409 || code !== "head_on_box") return null;
  return {
    run_id: String(detail.run_id ?? ""),
    task_id: detail.task_id ? String(detail.task_id) : null,
    title: detail.title ? String(detail.title) : null,
  };
}

/** Heads are off (`heads_enabled=false`) — every endpoint answers 404
 *  `heads_disabled`; the UI then shows nothing of the launcher. */
export function isHeadsDisabled(err: unknown): boolean {
  return parseHeadError(err).code === "heads_disabled";
}

// ── Remembered pair (per viewer, convenience only) ──────────────────────────

const REMEMBER_KEY = "mc.heads.lastPair";

export function loadRememberedPair(): string | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage.getItem(REMEMBER_KEY);
  } catch {
    return null;
  }
}

export function saveRememberedPair(key: string): void {
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(REMEMBER_KEY, key);
  } catch {
    // private window / blocked storage — a convenience, nothing breaks
  }
}

// ── Time ─────────────────────────────────────────────────────────────────────

function ts(value: string | null | undefined): number | null {
  if (!value) return null;
  const n = Date.parse(value);
  return Number.isNaN(n) ? null : n;
}

/** Seconds the run has been (or was) working: started → exited/now. */
export function runDurationSeconds(run: Pick<HeadRun, "started_at" | "created_at" | "exited_at">, now = Date.now()): number | null {
  const start = ts(run.started_at) ?? ts(run.created_at);
  if (start == null) return null;
  const end = ts(run.exited_at) ?? now;
  return Math.max(0, Math.round((end - start) / 1000));
}

/** Newest run first (by created_at, then run order). */
export function sortRunsNewestFirst(runs: HeadRun[]): HeadRun[] {
  return runs
    .map((r, i) => ({ r, i }))
    .sort((a, b) => (ts(b.r.created_at) ?? 0) - (ts(a.r.created_at) ?? 0) || b.i - a.i)
    .map(({ r }) => r);
}

/** Passed on a scratch repo whose origin is a local bare repo: no PR is
 *  possible there, the wrapper-verified pushed branch is the result. */
export function isScratchBranchPushed(run: Pick<HeadRun, "state" | "reason" | "pr_url">): boolean {
  return run.state === "passed" && run.reason === "scratch_branch_pushed" && !run.pr_url;
}

/** PR number from a GitHub PR URL, for "PR #712". */
export function prNumberFromUrl(url: string | null | undefined): number | null {
  const m = /\/pull\/(\d+)/.exec(url ?? "");
  return m ? Number(m[1]) : null;
}

// ── Restart ─────────────────────────────────────────────────────────────────

/**
 * Pairs for "Restart with …". The listing marks the run's OWN box as busy
 * while it still works; the backend's restart ignores that lock
 * (`ignore_run_id`), so such a pair counts as startable here. Every other
 * block stays.
 */
export function pairsForRestart(pairs: HeadPair[], runId: string): HeadPair[] {
  return pairs.map((p) =>
    p.status === "blocked" && p.reason_code === "box_busy" && p.busy_by?.run_id === runId
      ? { ...p, reason_code: null, busy_by: null, startable: true }
      : p,
  );
}

/** Pre-selection for a restart: the run's own pair while startable, else
 *  the usual local default. */
export function chooseRestartPair(
  pairs: HeadPair[],
  defaultPair: HeadPair | null,
  run: Pick<HeadRun, "harness" | "runtime_slug">,
): HeadPair | null {
  const own = pairs.find((p) => pairKey(p) === pairKey(run));
  if (own && own.startable) return own;
  return chooseInitialPair({ pairs, default_pair: defaultPair }, null);
}

/** True while the newest run of a `{runs}` listing is still active —
 *  drives the task-detail polling (off at a final state). */
export function headRunsActive(data: { runs?: HeadRun[] } | undefined | null): boolean {
  const runs = Array.isArray(data?.runs) ? data!.runs : [];
  return isHeadActive(sortRunsNewestFirst(runs)[0]);
}

// ── Transport shapes (heads-sichtbar PR 2) ──────────────────────────────────
//
// `GET /heads/{run_id}/chat/history` (backend `services/heads/transcript.py`
// — see its module docstring for the full contract) answers in the SAME
// shape an agent's `GET /agents/{id}/chat/history` does
// (`chatTypes.ChatHistoryResponse`), plus three fields an agent's own
// endpoint has no use for: `source`/`reader`/`reason` say WHY there is (or
// is not) a transcript at all. Declared here, not in `chatTypes.ts`, because
// nothing about an agent's chat ever needs them.

export type HeadTranscriptSource = "transcript" | "none";
export type HeadTranscriptReader = "claude" | "omp" | null;
export type HeadTranscriptReason = "no_reader" | "not_yet" | "no_transcript" | "too_large" | null;

export interface HeadChatHistoryResponse {
  events: import("./chatTypes").ChatEvent[];
  session: import("./chatTypes").ChatSession;
  hasMore: boolean;
  subagentRuns: import("./chatTypes").SubagentRun[];
  source: HeadTranscriptSource;
  reader: HeadTranscriptReader;
  reason: HeadTranscriptReason;
}

/** `GET /heads/{run_id}/summary` (backend `services/heads/summary.py`'s own
 *  docstring has the field-by-field contract) — every field but `run_id`/
 *  `branch`/`pr_url` can legitimately be `null`: the run record simply never
 *  recorded that fact, which is not an error. */
export interface HeadSummary {
  run_id: string;
  status: "running" | "passed" | "failed" | null;
  result_line: string | null;
  tests: { failed_before: string | null; passed_after: string | null };
  sabotage: boolean | null;
  kz_ok: boolean | null;
  review: "helper" | "self" | null;
  bypass: number | null;
  operator_minutes: number | null;
  helpers: number | null;
  branch: string | null;
  pr_url: string | null;
}

/** `GET /heads` with `recent_days`/`archived` — see `routers/heads.py`'s own
 *  docstring on `list_heads` for the exact boundary rule. `archived_count`
 *  is only present on the `recent_days` call, never on a plain or an
 *  `archived=true` one — callers must not assume it is always there. */
export interface HeadListResponse {
  runs: HeadRun[];
  archived_count?: number;
}

// ── Heads in Chats (heads-sichtbar PR 2) ────────────────────────────────────
//
// Everything below is new frontend-only model for "Heads" as a section in
// the Chats list and its own read-only chat view — nothing here changes the
// shape the backend already returns (`/heads`, `/heads/{id}/summary`).

/** "GLM-5.3-Flash-EXL3" → "GLM-5.3": the chat list has no room for a
 *  quantisation/variant tag the operator did not ask about (K4/K5) — the
 *  pair is already named in full on the task's own properties page. Strips
 *  known variant/quant suffixes off the END, one at a time, so a model
 *  carrying several of them ("-Flash-EXL3") loses all of them, not just
 *  the last. A model with none of these suffixes (or `null`) passes
 *  through unchanged — this never invents a shorter name, only removes
 *  recognised noise. */
const MODEL_VARIANT_SUFFIX =
  /-(Flash|Turbo|Mini|Nano|Preview|Instruct|Chat|Base|EXL2|EXL3|GGUF|AWQ|GPTQ|NVFP4|FP8|FP16|INT4|INT8|exp)$/i;

export function modelFamily(model: string | null | undefined): string {
  if (!model) return "—";
  let out = model;
  for (let guard = 0; guard < 6; guard++) {
    const next = out.replace(MODEL_VARIANT_SUFFIX, "");
    if (next === out) break;
    out = next;
  }
  return out || model;
}

/** "omp × GLM-5.3" (Mark's word for the pair, ADR-086 §5) — the SHORT form
 *  for a list row or a chat header, where `pairLabel`'s "harness · full
 *  runtime name" would not fit. Deliberately a separate function rather
 *  than changing `pairLabel`'s own separator or shortening: that change is
 *  PR 3's (bauplan §4, `pairLabel() Trenner "·" → "×"`), and touches every
 *  existing `pairLabel` caller/test — this one is additive and touches
 *  none of them. */
export function pairShort(run: Pick<HeadRun, "harness" | "model">): string {
  return `${harnessLabel(run.harness)} × ${modelFamily(run.model)}`;
}

/** The task title, as the Chats list shows it: a leading bracket tag
 *  (`"[fixture] scrubbed head run…"`, `"[night] …"`) is operator/tooling
 *  bookkeeping, not what the head is doing — K3 ("jede Angabe genau
 *  einmal"), the tag means nothing extra to someone scanning the list.
 *  More than one leading tag is stripped in one pass. A title that is
 *  NOTHING but tags (empty after stripping) falls back to the original,
 *  trimmed — never an empty list row. */
export function headListTitle(run: Pick<HeadRun, "title">): string {
  const raw = (run.title ?? "").trim();
  const stripped = raw.replace(/^(\[[^\]]*\]\s*)+/, "").trim();
  return stripped || raw;
}

/** "5/7 independent review · waiting for: reviewer" (the exact line
 *  `scripts/head/mc-head`'s wrapper asks the head to write, `step.txt`,
 *  with or without the leading "step " word some writers include) →
 *  `{n, total, name, waitingFor}`. `null` for anything that does not match
 *  that shape — an older/garbled `step.txt` must never be guessed at, it
 *  just renders as "no step reported yet" (existing `card.noStep` key),
 *  same contract `headStateKey`/`failReasonKey` already follow. */
export interface ParsedStep {
  n: number;
  total: number;
  name: string;
  waitingFor: string;
}

const STEP_RE = /^(?:step\s+)?(\d+)\s*\/\s*(\d+)\s+(.+?)\s*·\s*waiting for:\s*(.+?)\s*$/i;

export function parseStep(step: string | null | undefined): ParsedStep | null {
  if (!step) return null;
  const m = STEP_RE.exec(step.trim());
  if (!m) return null;
  return { n: Number(m[1]), total: Number(m[2]), name: m[3].trim(), waitingFor: m[4].trim() };
}

/** The 8 fixed steps of the head procedure (`~/.claude/skills/
 *  head-procedure`, `backend/templates/heads/head-AGENTS.md` §Workflow,
 *  0-indexed) — MC can translate the step NAME because every head follows
 *  the same 8, unlike `parseStep`'s free-text `name`/`waitingFor` (what the
 *  head itself wrote, shown as-is). Index = the parsed step's `n`. The i18n
 *  key is `heads.steps.<value>`. */
export const HEAD_STEP_KEYS = [
  "context", // 0 — context brief (kz brief), create the run record
  "plan", // 1 — plan
  "redTest", // 2 — failing test first
  "change", // 3 — implement
  "sabotage", // 4 — sabotage check
  "review", // 5 — independent review
  "pr", // 6 — push, open the pull request
  "runRecord", // 7 — finish the run record
] as const;

export function headStepKey(n: number): string | null {
  const key = HEAD_STEP_KEYS[n];
  return key ? `steps.${key}` : null;
}

/** List order inside the "Heads" section (bauplan §3.2 `SessionSidebar`):
 *  needs-you first (the operator is blocked on it), then active (running
 *  or still starting), then every ended run — newest end first, same
 *  "most recent first" rule the Archive sheet already uses
 *  (`sortRunsNewestFirst`). Stable within a group: ties keep their
 *  original relative order rather than reshuffling on every re-render. */
const LIST_GROUP: Record<HeadState, 0 | 1 | 2> = {
  needs_you: 0,
  running: 1,
  starting: 1,
  passed: 2,
  failed: 2,
  stopped: 2,
};

export function sortHeadsForList(runs: HeadRun[]): HeadRun[] {
  return runs
    .map((r, i) => ({ r, i }))
    .sort((a, b) => {
      const groupDiff = LIST_GROUP[a.r.state] - LIST_GROUP[b.r.state];
      if (groupDiff !== 0) return groupDiff;
      if (LIST_GROUP[a.r.state] === 2) {
        const endA = ts(a.r.exited_at) ?? ts(a.r.created_at) ?? 0;
        const endB = ts(b.r.exited_at) ?? ts(b.r.created_at) ?? 0;
        if (endA !== endB) return endB - endA;
      }
      return a.i - b.i;
    })
    .map(({ r }) => r);
}
