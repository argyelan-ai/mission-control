// Pure logic of the W1 test sheet (P3): what the operator picked, what goes
// to POST /bench/tests, and what the sheet says about time and money. Kept
// free of React so the rules (D3/D4 money, A-before-B, max 2 contestants,
// effort ladder) are tested on their own.
import type {
  BenchPlan,
  Blocker,
  Contestant,
  ContestantPick,
  CostKind,
  DurationRange,
  Effort,
  EffortOption,
  Harness,
  HarnessOption,
  Money,
  PresetView,
  RunClass,
} from "../types";

/** D4: above this many dollars per test MC asks a second time. The server's
 *  409 (`CostConfirmation.cap_usd`) is the authority; this is the default
 *  the sheet shows before it has asked. */
export const DEFAULT_CAP_USD = 1;

/** At most two contestants per video (plan.max_video_contestants). */
export const MAX_PICKS = 2;

export interface SheetSelection {
  /** Runtime of the model under test. */
  subjectId: string;
  /** Harnesses chosen for the subject: "api" (class A) and/or an agent (class B). */
  harnesses: Harness[];
  /** Effort levels for the subject's API path (several only on the ladder). */
  efforts: Effort[];
  /** Optional opponent (always the API path, D4/N7). */
  opponentId: string | null;
  opponentEfforts: Effort[];
}

/** Intensity order for the effort ladder: aus → … → max. */
const INTENSITY: Effort[] = ["off", "on", "low", "medium", "high", "max"];

export function defaultEffort(c: Contestant | undefined): Effort {
  const def = c?.effort.options.find((o) => o.is_default);
  return def?.level ?? "default";
}

/** The level an option really stands for ("default" merged into "high"). */
export function effectiveLevel(o: EffortOption, defaultObserved: "on" | "off" | "unknown"): Effort | null {
  if (o.level !== "default") return o.level;
  if (o.merged_level) return o.merged_level;
  if (defaultObserved === "on" || defaultObserved === "off") return defaultObserved;
  return null;
}

/** Options in the order the sheet shows them: backend order, the ladder by intensity. */
export function orderedOptions(c: Contestant, ladder: boolean): EffortOption[] {
  const opts = [...c.effort.options];
  if (!ladder) return opts;
  const rank = (o: EffortOption) => {
    const lvl = effectiveLevel(o, c.effort.default_observed);
    const i = lvl ? INTENSITY.indexOf(lvl) : -1;
    return i < 0 ? 2.5 : i; // unknown default sits in the middle
  };
  return opts.sort((a, b) => rank(a) - rank(b));
}

export function harnessOption(c: Contestant | undefined, h: Harness): HarnessOption | undefined {
  return c?.harnesses.find((o) => o.harness === h);
}

export function runClassOf(c: Contestant | undefined, h: Harness): RunClass {
  return harnessOption(c, h)?.run_class ?? (h === "api" ? "A" : "B");
}

/** Initial selection from the plan's own picks (incl. a duel from "Nochmal"). */
export function selectionFromPlan(plan: BenchPlan): SheetSelection | null {
  const subjectId = plan.picks[0]?.runtime_id ?? plan.contestants[0]?.runtime_id;
  if (!subjectId) return null;
  const subject = plan.contestants.find((c) => c.runtime_id === subjectId);
  const own = plan.picks.filter((p) => p.runtime_id === subjectId);
  const harnesses = own.length ? own.map((p) => p.harness) : (["api"] as Harness[]);
  const apiPick = own.find((p) => p.harness === "api");
  const other = plan.picks.find((p) => p.runtime_id !== subjectId);
  return {
    subjectId,
    harnesses,
    efforts: apiPick?.efforts.length ? [...apiPick.efforts] : [defaultEffort(subject)],
    opponentId: other?.runtime_id ?? null,
    opponentEfforts: other?.efforts.length ? [...other.efforts] : ["default"],
  };
}

