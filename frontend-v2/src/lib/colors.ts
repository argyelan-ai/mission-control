"use client";

/**
 * Mission Control — Color Tokens (v4 „Signal")
 * Design-Guideline: Serious. Dark. Achromatic. Colour means status — nothing else.
 *
 * Inspirations: Bloomberg Terminal, Linear.app, Stripe Dashboard
 * Principles:
 *   - NO chromatic brand accent. The primary accent is a near-white off-cream
 *     (#EBE8DE); it carries through brightness and area, never through hue.
 *   - Neutral off-blacks for structure (never pure #000)
 *   - The ONLY chromatic tokens are the four status hues. If something is
 *     coloured, it must mean something.
 *   - Error outranks warning through chroma (C .215 vs .095), not brightness.
 *   - No blur, no glass, no shadow-glow
 *
 * App-wide single source since June 2026 (previously components/homepage/colors.ts —
 * a re-export lives there for existing imports).
 * v4 (Juli 2026): argyelan-Cyan als Akzent entfernt (System A „Signal"),
 * blau-getönte Off-Blacks → neutrale Off-Blacks.
 */

// ── Theme plumbing (ADR-084) ─────────────────────────────────────────────────
// Every token below is a CSS variable. The dark values live in globals.css
// `@theme`, the light values in `:root[data-theme="light"]`; lib/theme.ts
// flips the attribute. Two consequences for callers:
//   • never concatenate an alpha suffix (token + "26") — use alpha(C.error, .15)
//   • canvas / xterm / anything that needs a real colour: resolveColor(C.x)

const v = (name: string) => `var(--color-${name})`;

/** Translucent version of a token (or any CSS colour): alpha(C.error, 0.15). */
export function alpha(color: string, a: number): string {
  return `color-mix(in srgb, ${color} ${Math.round(a * 100)}%, transparent)`;
}

/** Resolve a `var(--x)` token to its computed value (canvas, xterm). */
export function resolveColor(color: string, fallback = "#000000"): string {
  const m = /^var\((--[a-zA-Z0-9-]+)\)$/.exec(color.trim());
  if (!m) return color;
  if (typeof document === "undefined") return fallback;
  const val = getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim();
  return val || fallback;
}

export const C = {
  // Backgrounds — Stufung = Tiefe (dark: Off-Blacks · light: Papier → Weiss)
  bgDeep: v("bg-deep"),
  bgBase: v("bg-base"),
  bgSurface: v("bg-surface"),
  bgElevated: v("bg-elevated"),
  bgHover: v("bg-hover"),

  // Text — all body/label tones clear WCAG AA (≥4.5:1) in both themes.
  textPrimary: v("text-primary"),
  textSecondary: v("text-secondary"),
  textMuted: v("text-muted"),
  textDim: v("text-dim"),       // decoration / inactive icons ONLY — never body text

  // Borders
  borderSubtle: v("border-subtle"),
  border: v("border"),
  borderActive: v("border-strong"),
  borderAccent: v("border-accent"),

  // ONE accent — achromatisch (dark: Bone · light: Tinte): trägt über
  // Helligkeit und Fläche, nicht über Buntheit.
  accent: v("accent"),
  accentSubtle: v("accent-subtle"),
  accentHover: v("accent-hover"),
  accentDeep: v("accent-deep"),
  onAccent: v("on-accent"), // Text auf Akzent-Fläche

  // Fills — Messwerte (Balken, Heartbeat, Segmente). Nie reines Schwarz:
  // im hellen Theme dunkles Grau, damit Flächen nicht stanzen (Operator, 13.09.).
  fill: v("fill"),
  fillSoft: v("fill-soft"),

  // Status — die EINZIGEN bunten Tokens
  online: v("status-online"),
  warning: v("status-warning"),
  error: v("status-error"),
  info: v("status-info"),

  // Terminal — bleibt in beiden Themes dunkel (Konvention + Kontrast)
  term: v("term"),
  termFg: v("term-fg"),

  // Charts: Ressourcen-Serien tragen über Helligkeit, nicht über Farbton.
  chart: {
    cpu: v("chart-cpu"),
    ram: v("chart-ram"),
    disk: v("chart-disk"),
  },
} as const;

// ── Status & Lane vocabulary — single source (no purple, muted) ───────────────
// Replaces the ad-hoc inline hex that AgentStrip + PipelineView used to carry.

