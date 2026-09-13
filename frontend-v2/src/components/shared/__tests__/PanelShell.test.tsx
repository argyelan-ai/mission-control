import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { PanelHeader, PanelFooter, PanelSection } from "../PanelShell";

// One grammar for everything that opens (13.09.2026):
//   [Title] [one quiet explaining line]            [×]
//   [content — sentence-case section titles, no mono eyebrows, no rules]
//   [footer: Cancel · one primary action]

describe("PanelHeader", () => {
  it("renders the title as the dialog heading and the description under it", () => {
    render(<PanelHeader title="Add runtime" description="Paste a base URL — MC probes it." onClose={() => {}} />);
    expect(screen.getByRole("heading", { name: "Add runtime" })).toBeInTheDocument();
    expect(screen.getByText("Paste a base URL — MC probes it.")).toBeInTheDocument();
  });

  it("has an accessible close button that calls onClose", () => {
    const onClose = vi.fn();
    render(<PanelHeader title="New task" onClose={onClose} closeLabel="Close" />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("carries no eyebrow label", () => {
    const { container } = render(<PanelHeader title="New task" onClose={() => {}} />);
    expect(container.querySelector(".label-sys")).toBeNull();
  });
});

describe("PanelSection", () => {
  it("renders a sentence-case section title without a rule line", () => {
    const { container } = render(<PanelSection title="Assignment"><div>body</div></PanelSection>);
    const title = screen.getByText("Assignment");
    expect(title.className).not.toMatch(/uppercase/);
    expect(container.querySelector("hr")).toBeNull();
  });
});

describe("PanelFooter", () => {
  it("places the hint left and the actions right", () => {
    render(
      <PanelFooter hint="Cmd+Enter = create · Esc = close">
        <button>Cancel</button>
        <button>Create task</button>
      </PanelFooter>,
    );
    expect(screen.getByText("Cmd+Enter = create · Esc = close")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create task" })).toBeInTheDocument();
  });
});