/** The request picks. Agent harnesses send no effort (D5: display only). */
export function buildPicks(sel: SheetSelection): Required<ContestantPick>[] {
  const picks: Required<ContestantPick>[] = sel.harnesses.map((h) => ({
    runtime_id: sel.subjectId,
    harness: h,
    efforts: h === "api" ? [...sel.efforts] : ["default"],
  }));
  if (sel.opponentId) {
    picks.push({ runtime_id: sel.opponentId, harness: "api", efforts: [...sel.opponentEfforts] });
  }
  return picks.slice(0, MAX_PICKS);
}

export function samePicks(a: Required<ContestantPick>[], b: Required<ContestantPick>[]): boolean {
  if (a.length !== b.length) return false;
  const key = (p: Required<ContestantPick>) => `${p.runtime_id}|${p.harness}|${[...p.efforts].sort().join(",")}`;
  const ka = a.map(key).sort();
  const kb = b.map(key).sort();
  return ka.every((k, i) => k === kb[i]);
}

/** Toggle a harness chip. With an opponent there is room for one harness
 *  only (max 2 contestants), so the chip swaps instead of adding. Never
 *  leaves the subject without a harness. */
export function toggleHarness(sel: SheetSelection, h: Harness): SheetSelection {
  const has = sel.harnesses.includes(h);
  if (sel.opponentId) return { ...sel, harnesses: [h] };
  if (has) {
    if (sel.harnesses.length === 1) return sel;
    return { ...sel, harnesses: sel.harnesses.filter((x) => x !== h) };
  }
  // API first, then the agent — the order the runs happen in (A before B).
  const next = [...sel.harnesses, h].sort((a, b) => (a === "api" ? -1 : b === "api" ? 1 : 0));
  return { ...sel, harnesses: next.slice(0, MAX_PICKS) };
}

/** Effort chip: single choice, or the ladder's explicit multi-select (max 3). */
export function toggleEffort(sel: SheetSelection, level: Effort, ladder: boolean): SheetSelection {
  if (!ladder) return { ...sel, efforts: [level] };
  const has = sel.efforts.includes(level);
  if (has) {
    if (sel.efforts.length === 1) return sel;
    return { ...sel, efforts: sel.efforts.filter((e) => e !== level) };
  }
  if (sel.efforts.length >= 3) return sel;
  return { ...sel, efforts: [...sel.efforts, level] };
}

export function canAddOpponent(sel: SheetSelection): boolean {
  return !sel.opponentId && sel.harnesses.length < MAX_PICKS;
}

export interface MoneyView {
  kind: CostKind;
  /** Real dollars (api_price only). */
  usd: number | null;
  /** What the same runs would cost at API list price (D3, small text). */
  hypotheticalUsd: number | null;
  needsConfirmation: boolean;
  secondConfirmation: boolean;
  /** Provider of the subscription run(s), for "über dein Abo bei …". */
  provider: string | null;
}

const KIND_RANK: Record<CostKind, number> = { api_price: 3, subscription: 2, unknown: 1, local: 0 };

function addOpt(a: number | null, b: number | null): number | null {
  if (a == null) return b;
  if (b == null) return a;
  return a + b;
}

/** Money for the whole test: the most costly kind decides the wording
 *  (api_price > subscription > unknown > local); real dollars only from
 *  api_price contestants, never a bare $ on local/subscription (D3). */
