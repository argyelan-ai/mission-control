/**
 * Side panel width (ADR-099): drag handle limits, the remembered width and
 * the overlay rule. Pure functions — jsdom has no layout, these numbers are
 * what keeps the chat usable.
 */
import { describe, it, expect, beforeEach } from "vitest";
import {
  CHAT_MIN,
  PANEL_MAX_RATIO,
  PANEL_MIN,
  PANEL_WIDTH_STORAGE_KEY,
  clampPanelWidth,
  defaultPanelWidth,
  loadPanelWidth,
  panelOverlays,
  savePanelWidth,
} from "../panelWidth";

describe("clampPanelWidth", () => {
  it("never goes below 320 px", () => {
    expect(PANEL_MIN).toBe(320);
    expect(clampPanelWidth(100, 1400)).toBe(320);
  });

  it("never takes more than 70 % of the island", () => {
    expect(PANEL_MAX_RATIO).toBe(0.7);
    expect(clampPanelWidth(5000, 2000)).toBe(1400);
  });

  it("always leaves the chat at least 400 px", () => {
    expect(CHAT_MIN).toBe(400);
    // 70 % of 1000 would be 700 — that left the chat 300 px.
    expect(clampPanelWidth(900, 1000)).toBe(600);
  });

  it("caps a width remembered on a wide screen when the screen is smaller (2560 → 1280)", () => {
    const remembered = 1600;
    const island = 1000;
    const w = clampPanelWidth(remembered, island);
    expect(island - w).toBeGreaterThanOrEqual(CHAT_MIN);
  });

  it("keeps the minimum when the island is unknown (0) instead of collapsing", () => {
    expect(clampPanelWidth(500, 0)).toBe(500);
    expect(clampPanelWidth(200, 0)).toBe(320);
  });

  it("rounds to whole pixels", () => {
    expect(clampPanelWidth(456.7, 1400)).toBe(457);
  });
});

describe("defaultPanelWidth", () => {
  it("is 45 % of the island, inside the limits", () => {
    expect(defaultPanelWidth(1000)).toBe(450);
    expect(defaultPanelWidth(600)).toBe(320);
  });
});

describe("panelOverlays", () => {
  it("lies over the chat when chat (400) and panel (320) do not fit side by side", () => {
    expect(panelOverlays(719)).toBe(true);
    expect(panelOverlays(720)).toBe(false);
    // Unknown width (first render, jsdom): side by side, as before.
    expect(panelOverlays(0)).toBe(false);
  });
});

describe("loadPanelWidth / savePanelWidth", () => {
  let store: Record<string, string>;
  beforeEach(() => {
    store = {};
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => {
          store[k] = v;
        },
        removeItem: (k: string) => {
          delete store[k];
        },
      },
    });
  });

  it("remembers a width per browser", () => {
    savePanelWidth(512);
    expect(store[PANEL_WIDTH_STORAGE_KEY]).toBe("512");
    expect(loadPanelWidth()).toBe(512);
  });

  it("forgets the width on reset (null)", () => {
    savePanelWidth(512);
    savePanelWidth(null);
    expect(loadPanelWidth()).toBeNull();
  });

  it("ignores garbage", () => {
    store[PANEL_WIDTH_STORAGE_KEY] = "wide";
    expect(loadPanelWidth()).toBeNull();
    store[PANEL_WIDTH_STORAGE_KEY] = "-40";
    expect(loadPanelWidth()).toBeNull();
  });

  it("survives a blocked storage", () => {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
        removeItem: () => {
          throw new Error("blocked");
        },
      },
    });
    expect(loadPanelWidth()).toBeNull();
    expect(() => savePanelWidth(400)).not.toThrow();
  });
});
