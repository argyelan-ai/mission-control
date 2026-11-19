/**
 * Self-update from Settings (ADR-094, update plan PR 5) — the shapes the
 * backend sends and the pure rules the update card follows.
 *
 * The backend is only the messenger (routers/system_update.py): the
 * cli-bridge's update door on the machine decides, the engine
 * (scripts/mc_update.py) does the work and its journal is the truth. This
 * file turns one status answer into exactly one of the card's states, so the
 * card itself never decides "is this a failure?" — and a status call that
 * does not answer while MC restarts is *waiting*, never *failed*.
 */

// ── API shapes ──────────────────────────────────────────────────────────────

export type CheckStatus = "ok" | "warn" | "fail";

export interface UpdateCheck {
  id: string;
  status: CheckStatus;
  /** English, from the machine — the card words the check by id/status and
   *  only shows this as a folded detail. */
  message: string;
  /** What to type on the machine, where only the machine can help. */
  command?: string;
  code?: string;
}

export interface RunningWork {
  tasks: number;
  group_rounds: number;
}

export interface DrainView {
  active: boolean;
  run_id: string | null;
  expires_at: string | null;
}

export interface UpdatePreflight {
  current: string | null;
  target: string | null;
  up_to_date: boolean;
  bridge: { reachable: boolean; api_version: number | null };
  checks: UpdateCheck[];
  ok: boolean;
  running_work: RunningWork;
  drain: DrainView;
  starts?: { used: number | null; limit: number | null };
}

export interface RunEvent {
  at?: string;
  phase?: string;
  event?: string;
  detail?: string;
}

export interface UpdateRun {
  run_id: string;
  phase: string;
  phase_state?: string | null;
  status?: string | null;
  source?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  failure_kind?: string | null;
  backup?: string | null;
  interrupted?: boolean;
  from: { tag?: string | null; version?: string | null };
  target: { tag?: string | null; version?: string | null };
  running: boolean;
  events: RunEvent[];
}

export interface UpdateStatus {
  drain: DrainView;
  running_work: RunningWork;
  bridge_reachable: boolean;
  stale: boolean;
  run: UpdateRun | null;
  command?: string;
}

export interface UpdateHistoryRun {
  run_id: string;
  source: string | null;
  from_version: string | null;
  target_version: string | null;
  phase: string | null;
  error: string | null;
  failure_kind: string | null;
  backup: string | null;
  started_by: string | null;
  started_at: string | null;
  finished_at: string | null;
  rollback_requested_by: string | null;
  rollback_requested_at: string | null;
  drain_lifted_at: string | null;
}

export interface StepUpAvailability {
  available: boolean;
  reason: string | null;
  ttl_seconds: number;
}

/** The commands the backend names (mc_update_runner.CMD_*). */
export const CMD_BRIDGE = "./install.sh --bridge";
export const CMD_RESUME = "./install.sh --resume";
export const CMD_ROLLBACK = "./install.sh --rollback";
export const CMD_UPDATE = "./install.sh --update";

// ── API errors ──────────────────────────────────────────────────────────────

export interface UpdateApiError {
  /** HTTP status, or null when no answer came at all (network down, MC restarting). */
  status: number | null;
  code: string | null;
  message: string | null;
  command: string | null;
  checks: UpdateCheck[] | null;
  managed: boolean;
  /** Only on update_managed: is the feature switch on? */
  enabled: boolean | null;
  used: number | null;
  limit: number | null;
}

/** Read the `API <status>: <body>` error lib/api.ts throws (and a fetch
 *  TypeError, which means: nothing answered). */
export function parseUpdateError(err: unknown): UpdateApiError {
  const out: UpdateApiError = {
    status: null, code: null, message: null, command: null, checks: null,
    managed: false, enabled: null, used: null, limit: null,
  };
  const msg = err instanceof Error ? err.message : typeof err === "string" ? err : "";
  const m = /^API (\d{3}): ([\s\S]*)$/.exec(msg);
  if (!m) return out;
  out.status = Number(m[1]);
  try {
    const detail = JSON.parse(m[2])?.detail;
    if (detail && typeof detail === "object") {
      out.code = typeof detail.code === "string" ? detail.code : null;
      out.message = typeof detail.message === "string" ? detail.message : null;
      out.command = typeof detail.command === "string" ? detail.command : null;
      out.checks = Array.isArray(detail.checks) ? (detail.checks as UpdateCheck[]) : null;
      out.managed = detail.managed === true;
      out.enabled = typeof detail.enabled === "boolean" ? detail.enabled : null;
      out.used = typeof detail.used === "number" ? detail.used : null;
      out.limit = typeof detail.limit === "number" ? detail.limit : null;
    } else if (typeof detail === "string") {
      out.message = detail;
    }
  } catch {
    /* an HTML page from a proxy (502 while MC restarts) — no detail */
  }
  return out;
}

/** "MC does not answer right now" — what a restart looks like from the
 *  page: no answer at all, or a proxy/server error without our detail. */
