/**
 * useElementWidth — measures an element that appears AFTER the component
 * mounted (the sessions page's chat island renders only once AppShell's auth
 * check passed). A mount-time measurement never saw it, so the side panel
 * had no width cap in the real browser (01.11.2026).
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { useState } from "react";
import { useElementWidth } from "../useElementWidth";

function Late() {
  const [ref, width] = useElementWidth<HTMLDivElement>();
  const [shown, setShown] = useState(false);
  return (
    <>
      <button onClick={() => setShown(true)}>show</button>
      <span data-testid="width">{width}</span>
      {shown && <div data-testid="late" ref={ref} />}
    </>
  );
}

describe("useElementWidth", () => {
  afterEach(() => vi.restoreAllMocks());

  it("measures an element that attaches after mount", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      width: 1038, height: 700, top: 0, left: 0, right: 1038, bottom: 700, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect);
    render(<Late />);
    expect(screen.getByTestId("width")).toHaveTextContent("0");
    act(() => screen.getByText("show").click());
    expect(screen.getByTestId("width")).toHaveTextContent("1038");
  });
});
