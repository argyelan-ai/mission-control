/**
 * The UI language, kept in three places so it survives a missing cookie:
 *   1. NEXT_LOCALE cookie — what the server reads to render (i18n/request.ts)
 *   2. per-user setting `ui_language` — `{language}` in the generic
 *      preference store (GET/PUT /api/v1/settings/{key}), follows the user
 *      to every browser
 *   3. localStorage cache — lets the <head> script (lib/localeScript.ts)
 *      restore the cookie before the first paint, without a request
 * A load without cookie restores the language (saved → cached → browser
 * language) and writes the cookie again. Bug 19.11.2026: before, the choice
 * lived only in the cookie and the UI fell back to English without it.
 */
import { api } from "@/lib/api";
import { LOCALE_COOKIE, isLocale, type Locale } from "@/i18n/locales";
import { getToken } from "./authToken";
import {
  LOCALE_AUTO_KEY,
  LOCALE_CACHE_KEY,
  LOCALE_COOKIE_MAX_AGE,
  LOCALE_NONE_KEY,
  LOCALE_PENDING_ATTR,
  LOCALE_RELOAD_KEY,
} from "./localeScript";

export { LOCALE_AUTO_KEY, LOCALE_CACHE_KEY } from "./localeScript";
export const UI_LANGUAGE_SETTING_KEY = "ui_language";

// ── Pure decision ───────────────────────────────────────────────────────────

export interface StartupInput {
  /** NEXT_LOCALE cookie value at load (raw, may be invalid or null). */
  cookie: string | null;
  /** Language saved for the user on the server (null = none / logged out). */
  saved: Locale | null;
  /** localStorage cache of the last choice on this browser. */
  cached: Locale | null;
  /** localStorage marker: the cookie value that was set automatically. */
  auto: string | null;
  /** Language the server rendered this page in (<html lang>). */
  current: Locale;
  loggedIn: boolean;
  /** The server lookup failed (not 404): `saved` is unknown, not "none". */
  serverFailed?: boolean;
}

export interface StartupPlan {
  locale: Locale;
  writeCookie: boolean;
  /** Full reload so the server renders `locale` (only when it differs). */
  reload: boolean;
  /** Save this language for the user (server has none yet). */
  saveToDb: Locale | null;
  /** Write this to the localStorage cache (null = leave as is). */
  cache: Locale | null;
  /** The cookie is the browser language, not a choice. */
  markAuto: boolean;
}

export function planStartupLocale(i: StartupInput): StartupPlan {
  if (isLocale(i.cookie)) {
    // The cookie is this browser's choice — unless we filled it in ourselves
    // from the browser language. A cookie from before this fix (no marker)
    // was set by the language selector, so it counts as a choice.
    const isChoice = i.cached === i.cookie || i.auto !== i.cookie;
    // An automatic cookie is only the browser language: the language saved
    // for the user replaces it (critic 19.11.2026 — otherwise one load before
    // the row existed pinned that browser to the browser language for good).
    if (!isChoice && i.saved) {
      return {
        locale: i.saved,
        writeCookie: i.saved !== i.cookie,
        reload: i.saved !== i.current,
        saveToDb: null,
        cache: i.saved,
        markAuto: false,
      };
    }
    return {
      locale: i.cookie,
      writeCookie: false,
      reload: false,
      saveToDb: isChoice && i.loggedIn && i.saved === null ? i.cookie : null,
      cache: isChoice ? i.cookie : null,
      markAuto: false,
    };
  }
  const chosen = i.saved ?? i.cached;
  if (chosen) {
    return {
      locale: chosen,
      writeCookie: true,
      reload: chosen !== i.current,
      // Unknown row (lookup failed): never overwrite it with this browser's cache.
      saveToDb: i.loggedIn && i.saved === null && !i.serverFailed ? chosen : null,
      cache: chosen,
      markAuto: false,
    };
  }
  // The lookup failed: write nothing, so the next load asks again instead of
  // pinning the browser language (the page shows the browser language now).
  if (i.serverFailed) {
    return { locale: i.current, writeCookie: false, reload: false, saveToDb: null, cache: null, markAuto: false };
  }
  // Nothing chosen anywhere: keep what the server picked from the browser
  // language, and remember that this cookie is not a choice.
  return { locale: i.current, writeCookie: true, reload: false, saveToDb: null, cache: null, markAuto: true };
}

// ── Browser helpers (all storage access guarded) ────────────────────────────

function store(kind: "localStorage" | "sessionStorage"): Storage | null {
  try {
    return typeof window === "undefined" ? null : window[kind];
  } catch {
    return null;
  }
}
function get(kind: "localStorage" | "sessionStorage", key: string): string | null {
  try { return store(kind)?.getItem(key) ?? null; } catch { return null; }
}
function put(kind: "localStorage" | "sessionStorage", key: string, value: string | null) {
  try {
    if (value === null) store(kind)?.removeItem(key);
    else store(kind)?.setItem(key, value);
  } catch { /* storage blocked — the cookie still carries the language */ }
}

export function readLocaleCookie(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(new RegExp(`(?:^|;\\s*)${LOCALE_COOKIE}=([^;]*)`));
  return m ? m[1] : null;
}

export function writeLocaleCookie(locale: Locale) {
  document.cookie = `${LOCALE_COOKIE}=${locale}; path=/; max-age=${LOCALE_COOKIE_MAX_AGE}; samesite=lax`;
}

