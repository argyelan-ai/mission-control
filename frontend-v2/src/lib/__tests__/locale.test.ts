import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const settingsGet = vi.fn();
const settingsSet = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { settings: { get: (...a: unknown[]) => settingsGet(...a), set: (...a: unknown[]) => settingsSet(...a) } },
}));

import {
  LOCALE_AUTO_KEY,
  LOCALE_CACHE_KEY,
  UI_LANGUAGE_SETTING_KEY,
  adoptSavedLocaleAfterLogin,
  chooseUiLocale,
  planStartupLocale,
  readLocaleCookie,
  syncLocaleOnLoad,
} from "../locale";
import { LOCALE_INIT_SCRIPT, LOCALE_PENDING_ATTR } from "../localeScript";

// Bug 19.11.2026: the UI ran in English although German was chosen, whenever
// the NEXT_LOCALE cookie was missing — the choice lived ONLY in that cookie.
// Now the choice is also saved per user (settings key `ui_language`) and
// cached in localStorage; a load without cookie restores it and sets the
// cookie again.

function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() { return m.size; },
    clear: () => m.clear(),
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    key: (i: number) => Array.from(m.keys())[i] ?? null,
    removeItem: (k: string) => { m.delete(k); },
    setItem: (k: string, v: string) => { m.set(k, String(v)); },
  } as Storage;
}

function clearCookies() {
  for (const part of document.cookie.split(";")) {
    const name = part.split("=")[0]?.trim();
    if (name) document.cookie = `${name}=; path=/; max-age=0`;
  }
}

const html = () => document.documentElement;
const savedRow = (language: string) => ({ key: UI_LANGUAGE_SETTING_KEY, value: { language } });
const notFound = () => Promise.reject(new Error("API 404: Setting not found"));

describe("planStartupLocale", () => {
  const base = { cookie: null, saved: null, cached: null, auto: null, current: "en" as const, loggedIn: true };

  it("no cookie + German saved for the user -> German, cookie written, reload", () => {
    const p = planStartupLocale({ ...base, saved: "de" });
    expect(p).toMatchObject({ locale: "de", writeCookie: true, reload: true, saveToDb: null, cache: "de" });
  });

  it("no cookie + saved equals what the server rendered -> cookie written, no reload", () => {
    const p = planStartupLocale({ ...base, saved: "en" });
    expect(p).toMatchObject({ locale: "en", writeCookie: true, reload: false });
  });

  it("no cookie, nothing saved, cached choice -> cached wins and is saved for the user", () => {
    const p = planStartupLocale({ ...base, cached: "de" });
    expect(p).toMatchObject({ locale: "de", writeCookie: true, reload: true, saveToDb: "de" });
  });

  it("no cookie and no saved choice anywhere -> keeps the browser language, marks it automatic, saves nothing", () => {
    const p = planStartupLocale({ ...base, current: "de" });
    expect(p).toMatchObject({ locale: "de", writeCookie: true, reload: false, saveToDb: null, cache: null, markAuto: true });
  });

  it("a cookie from before this fix counts as a choice and is saved for the user", () => {
    const p = planStartupLocale({ ...base, cookie: "de", current: "de" });
    expect(p).toMatchObject({ locale: "de", writeCookie: false, reload: false, saveToDb: "de", cache: "de" });
  });

  it("an automatic cookie (browser language) is never saved as the user's choice", () => {
    const p = planStartupLocale({ ...base, cookie: "en", auto: "en" });
    expect(p).toMatchObject({ saveToDb: null, cache: null, reload: false });
  });

  // Critic 19.11.2026: an automatic cookie must not hide the language saved
  // for the user — it is only the browser language, not a choice.
  it("automatic cookie + another language saved for the user -> saved wins, cookie rewritten, reload", () => {
    const p = planStartupLocale({ ...base, cookie: "en", auto: "en", saved: "de", current: "en" });
    expect(p).toMatchObject({ locale: "de", writeCookie: true, reload: true, saveToDb: null, cache: "de", markAuto: false });
  });

  it("automatic cookie + the same language saved -> becomes the cached choice (no lookups any more)", () => {
    const p = planStartupLocale({ ...base, cookie: "de", auto: "de", saved: "de", current: "de" });
    expect(p).toMatchObject({ locale: "de", writeCookie: false, reload: false, cache: "de" });
  });

  it("a real cookie choice still beats the saved language", () => {
    const p = planStartupLocale({ ...base, cookie: "en", cached: "en", saved: "de" });
    expect(p).toMatchObject({ locale: "en", writeCookie: false, reload: false });
  });

  it("server lookup failed, no cookie, nothing cached -> writes NO cookie (asks again next load)", () => {
    const p = planStartupLocale({ ...base, serverFailed: true });
    expect(p).toMatchObject({ writeCookie: false, markAuto: false, saveToDb: null, reload: false });
  });

  it("server lookup failed, cached choice -> cookie from the cache, but never saved over the unknown row", () => {
    const p = planStartupLocale({ ...base, serverFailed: true, cached: "de" });
    expect(p).toMatchObject({ locale: "de", writeCookie: true, saveToDb: null });
  });

  it("not logged in -> never writes to the server", () => {
    const p = planStartupLocale({ ...base, loggedIn: false, cached: "de" });
    expect(p.saveToDb).toBeNull();
    expect(p.locale).toBe("de");
  });
});