export const STATUS: Record<string, string> = {
  online: C.online,        // #55A964
  busy: C.info,            // #5890CA — active work is an info state, not an accent
  idle: C.textDim,         // #8A8A8A
  offline: v("status-offline"),
  error: C.error,          // #FA4942
  warning: C.warning,      // #A67F3E
  provisioning: C.warning,
  restarting: C.warning,
};

export const LANE: Record<string, string> = {
  inbox: C.textMuted,      // neutral
  in_progress: C.info,     // #5890CA
  review: C.warning,       // #A67F3E
  // user_test = wartet auf den Operator → hellster Ton, nicht bunt (System A)
  user_test: C.accent,     // #EBE8DE
  waiting: C.info,         // #5890CA — answer-wait, same info family as in_progress
  blocked: C.error,
  failed: C.error,
  aborted: C.warning,
  done: C.online,
};

// ── Status text — AA-safe tones for body text on dark surfaces ──────────────
// Measured against bg-elevated #313131 (the brightest card surface):
// online 5.49:1, error 4.62:1, info 4.74:1 all clear AA unchanged. Only the
// warning ochre lands at 4.34:1, so body text gets a lifted tone.

export const STATUS_TEXT = {
  online: C.online,   // 5.49:1 on #313131 — usable unchanged
  warning: v("status-warning-text"), // lifted tone (dark #B98F4D · light #7A5A1F)
  error: C.error,     // 4.62:1 — usable unchanged
  info: C.info,       // 4.74:1 — usable unchanged
} as const;

// ── External brand colors — the only allowed non-token colors ──────────────
// Platform identities (logos, social badges) stay original, but are
// centralized here instead of scattered inline.

export const BRAND: Record<string, string> = {
  linkedin: "#0A66C2",
  // Social / content platforms
  youtube: "#FF0000",
  tiktok: "#000000",
  instagram: "#E1306C",
  x: "#1DA1F2",        // X / Twitter
  telegram: "#26A5E4",
  newsletter: "#FFB224", // internal neutral — amber for the Newsletter brand
  hackernews: "#FF6600",
  reddit: "#FF4500",
  anthropic: "#D4A373",
  openai: "#10A37F",
  // Language badge colors — external tool identities (GitDiffView EXT_COLOR)
  typescript: "#3178C6",
  react: "#61DAFB",
  javascript: "#F7DF1E",
  python: "#3776AB",
  rust: "#CE422B",
  golang: "#00ADD8",
  java: "#F89820",
  css: "#1572B6",
  scss: "#CC6699",
  html: "#E34F26",
  json: "#A8CC8C",
  yaml: "#CB171E",
  markdown: "#083FA1",
  shell: "#4EAA25",
  sql: "#CC2927",
  env: "#ECD53F",
};

// ── P2 „SIGNAL" shell token set (feat/ui-redesign-v3 → palette-signal) ───────
// The app shell (Sidebar, TopBar, StatusBar, MobileNav, WorkspaceSwitcher) uses
// P2 exclusively; C stays the single source for the pages. Mirrors
// --color-p2-* in styles/globals.css (keep both in sync).
//
// v4: P2 was a warm „PHOSPHOR+ CYAN" set (cream text #E9E0C8, cyan accent).
// System A's accent #EBE8DE sits 1.06:1 against that cream text — the shell
// accent would have been invisible. P2 is therefore mapped onto the same
// System A values as C, so shell and pages are one system again.
export const P2 = {
  // Surfaces (dark: neutrale Off-Blacks · light: Papier)
  bg: v("p2-bg"), // canvas
  pan: v("p2-pan"), // raised panel
  pan2: v("p2-pan2"), // hover / higher elevation
  inset: v("p2-inset"), // sunken (inputs, meters)

  // Lines — gleiche Alpha-Stufen wie C
  line: v("p2-line"), // panel border
  line2: v("p2-line2"), // hairline / dashed separators

  // Text — neutral, ≥4.5:1 on bg/pan
  txt: v("p2-txt"),
  dim: v("p2-dim"),
  faint: v("p2-faint"), // decoration only — never body text

  // Glass — floating overlays that should let the page show through.
  // Panel tone at ~70% so the blur behind it has something to work with;
  // the old voice drawer hardcoded rgba(13,13,15,0.92), which was both a
  // different (blue-tinted) family and barely transparent.
  glass: v("p2-glass"),
  glassLine: v("p2-glass-line"),

  // ONE accent — achromatisch; interaction/focus/selection only
  amb: v("p2-amb"),
  ambD: v("p2-amb-d"), // dimmed accent (borders, gradient start)
  inv: v("p2-inv"), // text on accent surfaces (reverse video)

  // Status trio — die einzigen bunten Shell-Tokens, kein Glow
  ok: v("p2-ok"),
  wrn: v("p2-wrn"),
  err: v("p2-err"),
} as const;

