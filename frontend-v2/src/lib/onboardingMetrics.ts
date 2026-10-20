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
import { api, getToken } from "./api";

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
/** Browser-local buffer for milestones/errors recorded before a session
 *  token exists (the login/register screen itself). See `hasToken()`. */
const PENDING_STORAGE_KEY = "mc_onboarding_pending";
const MAX_ERRORS = 10;

function emptyMetrics(): OnboardingMetrics {
  return { t0: null, elapsed_ms: {}, errors: [] };
}

interface PendingMetrics {
  elapsed_ms: Partial<Record<OnboardingMilestoneKey, number>>;
  errors: OnboardingErrorEntry[];
}

function emptyPending(): PendingMetrics {
  return { elapsed_ms: {}, errors: [] };
}

/**
 * Whether a session token exists. Before the first successful login or
 * registration there is no user to attach server-side `settings` state to:
 * `GET/PUT /api/v1/settings/{key}` would 401, and `request()` in `lib/api.ts`
 * treats any 401 outside `/auth/` as an expired session — it clears the
 * token and sends the whole page to `/login`, wiping whatever the operator
 * was just shown (including the error message this module was asked to
 * record). Everything below checks this first and buffers locally instead.
 */
function hasToken(): boolean {
  try {
    return getToken() !== "";
  } catch {
    return false;
  }
}

function readPending(): PendingMetrics {
  const ls = safeLocalStorage();
  if (!ls) return emptyPending();
  try {
    const raw = ls.getItem(PENDING_STORAGE_KEY);
    if (!raw) return emptyPending();
    const v = JSON.parse(raw) as Partial<PendingMetrics> | null;
    if (!v || typeof v !== "object") return emptyPending();
    return {
      elapsed_ms: v.elapsed_ms && typeof v.elapsed_ms === "object" ? v.elapsed_ms : {},
      errors: Array.isArray(v.errors) ? v.errors : [],
    };
  } catch {
    return emptyPending();
  }
}

function writePending(p: PendingMetrics): void {
  const ls = safeLocalStorage();
  if (!ls) return;
  try {
    ls.setItem(PENDING_STORAGE_KEY, JSON.stringify(p));
  } catch {
    // Best effort only, same as every other storage access in this module.
  }
}

function clearPending(): void {
  const ls = safeLocalStorage();
  try {
    ls?.removeItem(PENDING_STORAGE_KEY);
  } catch {
    // ignore
  }
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
  if (!hasToken()) {
    // No session yet (see hasToken docstring) — buffer locally rather than
    // hit a KV endpoint that would 401 and bounce the page to /login.
    const p = readPending();
    if (p.elapsed_ms[key] !== undefined) return;
    p.elapsed_ms = { ...p.elapsed_ms, [key]: computeElapsed() };
    writePending(p);
    return;
  }
  const m = await loadMetrics();
  if (m.elapsed_ms[key] !== undefined) return;
  if (m.t0 === null) m.t0 = readT0() ?? new Date().toISOString();
  const t0Ms = new Date(m.t0).getTime();
  const elapsed = Number.isFinite(t0Ms) ? Math.max(0, Date.now() - t0Ms) : 0;
  m.elapsed_ms = { ...m.elapsed_ms, [key]: elapsed };
  await saveMetrics(m);
}

/** Elapsed time since t0 (module docstring), 0 when no marker exists. */
function computeElapsed(): number {
  const t0 = readT0();
  const t0Ms = t0 ? new Date(t0).getTime() : NaN;
  return Number.isFinite(t0Ms) ? Math.max(0, Date.now() - t0Ms) : 0;
}

/**
 * Records a fixed-vocabulary error code with its timestamp, capped to the
 * most recent `MAX_ERRORS` so the list never grows without bound. Never
 * throws — a failure to store an error code must not raise a second error.
 *
 * Before login/registration succeeds there is no token (see `hasToken()`),
 * so this buffers the entry in `localStorage` instead of calling the
 * server — `flushPendingOnboardingMetrics()` merges it in once a session
 * exists.
 */
export async function recordOnboardingError(code: string): Promise<void> {
  const entry: OnboardingErrorEntry = { code, at: new Date().toISOString() };
  if (!hasToken()) {
    const p = readPending();
    p.errors = [...p.errors, entry].slice(-MAX_ERRORS);
    writePending(p);
    return;
  }
  const m = await loadMetrics();
  m.errors = [...m.errors, entry].slice(-MAX_ERRORS);
  await saveMetrics(m);
}

/**
 * Merges anything buffered by `recordOnboardingMilestone`/`recordOnboardingError`
 * while there was no session (the login/register screen) into the real,
 * per-user metrics now that one exists. Call once, right after a token is
 * stored (see `app/login/page.tsx`). Never throws.
 */
export async function flushPendingOnboardingMetrics(): Promise<void> {
  const pending = readPending();
  if (
    Object.keys(pending.elapsed_ms).length === 0 &&
    pending.errors.length === 0
  ) {
    return;
  }
  clearPending();
  try {
    const m = await loadMetrics();
    if (m.t0 === null) m.t0 = readT0() ?? new Date().toISOString();
    for (const [key, elapsed] of Object.entries(pending.elapsed_ms)) {
      const k = key as OnboardingMilestoneKey;
      if (m.elapsed_ms[k] === undefined) m.elapsed_ms[k] = elapsed;
    }
    m.errors = [...m.errors, ...pending.errors].slice(-MAX_ERRORS);
    await saveMetrics(m);
  } catch {
    // Metrics are a nice-to-have (module docstring) — losing a buffered
    // entry on a failed flush is acceptable, raising here is not.
  }
}

export async function getOnboardingMetrics(): Promise<OnboardingMetrics> {
  return loadMetrics();
}

/** Test seam: clears the browser-local t0 marker and any pending buffer. */
export function _resetOnboardingStartForTests(): void {
  const ls = safeLocalStorage();
  try {
    ls?.removeItem(T0_STORAGE_KEY);
  } catch {
    // ignore
  }
  clearPending();
}
