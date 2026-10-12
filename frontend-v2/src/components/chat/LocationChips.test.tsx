import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LocationChips } from "./LocationChips";

describe("LocationChips (ACP locations, chat package P1)", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows the file name, keeps the whole path as title", () => {
    render(<LocationChips paths={["/workspace/src/demo.py"]} />);
    const chip = screen.getByRole("button", { name: "Copy path /workspace/src/demo.py" });
    expect(chip).toHaveTextContent("demo.py");
    expect(chip).toHaveAttribute("title", "/workspace/src/demo.py");
  });

  it("tap copies the FULL path and says so", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<LocationChips paths={["/workspace/src/demo.py", "/workspace/b.txt"]} />);
    await user.click(screen.getByRole("button", { name: "Copy path /workspace/src/demo.py" }));
    expect(writeText).toHaveBeenCalledWith("/workspace/src/demo.py");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
    expect(screen.getByText("b.txt")).toBeInTheDocument();
  });

  it("renders nothing without paths", () => {
    const { container } = render(<LocationChips paths={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
