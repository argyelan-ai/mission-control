import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api } from "../api";

// Pattern from api-agents-lifecycle.test.ts: spy on fetch + stub localStorage
// (request()'s getToken() reads it).

describe("api.chat", () => {
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
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
  });

  it("sendText normalizes CRLF and bare CR to LF before POSTing", async () => {
    await api.chat.sendText("a1", "line one\r\nline two\rline three");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/agents/a1/chat/input");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(init?.body as string);
    expect(body.text).toBe("line one\nline two\nline three");
  });

  it("sendText leaves already-LF text untouched", async () => {
    await api.chat.sendText("a1", "already\nnormalized");
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init?.body as string);
    expect(body.text).toBe("already\nnormalized");
  });

  it("sendKeys POSTs the keys array as-is", async () => {
    await api.chat.sendKeys("a1", ["Escape", "Enter"]);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/agents/a1/chat/keys");
    const body = JSON.parse(init?.body as string);
    expect(body.keys).toEqual(["Escape", "Enter"]);
  });

  it("history forwards limit and beforeUuid as query params", async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ events: [], session: { sessionId: "s1", live: true, startedAt: null }, hasMore: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await api.chat.history("a1", { limit: 50, beforeUuid: "u9" });
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/agents/a1/chat/history");
    expect(String(url)).toContain("limit=50");
    expect(String(url)).toContain("before_uuid=u9");
  });

  it("diff defaults scope to worktree", async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    await api.chat.diff("a1");
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/agents/a1/chat/diff?scope=worktree");
  });

  it("streamUrl builds the SSE endpoint", () => {
    expect(api.chat.streamUrl("a1")).toContain("/api/v1/agents/a1/chat/stream");
  });
});
