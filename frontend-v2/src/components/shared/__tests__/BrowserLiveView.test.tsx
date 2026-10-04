import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserLiveView } from "../BrowserLiveView";
import { api } from "@/lib/api";
import type { BrowserLiveTarget, BrowserLiveTargetsResponse } from "@/lib/types";
import de from "../../../../messages/de.json";

// Stream auth: the WS URL carries a single-use ticket (lib/streamTicket.ts).
// `vi.fn()` (not a bare arrow function) so tests can assert HOW MANY TIMES a
// stream ticket was minted — each call is a real ticket spent server-side
// (review finding: a connect effect could fire twice on first mount, which
// wastes one and would be invisible if we only counted WebSocket objects,
// since the first connect's own cancellation guard silently drops the stale
// one before `new WebSocket(...)` is ever called).
const withStreamTicketMock = vi.fn(
  async (url: string) => `${url}${url.includes("?") ? "&" : "?"}ticket=test-ticket`,
);
vi.mock("@/lib/streamTicket", () => ({
  withStreamTicket: (url: string) => withStreamTicketMock(url),
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

// GET /api/v1/browser-live/targets now returns {targets, scopeUnavailable}
// (bauplan.md PR B1 round 2) — this wraps a plain target list the way every
// existing test in this file expects, with scoping reported as available by
// default (not under test here; see test_browser_live_agent_scope.py for
// the backend's own scopeUnavailable coverage).
function targetsResponse(targets: BrowserLiveTarget[]): BrowserLiveTargetsResponse {
  return { targets, scopeUnavailable: false };
}

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
    withStreamTicketMock.mockClear();
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse([]));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
    renderWithQuery(<BrowserLiveView />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toContain("/api/v1/browser-live/ws");
    expect(ws.url).toContain("ticket=test-ticket");
    expect(ws.url).not.toContain("token=");
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
  });

  it("shows a frame on the img after a fake 'frame' WS message", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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

    // The component's own empty state is what a genuinely empty push
    // renders — the old bug kept showing the stale picker with "Checkout
    // flow" selectable even though the tab was gone. The WS is still open
    // and connected (the browser itself is running, it just has zero open
    // tabs), so this is the less alarming "no open page" message, not
    // "not running" (finding, round 4: showing "not running" here used to
    // tell the operator the agent browser was down while it was fine).
    expect(
      await screen.findByText("No open page in the agent browser yet."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Browser page")).not.toBeInTheDocument();
  });

  it("reconnecting (e.g. the manual Reconnect button) carries the current follow state into the new WS URL", async () => {
    vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
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

  // ── bauplan.md PR B1: per-agent scoping ──────────────────────────────────

  describe("agent scoping (PR B1)", () => {
    function useMapLocalStorage() {
      const store = new Map<string, string>();
      const storage = {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => void store.set(k, v),
        removeItem: (k: string) => void store.delete(k),
        clear: () => store.clear(),
      };
      Object.defineProperty(globalThis, "localStorage", {
        value: storage, configurable: true, writable: true,
      });
      return store;
    }

    it("passes agentId through to both the REST fetch and the WS URL by default", async () => {
      useMapLocalStorage();
      const targetsSpy = vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      expect(targetsSpy).toHaveBeenCalledWith("agent-alpha");
      expect(FakeWebSocket.instances[0].url).toContain("agent_id=agent-alpha");
    });

    it("omits agent_id entirely when no agentId prop is given (unscoped panel, unchanged pre-B1 behaviour)", async () => {
      useMapLocalStorage();
      const targetsSpy = vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      expect(targetsSpy).toHaveBeenCalledWith(undefined);
      expect(FakeWebSocket.instances[0].url).not.toContain("agent_id");
    });

    it('shows "<name> has no open tab" with a "show all tabs" way out when scoped and empty', async () => {
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse([]));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));
      ws.onmessage?.({ data: JSON.stringify({ type: "targets", targets: [], activeId: null, followedId: null }) } as MessageEvent);

      expect(await screen.findByText("Alpha has no open tab right now.")).toBeInTheDocument();
      const button = await screen.findByText("Show all tabs");

      const targetsSpy = vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      await userEvent.click(button);

      // Clicking it reconnects WITHOUT the agent scope and persists the
      // choice for this agent (bauplan.md: "pro Gerät in localStorage").
      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(2));
      expect(FakeWebSocket.instances[1].url).not.toContain("agent_id");
      await waitFor(() => expect(targetsSpy).toHaveBeenCalledWith(undefined));
    });

    it("sabotage: a stale truthy showAllTabs in localStorage must unscope the panel on mount", async () => {
      // Proves the localStorage read is actually wired up, not just the
      // button's own setShowAllTabs call — flips the EXPECTED state of the
      // first test in this block if the initial-state read were removed.
      const store = useMapLocalStorage();
      store.set("mc.browserLive.showAllTabs.agent-alpha", "1");
      const targetsSpy = vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      expect(targetsSpy).toHaveBeenCalledWith(undefined);
      expect(FakeWebSocket.instances[0].url).not.toContain("agent_id");
    });

    it("the scope toggle button switches back to this agent's own tabs and updates localStorage", async () => {
      const store = useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);
      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));

      const toggle = await screen.findByText("Only Alpha");
      await userEvent.click(toggle);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(2));
      expect(FakeWebSocket.instances[1].url).not.toContain("agent_id");
      expect(store.get("mc.browserLive.showAllTabs.agent-alpha")).toBe("1");

      await userEvent.click(await screen.findByText("All tabs"));
      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(3));
      expect(FakeWebSocket.instances[2].url).toContain("agent_id=agent-alpha");
      expect(store.has("mc.browserLive.showAllTabs.agent-alpha")).toBe(false);
    });

    it("mints exactly ONE stream ticket on first mount with an agentId (no double-connect)", async () => {
      // Regression guard (review finding): the agentId effect used to bump
      // `connectKey` on every run including the very first one. The connect
      // effect's own cancellation guard means that never produced a SECOND
      // `WebSocket` object (the stale one is dropped before `new
      // WebSocket(...)` runs) — but it did mean `browserLiveWsUrl()`, and so
      // `withStreamTicket()`, ran twice, burning a real stream ticket for
      // nothing on every single panel open. Sabotage: dropping the
      // `didMountAgentEffect` ref guard must flip this to 2.
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(1));
      // Give any extra, erroneous connect effect a chance to fire too.
      await new Promise((r) => setTimeout(r, 50));
      expect(withStreamTicketMock).toHaveBeenCalledTimes(1);
      expect(FakeWebSocket.instances.length).toBe(1);
    });

    it('shows the scope-unavailable hint and switches the toggle state when the gateway is down', async () => {
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue({ targets: TARGETS, scopeUnavailable: true });
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));
      ws.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "frame", data: "ZmFrZQ==", metadata: {} }),
        }),
      );

      expect(
        await screen.findByText("Showing all tabs: agent attribution unavailable"),
      ).toBeInTheDocument();
    });

    it("the WS becoming authoritative clears a stale REST scope-unavailable hint (medium finding, round 5)", async () => {
      // The REST /targets call happened to land during a brief gateway
      // outage (scopeUnavailable: true), but the WS then connects with the
      // gateway back up and says so explicitly. The amber hint must go
      // away once the WS has spoken — it must never be stuck for the rest
      // of the session just because the one-off REST snapshot said so.
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue({ targets: TARGETS, scopeUnavailable: true });
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));

      expect(
        await screen.findByText("Showing all tabs: agent attribution unavailable"),
      ).toBeInTheDocument();

      ws.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "status", code: "scope_unavailable", active: false }),
        }),
      );

      await waitFor(() =>
        expect(
          screen.queryByText("Showing all tabs: agent attribution unavailable"),
        ).not.toBeInTheDocument(),
      );
    });

    // ── unassigned fallback (live finding 04.10.2026) ─────────────────────
    // Every tab came back unattributed, and the scoped panel said "Alpha has
    // no open tab right now" while Alpha had one open. When the agent owns
    // no tab but tabs assigned to NO agent exist, the server sends every tab
    // (unassigned ones flagged) plus an `unassigned_fallback` status; the
    // panel must show them with a clear hint, never the empty state.

    const FALLBACK_HINT = "No tab is assigned to Alpha — showing the tabs that aren't assigned to any agent.";

    it("shows the unassigned tabs with a 'not assigned' hint instead of claiming the agent has no tab", async () => {
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue({
        targets: [{ id: "free", title: "Example Domain", url: "https://example.org/", unassigned: true }],
        scopeUnavailable: false,
        unassignedFallback: true,
        unassignedCount: 1,
      });
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));
      ws.onmessage?.({ data: JSON.stringify({ type: "status", code: "scope_unavailable", active: false }) } as MessageEvent);
      ws.onmessage?.({ data: JSON.stringify({ type: "status", code: "unassigned_fallback", active: true, count: 1 }) } as MessageEvent);
      ws.onmessage?.({ data: JSON.stringify({
        type: "targets",
        targets: [{ id: "free", title: "Example Domain", url: "https://example.org/", unassigned: true }],
        activeId: "free", followedId: "free",
      }) } as MessageEvent);

      expect(await screen.findByText(FALLBACK_HINT)).toBeInTheDocument();
      expect(screen.queryByText("Alpha has no open tab right now.")).not.toBeInTheDocument();
      // The picker marks which tab nobody is assigned to.
      expect(screen.getByRole("option", { name: "Example Domain · not assigned" })).toBeInTheDocument();
    });

    it("the WS ending the fallback (agent now owns a tab) removes the hint and the tag", async () => {
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));
      ws.onmessage?.({ data: JSON.stringify({ type: "status", code: "unassigned_fallback", active: true, count: 1 }) } as MessageEvent);
      ws.onmessage?.({ data: JSON.stringify({
        type: "targets", targets: [{ ...TARGETS[0], unassigned: true }], activeId: "target-1", followedId: "target-1",
      }) } as MessageEvent);
      expect(await screen.findByText(FALLBACK_HINT)).toBeInTheDocument();

      ws.onmessage?.({ data: JSON.stringify({ type: "status", code: "unassigned_fallback", active: false, count: 0 }) } as MessageEvent);
      ws.onmessage?.({ data: JSON.stringify({
        type: "targets", targets: TARGETS, activeId: "target-1", followedId: "target-1",
      }) } as MessageEvent);

      await waitFor(() => expect(screen.queryByText(FALLBACK_HINT)).not.toBeInTheDocument());
      expect(screen.getByRole("option", { name: "Checkout flow" })).toBeInTheDocument();
    });

    it("a live connection problem wins over the unassigned hint in the shared banner slot", async () => {
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));
      ws.onmessage?.({ data: JSON.stringify({ type: "status", code: "unassigned_fallback", active: true, count: 1 }) } as MessageEvent);
      ws.onmessage?.({ data: JSON.stringify({
        type: "targets", targets: [{ ...TARGETS[0], unassigned: true }], activeId: "target-1", followedId: "target-1",
      }) } as MessageEvent);
      expect(await screen.findByText(FALLBACK_HINT)).toBeInTheDocument();

      ws.onmessage?.({ data: JSON.stringify({ type: "status", code: "connect_error" }) } as MessageEvent);
      expect(await screen.findByText("Connecting to the agent browser…")).toBeInTheDocument();
      expect(screen.queryByText(FALLBACK_HINT)).not.toBeInTheDocument();

      // Once a frame proves the stream is fine again, the hint is back.
      ws.onmessage?.({ data: JSON.stringify({ type: "frame", data: "ZmFrZQ==", metadata: {} }) } as MessageEvent);
      expect(await screen.findByText(FALLBACK_HINT)).toBeInTheDocument();
    });

    it("sabotage: no fallback hint when the server never reports one", async () => {
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));
      ws.onmessage?.({ data: JSON.stringify({ type: "frame", data: "ZmFrZQ==", metadata: {} }) } as MessageEvent);
      await screen.findByAltText("Live agent browser view");
      expect(screen.queryByText(/No tab is assigned to Alpha/)).not.toBeInTheDocument();
    });

    it("keeps the phone toolbar on one line: the picker shrinks and the filter chip has a compact label", async () => {
      // 393 px finding: the fullscreen button dropped to a second toolbar
      // line. The chip shows just the agent name below `sm` (full "Only
      // <name>" from `sm` up) and the picker takes whatever width is left.
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      // Accessible name = the full visible label's key, so it contains the
      // visible text in every language (WCAG 2.5.3) — German "Nur Alpha".
      const chip = await screen.findByRole("button", { name: "Only Alpha" });
      const compact = chip.querySelector("[data-chip-label='compact']");
      const full = chip.querySelector("[data-chip-label='full']");
      expect(compact?.textContent).toBe("Alpha");
      expect(compact?.className).toMatch(/\bsm:hidden\b/);
      expect(compact?.className).toMatch(/\btruncate\b/);
      expect(full?.textContent).toBe("Only Alpha");
      expect(full?.className).toMatch(/\bhidden\b.*\bsm:inline\b/);
      const picker = screen.getByLabelText("Browser page");
      expect(picker.className).toMatch(/\bmin-w-0\b/);
      expect(picker.className).toMatch(/\bflex-1\b/);
    });

    it("sabotage: the scope-unavailable hint never shows when the REST call reports scoping as available", async () => {
      useMapLocalStorage();
      vi.spyOn(api.browserLive, "targets").mockResolvedValue(targetsResponse(TARGETS));
      renderWithQuery(<BrowserLiveView agentId="agent-alpha" agentName="Alpha" />);

      await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      const ws = FakeWebSocket.instances[0];
      ws.onopen?.(new Event("open"));
      ws.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "frame", data: "ZmFrZQ==", metadata: {} }),
        }),
      );

      await screen.findByAltText("Live agent browser view");
      expect(
        screen.queryByText("Showing all tabs: agent attribution unavailable"),
      ).not.toBeInTheDocument();
    });
  });
});
