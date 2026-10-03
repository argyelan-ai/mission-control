import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserLiveView } from "../BrowserLiveView";
import { api } from "@/lib/api";
import type { BrowserLiveTarget } from "@/lib/types";
import de from "../../../../messages/de.json";

// Stream auth: the WS URL carries a single-use ticket (lib/streamTicket.ts).
vi.mock("@/lib/streamTicket", () => ({
  withStreamTicket: async (url: string) => `${url}${url.includes("?") ? "&" : "?"}ticket=test-ticket`,
}));

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
// network involved (view-only client never sends anything to Chromium; here
// we only assert on `sent`, the steering messages the UI sends the server).

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  url: string;
  readyState = 1;
  sent: string[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

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

  it("connects WITHOUT a click (PR A1: no more manual Connect button)", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toContain("/api/v1/browser-live/ws");
    expect(ws.url).toContain("ticket=test-ticket");
    expect(ws.url).not.toContain("token=");
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
  });

  it("shows a frame on the img after a fake 'frame' WS message", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];

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

  it("follows the active tab: a 'targets' push with a new activeId updates the picker", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));

    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({
          type: "targets",
          targets: [
            { id: "target-1", title: "Checkout flow", url: "https://example.com/checkout" },
            { id: "target-2", title: "New tab", url: "https://example.org" },
          ],
          activeId: "target-2",
          followedId: "target-2",
        }),
      }),
    );

    await waitFor(() => {
      const select = screen.getByLabelText("Browser page") as HTMLSelectElement;
      expect(select.value).toBe("target-2");
    });
  });

  it("manually selecting a page sends {select} and turns Follow off", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({
          type: "targets",
          targets: [
            { id: "target-1", title: "Checkout flow", url: "https://example.com/checkout" },
            { id: "target-2", title: "New tab", url: "https://example.org" },
          ],
          activeId: "target-1",
          followedId: "target-1",
        }),
      }),
    );

    const followBtn = await screen.findByRole("button", { name: "Follow" });
    expect(followBtn).toHaveAttribute("aria-pressed", "true");

    const select = screen.getByLabelText("Browser page") as HTMLSelectElement;
    await userEvent.selectOptions(select, "target-2");

    expect(ws.sent.some((m) => JSON.parse(m).select === "target-2")).toBe(true);
    expect(followBtn).toHaveAttribute("aria-pressed", "false");
  });

  it("shows a status message sent by the server, translated from its code", async () => {
    // The server sends a machine code, never free text (finding: it used to
    // send a hardcoded English sentence that showed up untranslated in the
    // German UI) — the client maps it through i18n.
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "status", code: "no_page" }),
      }),
    );

    expect(
      await screen.findByText("No open page in the agent browser yet."),
    ).toBeInTheDocument();
  });

  it("clears the status overlay once 'attached' arrives", async () => {
    // Finding: statusMessage was only cleared on reconnect, never on
    // 'attached'/the first 'frame' — the "No open page…" overlay used to sit
    // at the bottom of the viewport for the rest of the live session even
    // after a real page attached.
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));
    ws.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify({ type: "status", code: "no_page" }) }),
    );
    await screen.findByText("No open page in the agent browser yet.");

    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "attached", target: { id: "target-1", title: "Checkout flow" } }),
      }),
    );

    await waitFor(() =>
      expect(screen.queryByText("No open page in the agent browser yet.")).not.toBeInTheDocument(),
    );
  });

  it("clears the status overlay once the first frame arrives", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));
    ws.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify({ type: "status", code: "connect_error" }) }),
    );
    await screen.findByText(/Connecting to the agent browser/i);

    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "frame", data: "ZmFrZQ==", metadata: {} }),
      }),
    );

    await waitFor(() =>
      expect(screen.queryByText(/Connecting to the agent browser/i)).not.toBeInTheDocument(),
    );
  });

  it("clears the frozen last frame when every tab closes (empty 'targets' push)", async () => {
    // Finding: the no-pages branch used to `continue` before sending
    // anything, so the client's `hasFrame` stayed true forever and the
    // last screencast frame sat frozen on screen after the last tab died.
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({
          type: "targets",
          targets: [{ id: "target-1", title: "Checkout flow", url: "https://example.com/checkout" }],
          activeId: "target-1",
          followedId: "target-1",
        }),
      }),
    );
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "frame", data: "ZmFrZQ==", metadata: {} }),
      }),
    );
    await screen.findByAltText("Live agent browser view");

    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "targets", targets: [], activeId: null, followedId: null }),
      }),
    );

    await waitFor(() =>
      expect(screen.queryByAltText("Live agent browser view")).not.toBeInTheDocument(),
    );
  });

  it("a legitimately-empty 'targets' push clears the picker instead of falling back to the stale first-load list", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.(new Event("open"));

    // First push has the one target — the picker shows it.
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({
          type: "targets",
          targets: [{ id: "target-1", title: "Checkout flow", url: "https://example.com/checkout" }],
          activeId: "target-1",
          followedId: "target-1",
        }),
      }),
    );
    await screen.findByLabelText("Browser page");

    // Every tab closes — a real, empty push. Before the fix, `wsTargets.length
    // > 0 ? wsTargets : initialTargets` treated "[]" the same as "never
    // received a push yet" and fell back to the stale TARGETS from the
    // initial REST load, showing a tab that no longer exists.
    ws.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({ type: "targets", targets: [], activeId: null, followedId: null }),
      }),
    );

    // The component's own empty state ("not running") is what a genuinely
    // empty push renders — the old bug kept showing the stale picker with
    // "Checkout flow" selectable even though the tab was gone.
    expect(await screen.findByText(/Agent browser not running/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Browser page")).not.toBeInTheDocument();
  });

  it("reconnecting (e.g. the manual Reconnect button) carries the current follow state into the new WS URL", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(TARGETS);
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const first = FakeWebSocket.instances[0];
    first.onopen?.(new Event("open"));

    // Manually select a page — turns Follow off.
    first.onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({
          type: "targets",
          targets: [
            { id: "target-1", title: "Checkout flow", url: "https://example.com/checkout" },
            { id: "target-2", title: "New tab", url: "https://example.org" },
          ],
          activeId: "target-1",
          followedId: "target-1",
        }),
      }),
    );
    const select = (await screen.findByLabelText("Browser page")) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(2));
    await userEvent.selectOptions(select, "target-2");
    const followBtn = screen.getByRole("button", { name: "Follow" });
    expect(followBtn).toHaveAttribute("aria-pressed", "false");

    // The connection drops; the stream-ended Reconnect button appears.
    first.onclose?.(new CloseEvent("close", { code: 1006 }));
    const reconnectBtn = await screen.findByTitle("Reconnect");
    await userEvent.click(reconnectBtn);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(2));
    const second = FakeWebSocket.instances[1];
    // Before the fix this always opened with the server's default
    // (follow=1, no target), silently turning Follow back on server-side
    // even while the UI still showed a hand-picked tab with Follow off.
    expect(second.url).toContain("follow=0");
    expect(second.url).toContain("target=target-2");
  });

  it("has the German catalog for every string it renders", () => {
    // Sabotage check for the i18n namespace itself: if a key used by the
    // component is missing from messages/de.json this throws, since
    // browserLive.* must exist and have the same keys as English.
    const en = require("../../../../messages/en.json") as Record<string, unknown>;
    const browserLiveDe = (de as Record<string, unknown>).browserLive as Record<string, string>;
    const browserLiveEn = (en as Record<string, unknown>).browserLive as Record<string, string>;
    expect(browserLiveDe).toBeDefined();
    expect(Object.keys(browserLiveDe).sort()).toEqual(Object.keys(browserLiveEn).sort());

    // The nested "status" sub-catalog (machine codes → translated text) must
    // match too — the top-level key check above doesn't look inside it.
    const statusDe = browserLiveDe.status as unknown as Record<string, string>;
    const statusEn = browserLiveEn.status as unknown as Record<string, string>;
    expect(statusDe).toBeDefined();
    expect(Object.keys(statusDe).sort()).toEqual(Object.keys(statusEn).sort());
  });
});