function renderedLocale(): Locale {
  const lang = typeof document === "undefined" ? null : document.documentElement.lang;
  return isLocale(lang) ? lang : "en";
}

function reveal() {
  const e = document.documentElement;
  if (e.hasAttribute(LOCALE_PENDING_ATTR)) {
    e.removeAttribute(LOCALE_PENDING_ATTR);
    e.style.visibility = "";
  }
}

/** `null` = nothing saved (404), `"failed"` = unknown (5xx, offline, …). */
type SavedLookup = Locale | null | "failed";

async function fetchSavedLocale(): Promise<SavedLookup> {
  try {
    const res = await api.settings.get(UI_LANGUAGE_SETTING_KEY);
    const lang = (res?.value as { language?: unknown } | null)?.language;
    return isLocale(lang) ? lang : null;
  } catch (err) {
    // lib/api.ts request() throws "API <status>: …" for HTTP errors.
    return err instanceof Error && /^API 404\b/.test(err.message) ? null : "failed";
  }
}

async function saveLocale(locale: Locale) {
  try {
    await api.settings.set(UI_LANGUAGE_SETTING_KEY, { language: locale });
  } catch { /* the cookie + cache still hold the choice on this browser */ }
}

function cachedLocale(): Locale | null {
  const v = get("localStorage", LOCALE_CACHE_KEY);
  return isLocale(v) ? v : null;
}

// ── Entry points ────────────────────────────────────────────────────────────

function onLoginPage(): boolean {
  try {
    return window.location.pathname.replace(/\/+$/, "") === "/login";
  } catch {
    return false;
  }
}

/**
 * Decide and write cookie / cache / server row; reloading is up to the caller.
 * `prefetched` = a lookup the caller already made (after login).
 */
async function applyStartupLocale(opts: { lookup: boolean; prefetched?: SavedLookup }): Promise<StartupPlan> {
  const loggedIn = opts.lookup && !!getToken();
  const cookie = readLocaleCookie();
  // Ask the server only when it can change something: without a cookie, or
  // when the cookie is no cached choice (an automatic cookie, or a choice
  // from before this fix). "Nothing saved" is remembered for the browser
  // session, so an automatic cookie costs one lookup per session, not per load.
  const needServer =
    loggedIn &&
    (!isLocale(cookie) ||
      (get("localStorage", LOCALE_CACHE_KEY) !== cookie && get("sessionStorage", LOCALE_NONE_KEY) !== "1"));
  const lookup: SavedLookup = !needServer
    ? null
    : opts.prefetched !== undefined
      ? opts.prefetched
      : await fetchSavedLocale();
  if (needServer && lookup === null) put("sessionStorage", LOCALE_NONE_KEY, "1");
  const plan = planStartupLocale({
    cookie,
    saved: lookup === "failed" ? null : lookup,
    cached: cachedLocale(),
    auto: get("localStorage", LOCALE_AUTO_KEY),
    current: renderedLocale(),
    loggedIn: needServer,
    serverFailed: lookup === "failed",
  });
  if (plan.writeCookie) writeLocaleCookie(plan.locale);
  if (plan.cache) {
    put("localStorage", LOCALE_CACHE_KEY, plan.cache);
    put("localStorage", LOCALE_AUTO_KEY, null);
  }
  if (plan.markAuto) put("localStorage", LOCALE_AUTO_KEY, plan.locale);
  if (plan.saveToDb) void saveLocale(plan.saveToDb);
  return plan;
}

/**
 * Runs once per full page load (<LocaleSync> in the root layout).
 * `reload` is injectable: jsdom's `location.reload` cannot be replaced.
 */
export async function syncLocaleOnLoad(reload: () => void = () => window.location.reload()): Promise<void> {
  // No authenticated call on /login: a stale token would answer 401, and
  // lib/api.ts then reloads the login page (critic 19.11.2026). The saved
  // language is taken over right after login (adoptSavedLocaleAfterLogin).
  const plan = await applyStartupLocale({ lookup: !onLoginPage() });
  if (plan.reload && readLocaleCookie() === plan.locale && get("sessionStorage", LOCALE_RELOAD_KEY) !== plan.locale) {
    put("sessionStorage", LOCALE_RELOAD_KEY, plan.locale);
    reload(); // stays hidden if the head script hid it
    return;
  }
  reveal();
}

/** The user picked a language (Settings → Language). */
export async function chooseUiLocale(locale: Locale): Promise<void> {
  writeLocaleCookie(locale);
  put("localStorage", LOCALE_CACHE_KEY, locale);
  put("localStorage", LOCALE_AUTO_KEY, null);
  await saveLocale(locale);
}

/**
 * Right after login (token stored): take over the user's saved language
 * before the app opens. Returns true when it differs from the page language —
 * the caller then does a full navigation so the server renders it.
 */
export async function adoptSavedLocaleAfterLogin(): Promise<boolean> {
  put("sessionStorage", LOCALE_NONE_KEY, null); // maybe another user before
  const saved = await fetchSavedLocale();
  if (saved && saved !== "failed") {
    writeLocaleCookie(saved);
    put("localStorage", LOCALE_CACHE_KEY, saved);
    put("localStorage", LOCALE_AUTO_KEY, null);
    return saved !== renderedLocale();
  }
  // Nothing saved yet: keep this browser's language (and save it if it was a choice).
  return (await applyStartupLocale({ lookup: true, prefetched: saved })).reload;
}
