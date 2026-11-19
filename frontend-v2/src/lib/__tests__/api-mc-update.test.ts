import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { api } from "@/lib/api";

// The self-update calls (routers/system_update.py): start and rollback carry
// the fresh step-up token in X-MC-Step-Up next to the login — without it the
// backend answers 403 step_up_required. The password goes to /auth/step-up,
// where a wrong one is a 401 that must NOT sign the operator out.
describe("self-update API calls", () => {
  const realFetch = global.fetch;
  let fetchMock: ReturnType<typeof vi.fn>;

  function respond(status: number, body: unknown) {
    fetchMock = vi.fn().mockResolvedValue({
      ok: status < 400,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
      headers: new Headers({ "content-type": "application/json" }),
    } as unknown as Response);
    global.fetch = fetchMock as unknown as typeof fetch;
  }

  beforeEach(() => {
    vi.stubGlobal("localStorage", {
      getItem: () => "login-jwt",
      setItem: () => undefined,
      removeItem: () => undefined,
    });
  });
  afterEach(() => {
    global.fetch = realFetch;
    vi.unstubAllGlobals();
  });

  const call = () => fetchMock.mock.calls[0] as [string, RequestInit];
  const headers = () => call()[1].headers as Record<string, string>;

  it("start sends the step-up token and the offered release", async () => {
    respond(202, { run_id: "20261019T120000Z-aaaaaa", state: "started" });
    await api.system.update.start("v0.9.3", "step-up-tok");
    expect(call()[0]).toBe("/api/v1/system/update/start");
    expect(headers()["X-MC-Step-Up"]).toBe("step-up-tok");
    expect(headers().Authorization).toBe("Bearer login-jwt");
    expect(JSON.parse(String(call()[1].body))).toEqual({ expected_target: "v0.9.3" });
  });

  it("rollback sends the step-up token and the run", async () => {
    respond(202, { run_id: "20261019T120000Z-aaaaaa" });
    await api.system.update.rollback("20261019T120000Z-aaaaaa", "step-up-tok");
    expect(call()[0]).toBe("/api/v1/system/update/rollback");
    expect(headers()["X-MC-Step-Up"]).toBe("step-up-tok");
    expect(JSON.parse(String(call()[1].body))).toEqual({ run_id: "20261019T120000Z-aaaaaa" });
  });

  it("a wrong password at /auth/step-up is an error, not a sign-out", async () => {
    const href = window.location.href;
    respond(401, { detail: { code: "step_up_wrong_password", message: "Wrong password." } });
    await expect(api.auth.stepUp("nope")).rejects.toThrow(/API 401: .*step_up_wrong_password/);
    expect(window.location.href).toBe(href);
  });
});
