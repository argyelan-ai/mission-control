/**
 * Night shift — frontend model (ROADMAP E2).
 *
 * The operator marks tasks "run tonight" (a harness × runtime pair, local by
 * default); mc-worker starts them one after another as heads inside the
 * window from Settings and sends one morning report. Types for
 * `/api/v1/night-shift*` and the pure rules the UI follows.
 *
 *   - A pair may be marked when it could start now OR is only held back for
 *     today (`engine_not_ready`, `box_busy`) — by tonight the model can be up
 *     and the box free. Every other block stays a block.
 *   - The pre-selected pair is the head launcher's local default; a cloud
 *     pair is never pre-selected.
 *   - Texts never come from the backend: state and reason CODES map to keys
 *     in the `nightShift` i18n namespace.
 *   - MC is the operator's channel: the morning report and blocked notices
 *     show on Home ("Last night" card). Slack / Telegram get a copy only
 *     when `send_to_channels` is on (default off).
 */

import { chooseInitialPair, parseHeadError, type HeadPair, type HeadPairsResponse, type HeadRun } from "./heads";

export interface NightWindow {
  night: string;
  starts_at: string;
  ends_at: string;
}

export interface NightConfig {
  enabled: boolean;
  start: string;
  end: string;
  timezone: string;
  cloud_share: number;
  /** also send the report / notices to Slack or Telegram (default off) */
  send_to_channels: boolean;
  /** report channels configured right now ("telegram", "slack") */
  channels: string[];
  /** now inside the window */
  active: boolean;
  /** the current window, or the next one */
  window: NightWindow;
}

export type NightConfigUpdate = Partial<
  Pick<NightConfig, "enabled" | "start" | "end" | "timezone" | "cloud_share" | "send_to_channels">
>;

export type NightEntryState = "queued" | "waiting" | "skipped" | "started";

export interface NightEntry {
  task_id: string;
  title: string;
  task_status: string;
  harness: string;
  runtime_slug: string;
  locality: "local" | "cloud";
  marked_at: string | null;
  night: string | null;
  state: NightEntryState;
  reason: string | null;
  run: HeadRun | null;
}

export type NightCategory = "passed" | "failed" | "needs_you" | "blocked" | "running";

export interface NightReportEntry {
  task_id: string;
  title: string;
  started: boolean;
  state?: string | null;
  reason?: string | null;
  blocked?: string | null;
  pr_url?: string | null;
  category: NightCategory;
}

export interface NightReport {
  night: string;
  sent_at: string;
  delivered: boolean;
  /** sent · undelivered (channels on, sending failed) · stored (channels off: MC only) */
  state?: "sent" | "undelivered" | "stored" | "sending";
  entries: NightReportEntry[];
}

export interface NightTonight {
  config: NightConfig;
  entries: NightEntry[];
  last_report: NightReport | null;
}

// ── Last night (Home card) ───────────────────────────────────────────────────

export interface LastNightEntry extends NightReportEntry {
  run_id?: string | null;
  silent_s?: number | null;
  harness?: string | null;
  runtime_slug?: string | null;
  /** the live run — a question may be answered by now */
  run: HeadRun | null;
}

export interface LastNightReport {
  night: string;
  sent_at: string | null;
  /** also delivered to Slack / Telegram */
  delivered: boolean;
  dismissed: boolean;
  entries: LastNightEntry[];
}

/** A night head that is blocked right now (shown during the night). */
export interface NightNotice {
  task_id: string;
  title: string;
  kind: "needs_you" | "silent";
  silent_s: number | null;
  run: HeadRun;
}

export interface LastNight {
  report: LastNightReport | null;
  notices: NightNotice[];
}

/** Card order: what needs the operator first, what went fine last. */
const CARD_ORDER: NightCategory[] = ["needs_you", "blocked", "failed", "running", "passed"];

/** Report rows in card order (stable within a category). */
export function lastNightRows(report: Pick<LastNightReport, "entries"> | null | undefined): LastNightEntry[] {
  const rank = (c: NightCategory) => {
    const i = CARD_ORDER.indexOf(c);
    return i < 0 ? CARD_ORDER.length : i;
  };
  return [...(report?.entries ?? [])]
    .map((e, i) => ({ e, i }))
    .sort((a, b) => rank(a.e.category) - rank(b.e.category) || a.i - b.i)
    .map(({ e }) => e);
}

