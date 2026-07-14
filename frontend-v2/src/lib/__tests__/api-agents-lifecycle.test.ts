import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api } from "../api";

// BASE_URL is "" in vitest (NEXT_PUBLIC_API_URL unset), so fetch URLs are
// the bare /api/v1 paths. localStorage stub pattern from api-prompt-templates.test.ts
// (the brief's raw `global.fetch = vi.fn()` mock doesn't stub localStorage, which
// api.ts's request() helper reads via getToken() — spy on fetch + stub localStorage
// instead, matching the rest of this test suite).

describe("api.agents lifecycle", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: () => "tok",
        setItem: () => undefined,
        removeItem: () => undefined,
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ id: "a1", archived_at: "2026-07-13T00:00:00Z" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
  });

  it("archive POSTs to the archive endpoint", async () => {
    await api.agents.archive("a1");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/agents/a1/archive");
    expect(init?.method).toBe("POST");
  });

  it("restore POSTs to the restore endpoint", async () => {
    await api.agents.restore("a1");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/agents/a1/restore");
    expect(init?.method).toBe("POST");
  });

  it("list forwards include_archived when requested, without breaking includeUnassigned callers", async () => {
    fetchSpy.mockImplementation(
      async () =>
        new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } }),
    );

    await api.agents.list(undefined, true);
    let [url] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("include_unassigned=true");
    expect(String(url)).not.toContain("include_archived");

    fetchSpy.mockClear();
    await api.agents.list(undefined, undefined, true);
    [url] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("include_archived=true");
  });
});
