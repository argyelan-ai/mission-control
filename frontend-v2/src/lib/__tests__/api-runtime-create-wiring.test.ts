import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { api } from "@/lib/api";

// Wiring guard for the live 422 of 2026-08-08: SshProcessDeployDialog and
// BoxWizard send a RuntimeCreate body via api.runtimes.create, but the bare
// POST /api/v1/runtimes is the legacy LM-Studio endpoint that REQUIRES
// lms_identifier. Component tests mock the api module, so a wrong URL here is
// invisible to them — this test pins the URL itself.
describe("api.runtimes.create wiring", () => {
  const realFetch = global.fetch;
  beforeEach(() => {
    // jsdom in this suite has no functional localStorage; request() only
    // reads the auth token from it, an empty stub is enough.
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {}, removeItem: () => {} });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => "{}",
      headers: new Headers({ "content-type": "application/json" }),
    } as unknown as Response);
  });
  afterEach(() => {
    global.fetch = realFetch;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("posts RuntimeCreate bodies to /runtimes/db, not the legacy LMS endpoint", async () => {
    await api.runtimes.create({
      slug: "wiring-test",
      display_name: "Wiring Test",
      runtime_type: "vllm_docker",
      endpoint: "http://192.0.2.10:8000/v1",
    });
    const calledUrl = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(calledUrl).toContain("/api/v1/runtimes/db");
    expect(calledUrl).not.toMatch(/\/api\/v1\/runtimes$/);
  });
});