/** Show the card at all? Only when a night ran or a night head is blocked now. */
export function hasLastNight(data: LastNight | null | undefined): boolean {
  if (!data) return false;
  return (data.report?.entries.length ?? 0) > 0 || data.notices.length > 0;
}

/** "Answer" is offered while the live run still waits on its question. */
export function canAnswer(run: HeadRun | null | undefined): run is HeadRun {
  return !!run && run.state === "needs_you" && !!run.harness && !!run.runtime_slug;
}

export const NIGHT_CATEGORIES: NightCategory[] = ["passed", "failed", "needs_you", "blocked", "running"];

/** Reasons a pair may still be marked for tonight. */
const MARKABLE_BLOCKS = new Set(["engine_not_ready", "box_busy"]);

/** A pair that can be chosen for tonight (see module note). */
export function isMarkable(p: HeadPair): boolean {
  return p.startable || (p.status === "blocked" && MARKABLE_BLOCKS.has(p.reason_code ?? ""));
}

/**
 * Pairs for the "tonight" picker: today-only blocks become choosable. The
 * reason code stays, so the picker can still say "not running right now".
 */
export function pairsForTonight(pairs: HeadPair[]): HeadPair[] {
  return pairs.map((p) => (!p.startable && isMarkable(p) ? { ...p, startable: true } : p));
}

/** Initial pair for tonight: the launcher's rule (remembered local, else the
 *  local default) on the tonight-mapped list. Never a cloud pair. */
export function chooseTonightPair(resp: HeadPairsResponse | null | undefined, remembered: string | null): HeadPair | null {
  if (!resp) return null;
  const mapped = pairsForTonight(resp.pairs);
  const def = resp.default_pair ? (pairsForTonight([resp.default_pair])[0] ?? null) : null;
  return chooseInitialPair({ pairs: mapped, default_pair: def }, remembered);
}

/** i18n key under `nightShift.state` for an entry. A started entry shows its
 *  head state instead (`heads.state.*`). */
export function entryStateKey(entry: Pick<NightEntry, "state">): string {
  return `state.${entry.state}`;
}

/**
 * May this card be marked for tonight? Only when nobody else works on it: an
 * inbox card nothing holds (the mark will hold it), or an open card on
 * `manual_hold`. Same rule as the backend (`night.markable`, 409 `task_busy`).
 */
export function canMarkTonight(task: { status: string; run_control?: string | null }): boolean {
  if (task.status === "done" || task.status === "aborted") return false;
  const rc = task.run_control ?? null;
  return rc === "manual_hold" || (task.status === "inbox" && rc === null);
}

const REASONS = new Set([
  "task_moved",
  "task_move_failed",
  "lane_busy",
  "cloud_share",
  "engine_not_ready",
  "box_busy",
  "pair_blocked",
  "pair_gone",
  "repo_required",
  "head_active",
  "task_not_found",
  "spool_unavailable",
  "not_reached",
  "run_missing",
]);

/** i18n key under `nightShift.reason` for a waiting/skipped reason code. */
export function nightReasonKey(code: string | null | undefined): string {
  return code && REASONS.has(code) ? `reason.${code}` : "reason.other";
}

const ERROR_CODES = new Set([
  "heads_disabled",
  "task_not_found",
  "task_finished",
  "repo_required",
  "night_started",
  "night_busy",
  "task_busy",
  "head_active",
  "pair_blocked",
  "invalid_config",
]);

/** i18n key under `nightShift.errors` for any error of a night-shift call. */
export function nightErrorKey(err: unknown): string {
  const { code } = parseHeadError(err);
  return code && ERROR_CODES.has(code) ? `errors.${code}` : "errors.unknown";
}

/** Field codes of a 422 invalid_config. */
export function invalidConfigFields(err: unknown): string[] {
  const { code, detail } = parseHeadError(err);
  if (code !== "invalid_config" || !Array.isArray(detail.fields)) return [];
  return (detail.fields as unknown[]).map(String);
}

/** Counts per category of a morning report. */
export function reportCounts(report: Pick<NightReport, "entries"> | null | undefined): Record<NightCategory, number> {
  const out: Record<NightCategory, number> = { passed: 0, failed: 0, needs_you: 0, blocked: 0, running: 0 };
  for (const e of report?.entries ?? []) {
    if (e.category in out) out[e.category] += 1;
  }
  return out;
}

/** "HH:MM" check, same rule as the backend. */
export function isHHMM(value: string): boolean {
  return /^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(value);
}

/** The browser's IANA zone (the Settings page offers it as a one-click value). */
export function browserTimeZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}
