"use client";

/**
 * Anchored popovers — a box that hangs on a trigger and is never clipped.
 *
 * The composer's popovers (context box, slash palette, model list, effort
 * menu, effort reason) used to be `absolute bottom-full` children of the chat
 * column. Two ancestors in the sessions page carry `overflow-hidden` (the
 * island and the chat column), so whenever the side panel made the column
 * narrow, a box that reached past its edge was cut off. A higher z-index does
 * not help against clipping — only leaving the clipping ancestor does.
 *
 * So the box renders through a portal at the end of <body> and is placed with
 * `position: fixed` at the trigger's on-screen rectangle: above by default,
 * below when there is no room above, and shifted sideways so it never leaves
 * the viewport (the same rule OverflowMenu and BoardPicker follow inline).
 */
import { useCallback, useLayoutEffect, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

export type PopoverSide = "top" | "bottom";
export type PopoverAlign = "start" | "end";

export interface AnchorRect {
  top: number;
  left: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

export interface PlaceInput {
  anchor: AnchorRect;
  size: { width: number; height: number };
  viewport: { width: number; height: number };
  /** Preferred side; flips when the box does not fit there. Default "top". */
  side?: PopoverSide;
  /** "start" = left edges line up, "end" = right edges line up. */
  align?: PopoverAlign;
  /** Gap between trigger and box. */
  offset?: number;
  /** Minimum distance to every viewport edge. */
  margin?: number;
}

export interface Placement {
  top: number;
  left: number;
  side: PopoverSide;
  /** Room on the chosen side — the box scrolls inside instead of overflowing. */
  maxHeight: number;
  maxWidth: number;
}

export const POPOVER_OFFSET = 8;
export const POPOVER_MARGIN = 8;

export function placeAnchored({
  anchor,
  size,
  viewport,
  side = "top",
  align = "start",
  offset = POPOVER_OFFSET,
  margin = POPOVER_MARGIN,
}: PlaceInput): Placement {
  const above = Math.max(0, anchor.top - offset - margin);
  const below = Math.max(0, viewport.height - anchor.bottom - offset - margin);
  const preferred = side === "top" ? above : below;
  const other = side === "top" ? below : above;
  const otherSide: PopoverSide = side === "top" ? "bottom" : "top";

  let chosen: PopoverSide;
  if (size.height <= preferred) chosen = side;
  else if (size.height <= other) chosen = otherSide;
  else chosen = preferred >= other ? side : otherSide;

  const room = chosen === "top" ? above : below;
  const height = Math.min(size.height, room);
  const top = chosen === "top" ? anchor.top - offset - height : anchor.bottom + offset;

  const maxWidth = Math.max(0, viewport.width - 2 * margin);
  const width = Math.min(size.width, maxWidth);
  const wanted = align === "start" ? anchor.left : anchor.right - width;
  const left = Math.min(Math.max(margin, wanted), viewport.width - width - margin);

  return { top, left, side: chosen, maxHeight: room, maxWidth };
}

interface UseAnchoredOptions {
  side?: PopoverSide;
  align?: PopoverAlign;
  offset?: number;
  /** Make the box exactly as wide as the anchor (slash palette = input width). */
  matchWidth?: boolean;
  /** Lower bound for `matchWidth` (the palette's 320 px floor). */
  minWidth?: number;
}

export interface AnchoredStyle {
  position: "fixed";
  top: number;
  left: number;
  width?: number;
  maxHeight: number;
  maxWidth: number;
}

/**
 * Measures trigger and box after every render while `open`, and again on
 * resize and on any scroll (capture), so the box stays on its trigger while
 * the timeline scrolls underneath. Returns null until the first measurement;
 * callers render the box with `visibility: hidden` until then.
 */
export function useAnchoredPosition(
  open: boolean,
  anchorRef: RefObject<HTMLElement | null>,
  boxRef: RefObject<HTMLElement | null>,
  { side, align, offset, matchWidth, minWidth = 0 }: UseAnchoredOptions = {},
): AnchoredStyle | null {
  const [style, setStyle] = useState<AnchoredStyle | null>(null);

  const measure = useCallback(() => {
    const anchorEl = anchorRef.current;
    const box = boxRef.current;
    if (!anchorEl || !box) return;
    const a = anchorEl.getBoundingClientRect();
    const width = matchWidth ? Math.max(a.width, minWidth) : box.offsetWidth;
    const p = placeAnchored({
      anchor: a,
      size: { width, height: box.offsetHeight },
      viewport: { width: window.innerWidth, height: window.innerHeight },
      side,
      align,
      offset,
    });
    const next: AnchoredStyle = {
      position: "fixed",
      top: Math.round(p.top),
      left: Math.round(p.left),
      maxHeight: Math.round(p.maxHeight),
      maxWidth: Math.round(p.maxWidth),
      ...(matchWidth ? { width: Math.round(Math.min(width, p.maxWidth)) } : {}),
    };
    setStyle((prev) =>
      prev &&
      prev.top === next.top &&
      prev.left === next.left &&
      prev.width === next.width &&
      prev.maxHeight === next.maxHeight &&
      prev.maxWidth === next.maxWidth
        ? prev
        : next,
    );
  }, [anchorRef, boxRef, side, align, offset, matchWidth, minWidth]);

  // Every render while open: the box's own height changes with its content
  // (palette filters as you type), and the trigger moves with the composer.
  useLayoutEffect(() => {
    if (!open) {
      setStyle(null);
      return;
    }
    measure();
  });

  useLayoutEffect(() => {
    if (!open) return;
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [open, measure]);

  return style;
}

/** Portal to the end of <body>; nothing on the server. */
export function PopoverPortal({ children }: { children: ReactNode }) {
  if (typeof document === "undefined") return null;
  return createPortal(children, document.body);
}

/**
 * Inline style for a box placed by `useAnchoredPosition`. Always
 * `position: fixed` (also before the first measurement, so the box never
 * flashes in the document flow), hidden until measured.
 */
export function anchoredBoxStyle(style: AnchoredStyle | null): CSSProperties {
  if (!style) return { position: "fixed", top: 0, left: 0, visibility: "hidden" };
  return style;
}
