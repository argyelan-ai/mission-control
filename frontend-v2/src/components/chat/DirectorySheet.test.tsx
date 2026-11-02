/**
 * Directory sheet from the chat header (ADR-100 §4, stage 2d). Per row:
 * name, harness, machine, alive, listening, busy/task, last seen — "alive"
 * and "listening" shown apart, exactly as the backend said them.
 */
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { Directory, DirectoryRow } from "@/lib/comms";
import { DirectorySheet, ReachableButton } from "./DirectorySheet";

function row(o: Partial<DirectoryRow>): DirectoryRow {
  return {
    address: "lead", kind: "agent", label: "Lead", harness: "omp", machine: null,
    alive: "yes", listening: true, busy: false, accept: "accept", task: null,
    last_seen: new Date(Date.now() - 12_000).toISOString(), ...o,
  };
}

const dir: Directory = {
  warming_up: false,
  sessions: [
    row({ address: "helper", label: "Helper", alive: "yes", listening: false }),
    row({ address: "lead", label: "Lead", busy: true, task: "box question to runtimes" }),
    row({
      address: "claude@desk:runtimes", kind: "session", label: "runtimes", harness: "claude", machine: "desk",
      alive: "no", listening: null, last_seen: null,
    }),
  ],
};

describe("DirectorySheet", () => {
  it("one row per principal with both words kept apart", () => {
    render(<DirectorySheet open onClose={vi.fn()} directory={dir} />);
    expect(screen.getByRole("heading")).toHaveTextContent("Reachable");
    expect(screen.getByRole("heading")).toHaveTextContent("1 of 3");
    const rows = screen.getAllByTestId("directory-row");
    // reachable first
    expect(rows[0]).toHaveAttribute("data-reachable", "true");
    const lead = rows[0];
    expect(lead).toHaveTextContent("Lead");
    expect(lead).toHaveTextContent("omp");
    expect(within(lead).getByText("alive")).toBeInTheDocument();
    expect(within(lead).getByText("listening")).toBeInTheDocument();
    expect(lead).toHaveTextContent("working · box question to runtimes · seen");

    const helper = rows.find((r) => r.textContent?.includes("Helper"))!;
    expect(within(helper).getByText("alive")).toBeInTheDocument();
    expect(within(helper).getByText("paused")).toBeInTheDocument();
    expect(helper).toHaveTextContent("takes no messages");

    const term = rows.find((r) => r.textContent?.includes("claude@desk:runtimes"))!;
    expect(within(term).getByText("not alive")).toBeInTheDocument();
    expect(within(term).queryByText("listening")).toBeNull();
    // harness and machine are in the address — not repeated; a guest says so
    expect(term).toHaveTextContent("guest");
    expect(term).toHaveTextContent("not seen yet");
  });

  it("says when MC just started and the words may still be unknown", () => {
    render(<DirectorySheet open onClose={vi.fn()} directory={{ ...dir, warming_up: true }} />);
    expect(screen.getByTestId("directory-warming")).toBeInTheDocument();
  });

  it("empty: nobody may talk yet, and how to change that", () => {
    render(<DirectorySheet open onClose={vi.fn()} directory={{ sessions: [], warming_up: false }} />);
    expect(screen.getByTestId("directory-empty")).toHaveTextContent("comms:talk");
  });

  it("closes", () => {
    const onClose = vi.fn();
    render(<DirectorySheet open onClose={onClose} directory={dir} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("ReachableButton", () => {
  it("counts alive AND listening only, and opens the sheet", () => {
    const onOpen = vi.fn();
    render(<ReachableButton directory={dir} onOpen={onOpen} />);
    const btn = screen.getByTestId("reachable-button");
    expect(btn).toHaveTextContent("1 reachable");
    fireEvent.click(btn);
    expect(onOpen).toHaveBeenCalled();
  });

  it("shows nothing while the directory is unknown", () => {
    render(<ReachableButton directory={undefined} onOpen={vi.fn()} />);
    expect(screen.queryByTestId("reachable-button")).toBeNull();
  });
});
