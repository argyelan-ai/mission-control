import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SubagentGroup } from "./SubagentGroup";
import type { ChatEvent } from "@/lib/chatTypes";

const events: ChatEvent[] = [
  {
    kind: "message",
    uuid: "u1",
    ts: "2026-08-15T10:00:00Z",
    role: "user",
    text: "Recherchiere die API-Struktur",
    model: null,
    sidechain: true,
  },
  {
    kind: "tool",
    uuid: "u2",
    ts: "2026-08-15T10:00:01Z",
    name: "Read",
    title: "Read backend/app/main.py",
    detail: {},
    toolUseId: "t1",
    result: "ok",
    status: "done",
    stats: null,
    sidechain: true,
  },
  {
    kind: "message",
    uuid: "u3",
    ts: "2026-08-15T10:00:02Z",
    role: "assistant",
    text: "Fertig.",
    model: "claude-sonnet-5",
    sidechain: true,
  },
];

describe("SubagentGroup", () => {
  it("is collapsed by default and shows the header with first-event title + count", () => {
    render(<SubagentGroup events={events} />);
    expect(screen.getByText(/Agent: Recherchiere die API-Struktur/)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.queryByText("Fertig.")).not.toBeInTheDocument();
  });

  it("expands to render child events on click", async () => {
    const user = userEvent.setup();
    render(<SubagentGroup events={events} />);
    await user.click(screen.getByText(/Agent: Recherchiere die API-Struktur/));
    expect(screen.getByText("Fertig.")).toBeInTheDocument();
    expect(screen.getByText("Read backend/app/main.py")).toBeInTheDocument();
  });

  it("renders nothing for an empty events array", () => {
    const { container } = render(<SubagentGroup events={[]} />);
    expect(container.textContent).toBe("");
  });
});
