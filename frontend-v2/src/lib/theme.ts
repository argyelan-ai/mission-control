/**
 * theme — dark / light / system for the whole app (ADR-084).
 *
 * The palette lives in CSS variables (globals.css: `@theme` = dark,
 * `:root[data-theme="light"]` = light). Switching is one attribute on <html>;
 * every token in colors.ts is a `var(--color-…)` and follows. The choice is
 * stored per browser (localStorage) and applied before first paint by
 * THEME_INIT_SCRIPT so a light user never sees a dark flash.
 */

export type ThemeChoice = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

export const THEME_STORAGE_KEY = "mc_theme";
const CHOICES: ThemeChoice[] = ["dark", "light", "system"];

export function readStoredTheme(): ThemeChoice {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    return CHOICES.includes(raw as ThemeChoice) ? (raw as ThemeChoice) : "dark";
  } catch {
    return "dark";
  }
}

export function resolveTheme(choice: ThemeChoice, prefersLight: boolean): ResolvedTheme {
  if (choice === "system") return prefersLight ? "light" : "dark";
  return choice;
}

function systemPrefersLight(): boolean {
  try {
    return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  } catch {
    return false;
  }
}

export function applyTheme(resolved: ResolvedTheme): void {
  const el = document.documentElement;
  el.setAttribute("data-theme", resolved);
  el.style.colorScheme = resolved;
}

/** Persist the choice and apply it right away. */
export function setTheme(choice: ThemeChoice): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    /* private mode — still apply for this page */
  }
  applyTheme(resolveTheme(choice, systemPrefersLight()));
}

/** Apply whatever is stored (used once on mount as a safety net). */
export function applyStoredTheme(): ResolvedTheme {
  const resolved = resolveTheme(readStoredTheme(), systemPrefersLight());
  applyTheme(resolved);
  return resolved;
}

/**
 * Inline <script> for <head>: same logic as above, dependency-free, runs
 * before the first paint. Keep in sync with readStoredTheme/resolveTheme.
 */
export const THEME_INIT_SCRIPT = `(function(){try{var k="${THEME_STORAGE_KEY}",v=localStorage.getItem(k),s=(v==="system"?(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches?"light":"dark"):(v==="light"?"light":"dark")),e=document.documentElement;e.setAttribute("data-theme",s);e.style.colorScheme=s;}catch(_){}})();`;
