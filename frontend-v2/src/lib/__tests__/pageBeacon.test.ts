import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const token = vi.hoisted(() => ({ value: "" }));
vi.mock("../authToken", () => ({ BASE_URL: "", getToken: () => token.value }));

import { routePattern, sendPageView } from "../pageBeacon";

describe("routePattern", () => {
  it.each([
    ["/", "/"],
    ["/sessions", "/sessions"],
    ["/agents/boss", "/agents/:id"],
    ["/schedule/nightly-backup", "/schedule/:id"],
    ["/tasks/3f2a6c1e-0b7d-4a51-9a53-1b6f0f1e2d3c", "/tasks/:id"],
    ["/memory/graph/", "/memory/graph"],
    ["/tasks?view=board", "/tasks"],
  ])("%s -> %s", (input, expected) => {
    expect(routePattern(input)).toBe(expected);
  });
});

describe("sendPageView", () => {
  const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockClear();
  });
  afterEach(() => {
    token.value = "";
    vi.unstubAllGlobals();
  });

  it("sends only the route pattern", () => {
    token.value = "t0k";
    sendPageView("/agents/boss?tab=chat");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/usage\/page$/);
    expect(JSON.parse(init.body as string)).toEqual({ route: "/agents/:id" });
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer t0k");
  });

  it("does nothing without a login token", () => {
    sendPageView("/sessions");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
