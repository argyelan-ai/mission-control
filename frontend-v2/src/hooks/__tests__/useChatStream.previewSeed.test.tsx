/** Hook-Level-Probe (Review 4ec932ff): Vorschau-Zeilen warten auf den Seed.
 *
 *  Fix 1 des PR (#595, "flackert extrem beim Oeffnen"): Bevor die Historie
 *  eingespeist ist, geht eine per `chat_event` eintreffende Preview NICHT
 *  sofort in den Reducer — sie wartet im Live-Puffer und wird erst mit dem
 *  Seed gespielt (seedSequence entscheidet dann ueber Leben/Tod).
 *
 *  Der Vertragstest in useChatStream.test.ts ("preview wartet auf den Seed")
 *  prueft nur seedSequence; diese Datei prueft die Puffer-Regel im Hook
 *  selbst (MockEventSource-Muster wie useTerminalRemountSignal.test.tsx). */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useChatStream } from "../useChatStream";
import type { ChatEvent } from "@/lib/chatTypes";

// Stream auth: each (re)connect fetches a single-use ticket (lib/streamTicket.ts).
// Stubbed here — this test is about the stream, not the ticket round-trip.
vi.mock("@/lib/streamTicket", () => ({
  withStreamTicket: async (url: string) => `${url}${url.includes("?") ? "&" : "?"}ticket=test-ticket`,
}));

const { promise: historyPromise, resolve: resolveHistory } =
  Promise.withResolvers<unknown>();

vi.mock("@/lib/api", () => ({
  getToken: vi.fn(() => "test-token"),
  api: {
    chat: {
      history: vi.fn(() => historyPromise),
      streamUrl: vi.fn(() => "/api/v1/agents/a1/stream"),
    },
  },
}));

class MockEventSource {
  static instances: MockEventSource[] = [];
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  onmessage: ((e: MessageEvent) => void) | null = null;

  constructor(url: string) {
    void url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(fn);
  }

  removeEventListener(type: string, fn: (e: MessageEvent) => void) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn);
  }

  fire(type: string, data: unknown) {
    const evt = new MessageEvent(type, { data: JSON.stringify(data) });
    if (type === "message" && this.onmessage) this.onmessage(evt);
    (this.listeners[type] ?? []).forEach((f) => f(evt));
  }

  close() {}
}

function chatEvent(ev: Partial<ChatEvent> & { kind: ChatEvent["kind"] }): ChatEvent {
  return { uuid: null, ts: "2026-08-15T00:00:00Z", ...ev } as ChatEvent;
}

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useChatStream — Preview wartet auf den Seed (kein Flackern beim Oeffnen)", () => {
  let originalES: typeof EventSource;

  beforeEach(() => {
    MockEventSource.instances = [];
    originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      MockEventSource as unknown as typeof EventSource;
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: () => "test-token",
        setItem: () => {},
        removeItem: () => {},
        clear: () => {},
        length: 0,
        key: () => null,
      },
    });
  });

  afterEach(() => {
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });

  function fireChatEvent(payload: unknown) {
    const es = MockEventSource.instances.at(-1);
    expect(es, "EventSource muss verbunden sein").toBeTruthy();
    act(() => es!.fire("chat_event", payload));
  }

  it("eine Preview vor dem Seed erscheint NICHT sofort — sie wartet auf die Historie", async () => {
    const { result } = renderHook(() => useChatStream("a1"), { wrapper });

    await waitFor(() => expect(MockEventSource.instances.length).toBe(1));

    fireChatEvent(
      chatEvent({ kind: "preview", ts: "2026-08-15T00:00:01Z", text: "lebt", source: "acp" }),
    );

    // Vor dem Seed: nichts im Vorschau-Fach — der Erst-Aufbau bleibt ruhig.
    expect(result.current.preview).toBeNull();

    // Historie traf ein: Seed entscheidet (keine assistant-message in der
    // Historie — die Preview darf leben).
    act(() =>
      resolveHistory({
        session: { sessionId: "s1" },
        events: [],
        hasMore: false,
        capabilities: null,
        subagentRuns: [],
      }),
    );
    await waitFor(() => expect(result.current.preview?.text).toBe("lebt"));
  });

  it("nach dem Seed erreicht eine Live-Preview den Reducer sofort (Vorschau waechst mit)", async () => {
    const { result } = renderHook(() => useChatStream("a1"), { wrapper });
    await waitFor(() => expect(MockEventSource.instances.length).toBe(1));

    act(() =>
      resolveHistory({
        session: { sessionId: "s1" },
        events: [],
        hasMore: false,
        capabilities: null,
        subagentRuns: [],
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));

    fireChatEvent(
      chatEvent({ kind: "preview", ts: "2026-08-15T00:00:02Z", text: "zeile 1", source: "acp" }),
    );
    expect(result.current.preview?.text).toBe("zeile 1");

    fireChatEvent(
      chatEvent({
        kind: "preview",
        ts: "2026-08-15T00:00:03Z",
        text: "zeile 1\nzeile 2",
        source: "acp",
      }),
    );
    expect(result.current.preview?.text).toBe("zeile 1\nzeile 2");
  });
});
