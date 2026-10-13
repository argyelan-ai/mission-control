/**
 * P2 live finding 13.10.2026: omp's js browser tool screenshotted
 * example.com and example.org — both pages render identically, so both
 * tool results carry the SAME picture (same sha256 id). In one collapsed
 * group the strip then held two tiles with one React key: one picture
 * twice at best, a tile that vanishes on the next update at worst. The
 * strip shows each picture once; a single card still shows its own.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ToolGroup } from "./ToolGroup";
import { ChatMediaContext } from "./chatMedia";
import type { ToolEvent, ToolImage } from "@/lib/chatTypes";

function fakeResponse() {
  return { ok: true, status: 200, blob: async () => new Blob(["x"], { type: "image/webp" }) } as unknown as Response;
}

const SAME: ToolImage = {
  id: "fb94467986aa47ebe8ab906f027f7e8b5055e6e1ca1c8c7c6f984b1a0d04d5bd",
  mime: "image/webp",
  width: 800,
  height: 600,
  bytes: 24360,
};

function jsShot(id: string, site: string): ToolEvent {
  return {
    kind: "tool",
    uuid: id,
    ts: "2026-10-13T08:18:19Z",
    name: "bash",
    title: `$ [js] Capturing screenshot of ${site}`,
    detail: {},
    toolUseId: id,
    result: "Screenshot captured",
    status: "done",
    stats: null,
    sidechain: false,
    images: [SAME],
  };
}

let errors: string[] = [];
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => fakeResponse()));
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
  errors = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    errors.push(args.map(String).join(" "));
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the same picture from two tool calls", () => {
  it("shows once in a collapsed group, without a duplicate React key", () => {
    render(
      <ChatMediaContext.Provider value="11111111-2222-3333-4444-555555555555">
        <ToolGroup events={[jsShot("call_a", "example.com"), jsShot("call_b", "example.org")]} />
      </ChatMediaContext.Provider>,
    );
    expect(screen.getAllByTestId("tool-image")).toHaveLength(1);
    expect(errors.some((e) => /same key/i.test(e))).toBe(false);
  });
});
