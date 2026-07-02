"use client";

/**
 * Mission Control — Color Tokens („Der Leitstand", siehe DESIGN.md)
 * Design-Guideline: Serious. Dark. No neon. No purple.
 *
 * Inspirations: Bloomberg Terminal, Linear.app, Stripe Dashboard
 * Principles:
 *   - One accent only (Teal #0FA3A3 — subdued)
 *   - Greys only for structure (no cool/warm tint)
 *   - Status colors muted, never glowing
 *   - No blur, no glass, no shadow-glow
 *
 * App-weite Single Source seit Juni 2026 (vorher components/homepage/colors.ts —
 * dort liegt ein Re-Export für bestehende Imports).
 */

export const C = {
  // Backgrounds
  bgDeep: "#050505",
  bgBase: "#0A0A0A",
  bgSurface: "#111111",
  bgElevated: "#161616",
  bgHover: "#1C1C1C",

  // Text — all body/label tones clear WCAG AA (≥4.5:1) on bg #050505–#161616.
  textPrimary: "#EDEDED",
  textSecondary: "#A1A1A1", // ~7.3:1 (was #8C8C8C)
  textMuted: "#888888",     // ~5.3:1 (was #525252 = 2.4:1, AA fail)
  textDim: "#6E6E6E",       // decoration / inactive icons ONLY — never body text

  // Borders
  borderSubtle: "rgba(255,255,255,0.04)",
  border: "rgba(255,255,255,0.06)",
  borderActive: "rgba(255,255,255,0.10)",
  borderAccent: "rgba(15,163,163,0.30)",

  // ONE accent only — teal, subdued
  accent: "#0FA3A3",
  accentSubtle: "rgba(15,163,163,0.12)",
  accentHover: "#14C4C4",

  // Status (desaturated, never bright)
  online: "#2B9A4A",
  warning: "#B8870A",
  error: "#C23838",
  info: "#2E6FD8",

  chart: {
    cpu: "#0FA3A3",
    ram: "#6B8E8E",
    disk: "#86A0A0", // was #7A6B8E (purple, 3.89:1) — teal-grey, 6.8:1, no purple
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
  blocked: C.error,
  failed: C.error,
  aborted: C.warning,
  done: C.online,
};

// ── Status-Text — AA-sichere Stufen für Fliesstext auf dunklen Flächen ───────
// C.error (3.7:1) und C.info (3.8:1) sind als Border/Fläche/Icon ok, aber zu
// dunkel für Body-Text. Diese Stufen erfüllen ≥4.5:1 auf #050505–#161616.

export const STATUS_TEXT = {
  online: C.online,   // 5.0:1 — unverändert nutzbar
  warning: C.warning, // 5.6:1 — unverändert nutzbar
  error: "#D05F5F",   // Text-Stufe von C.error
  info: "#5A8CE0",    // Text-Stufe von C.info
} as const;

// ── Externe Marken-Farben — einzige erlaubte Nicht-Token-Farben ──────────────
// Plattform-Identitäten (Logos, Social-Badges) bleiben original, werden aber
// hier zentralisiert statt inline gestreut.

export const BRAND: Record<string, string> = {
  linkedin: "#0A66C2",
  // Social / content platforms
  youtube: "#FF0000",
  tiktok: "#000000",
  instagram: "#E1306C",
  x: "#1DA1F2",        // X / Twitter
  telegram: "#26A5E4",
  newsletter: "#FFB224", // internal neutral — amber für Newsletter-Brand
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
  if (hour < 6) return "Gute Nacht";
  if (hour < 12) return "Guten Morgen";
  if (hour < 18) return "Guten Tag";
  return "Guten Abend";
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