// ── Terminal (xterm.js) theme — „Der Leitstand" ANSI set ────────────────────
// Shared by the Sessions page, Agent CLI tab and Plugins shell. ANSI colors
// stay distinguishable (terminal content fidelity) but desaturated to match
// the Leitstand palette — and magenta is magenta, not the banned AI-violet.
// NOTE: the ANSI slots below (incl. `cyan`) are terminal CONTENT fidelity, not
// UI accent — programs emit ANSI 6/14 and expect a cyan. They are deliberately
// exempt from System A's no-chroma rule.
// The background does NOT track the UI surfaces either: a terminal is content,
// and it reads as a terminal because it is near-black. When the surfaces were
// lifted for daylight legibility this got dragged along to #181818 and the
// pane came out grey — it stays dark on purpose.
export const XTERM_THEME = {
  // Stays dark in BOTH themes (ADR-084): a terminal is content, not chrome.
  background: "#0E0E0E",
  foreground: "#F2F2F2",
  cursor: "#EBE8DE",
  cursorAccent: "#0E0E0E",
  black: "#313131",
  brightBlack: "#8A8A8A",
  red: "#FA4942",
  brightRed: "#FF7A74",
  green: "#55A964",
  brightGreen: "#7FCB8C",
  yellow: "#B98F4D",
  brightYellow: "#D6AC66",
  blue: "#5890CA",
  brightBlue: "#86B3E0",
  magenta: "#C06BB0",
  brightMagenta: "#D68CC8",
  cyan: "#4FA9B5",
  brightCyan: "#7FC9D3",
  white: "#F2F2F2",
  brightWhite: "#FFFFFF",
} as const;

// ── Workspace identity colors — user-choosable board identities (board.color).
// Deliberate small palette for variety beyond the app's structural tokens;
// pink/orange/blue are intentional extras, not structural (no purple).
export const WORKSPACE_COLORS = [
  C.accent, C.info, C.online, C.warning, C.error,
  "#EC4899", "#F97316", C.accentHover, "#3B82F6", C.online,
];

// ── Animation ────────────────────────────────────────────────────────────────

export const sectionVariants = {
  hidden: { opacity: 0, y: 12 },
  visible: (_i: number) => ({
    opacity: 1,
    y: 0,
    transition: {
      delay: _i * 0.05,
      duration: 0.4,
      ease: [0.16, 1, 0.3, 1],
    },
  }),
};

// ── Status helpers ───────────────────────────────────────────────────────────

export function resourceColor(pct: number): string {
  if (pct < 60) return C.textMuted;
  if (pct < 85) return C.warning;
  return C.error;
}

export function latencyColor(ms: number): string {
  if (ms < 50) return C.textMuted;
  if (ms < 200) return C.warning;
  return C.error;
}

export function serviceStatusColor(status: string): string {
  switch (status) {
    case "ok": case "running": return C.online;
    case "degraded": case "warning": return C.warning;
    case "error": case "down": case "offline": return C.error;
    default: return C.textDim;
  }
}

// Returns a `home.*` message key — translate with t() at the render site.
export function getGreetingKey(): "greetingNight" | "greetingMorning" | "greetingAfternoon" | "greetingEvening" {
  const hour = new Date().getHours();
  if (hour < 6) return "greetingNight";
  if (hour < 12) return "greetingMorning";
  if (hour < 18) return "greetingAfternoon";
  return "greetingEvening";
}

// Responsive bento grid
export const bentoMediaStyles = `
@media (max-width: 768px) {
  [style*="grid-template-areas"] {
    grid-template-columns: 1fr !important;
    grid-template-areas:
      "pipeline"
      "agents" !important;
  }
}`;
