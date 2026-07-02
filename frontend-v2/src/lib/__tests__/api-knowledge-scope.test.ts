import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api } from "@/lib/api";

// Phase 5 — MSY-05 frontend scope-filter wiring (Pitfall-7 alternative path).
// Tests that api.knowledge.list({scope: ...}) produces the expected URL
// (the API client URL builder is the contract under test). Avoids the
// QueryClientProvider render dependency entirely per Pitfall 7 of 05-RESEARCH.md.

describe("api.knowledge.list — scope param wiring (MSY-05, plan 05-01)", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // Stub localStorage to satisfy `getToken()` in api.ts (test runner ships
    // a partial Storage shim without `getItem`).
    const storage = {
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    Object.defineProperty(globalThis, "localStorage", {
      value: storage,
      configurable: true,
      writable: true,
    });
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it("passes scope=global to URL", async () => {
    await api.knowledge.list({ scope: "global" });
    const url = String(fetchSpy.mock.calls[0]?.[0] ?? "");
    expect(url).toContain("/api/v1/knowledge");
    expect(url).toContain("scope=global");
  });

  it("passes scope=board with board_id", async () => {
    await api.knowledge.list({ scope: "board", board_id: "abc-123" });
    const url = String(fetchSpy.mock.calls[0]?.[0] ?? "");
    expect(url).toContain("scope=board");
    expect(url).toContain("board_id=abc-123");
  });

  it("omits scope when not provided", async () => {
    await api.knowledge.list({});
    const url = String(fetchSpy.mock.calls[0]?.[0] ?? "");
    expect(url).not.toContain("scope=");
  });
});
