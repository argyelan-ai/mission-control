import { describe, it, expect, afterEach, vi } from "vitest";
import { CHAT_ENTRY_PARAM, chatEntryHref, chatUrl, isPhoneViewport } from "../chatHistory";

describe("chatHistory — chat URLs", () => {
  it("a chat's own URL names one chat, encoded", () => {
    expect(chatUrl("agent", "a1")).toBe("/sessions?agent=a1");
    expect(chatUrl("group", "g 1")).toBe("/sessions?group=g%201");
    expect(chatUrl("head", "run-9")).toBe("/sessions?head=run-9");
  });

  it("a link from elsewhere carries the entry marker on top of the chat's URL", () => {
    expect(chatEntryHref("head", "run 9")).toBe(`/sessions?head=run%209&${CHAT_ENTRY_PARAM}=1`);
  });
});

describe("chatHistory — phone viewport", () => {
  afterEach(() => {
    delete (window as { matchMedia?: unknown }).matchMedia;
  });

  it("follows the md breakpoint", () => {
    const seen: string[] = [];
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (q: string) => { seen.push(q); return { matches: true }; },
    });
    expect(isPhoneViewport()).toBe(true);
    expect(seen).toEqual(["(max-width: 767px)"]);
  });

  it("without matchMedia it is not a phone (desktop behaviour is the safe default)", () => {
    expect(isPhoneViewport()).toBe(false);
  });
});

describe("chatHistory — direct load", () => {
  afterEach(() => vi.restoreAllMocks());

  it("is true once, for a document loaded straight at the current URL", async () => {
    vi.resetModules();
    vi.spyOn(performance, "getEntriesByType").mockReturnValue([
      { type: "navigate", name: window.location.href } as unknown as PerformanceEntry,
    ]);
    const { takeDirectLoad } = await import("../chatHistory");
    expect(takeDirectLoad()).toBe(true);
    expect(takeDirectLoad()).toBe(false);
  });

  it("a reload or a back/forward load is not a direct load", async () => {
    for (const type of ["reload", "back_forward"]) {
      vi.resetModules();
      vi.spyOn(performance, "getEntriesByType").mockReturnValue([
        { type, name: window.location.href } as unknown as PerformanceEntry,
      ]);
      const { takeDirectLoad } = await import("../chatHistory");
      expect(takeDirectLoad()).toBe(false);
    }
  });

  it("a document that started on another page is not a direct load of this one", async () => {
    vi.resetModules();
    vi.spyOn(performance, "getEntriesByType").mockReturnValue([
      { type: "navigate", name: "http://example.test/" } as unknown as PerformanceEntry,
    ]);
    const { takeDirectLoad } = await import("../chatHistory");
    expect(takeDirectLoad()).toBe(false);
  });
});
