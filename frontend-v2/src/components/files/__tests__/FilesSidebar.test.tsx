import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FilesSidebar, TRASH_KEY } from "../FilesSidebar";
import type { FsRoot } from "@/lib/types";

const ROOTS: FsRoot[] = [
  { key: "deliverables", label: "Deliverables", icon: "Package", native_open: true, indexed_count: 7, deletable: true },
  { key: "vault", label: "Vault", icon: "BookOpen", native_open: true, indexed_count: 42, deletable: true },
];

describe("FilesSidebar", () => {
  it("renders every root as a button with its indexed count, plus a separate Trash entry", () => {
    render(<FilesSidebar roots={ROOTS} activeKey="deliverables" onSelect={() => {}} />);
    expect(screen.getByRole("button", { name: /Deliverables/ })).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Vault/ })).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Trash/ })).toBeInTheDocument();
  });

  it("calls onSelect with the clicked root's key", async () => {
    const onSelect = vi.fn();
    render(<FilesSidebar roots={ROOTS} activeKey="deliverables" onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /Vault/ }));
    expect(onSelect).toHaveBeenCalledWith("vault");
  });

  it("calls onSelect with the trash sentinel key when Trash is clicked", async () => {
    const onSelect = vi.fn();
    render(<FilesSidebar roots={ROOTS} activeKey="deliverables" onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /Trash/ }));
    expect(onSelect).toHaveBeenCalledWith(TRASH_KEY);
  });
});