export function isNoAnswer(e: UpdateApiError): boolean {
  if (e.status === null) return true;
  return e.status >= 500 && e.code === null;
}

/** What the status call's failure means for the card. */
export type StatusGate = "hidden" | "managed" | "shown";

export function statusGate(e: UpdateApiError | null): StatusGate {
  if (!e) return "shown";
  if (e.status === 409 && (e.code === "update_managed" || e.managed)) {
    // Nothing new on screen before the acceptance: the note shows only
    // once the feature switch is on (detail.enabled).
    return e.enabled === true ? "managed" : "hidden";
  }
  // 404 update_disabled (switch off), 403 (not an admin), 401 (signed out).
  if (e.status === 404 || e.status === 403 || e.status === 401) return "hidden";
  return "shown";
}

// ── phases ──────────────────────────────────────────────────────────────────

export const TERMINAL = ["done", "aborted", "rolled_back", "rollback_failed"] as const;
export const PRE_SWITCH = ["preflight", "drain", "fetch", "verify_release", "pull", "backup"] as const;

/** The five steps of the phase bar: download → back up → switch → restart → check. */
export const STEPS = ["download", "backup", "switch", "restart", "verify"] as const;
export type Step = (typeof STEPS)[number];

const PHASE_STEP: Record<string, number> = {
  fetch: 0,
  verify_release: 0,
  pull: 0,
  backup: 1,
  switch: 2,
  up: 3,
  verify: 4,
};

export function stepOf(phase: string | null | undefined): number | null {
  return phase != null && phase in PHASE_STEP ? PHASE_STEP[phase] : null;
}

export const isTerminal = (phase: string | null | undefined) =>
  !!phase && (TERMINAL as readonly string[]).includes(phase);

export const isPreSwitch = (phase: string | null | undefined) =>
  !!phase && (PRE_SWITCH as readonly string[]).includes(phase);

/** The step the run failed in (rolled back or the way back failed): the
 *  last step reached before the way back began. */
export function failedStep(run: UpdateRun): number | null {
  let step: number | null = null;
  for (const e of run.events ?? []) {
    if (e.phase === "rolling_back") break;
    const s = stepOf(e.phase);
    if (s !== null) step = s;
  }
  return step ?? stepOf(run.phase);
}

/** A rollback the operator asked for: the run had ended `done` first. */
export function wasRequested(run: UpdateRun): boolean {
  return (run.events ?? []).some((e) => e.phase === "done");
}

/** When the pre-update backup was taken (the journal's backup.done event). */
export function backupTakenAt(run: UpdateRun): string | null {
  const ev = (run.events ?? []).find((e) => e.phase === "backup" && e.event === "done");
  return ev?.at ?? null;
}

export const BACKUP_KEEP_DAYS = 14;
const DAY_MS = 24 * 3600 * 1000;
/** A finished run's result stays on the card this long; then the history has it. */
export const RESULT_SHOWN_MS = DAY_MS;
/** The open page asks this long through a restart before it says so (plan §2.6). */
export const RESTART_PATIENCE_MS = 25 * 60 * 1000;
/** The bridge may be gone for a moment (restarted) without the run being lost. */
export const STALE_GRACE_MS = 60 * 1000;

function ageMs(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? now - t : null;
}

/** "Back to <from>" is offered for a run that ended `done` within 14 days. */
export function canGoBack(run: UpdateRun | null, now: number): boolean {
  if (!run || run.phase !== "done" || !run.from?.tag) return false;
  const age = ageMs(run.finished_at ?? run.updated_at, now);
  return age !== null && age <= BACKUP_KEEP_DAYS * DAY_MS;
}

// ── the card's state ────────────────────────────────────────────────────────

export type RunState =
  | { kind: "idle" }
  | { kind: "pending"; action: "start" | "rollback"; work: RunningWork }
  | { kind: "waiting"; run: UpdateRun; work: RunningWork; stale: boolean }
  | { kind: "running"; run: UpdateRun; step: number; stale: boolean }
  | { kind: "rollingBack"; run: UpdateRun; step: number | null; stale: boolean }
  | { kind: "restarting"; since: number }
  | { kind: "restartTimeout"; since: number }
  | { kind: "success"; run: UpdateRun }
  | { kind: "rolledBack"; run: UpdateRun; step: number | null; requested: boolean }
  | { kind: "aborted"; run: UpdateRun }
  | { kind: "interrupted"; run: UpdateRun; bridgeReachable: boolean; command: string }
  | { kind: "rollbackFailed"; run: UpdateRun };

export interface DeriveInput {
  status: UpdateStatus | null;
  now: number;
  /** This page watched the run while it was active (a result then stays
   *  until the page is left, however old the run's start). */
  seenActive: boolean;
  /** The furthest step this page showed for the run — the bar never runs backwards. */
  maxStep: number;
  /** Since when the status call has not answered (a restart), or null. */
  noAnswerSince: number | null;
  /** The last run this page knew was active (or just started) — a missing
   *  answer only means "restarting" while one is. */
  activeKnown: boolean;
  /** Since when the bridge has not answered (status.stale), or null. */
  staleSince: number | null;
  /** What this page just asked for (a start, or a way back): until the
   *  status shows it, the card says so — not the previous run's result. */
  expect?: { runId: string | null; action: "start" | "rollback" } | null;
}

