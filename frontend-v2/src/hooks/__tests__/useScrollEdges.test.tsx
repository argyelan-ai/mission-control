/**
 * Round-2 Q5 (t4 N9): the task tabs are 545 px wide in a 390 px phone; the
 * static edge fade did not tell that "Timeline" and "History" exist. The
 * strip now knows whether more tabs hide left or right of it.
 */
import { describe, it, expect } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useScrollEdges } from "../useScrollEdges";

function Strip({ watch = 0 }: { watch?: unknown }) {
  const { ref, start, end } = useScrollEdges<HTMLDivElement>(watch);
  return (
    <>
      <div ref={ref} data-testid="strip" />
      <span data-testid="edges">{`${start ? "start" : "-"} ${end ? "end" : "-"}`}</span>
    </>
  );
}

function size(el: HTMLElement, { scrollWidth, clientWidth, scrollLeft }: Record<string, number>) {
  Object.defineProperty(el, "scrollWidth", { configurable: true, value: scrollWidth });
  Object.defineProperty(el, "clientWidth", { configurable: true, value: clientWidth });
  Object.defineProperty(el, "scrollLeft", { configurable: true, writable: true, value: scrollLeft });
}

describe("useScrollEdges", () => {
  it("nothing hidden when everything fits", () => {
    const { rerender } = render(<Strip />);
    size(screen.getByTestId("strip"), { scrollWidth: 300, clientWidth: 390, scrollLeft: 0 });
    rerender(<Strip watch={1} />);
    expect(screen.getByTestId("edges")).toHaveTextContent("- -");
  });

  it("more to the right at the start, more to the left at the end, both in between", () => {
    const { rerender } = render(<Strip />);
    const strip = screen.getByTestId("strip");
    size(strip, { scrollWidth: 545, clientWidth: 390, scrollLeft: 0 });
    rerender(<Strip watch={1} />);
    expect(screen.getByTestId("edges")).toHaveTextContent("- end");

    act(() => {
      size(strip, { scrollWidth: 545, clientWidth: 390, scrollLeft: 80 });
      fireEvent.scroll(strip);
    });
    expect(screen.getByTestId("edges")).toHaveTextContent("start end");

    act(() => {
      size(strip, { scrollWidth: 545, clientWidth: 390, scrollLeft: 155 });
      fireEvent.scroll(strip);
    });
    expect(screen.getByTestId("edges")).toHaveTextContent("start -");
  });
});