export function moneyFor(
  picks: Required<ContestantPick>[],
  contestants: Contestant[],
  capUsd: number = DEFAULT_CAP_USD,
): MoneyView {
  let kind: CostKind = "local";
  let total: number | null = null;
  let hypo: number | null = null;
  let provider: string | null = null;
  let second = false;
  for (const p of picks) {
    const c = contestants.find((x) => x.runtime_id === p.runtime_id);
    if (!c) continue;
    // Agent runs on a local box cost nothing extra and have no API price.
    const m: Money = p.harness === "api" ? c.money : { ...c.money, usd: null, hypothetical_usd: null };
    const factor = Math.max(1, p.efforts.length);
    if (KIND_RANK[m.cost_kind] > KIND_RANK[kind]) kind = m.cost_kind;
    if (m.cost_kind === "api_price" && m.usd != null) total = addOpt(total, m.usd * factor);
    if (m.cost_kind !== "api_price" && m.hypothetical_usd != null) hypo = addOpt(hypo, m.hypothetical_usd * factor);
    if (m.cost_kind === "subscription" && !provider) provider = c.provider;
    if (m.second_confirmation) second = true;
  }
  const needs = kind === "api_price";
  return {
    kind,
    usd: needs ? total : null,
    hypotheticalUsd: hypo,
    needsConfirmation: needs,
    secondConfirmation: needs && (second || (total ?? 0) > capUsd),
    provider,
  };
}

/** The plan's own money, in the same shape. */
export function moneyFromPlan(m: Money, provider: string | null): MoneyView {
  const needs = m.cost_kind === "api_price";
  return {
    kind: m.cost_kind,
    usd: needs ? m.usd : null,
    hypotheticalUsd: needs ? null : m.hypothetical_usd,
    needsConfirmation: needs || m.needs_confirmation,
    secondConfirmation: m.second_confirmation,
    provider,
  };
}

export interface EstimateView {
  /** Class A (model pure) range in seconds, summed over contestants and levels. */
  a: DurationRange | null;
  /** Class B (agent builds) range from history ("bisher 4–92 min"). */
  b: DurationRange | null;
}

function sumRange(a: DurationRange | null, r: DurationRange, factor: number): DurationRange {
  const scale = (v: number | null) => (v == null ? null : v * factor);
  if (!a) return { min_s: scale(r.min_s), max_s: scale(r.max_s), basis: r.basis };
  return {
    min_s: addOpt(a.min_s, scale(r.min_s)),
    max_s: addOpt(a.max_s, scale(r.max_s)),
    basis: a.basis === "history" && r.basis === "history" ? "history" : r.basis,
  };
}

export function estimateFor(picks: Required<ContestantPick>[], contestants: Contestant[]): EstimateView {
  let a: DurationRange | null = null;
  const agent: DurationRange[] = [];
  for (const p of picks) {
    const c = contestants.find((x) => x.runtime_id === p.runtime_id);
    const opt = harnessOption(c, p.harness);
    if (!opt) continue;
    if (opt.run_class === "A") a = sumRange(a, opt.estimate, Math.max(1, p.efforts.length));
    else agent.push(opt.estimate);
  }
  // Agent runs: the widest history range (n=1 each, one after the other).
  const b: DurationRange | null = agent.length
    ? agent.reduce((x, y) => ({ ...x, max_s: Math.max(x.max_s ?? 0, y.max_s ?? 0) }))
    : null;
  return { a, b };
}

/** Blockers of the plan plus every picked contestant's, once per code. */
export function blockersFor(
  plan: BenchPlan,
  picks: Required<ContestantPick>[],
  contestants: Contestant[],
  usePlanBlockers: boolean,
): Blocker[] {
  const seen = new Set<string>();
  const out: Blocker[] = [];
  const add = (b: Blocker) => {
    if (seen.has(b.code)) return;
    seen.add(b.code);
    out.push(b);
  };
  if (usePlanBlockers) plan.blockers.forEach(add);
  for (const p of picks) {
    const c = contestants.find((x) => x.runtime_id === p.runtime_id);
    c?.blockers.forEach(add);
  }
  return out;
}

/** Presets offered as chips: the plan's own, plus "Nochmal" only for a re-run. */
export function presetChoices(plan: BenchPlan, hasRerun: boolean): PresetView[] {
  const list = plan.presets.filter((p) => p.id !== "rerun");
  if (hasRerun) {
    const rerun = plan.presets.find((p) => p.id === "rerun") ?? (plan.preset.id === "rerun" ? plan.preset : null);
    list.push(rerun ?? { ...plan.preset, id: "rerun" });
  }
  return list;
}
