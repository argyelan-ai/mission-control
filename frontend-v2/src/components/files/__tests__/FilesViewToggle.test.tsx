import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FilesViewToggle } from "../FilesViewToggle";

describe("FilesViewToggle", () => {
  it("marks the active view as pressed", () => {
    render(<FilesViewToggle view="grid" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Grid view" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "List view" })).toHaveAttribute("aria-pressed", "false");
  });

  it("calls onChange with the clicked mode", async () => {
    const onChange = vi.fn();
    render(<FilesViewToggle view="list" onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Grid view" }));
    expect(onChange).toHaveBeenCalledWith("grid");
  });
});
