/**
 * ArchivedGroupsSection — vitest.
 *
 * Abgedeckt: kein leerer Kasten (nichts archiviert → gar nichts), zugeklappt
 * als Grundzustand, Aufklappen zeigt die Zeilen, Zeile öffnet den Raum,
 * „Aus dem Archiv holen" holt zurück OHNE den Raum zu öffnen, Touch-Höhen
 * der Mobil-Variante.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ArchivedGroupsSection } from "./ArchivedGroupsSection";
import type { GroupSummary } from "@/lib/groupTypes";

function mkGroup(overrides: Partial<GroupSummary> = {}): GroupSummary {
  return {
    id: "group-1",
    thread_id: "thread-1",
    name: "DFlash2 vs. vLLM",
    goal: "Assess DFlash2 as the default",
    status: "done",
    lifecycle: "one_shot",
    member_count: 2,
    rounds_completed: 3,
    current_round_no: 3,
    max_rounds: 3,
    created_at: "2026-08-20T10:00:00Z",
    archived_at: "2026-08-22T10:00:00Z",
    last_message: null,
    member_avatars: [
      { id: "m-1", emoji: null, name: "Rex" },
      { id: "m-2", emoji: null, name: "Cody" },
    ],
    ...overrides,
  };
}

const noop = () => {};

describe("ArchivedGroupsSection", () => {
  it("renders nothing at all when no group is archived", () => {
    const { container } = render(
      <ArchivedGroupsSection groups={[]} selectedGroupId={null} onSelectGroup={noop} onUnarchive={noop} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("starts collapsed: the header is there, the rows are not", () => {
    render(
      <ArchivedGroupsSection groups={[mkGroup()]} selectedGroupId={null} onSelectGroup={noop} onUnarchive={noop} />
    );
    const header = screen.getByRole("button", { name: /Archive/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("DFlash2 vs. vLLM")).not.toBeInTheDocument();
  });

  it("shows the archived-group count in the header", () => {
    render(
      <ArchivedGroupsSection
        groups={[mkGroup(), mkGroup({ id: "group-2", name: "Zweite" })]}
        selectedGroupId={null}
        onSelectGroup={noop}
        onUnarchive={noop}
      />
    );
    expect(screen.getByRole("button", { name: /Archive/ })).toHaveTextContent("2");
  });

  it("expanding reveals every archived group", async () => {
    const user = userEvent.setup();
    render(
      <ArchivedGroupsSection
        groups={[mkGroup(), mkGroup({ id: "group-2", name: "Zweite Gruppe" })]}
        selectedGroupId={null}
        onSelectGroup={noop}
        onUnarchive={noop}
      />
    );
    await user.click(screen.getByRole("button", { name: /Archive/ }));
    expect(screen.getByRole("button", { name: /Archive/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("DFlash2 vs. vLLM")).toBeInTheDocument();
    expect(screen.getByText("Zweite Gruppe")).toBeInTheDocument();
  });

  it("clicking a row opens the archived room", async () => {
    const user = userEvent.setup();
    const onSelectGroup = vi.fn();
    render(
      <ArchivedGroupsSection
        groups={[mkGroup({ id: "group-42" })]}
        selectedGroupId={null}
        onSelectGroup={onSelectGroup}
        onUnarchive={noop}
      />
    );
    await user.click(screen.getByRole("button", { name: /Archive/ }));
    await user.click(screen.getByText("DFlash2 vs. vLLM"));
    expect(onSelectGroup).toHaveBeenCalledWith("group-42");
  });

  it("the restore button unarchives WITHOUT opening the room", async () => {
    const user = userEvent.setup();
    const onSelectGroup = vi.fn();
    const onUnarchive = vi.fn();
    render(
      <ArchivedGroupsSection
        groups={[mkGroup({ id: "group-42" })]}
        selectedGroupId={null}
        onSelectGroup={onSelectGroup}
        onUnarchive={onUnarchive}
      />
    );
    await user.click(screen.getByRole("button", { name: /Archive/ }));
    await user.click(screen.getByRole("button", { name: "Restore from archive" }));
    expect(onUnarchive).toHaveBeenCalledWith("group-42");
    expect(onSelectGroup).not.toHaveBeenCalled();
  });

  it("marks the open archived room as selected for assistive tech", async () => {
    const user = userEvent.setup();
    render(
      <ArchivedGroupsSection
        groups={[mkGroup({ id: "group-42" })]}
        selectedGroupId="group-42"
        onSelectGroup={noop}
        onUnarchive={noop}
      />
    );
    await user.click(screen.getByRole("button", { name: /Archive/ }));
    expect(screen.getByRole("option")).toHaveAttribute("aria-selected", "true");
  });

  it("gives the list variant touch-sized rows and restore button (≥44px)", async () => {
    const user = userEvent.setup();
    render(
      <ArchivedGroupsSection
        groups={[mkGroup()]}
        selectedGroupId={null}
        onSelectGroup={noop}
        onUnarchive={noop}
        variant="list"
      />
    );
    const header = screen.getByRole("button", { name: /Archive/ });
    expect(header.className).toContain("min-h-[44px]");
    await user.click(header);
    expect(screen.getByRole("option").parentElement?.className).toContain("min-h-[52px]");
    expect(screen.getByRole("button", { name: "Restore from archive" }).className).toContain("w-11 h-11");
  });

  it("keeps the rail variant compact (no touch height)", async () => {
    const user = userEvent.setup();
    render(
      <ArchivedGroupsSection
        groups={[mkGroup()]}
        selectedGroupId={null}
        onSelectGroup={noop}
        onUnarchive={noop}
        variant="rail"
      />
    );
    await user.click(screen.getByRole("button", { name: /Archive/ }));
    expect(screen.getByRole("option").parentElement?.className).not.toContain("min-h-[52px]");
  });
});