describe("runtime", () => {
  let reload: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.stubGlobal("localStorage", memoryStorage());
    vi.stubGlobal("sessionStorage", memoryStorage());
    reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    clearCookies();
    settingsGet.mockReset();
    settingsSet.mockReset().mockResolvedValue({});
    html().lang = "en";
    html().removeAttribute(LOCALE_PENDING_ATTR);
    html().style.visibility = "";
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    clearCookies();
  });

  it("first load without cookie: the user's saved German applies and the cookie is set afterwards", async () => {
    localStorage.setItem("mc_auth_token", "t");
    settingsGet.mockResolvedValue(savedRow("de"));
    await syncLocaleOnLoad();
    expect(settingsGet).toHaveBeenCalledWith(UI_LANGUAGE_SETTING_KEY);
    expect(readLocaleCookie()).toBe("de");
    expect(localStorage.getItem(LOCALE_CACHE_KEY)).toBe("de");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload twice in a row for the same language (no loop when cookies are blocked)", async () => {
    localStorage.setItem("mc_auth_token", "t");
    settingsGet.mockResolvedValue(savedRow("de"));
    await syncLocaleOnLoad();
    clearCookies();
    await syncLocaleOnLoad();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("without a saved row: browser language stays, cookie is set, the page is shown again", async () => {
    localStorage.setItem("mc_auth_token", "t");
    html().setAttribute(LOCALE_PENDING_ATTR, "");
    html().style.visibility = "hidden";
    settingsGet.mockImplementation(notFound);
    await syncLocaleOnLoad();
    expect(readLocaleCookie()).toBe("en");
    expect(reload).not.toHaveBeenCalled();
    expect(settingsSet).not.toHaveBeenCalled();
    expect(html().style.visibility).toBe("");
    expect(html().hasAttribute(LOCALE_PENDING_ATTR)).toBe(false);
  });

  it("logged out: no API call at all, cookie still set", async () => {
    await syncLocaleOnLoad();
    expect(settingsGet).not.toHaveBeenCalled();
    expect(readLocaleCookie()).toBe("en");
  });

  it("an existing cookie is saved for the user when the server has none yet", async () => {
    localStorage.setItem("mc_auth_token", "t");
    document.cookie = "NEXT_LOCALE=de; path=/";
    html().lang = "de";
    settingsGet.mockImplementation(notFound);
    await syncLocaleOnLoad();
    expect(settingsSet).toHaveBeenCalledWith(UI_LANGUAGE_SETTING_KEY, { language: "de" });
    expect(reload).not.toHaveBeenCalled();
  });

  it("an automatic cookie from earlier gives way to the language saved later", async () => {
    localStorage.setItem("mc_auth_token", "t");
    settingsGet.mockImplementation(notFound);
    await syncLocaleOnLoad(); // nothing saved yet -> auto cookie "en"
    expect(readLocaleCookie()).toBe("en");
    sessionStorage.clear(); // a later visit (new tab / session)
    settingsGet.mockReset().mockResolvedValue(savedRow("de"));
    await syncLocaleOnLoad();
    expect(readLocaleCookie()).toBe("de");
    expect(localStorage.getItem(LOCALE_CACHE_KEY)).toBe("de");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("a failed lookup (502 / offline) does not pin the browser language", async () => {
    localStorage.setItem("mc_auth_token", "t");
    settingsGet.mockRejectedValue(new Error("API 502: Bad Gateway"));
    html().setAttribute(LOCALE_PENDING_ATTR, "");
    html().style.visibility = "hidden";
    await syncLocaleOnLoad();
    expect(readLocaleCookie()).toBeNull();
    expect(localStorage.getItem(LOCALE_AUTO_KEY)).toBeNull();
    expect(html().style.visibility).toBe(""); // shown again anyway
    settingsGet.mockReset().mockResolvedValue(savedRow("de"));
    await syncLocaleOnLoad();
    expect(readLocaleCookie()).toBe("de");
  });

  it("automatic cookie and nothing saved: at most one lookup per browser session", async () => {
    localStorage.setItem("mc_auth_token", "t");
    settingsGet.mockImplementation(notFound);
    await syncLocaleOnLoad();
    await syncLocaleOnLoad();
    await syncLocaleOnLoad();
    expect(settingsGet).toHaveBeenCalledTimes(1);
  });

  it("on /login no authenticated lookup is made (a stale token must not bounce the page)", async () => {
    localStorage.setItem("mc_auth_token", "stale");
    vi.stubGlobal("location", { ...window.location, reload, pathname: "/login" });
    settingsGet.mockResolvedValue(savedRow("de"));
    await syncLocaleOnLoad();
    expect(settingsGet).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it("after login an automatic cookie from the login page does not block the saved language", async () => {
    document.cookie = "NEXT_LOCALE=en; path=/";
    localStorage.setItem(LOCALE_AUTO_KEY, "en");
    settingsGet.mockResolvedValue(savedRow("de"));
    expect(await adoptSavedLocaleAfterLogin()).toBe(true);
    expect(readLocaleCookie()).toBe("de");
  });

  it("chooseUiLocale writes cookie, cache and the per-user setting", async () => {
    localStorage.setItem(LOCALE_AUTO_KEY, "en");
    await chooseUiLocale("de");
    expect(readLocaleCookie()).toBe("de");
    expect(localStorage.getItem(LOCALE_CACHE_KEY)).toBe("de");
    expect(localStorage.getItem(LOCALE_AUTO_KEY)).toBeNull();
    expect(settingsSet).toHaveBeenCalledWith(UI_LANGUAGE_SETTING_KEY, { language: "de" });
  });

  it("chooseUiLocale keeps the local choice when saving fails", async () => {
    settingsSet.mockRejectedValue(new Error("offline"));
    await expect(chooseUiLocale("de")).resolves.toBeUndefined();
    expect(readLocaleCookie()).toBe("de");
  });

  it("after login the saved language is adopted before the app opens", async () => {
    settingsGet.mockResolvedValue(savedRow("de"));
    expect(await adoptSavedLocaleAfterLogin()).toBe(true);
    expect(readLocaleCookie()).toBe("de");
  });

  it("after login with the same language: no full reload needed", async () => {
    settingsGet.mockResolvedValue(savedRow("en"));
    expect(await adoptSavedLocaleAfterLogin()).toBe(false);
  });
});

describe("LOCALE_INIT_SCRIPT (runs in <head> before first paint)", () => {
  const run = () => new Function(LOCALE_INIT_SCRIPT)();
  let reload: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.stubGlobal("localStorage", memoryStorage());
    vi.stubGlobal("sessionStorage", memoryStorage());
    reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    clearCookies();
    html().lang = "en";
    html().removeAttribute(LOCALE_PENDING_ATTR);
    html().style.visibility = "";
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    clearCookies();
  });

  it("no cookie + cached German: sets the cookie and reloads hidden — English is never painted", () => {
    localStorage.setItem(LOCALE_CACHE_KEY, "de");
    run();
    expect(readLocaleCookie()).toBe("de");
    expect(html().style.visibility).toBe("hidden");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("no cookie + cached language equal to the rendered one: cookie only, stays visible", () => {
    localStorage.setItem(LOCALE_CACHE_KEY, "en");
    run();
    expect(readLocaleCookie()).toBe("en");
    expect(html().style.visibility).toBe("");
    expect(reload).not.toHaveBeenCalled();
  });

  it("no cookie, no cache, logged in: hides until the saved language is known", () => {
    localStorage.setItem("mc_auth_token", "t");
    run();
    expect(html().hasAttribute(LOCALE_PENDING_ATTR)).toBe(true);
    expect(html().style.visibility).toBe("hidden");
    expect(reload).not.toHaveBeenCalled();
  });

  it("with a cookie it does nothing", () => {
    document.cookie = "NEXT_LOCALE=de; path=/";
    localStorage.setItem(LOCALE_CACHE_KEY, "en");
    run();
    expect(reload).not.toHaveBeenCalled();
    expect(html().style.visibility).toBe("");
  });

  it("never throws when storage is blocked", () => {
    const boom = () => { throw new Error("SecurityError"); };
    vi.stubGlobal("localStorage", { getItem: boom, setItem: boom, removeItem: boom } as unknown as Storage);
    expect(run).not.toThrow();
  });

  it("is dependency-free (inline script)", () => {
    expect(LOCALE_INIT_SCRIPT).not.toMatch(/import\s|require\(/);
  });
});
