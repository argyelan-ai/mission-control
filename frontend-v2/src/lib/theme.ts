/**
 * theme — dark (default) / light / system, per browser (ADR-087).
 *
 * Dark is Mission Control's default and character ("Serious. Dark."); light
 * is an option. The palette lives in CSS variables (globals.css: `:root` =
 * dark, `:root[data-theme="light"]` = light); switching is one attribute on
 * <html> and every token in lib/colors.ts follows.
 *
 * The choice is stored per browser (localStorage, every access guarded — it
 * throws in private mode / with blocked site data) and applied before the
 * first paint by THEME_INIT_SCRIPT, so a light user never sees a dark flash.
 * startThemeSync() (mounted once in Providers) keeps the page in step with an
 * OS switch while the choice is "system" and with a choice made in another tab.
 */
import { useSyncExternalStore } from "react";

import {
  LIGHT_QUERY,
  THEME_COLOR,
  THEME_STORAGE_KEY,
  type ResolvedTheme,
  type ThemeChoice,
} from "./themeScript";

export { THEME_COLOR, THEME_INIT_SCRIPT, THEME_STORAGE_KEY } from "./themeScript";
export type { ResolvedTheme, ThemeChoice } from "./themeScript";

export const THEME_CHOICES: readonly ThemeChoice[] = ["dark", "light", "system"];

export function readStoredTheme(): ThemeChoice {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    return THEME_CHOICES.includes(raw as ThemeChoice) ? (raw as ThemeChoice) : "dark";
  } catch {
    return "dark";
  }
}

export function resolveTheme(choice: ThemeChoice, prefersLight: boolean): ResolvedTheme {
  if (choice === "system") return prefersLight ? "light" : "dark";
  return choice;
}

function lightQuery(): MediaQueryList | null {
  try {
    return typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(LIGHT_QUERY)
      : null;
  } catch {
    return null;
  }
}

function systemPrefersLight(): boolean {
  return !!lightQuery()?.matches;
}

export function applyTheme(resolved: ResolvedTheme): void {
  if (typeof document === "undefined") return;
  const el = document.documentElement;
  el.setAttribute("data-theme", resolved);
  el.style.colorScheme = resolved;
  document
    .querySelectorAll('meta[name="theme-color"]')
    .forEach((m) => m.setAttribute("content", THEME_COLOR[resolved]));
}

// ── Store (one per page) ─────────────────────────────────────────────────────

interface ThemeState {
  choice: ThemeChoice;
  resolved: ResolvedTheme;
}

const SERVER_STATE: ThemeState = { choice: "dark", resolved: "dark" };
let state: ThemeState | null = null;
const listeners = new Set<() => void>();

function commit(choice: ThemeChoice): void {
  const resolved = resolveTheme(choice, systemPrefersLight());
  applyTheme(resolved);
  if (!state || state.choice !== choice || state.resolved !== resolved) {
    state = { choice, resolved };
    listeners.forEach((fn) => fn());
  }
}

/** Persist the choice (best effort) and apply it right away. */
export function setTheme(choice: ThemeChoice): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    /* private mode — still apply for this page */
  }
  commit(choice);
}

export function subscribeTheme(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function getSnapshot(): ThemeState {
  if (!state) {
    const choice = readStoredTheme();
    state = { choice, resolved: resolveTheme(choice, systemPrefersLight()) };
  }
  return state;
}

/**
 * Wire the page to outside changes: OS light/dark switch (only matters while
 * the choice is "system") and a choice saved in another tab. Returns cleanup.
 */
export function startThemeSync(): () => void {
  commit(readStoredTheme());
  const mql = lightQuery();
  const onSystem = () => {
    if (getSnapshot().choice === "system") commit("system");
  };
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key === THEME_STORAGE_KEY) commit(readStoredTheme());
  };
  mql?.addEventListener?.("change", onSystem);
  window.addEventListener("storage", onStorage);
  return () => {
    mql?.removeEventListener?.("change", onSystem);
    window.removeEventListener("storage", onStorage);
  };
}

/** Current choice + resolved theme; re-renders on every switch. */
export function useTheme(): ThemeState & { setTheme: (c: ThemeChoice) => void } {
  const s = useSyncExternalStore(subscribeTheme, getSnapshot, () => SERVER_STATE);
  return { ...s, setTheme };
}
