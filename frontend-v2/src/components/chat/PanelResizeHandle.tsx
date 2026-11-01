"use client";

/**
 * The drag handle on the side panel's left edge (ADR-099, operator decision
 * 01.11.2026: "the panel should be resizable in the browser").
 *
 * W3C APG "window splitter": a focusable `role="separator"` that states its
 * value and limits. Mouse/pen/touch drag it; arrow keys move it 16 px (left =
 * wider, because the panel sits on the right); Home/End jump to the limits;
 * double-click resets to the default width.
 *
 * The 1 px line it draws IS the panel's left border (the panel has none of
 * its own); the hit area is 8 px wide and lies inside the panel, so the
 * panel's `overflow-hidden` cannot clip it. Pointer capture keeps the drag
 * alive when the pointer crosses the browser panel's picture.
 */
import { useRef, type KeyboardEvent, type PointerEvent } from "react";
import { useTranslations } from "next-intl";
import { PANEL_MIN, PANEL_STEP, clampPanelWidth, panelMaxWidth } from "@/lib/panelWidth";

interface PanelResizeHandleProps {
  /** Current panel width in px. */
  width: number;
  /** Width of the island holding chat + panel (0 = not measured yet). */
  islandWidth: number;
  /** Live width while dragging / stepping. */
  onResize: (width: number) => void;
  /** Width to remember: on release and on every key step. */
  onCommit: (width: number) => void;
  /** Double-click: back to the default width. */
  onReset: () => void;
}

export function PanelResizeHandle({ width, islandWidth, onResize, onCommit, onReset }: PanelResizeHandleProps) {
  const t = useTranslations("sessions");
  const drag = useRef<{ startX: number; startWidth: number; last: number } | null>(null);
  const max = panelMaxWidth(islandWidth);

  const clamp = (w: number) => clampPanelWidth(w, islandWidth);

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    e.preventDefault();
    drag.current = { startX: e.clientX, startWidth: width, last: width };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      // jsdom and old engines: the drag still works while the pointer stays on it.
    }
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    const d = drag.current;
    if (!d) return;
    const next = clamp(d.startWidth + (d.startX - e.clientX));
    if (next === d.last) return;
    d.last = next;
    onResize(next);
  }

  function endDrag(e: PointerEvent<HTMLDivElement>) {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      // nothing to release
    }
    if (d.last !== d.startWidth) onCommit(d.last);
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    let next: number | null = null;
    if (e.key === "ArrowLeft") next = clamp(width + PANEL_STEP);
    else if (e.key === "ArrowRight") next = clamp(width - PANEL_STEP);
    else if (e.key === "Home") next = PANEL_MIN;
    else if (e.key === "End") next = clamp(max);
    if (next == null) return;
    e.preventDefault();
    onResize(next);
    onCommit(next);
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t("panels.resize")}
      title={t("panels.resizeHint")}
      aria-valuenow={Math.round(width)}
      aria-valuemin={PANEL_MIN}
      aria-valuemax={Number.isFinite(max) ? Math.round(max) : undefined}
      tabIndex={0}
      data-testid="panel-resize-handle"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={onReset}
      // `touch-none`: a touch drag must move the handle, not scroll the page.
      className="group hidden md:block absolute inset-y-0 left-0 z-10 w-2 cursor-col-resize touch-none focus-visible:outline-none"
    >
      {/* The visible line: hairline at rest, accent while hovered, dragged
          or focused (focus needs its own sign — the global focus ring would
          sit outside the panel and be clipped). */}
      <span
        aria-hidden="true"
        // Classes only, no inline background: an inline style would beat
        // the hover/focus classes (cascade note in globals.css).
        className="absolute inset-y-0 left-0 w-px transition-colors bg-[var(--color-border)] group-hover:bg-[var(--color-border-accent)] group-focus-visible:bg-[var(--color-accent)] group-active:bg-[var(--color-accent)]"
      />
    </div>
  );
}
