/**
 * Server-safe theme constants (no React import) — used by app/layout.tsx
 * (a server component) and re-exported by lib/theme.ts. ADR-087.
 */
export type ThemeChoice = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

export const THEME_STORAGE_KEY = "mc_theme";
export const LIGHT_QUERY = "(prefers-color-scheme: light)";

/** Browser chrome colour (<meta name="theme-color">) = the page ground. */
export const THEME_COLOR: Record<ResolvedTheme, string> = {
  dark: "#1C1C1C", // --color-bg-deep (dark)
  light: "#F3F1EB", // --color-bg-deep (light)
};

/**
 * Inline <script> for <head>: same logic as readStoredTheme/resolveTheme in
 * lib/theme.ts, dependency-free, runs before the first paint. Dark unless a
 * valid light/system choice is stored. theme.test executes it.
 */
export const THEME_INIT_SCRIPT = `(function(){var s="dark";try{var v=localStorage.getItem("${THEME_STORAGE_KEY}");if(v==="light")s="light";else if(v==="system"&&window.matchMedia&&window.matchMedia("${LIGHT_QUERY}").matches)s="light";}catch(_){}try{var e=document.documentElement;e.setAttribute("data-theme",s);e.style.colorScheme=s;}catch(_){}})();`;
