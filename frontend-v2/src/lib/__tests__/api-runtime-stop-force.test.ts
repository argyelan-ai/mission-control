import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { api } from "@/lib/api";

// Buehne v2 §5/§7 PR 2 — the Stop button needs a force override for the
// Dispatch-Gate 409 (`agent_busy`). This pins the URL contract so a future
// refactor of api.runtimes.stop can't silently drop the query param.
describe("api.runtimes.stop force wiring", () => {
  const realFetch = global.fetch;
  beforeEach(() => {
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {}, removeItem: () => {} });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, message: "gestoppt" }),
      text: async () => "{}",
      headers: new Headers({ "content-type": "application/json" }),
    } as unknown as Response);
  });
  afterEach(() => {
    global.fetch = realFetch;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("posts without a query param by default", async () => {
    await api.runtimes.stop("rt-1");
    const calledUrl = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(calledUrl).toBe("/api/v1/runtimes/rt-1/stop");
  });

  it("appends ?force=true when overriding the Dispatch-Gate", async () => {
    await api.runtimes.stop("rt-1", { force: true });
    const calledUrl = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(calledUrl).toBe("/api/v1/runtimes/rt-1/stop?force=true");
  });
});
