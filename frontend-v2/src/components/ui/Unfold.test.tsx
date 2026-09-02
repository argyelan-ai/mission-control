import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { Unfold } from "./Unfold";

/** jsdom kennt kein `matchMedia` — für Unfold heisst das „keine Bewegung".
 *  Die Motion-Fälle stubben es explizit. */
function stubMotion(reduce: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((q: string) => ({
      matches: q.includes("reduce") ? reduce : false,
      media: q,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

afterEach(() => {
  // @ts-expect-error — Test-Stub wieder entfernen
  delete window.matchMedia;
});

describe("Unfold", () => {
  it("mounts nothing while closed and the content once open", () => {
    const { rerender } = render(<Unfold open={false}><p>Inhalt</p></Unfold>);
    expect(screen.queryByText("Inhalt")).not.toBeInTheDocument();
    rerender(<Unfold open><p>Inhalt</p></Unfold>);
    expect(screen.getByText("Inhalt")).toBeInTheDocument();
    expect(screen.getByTestId("unfold")).toHaveAttribute("data-state", "open");
  });

  it("unmounts immediately when the viewer prefers reduced motion", () => {
    stubMotion(true);
    const { rerender } = render(<Unfold open><p>Inhalt</p></Unfold>);
    rerender(<Unfold open={false}><p>Inhalt</p></Unfold>);
    expect(screen.queryByText("Inhalt")).not.toBeInTheDocument();
  });

  it("without matchMedia (jsdom) it also closes instantly — tests stay deterministic", () => {
    const { rerender } = render(<Unfold open><p>Inhalt</p></Unfold>);
    rerender(<Unfold open={false}><p>Inhalt</p></Unfold>);
    expect(screen.queryByText("Inhalt")).not.toBeInTheDocument();
  });

  it("with motion: keeps the content mounted while closing, then removes it on transitionend", () => {
    stubMotion(false);
    const { rerender } = render(<Unfold open><p>Inhalt</p></Unfold>);
    rerender(<Unfold open={false}><p>Inhalt</p></Unfold>);
    const box = screen.getByTestId("unfold");
    expect(box).toHaveAttribute("data-state", "closing");
    expect(screen.getByText("Inhalt")).toBeInTheDocument();
    act(() => {
      fireEvent.transitionEnd(box, { propertyName: "grid-template-rows" });
    });
    expect(screen.queryByText("Inhalt")).not.toBeInTheDocument();
  });

  it("with motion: a re-open during closing cancels the removal", () => {
    stubMotion(false);
    const { rerender } = render(<Unfold open><p>Inhalt</p></Unfold>);
    rerender(<Unfold open={false}><p>Inhalt</p></Unfold>);
    rerender(<Unfold open><p>Inhalt</p></Unfold>);
    const box = screen.getByTestId("unfold");
    expect(box).toHaveAttribute("data-state", "open");
    act(() => {
      fireEvent.transitionEnd(box, { propertyName: "grid-template-rows" });
    });
    expect(screen.getByText("Inhalt")).toBeInTheDocument();
  });

  it("falls back to a timer when no transitionend ever arrives", () => {
    vi.useFakeTimers();
    stubMotion(false);
    const { rerender } = render(<Unfold open><p>Inhalt</p></Unfold>);
    rerender(<Unfold open={false}><p>Inhalt</p></Unfold>);
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(screen.queryByText("Inhalt")).not.toBeInTheDocument();
    vi.useRealTimers();
  });
});
