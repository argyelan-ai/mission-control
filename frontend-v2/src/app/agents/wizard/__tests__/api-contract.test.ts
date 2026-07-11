import { describe, it, expect, vi, beforeEach } from "vitest";
import { api } from "@/lib/api";

describe("wizard api surface", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ soul_md: "ok", ready: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    // Node's built-in localStorage global shadows jsdom's in this environment
    // and lacks setItem — stub it directly (same pattern as SetupWizard.test.tsx).
    const store: Record<string, string> = {};
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = v; },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });
    localStorage.setItem("mc_auth_token", "t");
  });

  it("previewSoul posts to the preview endpoint", async () => {
    await api.agents.previewSoul({ name: "X" });
    const url = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(String(url)).toContain("/api/v1/agents/preview-soul");
  });

  it("healthCheck posts to the health-check endpoint", async () => {
    await api.agents.healthCheck("abc");
    const url = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(String(url)).toContain("/api/v1/agents/abc/health-check");
  });

  it("create accepts harness + scopes without a type error", async () => {
    await api.agents.create({ name: "X", harness: "omp", scopes: ["tasks:read"] });
    expect(fetch).toHaveBeenCalled();
  });
});
