/**
 * graphConfig.ts — Obsidian-style 2D Vault Graph visual constants
 *
 * Single source of truth for node/edge styling used by MemoryGraph2D.
 * Color tokens: import { C } from "@/lib/colors" — no purple, no inline hex.
 */

import type { VaultNoteType } from "@/lib/types";
import { C } from "@/lib/colors";

// ── Accent alias — single source for selected/hover/highlight states ──────────
// (Was purple #7C3AED; replaced with teal per Lila-Null-Regel)

export const GRAPH_SELECTED = C.accent; // teal #0FA3A3

// ── Memory layer color schema — consistent tokens, defined once ───────────────
// Consumers (MemoryLayerTabs, MemoryQueryBar, etc.) import from here.

export const LAYER_COLORS = {
  semantic:   C.info,      // #2E6FD8 — knowledge/reference layer
  episodic:   C.online,    // #2B9A4A — journal/timeline layer
  agent:      C.error,     // #C23838 — agent lessons/actions layer
  topics:     C.warning,   // #B8870A — topic clusters layer
} as const;

// ── Node type colors ──────────────────────────────────────────────────────────

export const TYPE_COLORS: Record<VaultNoteType, string> = {
  lesson:       C.online,    // #2B9A4A — green
  knowledge:    C.info,      // #2E6FD8 — blue
  reference:    C.accent,    // #0FA3A3 — teal (was violet)
  journal:      C.warning,   // #B8870A — warm amber
  weekly_review:C.error,     // #C23838 — red
  note:         C.textMuted, // #888888 — grey (default / untyped)
  deliverable:  C.accent,    // #0FA3A3 — teal (file attachment wrapper)
};

// ── Human-readable type labels (German) ──────────────────────────────────────

export const TYPE_LABELS_DE: Record<VaultNoteType, string> = {
  lesson:       "Lesson",
  knowledge:    "Wissen",
  reference:    "Referenz",
  journal:      "Journal",
  weekly_review:"Weekly Review",
  note:         "Notiz",
  deliverable:  "Anhang",
};

// ── Node sizing ───────────────────────────────────────────────────────────────

/**
 * Obsidian-style node sizing by link count.
 *
 * sqrt scaling tuned for our wikilink-density (~4/note avg, hubs at 20-40).
 * Wider 2-18px range than the previous 3-12 to make the hub hierarchy
 * actually readable at overview zoom.
 *
 *   linkCount  0 → 2px   (isolated)
 *   linkCount  1 → 4px   (leaf)
 *   linkCount  4 → 6px
 *   linkCount 12 → 9px
 *   linkCount 30 → 12px  (hub)
 *   linkCount 60 → 16px  (super-hub)
 *   linkCount 100+ → 18px (cap)
 */
export function nodeRadiusFromLinkCount(linkCount: number): number {
  return Math.min(18, 2 + Math.sqrt(linkCount ?? 0) * 2);
}

/** Legacy view-count based sizing — kept as fallback only. */
export function nodeRadiusFromViewCount(vc: number): number {
  return Math.log2((vc ?? 0) + 1) * 1.2 + 6;
}

/** When a node is filtered-out by the user, fade it to this opacity. */
export const NODE_DIMMED_OPACITY = 0.12;

// ── Edge styling ──────────────────────────────────────────────────────────────

/** Default link colour — Obsidian-style subtle hairlines */
export const EDGE_COLOR_DEFAULT = "rgba(255,255,255,0.18)";

/** Brighter link colour when source or target is hovered */
export const EDGE_COLOR_HOVER   = "rgba(255,255,255,0.55)";

/** Link line width in world units */
export const EDGE_WIDTH = 0.5;

// ── Camera ────────────────────────────────────────────────────────────────────

export const ZOOM_DURATION_MS    = 1200;   // spring to cluster centroid
export const RESET_DURATION_MS   = 800;    // zoom-to-fit reset
export const ZOOM_TARGET_LEVEL   = 3;      // zoom level when flying to a node cluster
export const ZOOM_FIT_PADDING_PX = 60;     // padding around all nodes on reset

// ── Community color palette (12 distinct hues — no purple) ───────────────────
export const COMMUNITY_PALETTE = [
  C.accent,   "#10B981", "#F59E0B", "#3B82F6", "#EC4899", "#06B6D4",
  "#EF4444",  C.info,    "#22C55E", "#FB923C", "#0EA5E9", "#F472B6",
];

export function colorForCommunity(communityId: number): string {
  return COMMUNITY_PALETTE[communityId % COMMUNITY_PALETTE.length];
}
