/**
 * FlowEdge — das Lauflicht respektiert `prefers-reduced-motion` (kein rAF,
 * Spec §2 Zone 0) und startet nur, wenn sichtbar + gross genug
 * (IntersectionObserver + ResizeObserver). jsdom hat beide APIs nicht — die
 * Sabotage-Probe hier ist echt: ohne den `reduced`-Guard würde der zweite
 * Test (reduced:false) auch beim ersten (reduced:true) grün animieren.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { FlowEdge } from "../FlowEdge";

class StubObserver {
  static instances: StubObserver[] = [];
  cb: (entries: unknown[]) => void;
  constructor(cb: (entries: unknown[]) => void) {
    this.cb = cb;
    StubObserver.instances.push(this);
  }
  observe(el: Element) {
    // Serves both observers: IntersectionObserver shape + ResizeObserver
    // shape (FlowEdge reads the layout size from `contentRect`, never
    // from getBoundingClientRect — see FlowEdge.geometry.test.tsx).
    this.cb([
      {
        target: el,
        isIntersecting: true,
        contentRect: { width: 300, height: 100, x: 0, y: 0, top: 0, left: 0, right: 300, bottom: 100 },
      },
    ]);
  }
  unobserve() {}
  disconnect() {}
}

function mockMatchMedia(reduced: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reduced && query.includes("prefers-reduced-motion"),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

function mockObservers() {
  (global as unknown as { IntersectionObserver: unknown }).IntersectionObserver = StubObserver;
  (global as unknown as { ResizeObserver: unknown }).ResizeObserver = StubObserver;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  delete (global as any).IntersectionObserver;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  delete (global as any).ResizeObserver;
});

describe("FlowEdge", () => {
  it("prefers-reduced-motion: reduce → never animates (no rAF loop)", async () => {
    mockMatchMedia(true);
    mockObservers();
    Element.prototype.getBoundingClientRect = () =>
      ({ width: 300, height: 100, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON() {} }) as DOMRect;

    render(
      <div style={{ position: "relative" }}>
        <FlowEdge kind="live" />
      </div>
    );
    const svg = screen.getByTestId("flow-edge");
    // Give the ResizeObserver stub + effects a tick to settle.
    await waitFor(() => expect(svg.getAttribute("data-animating")).toBe("false"));
  });

  it("no reduced-motion + visible + sized → animates", async () => {
    mockMatchMedia(false);
    mockObservers();
    Element.prototype.getBoundingClientRect = () =>
      ({ width: 300, height: 100, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON() {} }) as DOMRect;

    render(
      <div style={{ position: "relative" }}>
        <FlowEdge kind="live" />
      </div>
    );
    const svg = screen.getByTestId("flow-edge");
    await waitFor(() => expect(svg.getAttribute("data-animating")).toBe("true"));
  });

  it("kind=dead never animates (duration 0 — a still, dashed edge)", async () => {
    mockMatchMedia(false);
    mockObservers();
    Element.prototype.getBoundingClientRect = () =>
      ({ width: 300, height: 100, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON() {} }) as DOMRect;

    render(
      <div style={{ position: "relative" }}>
        <FlowEdge kind="dead" />
      </div>
    );
    const svg = screen.getByTestId("flow-edge");
    await waitFor(() => expect(svg.getAttribute("data-animating")).toBe("false"));
  });
});
