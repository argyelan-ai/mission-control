/**
 * Round-4 review r2 N1: on the desktop the memory graph filled the canvas at
 * 0.8 s and sat as a small knot in ~10 % of it five seconds later.
 *
 * Two causes, both read in the code:
 *   1. The layout forces (charge, short links, pull to the middle) were set
 *      in an effect that ran while the canvas was not mounted yet (it waits
 *      for the container size) and never ran again — the graph used the
 *      library defaults, loose nodes drifted far out.
 *   2. The camera was framed once, by a 1.5 s fallback while the simulation
 *      still moved; when the layout settled, the "once" guard kept the
 *      camera on the old, much larger frame.
 * Now the forces are set as soon as the canvas exists, and the camera frames
 * again when the layout settles — until the operator pans, zooms or drags.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, cleanup, fireEvent, screen } from "@testing-library/react";
import type { VaultGraphResponse } from "@/lib/types";

const fg = vi.hoisted(() => ({
  props: {} as { onEngineStop?: () => void },
  d3Force: vi.fn(),
  zoomToFit: vi.fn(),
  centerAt: vi.fn(),
}));

vi.mock("react-force-graph-2d", async () => {
  const React = await import("react");
  const Stub = React.forwardRef((props: { onEngineStop?: () => void }, ref) => {
    fg.props = props;
    React.useImperativeHandle(ref, () => ({
      d3Force: fg.d3Force,
      d3ReheatSimulation: vi.fn(),
      zoomToFit: fg.zoomToFit,
      centerAt: fg.centerAt,
      graph2ScreenCoords: () => ({ x: 0, y: 0 }),
    }));
    return <div data-testid="force-graph-stub" />;
  });
  Stub.displayName = "ForceGraph2DStub";
  return { default: Stub };
});

import { MemoryGraph2D } from "../MemoryGraph2D";

const data: VaultGraphResponse = {
  nodes: [
    { id: "a", label: "a", type: "note", agent: "boss", tags: [], viewCount: 0, cluster_id: null },
    { id: "b", label: "b", type: "note", agent: "boss", tags: [], viewCount: 0, cluster_id: null },
  ],
  edges: [{ source: "a", target: "b", weight: 1 }],
  clusters: [],
  built_at: "2026-10-23T00:00:00Z",
  stats: { nodes: 2, edges: 1, clusters: 0, build_ms: 1 },
};

class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.useFakeTimers();
  globalThis.ResizeObserver = RO as unknown as typeof ResizeObserver;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 960, height: 600, top: 0, left: 0, right: 960, bottom: 600, x: 0, y: 0, toJSON: () => ({}),
  });
  fg.d3Force.mockReset().mockImplementation(() => ({ strength: vi.fn(), distance: vi.fn() }));
  fg.zoomToFit.mockReset();
  fg.centerAt.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function mount() {
  render(<MemoryGraph2D data={data} onNodeClick={() => {}} />);
  expect(screen.getByTestId("force-graph-stub")).toBeInTheDocument();
}

describe("MemoryGraph2D — layout and framing", () => {
  it("sets its layout forces once the canvas exists", () => {
    mount();
    const names = fg.d3Force.mock.calls.map((c) => c[0]);
    expect(names).toContain("charge");
    expect(names).toContain("x");
    expect(names).toContain("y");
  });

  it("frames again when the layout settles after the early fallback", () => {
    mount();
    act(() => { vi.advanceTimersByTime(1500); });
    expect(fg.zoomToFit).toHaveBeenCalledTimes(1);
    act(() => { fg.props.onEngineStop?.(); });
    expect(fg.zoomToFit).toHaveBeenCalledTimes(2);
  });

  it("leaves the camera alone once the operator has moved it", () => {
    mount();
    act(() => { vi.advanceTimersByTime(1500); });
    expect(fg.zoomToFit).toHaveBeenCalledTimes(1);
    fireEvent.wheel(screen.getByTestId("force-graph-stub"));
    act(() => { fg.props.onEngineStop?.(); });
    act(() => { vi.advanceTimersByTime(4000); });
    expect(fg.zoomToFit).toHaveBeenCalledTimes(1);
  });
});
