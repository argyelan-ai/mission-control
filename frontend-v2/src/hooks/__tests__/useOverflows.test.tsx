import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { useOverflows } from "../useOverflows";

function Probe({ tall }: { tall: boolean }) {
  const { ref, overflows } = useOverflows<HTMLDivElement>(tall);
  return (
    <div
      ref={(node) => {
        if (node) {
          Object.defineProperty(node, "clientHeight", { value: 100, configurable: true });
          Object.defineProperty(node, "scrollHeight", { value: tall ? 180 : 100, configurable: true });
        }
        ref(node);
      }}
      data-testid="box"
      data-overflows={overflows}
    />
  );
}

describe("useOverflows", () => {
  it("is true only when the content is taller than the box", () => {
    const { rerender } = render(<Probe tall={false} />);
    expect(screen.getByTestId("box")).toHaveAttribute("data-overflows", "false");
    rerender(<Probe tall />);
    expect(screen.getByTestId("box")).toHaveAttribute("data-overflows", "true");
  });
});
