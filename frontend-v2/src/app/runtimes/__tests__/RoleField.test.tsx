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
import { useState } from "react";
import { RoleChip, RoleField, suggestRole } from "../RoleField";
import type { HostRole } from "@/lib/types";

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

describe("RoleField — keyboard (ARIA radiogroup)", () => {
  it("roving tabIndex: only the checked radio is in the tab order", () => {
    render(<RoleField value="worker" onChange={() => {}} labelClassName="text-xs" allowNone />);
    expect(screen.getByRole("radio", { name: "Head" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("radio", { name: "Worker" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "None" })).toHaveAttribute("tabindex", "-1");
  });

  it("with nothing checked the first radio is the tab stop", () => {
    render(<RoleField value={null} onChange={() => {}} labelClassName="text-xs" />);
    expect(screen.getByRole("radio", { name: "Head" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Worker" })).toHaveAttribute("tabindex", "-1");
  });

  it("arrow keys move the choice and the focus, wrapping around", async () => {
    // Gesteuert wie im echten Formular: der Wert folgt onChange.
    const seen: Array<HostRole | null> = [];
    function Harness() {
      const [v, setV] = useState<HostRole | null>("head");
      return <RoleField value={v} onChange={(r) => { seen.push(r); setV(r); }} labelClassName="text-xs" allowNone />;
    }
    render(<Harness />);
    screen.getByRole("radio", { name: "Head" }).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(seen.at(-1)).toBe("worker");
    expect(screen.getByRole("radio", { name: "Worker" })).toHaveFocus();
    expect(screen.getByRole("radio", { name: "Worker" })).toHaveAttribute("aria-checked", "true");
    await userEvent.keyboard("{ArrowLeft}");
    expect(seen.at(-1)).toBe("head");
    expect(screen.getByRole("radio", { name: "Head" })).toHaveFocus();
    // Wrap: von Head nach links/oben landet auf „None" (letzte Option) …
    await userEvent.keyboard("{ArrowUp}");
    expect(seen.at(-1)).toBe(null);
    expect(screen.getByRole("radio", { name: "None" })).toHaveFocus();
    // … und von „None" nach rechts wieder auf Head.
    await userEvent.keyboard("{ArrowDown}");
    expect(seen.at(-1)).toBe("head");
  });

  it("ArrowDown from an unselected group picks the first option", async () => {
    const onChange = vi.fn();
    render(<RoleField value={null} onChange={onChange} labelClassName="text-xs" />);
    screen.getByRole("radio", { name: "Head" }).focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(onChange).toHaveBeenLastCalledWith("head");
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
