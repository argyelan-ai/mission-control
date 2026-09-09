/**
 * FlowEdge — Geometrie des Lauflichts (Nachlese 09.09.: „komplett buggy").
 *
 * Vier Fehler, die im Browser sichtbar waren:
 *  1. Grösse aus `getBoundingClientRect()` gelesen → während des
 *     framer-motion-Layout-Übergangs (scaleY 0.965) liefert das die optisch
 *     gestauchte Höhe; der ResizeObserver feuert danach nie mehr → Licht lief
 *     dauerhaft 14 px über dem unteren Kartenrand. Layout-Grösse ist
 *     transform-unabhängig (`contentRect` des Observers).
 *  2. Eckradius fix 8 px, Karte hat 20 px → Licht schnitt die Ecken.
 *  3. Umfang als 2·(b+h) gerechnet, der Pfad mit runden Ecken ist kürzer →
 *     Muster-Periode ≠ Pfadlänge → Aussetzer pro Runde.
 *  4. Alle Lagen starteten am selben Punkt → der blasse Schweif lag VOR dem
 *     hellen Kopf; jetzt enden alle Lagen am Kopf (Schweif hinten).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { FlowEdge, layerDashOffsets, roundedRectPerimeter } from "../FlowEdge";

const LAYOUT = { width: 300, height: 100 };

class StubIntersection {
  cb: (entries: unknown[]) => void;
  constructor(cb: (entries: unknown[]) => void) {
    this.cb = cb;
  }
  observe(el: Element) {
    this.cb([{ target: el, isIntersecting: true }]);
  }
  unobserve() {}
  disconnect() {}
}

class StubResize {
  cb: (entries: unknown[]) => void;
  constructor(cb: (entries: unknown[]) => void) {
    this.cb = cb;
  }
  observe(el: Element) {
    // Layout-Grösse, wie sie der echte Observer liefert — unabhängig von Transforms.
    this.cb([{ target: el, contentRect: { ...LAYOUT, x: 0, y: 0, top: 0, left: 0 } }]);
  }
  unobserve() {}
  disconnect() {}
}

function setup() {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
  (global as unknown as { IntersectionObserver: unknown }).IntersectionObserver = StubIntersection;
  (global as unknown as { ResizeObserver: unknown }).ResizeObserver = StubResize;
  // Sabotage: die optisch gestauchte Grösse (scaleY 0.965 mitten im Layout-Übergang).
  Element.prototype.getBoundingClientRect = () =>
    ({ width: 300, height: 96.5, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON() {} }) as DOMRect;
}

afterEach(() => {
  vi.restoreAllMocks();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  delete (global as any).IntersectionObserver;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  delete (global as any).ResizeObserver;
});

describe("FlowEdge geometry", () => {
  it("uses the layout size from the ResizeObserver, not the transform-scaled bounding rect", async () => {
    setup();
    render(
      <div style={{ position: "relative", borderRadius: "20px" }}>
        <FlowEdge kind="live" />
      </div>
    );
    const svg = screen.getByTestId("flow-edge");
    await waitFor(() => expect(svg.querySelectorAll("rect").length).toBe(4));
    const rect = svg.querySelector("rect")!;
    // 100 Layout-Höhe minus 2 × 0.75 Strich-Inset — NICHT 96.5 − 1.5.
    expect(parseFloat(rect.getAttribute("height")!)).toBeCloseTo(98.5, 5);
    expect(parseFloat(rect.getAttribute("width")!)).toBeCloseTo(298.5, 5);
  });

  it("follows the card's border-radius (stroke runs concentric, 1.75 px inside the outer edge)", async () => {
    setup();
    render(
      <div style={{ position: "relative", borderRadius: "20px" }}>
        <FlowEdge kind="live" />
      </div>
    );
    const svg = screen.getByTestId("flow-edge");
    await waitFor(() => expect(svg.querySelectorAll("rect").length).toBe(4));
    expect(parseFloat(svg.querySelector("rect")!.getAttribute("rx")!)).toBeCloseTo(18.25, 5);
  });

  it("dash period equals the rounded-rect path length (no seam gap per lap)", async () => {
    setup();
    render(
      <div style={{ position: "relative", borderRadius: "20px" }}>
        <FlowEdge kind="live" />
      </div>
    );
    const svg = screen.getByTestId("flow-edge");
    await waitFor(() => expect(svg.querySelectorAll("rect").length).toBe(4));
    const expected = roundedRectPerimeter(298.5, 98.5, 18.25);
    for (const rect of svg.querySelectorAll("rect")) {
      const [dash, gap] = rect.getAttribute("stroke-dasharray")!.split(" ").map(Number);
      expect(dash + gap).toBeCloseTo(expected, 5);
    }
  });
});

describe("roundedRectPerimeter", () => {
  it("is shorter than 2(w+h) by the four corner cut-offs", () => {
    // Vier Ecken: je 2r gerade Strecke ersetzt durch einen Viertelkreis (πr/2).
    expect(roundedRectPerimeter(300, 100, 0)).toBeCloseTo(800, 9);
    expect(roundedRectPerimeter(300, 100, 20)).toBeCloseTo(800 - 8 * 20 + 2 * Math.PI * 20, 9);
  });

  it("clamps the radius to half the shorter side", () => {
    expect(roundedRectPerimeter(300, 100, 500)).toBeCloseTo(roundedRectPerimeter(300, 100, 50), 9);
  });
});

describe("layerDashOffsets", () => {
  it("makes every tail layer END at the head's front so the tail trails behind", () => {
    // Kopf (Lage 0) belegt [o, o+len0]; jede längere Lage soll [o+len0−len_i, o+len0] belegen.
    const head = 10;
    const lens = [10, 25, 40, 60];
    const offsets = layerDashOffsets(head, lens);
    // Sichtbarer Bereich einer Lage beginnt bei −dashoffset.
    expect(offsets.map((o) => -o + 0)).toEqual([head, head - 15, head - 30, head - 50]);
    // Und alle enden am selben Punkt: start + len == head + len0.
    for (let i = 0; i < lens.length; i++) expect(-offsets[i] + lens[i]).toBeCloseTo(head + lens[0], 9);
  });
});
