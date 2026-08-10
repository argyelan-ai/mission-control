/**
 * MemoryGraph2D — Task #13 (memory graph iOS-PWA render bug), sizing only.
 *
 * Root cause (verified by reading node_modules/force-graph/src/force-graph.js):
 * react-force-graph-2d defaults its canvas width/height to
 * window.innerWidth/window.innerHeight when the consumer doesn't pass
 * explicit width/height props, and NEVER re-measures on resize. iOS PWA
 * standalone mode reports window.innerHeight ~793px on a device whose real
 * viewport is 852px (ios-pwa-lvh-viewport memory finding) — so the canvas
 * froze at the wrong size forever.
 *
 * This test mocks react-force-graph-2d itself and asserts MemoryGraph2D
 * feeds it width/height derived from ITS OWN CONTAINER (via
 * useContainerSize), never from window.innerWidth/innerHeight — the actual
 * integration point of the fix, not just the extracted hook in isolation.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import type { VaultGraphResponse } from "@/lib/types";

// ── Mock react-force-graph-2d — capture the width/height props it receives ──
const forceGraphProps: { width?: number; height?: number }[] = [];

vi.mock("react-force-graph-2d", () => ({
  default: (props: { width?: number; height?: number }) => {
    forceGraphProps.push({ width: props.width, height: props.height });
    return (
      <div data-testid="force-graph-stub">
        {props.width}x{props.height}
      </div>
    );
  },
}));

import { MemoryGraph2D } from "../MemoryGraph2D";

type ROCallback = (entries: ResizeObserverEntry[], observer: ResizeObserver) => void;

class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  callback: ROCallback;
  constructor(callback: ROCallback) {
    this.callback = callback;
    MockResizeObserver.instances.push(this);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  trigger(rect: { width: number; height: number }) {
    this.callback([{ contentRect: rect } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
}

function mockRect(width: number, height: number) {
  return vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width,
    height,
    top: 0,
    left: 0,
    right: width,
    bottom: height,
    x: 0,
    y: 0,
    toJSON() {
      return {};
    },
  });
}

const emptyData: VaultGraphResponse = {
  nodes: [
    { id: "a", label: "a", type: "note", agent: "boss", tags: [], viewCount: 0, cluster_id: null },
    { id: "b", label: "b", type: "note", agent: "boss", tags: [], viewCount: 0, cluster_id: null },
  ],
  edges: [{ source: "a", target: "b", weight: 1 }],
  clusters: [],
  built_at: "2026-08-08T00:00:00Z",
  stats: { nodes: 2, edges: 1, clusters: 0, build_ms: 1 },
};

describe("MemoryGraph2D sizing", () => {
  let originalRO: typeof ResizeObserver;

  beforeEach(() => {
    forceGraphProps.length = 0;
    MockResizeObserver.instances = [];
    originalRO = globalThis.ResizeObserver;
    globalThis.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
    // iOS-PWA-standalone sentinel: window reports the real measured-wrong
    // number (793) from the memory finding.
    Object.defineProperty(window, "innerWidth", { value: 793, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 793, configurable: true });
  });

  afterEach(() => {
    cleanup();
    globalThis.ResizeObserver = originalRO;
    vi.restoreAllMocks();
  });

  it("does not mount ForceGraph2D before the container has been measured", () => {
    // No getBoundingClientRect stub → jsdom's default 0x0 — must not fall
    // through to window.innerWidth/innerHeight in the meantime.
    render(<MemoryGraph2D data={emptyData} onNodeClick={() => {}} />);
    expect(screen.queryByTestId("force-graph-stub")).toBeNull();
  });

  it("passes the CONTAINER's size to ForceGraph2D, not window.innerWidth/innerHeight", () => {
    const rectSpy = mockRect(375, 852); // the container's true (correct) size
    render(<MemoryGraph2D data={emptyData} onNodeClick={() => {}} />);

    expect(screen.getByTestId("force-graph-stub").textContent).toBe("375x852");
    expect(forceGraphProps.at(-1)).toEqual({ width: 375, height: 852 });
    rectSpy.mockRestore();
  });

  it("re-sizes ForceGraph2D when the container is resized (rotation, tab switch)", () => {
    const rectSpy = mockRect(375, 852);
    render(<MemoryGraph2D data={emptyData} onNodeClick={() => {}} />);
    expect(screen.getByTestId("force-graph-stub").textContent).toBe("375x852");

    const observer = MockResizeObserver.instances[0];
    act(() => {
      observer.trigger({ width: 852, height: 375 }); // rotated to landscape
    });

    expect(screen.getByTestId("force-graph-stub").textContent).toBe("852x375");
    rectSpy.mockRestore();
  });
});
