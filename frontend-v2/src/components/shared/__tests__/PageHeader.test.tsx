import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { PageHeader, PageTabs } from "../PageHeader";

// One header grammar for every page (13.09.2026): title · quiet meta · one
// primary action · optional tabs. No mono eyebrow, no rule line.

describe("PageHeader", () => {
  it("renders the title as the page's h1", () => {
    render(<PageHeader title="Agents" />);
    expect(screen.getByRole("heading", { level: 1, name: "Agents" })).toBeInTheDocument();
  });

  it("renders meta next to the title and the action on the right", () => {
    render(<PageHeader title="Agents" meta="15/15 online" actions={<button>New agent</button>} />);
    expect(screen.getByText("15/15 online")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New agent" })).toBeInTheDocument();
  });

  it("carries no eyebrow label and no rule line", () => {
    const { container } = render(<PageHeader title="Inbox" meta="3 pending" />);
    expect(container.querySelector(".label-sys")).toBeNull();
    expect(container.querySelector("[data-rule]")).toBeNull();
  });

  it("renders tabs below the title row when given", () => {
    render(
      <PageHeader
        title="Runtimes"
        tabs={<PageTabs items={[{ key: "fleet", label: "Fleet" }, { key: "cloud", label: "Cloud" }]} active="fleet" onChange={() => {}} />}
      />,
    );
    expect(screen.getByRole("tablist")).toBeInTheDocument();
  });
});

describe("PageTabs", () => {
  const items = [
    { key: "agents", label: "Agents", count: 15 },
    { key: "templates", label: "Templates" },
  ];

  it("marks the active tab and switches on click", () => {
    const onChange = vi.fn();
    render(<PageTabs items={items} active="agents" onChange={onChange} />);
    const agents = screen.getByRole("tab", { name: /Agents/ });
    const templates = screen.getByRole("tab", { name: "Templates" });
    expect(agents).toHaveAttribute("aria-selected", "true");
    expect(templates).toHaveAttribute("aria-selected", "false");
    fireEvent.click(templates);
    expect(onChange).toHaveBeenCalledWith("templates");
  });

  it("shows a count next to the label when provided", () => {
    render(<PageTabs items={items} active="agents" onChange={() => {}} />);
    expect(screen.getByRole("tab", { name: /Agents/ })).toHaveTextContent("15");
  });
});
