/**
 * Onboarding metrics (Welle 3, PR 3-4).
 *
 * Times a handful of first-run moments the plan asks to measure — time to
 * login, to the first green provider key, to the first agent online, to the
 * first question card — plus a short list of fixed-vocabulary error codes.
 * No new table: it rides the existing per-user `settings` KV
 * (`GET/PUT /api/v1/settings/{key}`, `backend/app/routers/settings.py`,
 * `UserSettings`) under one key, and nothing ever leaves this install:
 * no analytics call, no external beacon. "Copy setup report"
 * (`lib/setupReport.ts`) is the only way this data is ever copied anywhere,
 * and only on the operator's own click.
 *
 * Privacy (AGENTS.md, public repo): only timings and fixed string codes are
 * stored here — never raw server error text, a secret value, a hostname or
 * an e-mail. Call sites pass one of a small, known vocabulary of codes
 * (see `checkReason.ts` for the config-check reasons, reused as-is).
 */
import { api } from "./api";

export type OnboardingMilestoneKey =
  | "login"
  | "first_key_green"
  | "first_agent_online"
  | "first_question_card";

export const ONBOARDING_MILESTONE_ORDER: readonly OnboardingMilestoneKey[] = [
  "login",
  "first_key_green",
  "first_agent_online",
  "first_question_card",
];

export interface OnboardingErrorEntry {
  /** A fixed vocabulary code — never free text (see module docstring). */
  code: string;
  at: string; // ISO timestamp
}

export interface OnboardingMetrics {
  /** ISO timestamp of the best-effort "onboarding started" marker this
   *  install is measured against. null before anything was ever recorded. */
  t0: string | null;
  elapsed_ms: Partial<Record<OnboardingMilestoneKey, number>>;
  errors: OnboardingErrorEntry[];
}

const SETTINGS_KEY = "onboarding_metrics";
const T0_STORAGE_KEY = "mc_onboarding_t0";
const MAX_ERRORS = 10;

function emptyMetrics(): OnboardingMetrics {
  return { t0: null, elapsed_ms: {}, errors: [] };
}

/** Browser storage can throw (private mode, blocked site data) — every
 *  access here is best-effort and never breaks the page it is called from. */
function safeLocalStorage(): Storage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Best-effort "onboarding started" marker. Set once, the first time any page
 * runs this — typically the login screen, before an account or a session
 * exists to attach server-side state to. Browser-local only; it never
 * travels anywhere by itself, only as the base a later milestone measures
 * its elapsed time from.
 */
export function markOnboardingStart(): void {
  const ls = safeLocalStorage();
  if (!ls) return;
  try {
    if (!ls.getItem(T0_STORAGE_KEY)) {
      ls.setItem(T0_STORAGE_KEY, new Date().toISOString());
    }
  } catch {
    // Best effort only — a missing t0 just means a later milestone falls
    // back to "now" (see recordOnboardingMilestone).
  }
}

function readT0(): string | null {
  const ls = safeLocalStorage();
  if (!ls) return null;
  try {
    return ls.getItem(T0_STORAGE_KEY);
  } catch {
    return null;
  }
}

async function loadMetrics(): Promise<OnboardingMetrics> {
  try {
    const res = await api.settings.get(SETTINGS_KEY);
    const v = res.value as Partial<OnboardingMetrics> | null | undefined;
    if (!v || typeof v !== "object") return emptyMetrics();
    return {
      t0: typeof v.t0 === "string" ? v.t0 : null,
      elapsed_ms: v.elapsed_ms && typeof v.elapsed_ms === "object" ? v.elapsed_ms : {},
      errors: Array.isArray(v.errors) ? v.errors : [],
    };
  } catch {
    // Not found (fresh install) or offline — either way, start from empty.
    return emptyMetrics();
  }
}

async function saveMetrics(m: OnboardingMetrics): Promise<void> {
  try {
    await api.settings.set(SETTINGS_KEY, m);
  } catch {
    // Metrics are a nice-to-have, never a blocker: swallow and move on.
  }
}

/**
 * Records a milestone the first time it happens. A later call for the same
 * key is a no-op — the earliest time is the one the plan asks for, and a
 * milestone that already fired (e.g. a second provider key tested green)
 * must not overwrite it. Never throws.
 */
export async function recordOnboardingMilestone(
  key: OnboardingMilestoneKey,
): Promise<void> {
  const m = await loadMetrics();
  if (m.elapsed_ms[key] !== undefined) return;
  if (m.t0 === null) m.t0 = readT0() ?? new Date().toISOString();
  const t0Ms = new Date(m.t0).getTime();
  const elapsed = Number.isFinite(t0Ms) ? Math.max(0, Date.now() - t0Ms) : 0;
  m.elapsed_ms = { ...m.elapsed_ms, [key]: elapsed };
  await saveMetrics(m);
}

/**
 * Records a fixed-vocabulary error code with its timestamp, capped to the
 * most recent `MAX_ERRORS` so the list never grows without bound. Never
 * throws — a failure to store an error code must not raise a second error.
 */
export async function recordOnboardingError(code: string): Promise<void> {
  const m = await loadMetrics();
  m.errors = [...m.errors, { code, at: new Date().toISOString() }].slice(-MAX_ERRORS);
  await saveMetrics(m);
}

export async function getOnboardingMetrics(): Promise<OnboardingMetrics> {
  return loadMetrics();
}

/** Test seam: clears the browser-local t0 marker. */
export function _resetOnboardingStartForTests(): void {
  const ls = safeLocalStorage();
  try {
    ls?.removeItem(T0_STORAGE_KEY);
  } catch {
    // ignore
  }
}
