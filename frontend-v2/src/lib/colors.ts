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

export const C = {
  // Backgrounds — neutrale Off-Blacks (Stufung = Tiefe)
  bgDeep: "#1C1C1C",
  bgBase: "#181818",
  bgSurface: "#262626",
  bgElevated: "#313131",
  bgHover: "#3A3A3A",

  // Text — all body/label tones clear WCAG AA (≥4.5:1) on bg #1C1C1C–#313131.
  textPrimary: "#F2F2F2",
  textSecondary: "#C9C9C9",
  textMuted: "#A3A3A3",
  textDim: "#8A8A8A",       // decoration / inactive icons ONLY — never body text

  // Borders — neutral (Basisfarbe #A8A8A8, bestehende Alpha-Stufen)
  borderSubtle: "rgba(168,168,168,0.05)",
  border: "rgba(168,168,168,0.10)",
  borderActive: "rgba(168,168,168,0.16)",
  borderAccent: "rgba(235,232,222,0.30)",

  // ONE accent — achromatisch hell (System A „Signal"): trägt über Helligkeit
  // und Fläche, nicht über Buntheit.
  accent: "#EBE8DE",
  accentSubtle: "rgba(235,232,222,0.10)",
  accentHover: "#F9F7EF",
  accentDeep: "#C1BEB2",
  onAccent: "#151411", // Text auf Akzent-Fläche

  // Status — die EINZIGEN bunten Tokens
  online: "#55A964",
  warning: "#A67F3E",
  error: "#FA4942",
  info: "#5890CA",

  // Charts: Ressourcen-Serien tragen über Helligkeit, nicht über Farbton.
  chart: {
    cpu: "#EBE8DE",
    ram: "#A3A3A3",
    disk: "#8A8A8A",
  },
} as const;

// ── Status & Lane vocabulary — single source (no purple, muted) ───────────────
// Replaces the ad-hoc inline hex that AgentStrip + PipelineView used to carry.

export const STATUS: Record<string, string> = {
  online: C.online,        // #55A964
  busy: C.info,            // #5890CA — active work is an info state, not an accent
  idle: C.textDim,         // #8A8A8A
  offline: "#3A3A3A",
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
  warning: "#B98F4D", // 5.38:1 — lifted tone derived from C.warning (#A67F3E = 4.34:1)
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
  // Surfaces — neutrale Off-Blacks
  bg: "#1C1C1C", // canvas
  pan: "#262626", // raised panel
  pan2: "#313131", // hover / higher elevation
  inset: "#181818", // sunken (inputs, meters)

  // Lines — Border-Basisfarbe #A8A8A8, gleiche Alpha-Stufen wie C
  line: "rgba(168,168,168,0.16)", // panel border
  line2: "rgba(168,168,168,0.08)", // hairline / dashed separators

  // Text — neutral, ≥4.5:1 on bg/pan
  txt: "#F2F2F2",
  dim: "#A3A3A3",
  faint: "#8A8A8A", // decoration only — never body text

  // ONE accent — achromatisch; interaction/focus/selection only
  amb: "#EBE8DE",
  ambD: "#C1BEB2", // dimmed accent (borders, gradient start)
  inv: "#151411", // text on accent surfaces (reverse video)

  // Status trio — die einzigen bunten Shell-Tokens, kein Glow
  ok: "#55A964",
  wrn: "#A67F3E",
  err: "#FA4942",
} as const;

// ── Terminal (xterm.js) theme — „Der Leitstand" ANSI set ────────────────────
// Shared by the Sessions page, Agent CLI tab and Plugins shell. ANSI colors
// stay distinguishable (terminal content fidelity) but desaturated to match
// the Leitstand palette — and magenta is magenta, not the banned AI-violet.
// NOTE: the ANSI slots below (incl. `cyan`) are terminal CONTENT fidelity, not
// UI accent — programs emit ANSI 6/14 and expect a cyan. They are deliberately
// exempt from System A's no-chroma rule; only the chrome (bg/fg/cursor/black)
// follows the tokens.
export const XTERM_THEME = {
  background: "#181818",
  foreground: "#F2F2F2",
  cursor: C.accent,
  cursorAccent: "#181818",
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
