import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserLiveView } from "../BrowserLiveView";
import { api } from "@/lib/api";
import type { BrowserLiveTarget } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const TARGETS: BrowserLiveTarget[] = [
  { id: "target-1", title: "Checkout flow", url: "https://example.com/checkout" },
];

// ── WebSocket stub ───────────────────────────────────────────────────────────
// A minimal fake that records the last instance so tests can push server
// messages by calling `instance.onmessage({ data: ... })` directly — no real
// network involved (view-only client never sends anything).

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send() {}
  close() {
    this.closed = true;
    this.onclose?.(new CloseEvent("close", { code: 1000 }));
  }
}

describe("BrowserLiveView", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    FakeWebSocket.instances = [];
    // @ts-expect-error -- test stub, not a full WebSocket implementation
    global.WebSocket = FakeWebSocket;
    // Node's built-in localStorage stub lacks a backing file in this sandbox
    // (see FilePreview.test.tsx for the same workaround) — getToken() would
    // otherwise throw before the WS URL can be built.
    const storage = {
      getItem: () => "tok",
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    Object.defineProperty(globalThis, "localStorage", {
      value: storage, configurable: true, writable: true,
    });
  });

  afterEach(() => {
    // @ts-expect-error -- restore is not meaningful here, just avoid leaking across files
    delete global.WebSocket;
  });

  it("renders empty state when there are no open targets", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue([]);
    renderWithQuery(<BrowserLiveView />);

    expect(
      await screen.findByText(/Agent browser not running/i),
    ).toBeInTheDocument();
  });

  it("renders empty state when the cdp-browser container is unreachable (502)", async () => {
    vi.spyOn(api.browserLive, "targets").mockRejectedValue(
      new Error("API 502: Agent-Browser (cdp-browser) nicht erreichbar"),
    );
    renderWithQuery(<BrowserLiveView />);

    expect(
      await screen.findByText(/Agent browser not running/i),
    ).toBeInTheDocument();
  });

  it("shows a frame on the canvas/img after a fake 'frame' WS message", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await screen.findByText("Checkout flow");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toContain("/api/v1/browser-live/ws");
    expect(ws.url).toContain("target=target-1");

    ws.onopen?.(new Event("open"));
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "frame", data: "ZmFrZWpwZWc=", metadata: {} }),
      }),
    );

    const img = await screen.findByAltText("Live agent browser view");
    expect(img).toHaveAttribute("src", "data:image/jpeg;base64,ZmFrZWpwZWc=");
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("shows a status message sent by the server", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await screen.findByText("Checkout flow");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "status", message: "No open page in the agent browser yet." }),
      }),
    );

    expect(
      await screen.findByText("No open page in the agent browser yet."),
    ).toBeInTheDocument();
  });
});
