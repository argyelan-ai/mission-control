/**
 * RoleField / RoleChip / suggestRole — Rezept-Umschalter P2.
 *
 * Sabotage-Probe (manuell, 02.09.2026): `suggestRole` auf `return "head"`
 * gesetzt → „1 existing → worker" rot; `RoleChip` ohne `if (!role) return
 * null` → „renders nothing without a role" rot. Beide Tests prüfen also etwas.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RoleChip, RoleField, suggestRole } from "../RoleField";

describe("suggestRole", () => {
  it("first box → head, every further box → worker", () => {
    expect(suggestRole(0)).toBe("head");
    expect(suggestRole(1)).toBe("worker");
    expect(suggestRole(7)).toBe("worker");
  });
});

describe("RoleField", () => {
  it("offers Head and Worker as a radiogroup and reports the click", async () => {
    const onChange = vi.fn();
    render(<RoleField value="head" onChange={onChange} labelClassName="text-xs" suggested />);

    expect(screen.getByRole("radiogroup", { name: "Role" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Head" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Worker" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/Single-box recipes ignore the role/)).toBeInTheDocument();
    expect(screen.getByTestId("host-role-suggested")).toHaveTextContent(/Suggestion: first box head/);

    await userEvent.click(screen.getByRole("radio", { name: "Worker" }));
    expect(onChange).toHaveBeenCalledWith("worker");
  });

  it("without `suggested` the suggestion sentence is gone; `allowNone` adds a third choice that sends null", async () => {
    const onChange = vi.fn();
    render(<RoleField value={null} onChange={onChange} labelClassName="text-xs" allowNone />);

    expect(screen.queryByTestId("host-role-suggested")).toBeNull();
    const none = screen.getByRole("radio", { name: "None" });
    expect(none).toHaveAttribute("aria-checked", "true");

    await userEvent.click(screen.getByRole("radio", { name: "Head" }));
    expect(onChange).toHaveBeenCalledWith("head");
    await userEvent.click(none);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("without `allowNone` there is no third choice", () => {
    render(<RoleField value="worker" onChange={() => {}} labelClassName="text-xs" />);
    expect(screen.queryByRole("radio", { name: "None" })).toBeNull();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
  });
});

describe("RoleChip", () => {
  it("renders nothing without a role", () => {
    const { container } = render(<RoleChip role={null} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<RoleChip role={undefined} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it("renders the mono chip for head and worker", () => {
    render(<RoleChip role="head" />);
    expect(screen.getByTestId("host-role-chip")).toHaveTextContent("Head");
    expect(screen.getByTestId("host-role-chip")).toHaveAttribute("data-role", "head");
    expect(screen.getByTestId("host-role-chip").className).toMatch(/font-mono/);
  });
});
