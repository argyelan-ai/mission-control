/**
 * The arithmetic behind the full-screen image viewer's touch gestures —
 * pure functions, so the thresholds are tested without a touch screen.
 *
 * One finger at zoom 1: a mostly vertical drag downwards closes (photo-app
 * convention), a mostly horizontal one pages. Zoomed in, one finger pans and
 * nothing closes — otherwise reading a detail of a screenshot would throw
 * the viewer away. Two fingers pinch.
 */

export const MIN_SCALE = 1;
export const MAX_SCALE = 5;
/** Drag this far down (css px) and the viewer closes on release. */
export const CLOSE_DISTANCE = 110;
/** Drag this far sideways and it pages. */
export const PAGE_DISTANCE = 60;
/** Two taps within this many ms are a double tap. */
export const DOUBLE_TAP_MS = 300;
export const DOUBLE_TAP_SCALE = 2.5;

export function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return MIN_SCALE;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

export function distance(a: { x: number; y: number }, b: { x: number; y: number }): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

/** New zoom after a pinch moved from `startDist` to `dist` apart. */
export function pinchScale(startScale: number, startDist: number, dist: number): number {
  if (startDist <= 0) return clampScale(startScale);
  return clampScale((startScale * dist) / startDist);
}

/** How far the picture may be moved at `scale` without leaving a gap
 *  bigger than half the stage — keeps it from being flung out of sight. */
export function clampPan(
  pan: { x: number; y: number },
  scale: number,
  stage: { width: number; height: number },
): { x: number; y: number } {
  const maxX = (stage.width * (scale - 1)) / 2 + stage.width / 4;
  const maxY = (stage.height * (scale - 1)) / 2 + stage.height / 4;
  if (scale <= MIN_SCALE) return { x: 0, y: 0 };
  return {
    x: Math.max(-maxX, Math.min(maxX, pan.x)),
    y: Math.max(-maxY, Math.min(maxY, pan.y)),
  };
}

export type SwipeOutcome = "close" | "next" | "prev" | null;

/** What a one-finger drag of (dx, dy) does on release. */
export function swipeOutcome(dx: number, dy: number, scale: number): SwipeOutcome {
  if (scale > MIN_SCALE) return null;
  if (Math.abs(dy) >= Math.abs(dx)) {
    return dy >= CLOSE_DISTANCE ? "close" : null;
  }
  if (dx <= -PAGE_DISTANCE) return "next";
  if (dx >= PAGE_DISTANCE) return "prev";
  return null;
}

/** Backdrop opacity while dragging down to close: fades with the distance. */
export function dragFade(dy: number): number {
  if (dy <= 0) return 1;
  return Math.max(0.35, 1 - dy / (CLOSE_DISTANCE * 3));
}
