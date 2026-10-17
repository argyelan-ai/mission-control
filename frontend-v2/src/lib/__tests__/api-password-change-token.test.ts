import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { AUTH_TOKEN_KEY, api } from "@/lib/api";

// A password change signs out every other session (backend bumps
// token_version). The calling session survives only because the response
// carries a fresh access_token — the client must store it right away, or the
// very next request would 401 and throw the user to /login.
describe("password change adopts the fresh token", () => {
  const realFetch = global.fetch;
  let store: Record<string, string>;

  function mockResponse(body: unknown) {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
      headers: new Headers({ "content-type": "application/json" }),
    } as unknown as Response);
  }

  beforeEach(() => {
    store = { [AUTH_TOKEN_KEY]: "old-token" };
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
    });
  });
  afterEach(() => {
    global.fetch = realFetch;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("stores access_token from PATCH /auth/me", async () => {
    mockResponse({ id: "u1", email: "a@b.c", name: "A", role: "admin", access_token: "fresh-token" });
    const res = await api.auth.updateProfile({ current_password: "x", new_password: "yyyyyy" });
    expect(store[AUTH_TOKEN_KEY]).toBe("fresh-token");
    expect(res.email).toBe("a@b.c");
  });

  it("stores access_token when an admin resets their own password", async () => {
    mockResponse({ id: "u1", email: "a@b.c", name: "A", role: "admin", access_token: "fresh-token" });
    await api.auth.users.update("u1", { password: "yyyyyy" });
    expect(store[AUTH_TOKEN_KEY]).toBe("fresh-token");
  });

  it("keeps the token when the response has none (plain profile edit)", async () => {
    mockResponse({ id: "u1", email: "a@b.c", name: "New", role: "admin" });
    await api.auth.updateProfile({ name: "New" });
    expect(store[AUTH_TOKEN_KEY]).toBe("old-token");
  });
});
