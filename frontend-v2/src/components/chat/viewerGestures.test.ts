import { describe, it, expect } from "vitest";
import {
  CLOSE_DISTANCE,
  MAX_SCALE,
  PAGE_DISTANCE,
  clampPan,
  clampScale,
  dragFade,
  pinchScale,
  swipeOutcome,
} from "./viewerGestures";

describe("viewer gestures", () => {
  it("a pinch scales with the finger distance, clamped to 1…5", () => {
    expect(pinchScale(1, 100, 200)).toBe(2);
    expect(pinchScale(2, 100, 50)).toBe(1);
    expect(pinchScale(1, 100, 10_000)).toBe(MAX_SCALE);
    expect(pinchScale(1, 0, 50)).toBe(1);
    expect(clampScale(Number.NaN)).toBe(1);
  });

  it("dragging down far enough closes, a short drag does not", () => {
    expect(swipeOutcome(5, CLOSE_DISTANCE + 1, 1)).toBe("close");
    expect(swipeOutcome(0, CLOSE_DISTANCE - 20, 1)).toBeNull();
    // upwards never closes
    expect(swipeOutcome(0, -CLOSE_DISTANCE * 2, 1)).toBeNull();
  });

  it("a sideways swipe pages", () => {
    expect(swipeOutcome(-PAGE_DISTANCE - 1, 10, 1)).toBe("next");
    expect(swipeOutcome(PAGE_DISTANCE + 1, 10, 1)).toBe("prev");
    expect(swipeOutcome(PAGE_DISTANCE - 10, 0, 1)).toBeNull();
  });

  it("zoomed in, nothing closes or pages — one finger pans", () => {
    expect(swipeOutcome(0, CLOSE_DISTANCE * 3, 2)).toBeNull();
    expect(swipeOutcome(-PAGE_DISTANCE * 3, 0, 2)).toBeNull();
  });

  it("pan is clamped so the picture stays in reach, and resets at zoom 1", () => {
    const stage = { width: 400, height: 800 };
    expect(clampPan({ x: 9999, y: -9999 }, 2, stage)).toEqual({ x: 300, y: -600 });
    expect(clampPan({ x: 50, y: 50 }, 1, stage)).toEqual({ x: 0, y: 0 });
  });

  it("the backdrop fades while dragging down, never fully", () => {
    expect(dragFade(0)).toBe(1);
    expect(dragFade(CLOSE_DISTANCE)).toBeLessThan(1);
    expect(dragFade(100_000)).toBe(0.35);
  });
});
