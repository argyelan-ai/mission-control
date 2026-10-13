/**
 * P2 — where a tool's pictures appear: on the row WITHOUT opening it, and
 * below a COLLAPSED activity group (never twice when the group is open).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolRow } from "./ToolRow";
import { ToolGroup } from "./ToolGroup";
import { ChatMediaContext } from "./chatMedia";
import type { ToolEvent, ToolImage } from "@/lib/chatTypes";

const SHOT: ToolImage = { id: "a".repeat(64), mime: "image/png", width: 1280, height: 800, bytes: 9000 };

function tool(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool",
    uuid: "u1",
    ts: "2026-10-13T08:00:00Z",
    name: "mcp__playwright__browser_take_screenshot",
    title: "Screenshot",
    detail: { type: "png" },
    toolUseId: "tool-1",
    result: "Took the viewport screenshot",
    status: "done",
    stats: null,
    sidechain: false,
    ...overrides,
  };
}

const wrap = (ui: React.ReactNode) => (
  <ChatMediaContext.Provider value="11111111-2222-3333-4444-555555555555">{ui}</ChatMediaContext.Provider>
);

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(new Blob(["x"]), { status: 200 })));
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
});
afterEach(() => vi.unstubAllGlobals());

describe("tool pictures on the cards", () => {
  it("a collapsed row shows its screenshot, not its JSON", () => {
    render(wrap(<ToolRow ev={tool({ images: [SHOT] })} detailLevel="normal" />));
    expect(screen.getByTestId("tool-images")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-result")).toBeNull();
  });

  it("a row without pictures renders no picture area", () => {
    render(wrap(<ToolRow ev={tool()} detailLevel="normal" />));
    expect(screen.queryByTestId("tool-images")).toBeNull();
  });

  it("a collapsed group shows the run's pictures; opened, each row shows its own once", async () => {
    const events = [
      tool({ uuid: "u1", toolUseId: "t1", images: [SHOT] }),
      tool({ uuid: "u2", toolUseId: "t2", name: "Bash", title: "$ ls", images: null }),
    ];
    render(wrap(<ToolGroup events={events} />));
    expect(screen.getByTestId("tool-group-images")).toBeInTheDocument();
    expect(screen.getAllByTestId("tool-image")).toHaveLength(1);

    await userEvent.setup().click(screen.getByRole("button", { expanded: false }));
    expect(screen.queryByTestId("tool-group-images")).toBeNull();
    expect(screen.getAllByTestId("tool-image")).toHaveLength(1);
  });
});
