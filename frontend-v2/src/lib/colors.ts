"use client";

/**
 * Mission Control — Color Tokens (v3 „Leitstand · argyelan Edition")
 * Design-Guideline: Serious. Dark. Cyan-solo. No purple.
 *
 * Inspirations: Bloomberg Terminal, Linear.app, Stripe Dashboard
 * Principles:
 *   - One accent only (argyelan-Cyan #00E5FF — die Brand-Farbe)
 *   - Blue-tinted off-blacks for structure (never neutral grey, never pure #000)
 *   - Status colors muted, never glowing
 *   - No blur, no glass, no shadow-glow
 *
 * App-wide single source since June 2026 (previously components/homepage/colors.ts —
 * a re-export lives there for existing imports).
 * v3 (Juli 2026): Teal → argyelan-Cyan, neutrale Graus → blau-getönte Off-Blacks.
 */

export const C = {
  // Backgrounds — blau-getönte Off-Blacks (Stufung = Tiefe)
  bgDeep: "#04070C",
  bgBase: "#070B12",
  bgSurface: "#0B111C",
  bgElevated: "#101827",
  bgHover: "#162134",

  // Text — all body/label tones clear WCAG AA (≥4.5:1) on bg #04070C–#101827.
  textPrimary: "#EDF2FA",
  textSecondary: "#A5B0C2", // ~7.4:1
  textMuted: "#7E8A9E",     // ~5.1:1
  textDim: "#566178",       // decoration / inactive icons ONLY — never body text

  // Borders — kalt getönt
  borderSubtle: "rgba(146,170,206,0.05)",
  border: "rgba(146,170,206,0.10)",
  borderActive: "rgba(146,170,206,0.16)",
  borderAccent: "rgba(0,229,255,0.30)",

  // ONE accent only — argyelan Cyan
  accent: "#00E5FF",
  accentSubtle: "rgba(0,229,255,0.10)",
  accentHover: "#6BEAFF",
  accentDeep: "#00B4CC",
  onAccent: "#00252B", // Text auf Cyan-Fläche

  // Status (desaturated, never bright)
  online: "#2B9A4A",
  warning: "#B8870A",
  error: "#C23838",
  info: "#2E6FD8",

  chart: {
    cpu: "#00E5FF",
    ram: "#5E83A8",
    disk: "#7D92AD",
  },
} as const;

// ── Status & Lane vocabulary — single source (no purple, muted) ───────────────
// Replaces the ad-hoc inline hex that AgentStrip + PipelineView used to carry.

export const STATUS: Record<string, string> = {
  online: C.online,        // #2B9A4A
  busy: C.accent,          // teal — active work (was purple #8B5CF6)
  idle: C.textDim,         // #6E6E6E
  offline: "#3A3A3A",
  error: C.error,          // #C23838
  warning: C.warning,      // #B8870A
  provisioning: C.warning,
  restarting: C.warning,
};

export const LANE: Record<string, string> = {
  inbox: C.textMuted,      // neutral
  in_progress: C.info,     // #2E6FD8
  review: C.warning,       // #B8870A
  user_test: C.accent,     // teal (was purple #8B5CF6)
  waiting: C.info,         // #2E6FD8 — answer-wait, same info-blue family as in_progress
  blocked: C.error,
  failed: C.error,
  aborted: C.warning,
  done: C.online,
};

// ── Status text — AA-safe tones for body text on dark surfaces ──────────────
// C.error (3.7:1) and C.info (3.8:1) are fine as border/surface/icon, but too
// dark for body text. These tones meet ≥4.5:1 on #050505–#161616.

export const STATUS_TEXT = {
  online: C.online,   // 5.0:1 — usable unchanged
  warning: C.warning, // 5.6:1 — usable unchanged
  error: "#D05F5F",   // Text tone derived from C.error
  info: "#5A8CE0",    // Text tone derived from C.info
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

// ── P2 „PHOSPHOR+ CYAN" (feat/ui-redesign-v3) — B2 token set ─────────────────
// New components (shell first, then page-by-page per 00-redesign-brief) use P2
// exclusively. C stays the single source for unconverted pages during the
// transition. Mirrors --color-p2-* in styles/globals.css (keep both in sync).
// Accent: argyelan-Cyan #00E5FF (Marks Colorway-Wahl 03, 23.07.2026).
export const P2 = {
  // Surfaces — warm off-blacks (bewusst wärmer als die alte blau-getönte Welt)
  bg: "#080705", // canvas
  pan: "#100E08", // raised panel
  pan2: "#17140C", // hover / higher elevation
  inset: "#0B0906", // sunken (inputs, meters)

  // Lines
  line: "#2A2517", // panel border
  line2: "#1E1B10", // hairline / dashed separators

  // Text — warm phosphor whites, ≥4.5:1 on bg/pan
  txt: "#E9E0C8",
  dim: "#847C68",
  faint: "#5A5442", // decoration only — never body text

  // ONE accent — argyelan Cyan; interaction/focus/selection only
  amb: "#00E5FF",
  ambD: "#0E6E7A", // dimmed accent (borders, gradient start)
  inv: "#141008", // text on accent surfaces (reverse video)

  // Status trio — distinct from accent, contextual glow allowed on dots
  ok: "#4FD67E",
  wrn: "#FFD84D",
  err: "#FF5C47",
} as const;

// ── Terminal (xterm.js) theme — „Der Leitstand" ANSI set ────────────────────
// Shared by the Sessions page, Agent CLI tab and Plugins shell. ANSI colors
// stay distinguishable (terminal content fidelity) but desaturated to match
// the Leitstand palette — and magenta is magenta, not the banned AI-violet.
export const XTERM_THEME = {
  background: "#070B12",
  foreground: "#E5EAF2",
  cursor: C.accent,
  cursorAccent: "#070B12",
  black: "#101827",
  brightBlack: "#566178",
  red: "#D05F5F",
  brightRed: "#E08080",
  green: "#3FA96C",
  brightGreen: "#5FC98C",
  yellow: "#C9A227",
  brightYellow: "#E0BE55",
  blue: "#5A8CE0",
  brightBlue: "#88AEE8",
  magenta: "#C06BB0",
  brightMagenta: "#D68CC8",
  cyan: "#00D5EE",
  brightCyan: "#6BEAFF",
  white: "#E5EAF2",
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

export function getGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 6) return "Good night";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
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
