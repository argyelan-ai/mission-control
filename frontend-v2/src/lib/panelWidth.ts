/**
 * Width of the side panel next to the chat (changes, browser, group result
 * document) — ADR-099, operator decision 01.11.2026.
 *
 * The panel used to be a fixed `45 %` (at most 720 px). It now has a drag
 * handle; these are its limits and its memory. All widths are pixels of the
 * chat island (the box holding chat + panel), not of the viewport.
 */

/** Below this the panel's own content (diff lines, the browser picture) breaks. */
export const PANEL_MIN = 320;
/** The panel never takes more than this share of the island. */
export const PANEL_MAX_RATIO = 0.7;
/** The chat keeps at least this much: the composer's popovers are up to
 *  320 px wide plus their margins. */
export const CHAT_MIN = 400;
/** First width and the double-click reset. */
export const PANEL_DEFAULT_RATIO = 0.45;
/** One arrow-key step. */
export const PANEL_STEP = 16;

/** One width per browser, like the open panel itself (`mc.chat.panel`). */
export const PANEL_WIDTH_STORAGE_KEY = "mc.chat.panelWidth";

/** Largest allowed panel width for an island of this width. */
export function panelMaxWidth(islandWidth: number): number {
  if (islandWidth <= 0) return Number.POSITIVE_INFINITY;
  return Math.max(PANEL_MIN, Math.min(islandWidth * PANEL_MAX_RATIO, islandWidth - CHAT_MIN));
}

/**
 * Keeps a wanted width inside the limits. `islandWidth` 0 = not measured yet
 * (first render, tests): only the floor applies, the real cap follows with
 * the first measurement. A width remembered on a wide screen is capped the
 * same way on a smaller one — the chat never shrinks to nothing.
 */
export function clampPanelWidth(width: number, islandWidth: number): number {
  return Math.round(Math.max(PANEL_MIN, Math.min(width, panelMaxWidth(islandWidth))));
}

export function defaultPanelWidth(islandWidth: number): number {
  return clampPanelWidth(islandWidth * PANEL_DEFAULT_RATIO, islandWidth);
}

/**
 * Chat and panel do not fit side by side → the panel lies over the chat.
 * With today's shell that happens below roughly 1100 px of viewport.
 */
export function panelOverlays(islandWidth: number): boolean {
  return islandWidth > 0 && islandWidth < CHAT_MIN + PANEL_MIN;
}

export function loadPanelWidth(): number | null {
  try {
    const raw = localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
    if (raw == null) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

/** `null` forgets the width (double-click reset). */
export function savePanelWidth(width: number | null): void {
  try {
    if (width == null) localStorage.removeItem(PANEL_WIDTH_STORAGE_KEY);
    else localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(Math.round(width)));
  } catch {
    // Blocked storage (private mode): the width simply is not remembered.
  }
}
