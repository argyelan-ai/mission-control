/**
 * Motif — das gezeichnete Herkunfts-Zeichen einer Ereignis-Karte.
 *
 * jsdom hat keinen Canvas-Kontext (`getContext` liefert null). Getestet wird
 * darum nicht das Bild, sondern der Vertrag drumherum: welches Motiv zu
 * welcher Herkunft gehoert, und WANN ueberhaupt animiert wird — nur lebendig,
 * nie bei reduzierter Bewegung, nie im Ruhezustand.
 */
import React from "react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { Motif, motifForSource } from "./Motif";

function stubReducedMotion(matches: boolean) {
  const original = window.matchMedia;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes("prefers-reduced-motion") ? matches : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  return () => {
    window.matchMedia = original;
  };
}

describe("motifForSource", () => {
  it("maps every source kind to its own motif", () => {
    expect(motifForSource("task")).toBe("woge");
    expect(motifForSource("nudge")).toBe("strom");
    expect(motifForSource("inbox")).toBe("strom");
    expect(motifForSource("teammate")).toBe("iris");
    expect(motifForSource("system")).toBe("kern");
  });
});

/** jsdom kennt keinen 2D-Kontext. Diese Attrappe nimmt jeden Zeichenbefehl
 *  entgegen und wirft ihn weg — so laeuft der echte Pfad bis zur Schleife. */
function stubCanvasContext() {
  const gradient = { addColorStop: () => undefined };
  const ctx = new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === "createLinearGradient" || prop === "createRadialGradient") return () => gradient;
        return () => undefined;
      },
      set: () => true,
    },
  );
  const original = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = (() => ctx) as unknown as typeof original;
  return () => {
    HTMLCanvasElement.prototype.getContext = original;
  };
}

describe("Motif", () => {
  let restore: (() => void) | null = null;
  let restoreCtx: (() => void) | null = null;
  beforeEach(() => {
    restoreCtx = stubCanvasContext();
  });
  afterEach(() => {
    restore?.();
    restore = null;
    restoreCtx?.();
    restoreCtx = null;
    vi.restoreAllMocks();
  });

  it("renders a canvas tagged with kind and liveness, decorative for AT", () => {
    render(<Motif kind="woge" live={false} size={24} />);
    const cvs = screen.getByTestId("motif");
    expect(cvs.tagName).toBe("CANVAS");
    expect(cvs).toHaveAttribute("data-kind", "woge");
    expect(cvs).toHaveAttribute("data-live", "false");
    expect(cvs).toHaveAttribute("aria-hidden", "true");
  });

  it("runs the animation loop only while live", () => {
    restore = stubReducedMotion(false);
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
    render(<Motif kind="strom" live={false} size={24} />);
    expect(raf).not.toHaveBeenCalled();
    render(<Motif kind="strom" live size={24} />);
    expect(raf).toHaveBeenCalled();
  });

  it("stays still under prefers-reduced-motion even when live", () => {
    restore = stubReducedMotion(true);
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
    render(<Motif kind="iris" live size={24} />);
    expect(raf).not.toHaveBeenCalled();
    expect(screen.getByTestId("motif")).toHaveAttribute("data-live", "false");
  });

  it("stops the loop when it goes from live to settled", () => {
    restore = stubReducedMotion(false);
    vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 7);
    const caf = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    const { rerender } = render(<Motif kind="kern" live size={24} />);
    rerender(<Motif kind="kern" live={false} size={24} />);
    expect(caf).toHaveBeenCalledWith(7);
  });
});
