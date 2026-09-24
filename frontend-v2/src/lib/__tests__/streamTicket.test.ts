import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

/**
 * Security finding 24.09.2026: SSE/WebSocket URLs carried the login JWT as
 * `?token=` and the reverse proxy logged every one of them. These tests pin
 * the replacement: a single-use stream ticket per connection, the JWT only in
 * the Authorization header of the ticket request.
 */

const JWT = "header.payload.signature";

const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
  new Response(JSON.stringify({ ticket: "t/1+x", expires_in: 60 }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }),
);

beforeEach(() => {
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("localStorage", {
    getItem: () => JWT,
    setItem: () => {},
    removeItem: () => {},
    clear: () => {},
    length: 0,
    key: () => null,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("withStreamTicket", () => {
  it("asks for a ticket for the URL's path and appends only the ticket", async () => {
    const { withStreamTicket } = await import("@/lib/streamTicket");
    const url = await withStreamTicket("/api/v1/agents/stream");

    expect(url).toBe("/api/v1/agents/stream?ticket=t%2F1%2Bx");
    expect(url).not.toContain("token=");
    expect(url).not.toContain(JWT);

    const [calledUrl, init] = fetchMock.mock.calls[0];
    expect(calledUrl).toBe("/api/v1/auth/stream-ticket");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe(`Bearer ${JWT}`);
    expect(JSON.parse(String(init?.body))).toEqual({ path: "/api/v1/agents/stream" });
  });

  it("binds the ticket to the path of ws(s) URLs and keeps other params", async () => {
    const { withStreamTicket } = await import("@/lib/streamTicket");
    const url = await withStreamTicket("wss://mc.example.test/api/v1/browser-live/ws?target=abc");

    expect(url).toBe("wss://mc.example.test/api/v1/browser-live/ws?target=abc&ticket=t%2F1%2Bx");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      path: "/api/v1/browser-live/ws",
    });
  });

  it("fetches a fresh ticket on every call (tickets are single-use)", async () => {
    const { withStreamTicket } = await import("@/lib/streamTicket");
    await withStreamTicket("/api/v1/activity/stream");
    await withStreamTicket("/api/v1/activity/stream");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("rejects when the backend refuses a ticket (caller retries with backoff)", async () => {
    fetchMock.mockResolvedValueOnce(new Response("nope", { status: 401 }));
    const { withStreamTicket } = await import("@/lib/streamTicket");
    await expect(withStreamTicket("/api/v1/agents/stream")).rejects.toThrow(/401/);
  });
});

describe("no stream URL carries the login token", () => {
  // Static guard over every non-test source file: building a URL with a
  // `token` query parameter from the login token is exactly the leak this
  // finding closed. The only allowed `?token=` is the bench view link, which
  // carries a resource-scoped 30-minute view token, never the login JWT.
  const SRC = path.resolve(__dirname, "../..");
  const ALLOWED = new Set([path.join("verticals", "bench_studio", "api.ts")]);

  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = path.join(dir, name);
      if (statSync(full).isDirectory()) {
        return name === "__tests__" || name === "node_modules" ? [] : sourceFiles(full);
      }
      return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [full] : [];
    });
  }

  it("finds no ?token= / &token= URL construction and no getToken() inside a URL", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const rel = path.relative(SRC, file);
      if (ALLOWED.has(rel)) continue;
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, i) => {
          const code = line.replace(/\/\/.*$/, "").replace(/^\s*\*.*$/, "");
          if (/[?&]token=/.test(code) || /searchParams\.(set|append)\(\s*["']token["']/.test(code)) {
            offenders.push(`${rel}:${i + 1}: ${line.trim()}`);
          }
        });
    }
    expect(offenders).toEqual([]);
  });

  it("the guard itself catches the old pattern (sabotage probe)", () => {
    const old = "return `${ws}/api/v1/plugins/shell/ws?token=${getToken()}`;";
    expect(/[?&]token=/.test(old)).toBe(true);
  });
});

describe("openTicketedWebSocket reconnects with a fresh ticket", () => {
  class MockWebSocket {
    static instances: MockWebSocket[] = [];
    url: string;
    private listeners: Record<string, ((ev: { code?: number }) => void)[]> = {};
    onclose: ((ev: { code?: number }) => void) | null = null;
    constructor(url: string) {
      this.url = url;
      MockWebSocket.instances.push(this);
    }
    addEventListener(type: string, fn: (ev: { code?: number }) => void) {
      (this.listeners[type] ??= []).push(fn);
    }
    emit(type: string, ev: { code?: number } = {}) {
      (this.listeners[type] ?? []).forEach((fn) => fn(ev));
      if (type === "close") this.onclose?.(ev);
    }
    close() {
      this.emit("close", { code: 1000 });
    }
  }

  let n = 0;
  beforeEach(() => {
    MockWebSocket.instances = [];
    n = 0;
    vi.stubGlobal("WebSocket", MockWebSocket);
    fetchMock.mockImplementation(async () => {
      n += 1;
      return new Response(JSON.stringify({ ticket: `tk${n}`, expires_in: 60 }), { status: 200 });
    });
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const ticketOf = (ws: MockWebSocket) => new URL(ws.url).searchParams.get("ticket");

  it("after an unexpected close (backend restart) it opens again with a new ticket", async () => {
    const { openTicketedWebSocket } = await import("@/lib/streamTicket");
    const setup = vi.fn();
    const cleanup = openTicketedWebSocket("/api/v1/vault/stream", setup);
    await vi.advanceTimersByTimeAsync(0);
    expect(MockWebSocket.instances).toHaveLength(1);

    MockWebSocket.instances[0].emit("close", { code: 1012 });
    await vi.advanceTimersByTimeAsync(1_100);
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(ticketOf(MockWebSocket.instances[1])).not.toBe(ticketOf(MockWebSocket.instances[0]));
    expect(setup).toHaveBeenCalledTimes(2);

    cleanup(); // intentional close: no further reconnect
    await vi.advanceTimersByTimeAsync(60_000);
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("retries when the ticket request itself fails", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    fetchMock.mockImplementationOnce(async () => new Response("down", { status: 503 }));
    const { openTicketedWebSocket } = await import("@/lib/streamTicket");
    const cleanup = openTicketedWebSocket("/api/v1/vault/voice-display", () => {});
    await vi.advanceTimersByTimeAsync(0);
    expect(MockWebSocket.instances).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1_100);
    expect(MockWebSocket.instances).toHaveLength(1);
    cleanup();
    warn.mockRestore();
  });

  it("a normal close (code 1000) from the server does not reconnect", async () => {
    const { openTicketedWebSocket } = await import("@/lib/streamTicket");
    const cleanup = openTicketedWebSocket("/api/v1/vault/stream", () => {});
    await vi.advanceTimersByTimeAsync(0);
    MockWebSocket.instances[0].emit("close", { code: 1000 });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    cleanup();
  });
});