/** Has the status caught up with what this page asked for? */
export function expectArrived(
  expect: { runId: string | null; action: "start" | "rollback" },
  run: UpdateRun | null,
): boolean {
  if (!run) return false;
  if (expect.runId ? run.run_id !== expect.runId : isTerminal(run.phase)) return false;
  // A way back runs on the finished run itself: it has arrived once that
  // run is no longer "done".
  return !(expect.action === "rollback" && run.phase === "done");
}

export function deriveRunState(input: DeriveInput): RunState {
  const { status, now, seenActive, maxStep, noAnswerSince, activeKnown, staleSince } = input;

  if (noAnswerSince !== null && activeKnown) {
    return now - noAnswerSince >= RESTART_PATIENCE_MS
      ? { kind: "restartTimeout", since: noAnswerSince }
      : { kind: "restarting", since: noAnswerSince };
  }
  const run = status?.run ?? null;
  if (input.expect && !expectArrived(input.expect, run)) {
    return {
      kind: "pending",
      action: input.expect.action,
      work: status?.running_work ?? { tasks: 0, group_rounds: 0 },
    };
  }
  if (!status || !run) return { kind: "idle" };

  const recent = (iso: string | null | undefined) => {
    if (seenActive) return true;
    const age = ageMs(iso, now);
    return age !== null && age <= RESULT_SHOWN_MS;
  };

  if (isTerminal(run.phase)) {
    const finished = run.finished_at ?? run.updated_at;
    switch (run.phase) {
      case "rollback_failed":
        return { kind: "rollbackFailed", run };
      case "done":
        return recent(finished) ? { kind: "success", run } : { kind: "idle" };
      case "rolled_back":
        return recent(finished)
          ? { kind: "rolledBack", run, step: failedStep(run), requested: wasRequested(run) }
          : { kind: "idle" };
      default:
        return recent(finished) ? { kind: "aborted", run } : { kind: "idle" };
    }
  }

  // A live (non-terminal) run.
  const staleLong = status.stale && staleSince !== null && now - staleSince >= STALE_GRACE_MS;
  if (run.interrupted || (status.stale && !run.running) || staleLong) {
    const bridgeReachable = status.bridge_reachable;
    return {
      kind: "interrupted",
      run,
      bridgeReachable,
      command: status.command ?? CMD_RESUME,
    };
  }
  const stale = status.stale;
  if (run.phase === "rolling_back") return { kind: "rollingBack", run, step: failedStep(run), stale };
  const step = stepOf(run.phase);
  if (step === null) return { kind: "waiting", run, work: status.running_work, stale };
  return { kind: "running", run, step: Math.max(step, maxStep), stale };
}

/** Is a run under way (the status should be asked often, a missing answer is a restart)? */
export function isActive(state: RunState): boolean {
  return (
    state.kind === "pending" ||
    state.kind === "waiting" ||
    state.kind === "running" ||
    state.kind === "rollingBack" ||
    state.kind === "restarting"
  );
}

/** The log excerpt "copy log" puts on the clipboard: the journal's last events. */
export function logText(run: UpdateRun): string {
  const lines = (run.events ?? []).map((e) =>
    [e.at ?? "", e.phase ?? "", e.event ?? "", e.detail ?? ""].filter(Boolean).join("  "),
  );
  const head = `run ${run.run_id}: ${run.from?.tag ?? "?"} -> ${run.target?.tag ?? "?"} · ${run.phase}`;
  return [head, ...(run.error ? [`error: ${run.error}`] : []), ...lines].join("\n");
}

/** The version without the tag's "v" — the card says "0.9.3", not "v0.9.3". */
export function plainVersion(v: string | null | undefined): string {
  if (!v) return "";
  return v.startsWith("v") ? v.slice(1) : v;
}

/** Check ids the card has words for (EN/DE); anything else reads generic. */
export const KNOWN_CHECKS = [
  "bridge", "linger", "detach", "install", "channel", "lock", "docker", "project",
  "checkout", "release", "database", "work", "disk", "step_up", "schedules",
  "group_rounds", "loops", "engine",
] as const;

export function checkKey(c: UpdateCheck): string | null {
  return (KNOWN_CHECKS as readonly string[]).includes(c.id) ? `${c.id}.${c.status}` : null;
}

/** Red first, then yellow, then green — the reason a button is off reads first. */
export function sortChecks(checks: UpdateCheck[]): UpdateCheck[] {
  const rank: Record<CheckStatus, number> = { fail: 0, warn: 1, ok: 2 };
  return [...checks].sort((a, b) => rank[a.status] - rank[b.status]);
}
