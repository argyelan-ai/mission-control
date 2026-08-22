/**
 * GroupRow — vitest.
 *
 * Abgedeckt: Name + Vorschau (letzte Nachricht bzw. Ziel als Rückfall), genau
 * EIN Zustands-Chip je Status samt englischem Katalog-Text, der pulsierende
 * Punkt nur beim Gate, Auswahl-Zustand und Klick-Rückmeldung.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GroupRow } from "./GroupRow";
import type { GroupSummary } from "@/lib/groupTypes";

function mkGroup(overrides: Partial<GroupSummary> = {}): GroupSummary {
  return {
    id: "group-1",
    thread_id: "thread-1",
    name: "DFlash2 vs. vLLM",
    goal: "Assess DFlash2 as the default",
    status: "idle",
    lifecycle: "one_shot",
    member_count: 2,
    rounds_completed: 0,
    current_round_no: 0,
    max_rounds: 3,
    created_at: "2026-08-20T10:00:00Z",
    last_message: null,
    member_avatars: [
      { id: "m-1", emoji: null, name: "Rex" },
      { id: "m-2", emoji: null, name: "Cody" },
    ],
    ...overrides,
  };
}

function row(): HTMLElement {
  return screen.getByRole("option");
}

describe("GroupRow", () => {
  it("shows the group name and the last message as sender: body", () => {
    render(
      <GroupRow
        group={mkGroup({
          last_message: { sender: "Rex", body: "DFlash2 wins on throughput", created_at: null },
        })}
        selected={false}
        onSelect={() => {}}
      />
    );
    expect(screen.getByText("DFlash2 vs. vLLM")).toBeInTheDocument();
    expect(screen.getByText("Rex: DFlash2 wins on throughput")).toBeInTheDocument();
  });

  it("falls back to the goal when the group has no message yet", () => {
    render(<GroupRow group={mkGroup({ last_message: null })} selected={false} onSelect={() => {}} />);
    expect(screen.getByText("Assess DFlash2 as the default")).toBeInTheDocument();
  });

  it('shows the "waiting" chip with a pulsing dot when the group waits at a gate', () => {
    render(<GroupRow group={mkGroup({ status: "waiting_gate" })} selected={false} onSelect={() => {}} />);
    const chip = screen.getByTestId("group-chip");
    expect(within(chip).getByText("waiting")).toBeInTheDocument();
    expect(chip.querySelector(".animate-pulse")).not.toBeNull();
  });

  it('shows the round chip "R 2/3" while rounds are running, without the gate dot', () => {
    render(
      <GroupRow
        group={mkGroup({ status: "running", current_round_no: 2, max_rounds: 3 })}
        selected={false}
        onSelect={() => {}}
      />
    );
    const chip = screen.getByTestId("group-chip");
    expect(within(chip).getByText("R 2/3")).toBeInTheDocument();
    expect(chip.querySelector(".animate-pulse")).toBeNull();
  });

  it("shows no chip at all while the group is idle (live mode)", () => {
    render(<GroupRow group={mkGroup({ status: "idle" })} selected={false} onSelect={() => {}} />);
    expect(screen.queryByTestId("group-chip")).not.toBeInTheDocument();
  });

  it("shows exactly one chip, never two, for a finished group", () => {
    render(<GroupRow group={mkGroup({ status: "done" })} selected={false} onSelect={() => {}} />);
    const chips = screen.getAllByTestId("group-chip");
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent("done");
  });

  it('shows the "failed" chip for a broken group', () => {
    render(<GroupRow group={mkGroup({ status: "failed" })} selected={false} onSelect={() => {}} />);
    expect(screen.getByTestId("group-chip")).toHaveTextContent("failed");
  });

  it("reports the group id when the row is clicked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<GroupRow group={mkGroup({ id: "group-42" })} selected={false} onSelect={onSelect} />);
    await user.click(screen.getByText("DFlash2 vs. vLLM"));
    expect(onSelect).toHaveBeenCalledWith("group-42");
  });

  it("marks the row as selected for assistive tech", () => {
    const { rerender } = render(
      <GroupRow group={mkGroup()} selected={false} onSelect={() => {}} />
    );
    expect(row()).toHaveAttribute("aria-selected", "false");
    rerender(<GroupRow group={mkGroup()} selected onSelect={() => {}} />);
    expect(row()).toHaveAttribute("aria-selected", "true");
  });

  it("renders the member avatars from member_avatars", () => {
    render(<GroupRow group={mkGroup()} selected={false} onSelect={() => {}} />);
    expect(within(row()).getByRole("img", { name: "Rex, Cody" })).toBeInTheDocument();
  });

  it("survives a group whose backend sent no member_avatars", () => {
    render(
      <GroupRow group={mkGroup({ member_avatars: undefined })} selected={false} onSelect={() => {}} />
    );
    expect(within(row()).queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("DFlash2 vs. vLLM")).toBeInTheDocument();
  });

  it("gives the list variant a touch-sized row height", () => {
    render(<GroupRow group={mkGroup()} selected={false} onSelect={() => {}} variant="list" />);
    expect(row().className).toContain("min-h-[52px]");
  });

  it("keeps the rail variant compact (no touch height)", () => {
    render(<GroupRow group={mkGroup()} selected={false} onSelect={() => {}} variant="rail" />);
    expect(row().className).not.toContain("min-h-[52px]");
  });
});
