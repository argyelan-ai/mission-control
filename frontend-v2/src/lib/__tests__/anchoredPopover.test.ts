/**
 * placeAnchored — where a popover goes when it hangs on a trigger.
 *
 * The composer's popovers (context box, slash palette, model list, effort
 * menu) used to be `absolute bottom-full` children of a column with
 * `overflow-hidden`, so a narrow chat column (side panel open) cut them off.
 * They now live in a portal with `position: fixed`; this function is the
 * geometry: above by default, below when there is no room above, and shifted
 * sideways so they never leave the viewport.
 */
import { describe, it, expect } from "vitest";
import { placeAnchored } from "../anchoredPopover";

const VIEWPORT = { width: 1440, height: 900 };
const rect = (left: number, top: number, width = 40, height = 24) => ({
  left,
  top,
  width,
  height,
  right: left + width,
  bottom: top + height,
});

describe("placeAnchored", () => {
  it("sits above the trigger, start-aligned, with the offset as gap", () => {
    const p = placeAnchored({
      anchor: rect(400, 800),
      size: { width: 300, height: 200 },
      viewport: VIEWPORT,
      offset: 8,
    });
    expect(p.side).toBe("top");
    expect(p.left).toBe(400);
    expect(p.top).toBe(800 - 8 - 200);
  });

  it("end-aligns to the trigger's right edge", () => {
    const p = placeAnchored({
      anchor: rect(400, 800, 40),
      size: { width: 256, height: 100 },
      viewport: VIEWPORT,
      align: "end",
    });
    expect(p.left).toBe(440 - 256);
  });

  it("shifts left so a box near the right edge stays fully on screen", () => {
    // The bug case: the ring sits close to the chat column's right edge.
    const p = placeAnchored({
      anchor: rect(1300, 800),
      size: { width: 300, height: 200 },
      viewport: VIEWPORT,
      margin: 8,
    });
    expect(p.left).toBe(1440 - 300 - 8);
    expect(p.left + 300).toBeLessThanOrEqual(1440 - 8);
  });

  it("shifts right so a box never starts left of the margin", () => {
    const p = placeAnchored({
      anchor: rect(20, 800, 40),
      size: { width: 256, height: 100 },
      viewport: VIEWPORT,
      align: "end",
      margin: 8,
    });
    expect(p.left).toBe(8);
  });

  it("flips below when there is no room above", () => {
    const p = placeAnchored({
      anchor: rect(400, 60),
      size: { width: 300, height: 200 },
      viewport: VIEWPORT,
      offset: 8,
    });
    expect(p.side).toBe("bottom");
    expect(p.top).toBe(60 + 24 + 8);
  });

  it("takes the roomier side and caps the height when neither side fits", () => {
    const p = placeAnchored({
      anchor: rect(400, 300),
      size: { width: 300, height: 700 },
      viewport: { width: 1440, height: 500 },
      offset: 8,
      margin: 8,
    });
    // above: 300 - 8 - 8 = 284; below: 500 - 324 - 8 - 8 = 160
    expect(p.side).toBe("top");
    expect(p.maxHeight).toBe(284);
    expect(p.top).toBe(8);
  });

  it("is never wider than the viewport allows", () => {
    const p = placeAnchored({
      anchor: rect(10, 700),
      size: { width: 500, height: 100 },
      viewport: { width: 390, height: 844 },
      margin: 8,
    });
    expect(p.left).toBe(8);
    expect(p.maxWidth).toBe(390 - 16);
  });
});
