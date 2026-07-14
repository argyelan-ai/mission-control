import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FilesSearchFilters } from "../FilesSearchFilters";
import type { FsRoot } from "@/lib/types";

const ROOTS: FsRoot[] = [
  { key: "deliverables", label: "Deliverables", icon: "Package", native_open: true, indexed_count: 7, deletable: true },
  { key: "vault", label: "Vault", icon: "BookOpen", native_open: true, indexed_count: 42, deletable: true },
];

describe("FilesSearchFilters", () => {
  it("renders Type, Agent, and Root selects", () => {
    render(
      <FilesSearchFilters filters={{}} onChange={() => {}} roots={ROOTS} agents={["rex", "sparky"]} />
    );
    expect(screen.getByLabelText("Type")).toBeInTheDocument();
    expect(screen.getByLabelText("Agent")).toBeInTheDocument();
    expect(screen.getByLabelText("Root")).toBeInTheDocument();
  });

  it("only offers agents passed in (derived from search results)", () => {
    render(
      <FilesSearchFilters filters={{}} onChange={() => {}} roots={ROOTS} agents={["rex", "sparky"]} />
    );
    const agentSelect = screen.getByLabelText("Agent") as HTMLSelectElement;
    const optionValues = [...agentSelect.options].map((o) => o.value);
    expect(optionValues).toEqual(["", "rex", "sparky"]);
  });

  it("calls onChange with the picked type, clearing it back to undefined when reset", async () => {
    const onChange = vi.fn();
    render(
      <FilesSearchFilters filters={{}} onChange={onChange} roots={ROOTS} agents={[]} />
    );
    await userEvent.selectOptions(screen.getByLabelText("Type"), "image");
    expect(onChange).toHaveBeenCalledWith({ type: "image" });
  });

  it("calls onChange with the picked root", async () => {
    const onChange = vi.fn();
    render(
      <FilesSearchFilters filters={{}} onChange={onChange} roots={ROOTS} agents={[]} />
    );
    await userEvent.selectOptions(screen.getByLabelText("Root"), "vault");
    expect(onChange).toHaveBeenCalledWith({ root: "vault" });
  });
});
