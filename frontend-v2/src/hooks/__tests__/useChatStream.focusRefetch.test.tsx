/** Der Vertrag der Historien-Abfrage: WANN wird /chat/history neu geladen?
 *
 *  Operator-Befund 17./18.09.2026: "es laedt alles neu". Messung auf dem
 *  echten Pfad (SessionsPage → ChatView → useChatStream) ergab fuenf Abrufe
 *  pro Sitzung, davon zwei reine Fokus-Refetches ohne jede Bedienung — die
 *  Abfrage erbte die globalen Vorgaben aus providers.tsx (staleTime 5 s,
 *  refetchOnWindowFocus true) und trug den GANZEN Verlauf.
 *
 *  Geprueft wird darum genau zweierlei, auf dem echten Pfad (echter
 *  useChatStream-Hook, echtes useSSE, echter QueryClient — kein Nachbau):
 *    1. Ein echter Fokuswechsel laedt NICHT neu, auch wenn die Abfrage
 *       laengst veraltet ist. (Der QueryClient unten setzt dafuer absichtlich
 *       `staleTime: 0` — schaerfer als die 5 s aus providers.tsx: wenn schon
 *       eine sofort veraltete Abfrage beim Fokus stillhaelt, haelt die echte
 *       erst recht.)
 *    2. Ein ECHTER Wiederaufbau des Live-Stroms laedt genau EINMAL neu — das
 *       ist der einzige Grund, der ihn rechtfertigt (der Tailer steigt am
 *       Dateiende ein und spielt Versaeumtes nicht nach), und der erste Mount
 *       darf ihn nicht ausloesen.
 *
 *  MockEventSource-Muster wie useTerminalRemountSignal.test.tsx. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useChatStream } from "../useChatStream";

const mocks = vi.hoisted(() => ({
  history: vi.fn(async (agentId: string) => ({
    events: [],
    session: { sessionId: `sess-${agentId}`, live: true, startedAt: null },
    hasMore: false,
    subagentRuns: [],
    capabilities: null,
  })),
}));

vi.mock("@/lib/api", () => ({
  getToken: vi.fn(() => "test-token"),
  api: {
    chat: {
      history: mocks.history,
      streamUrl: vi.fn((agentId: string) => `/api/v1/agents/${agentId}/chat/stream`),
    },
  },
}));

class MockEventSource {
  static CLOSED = 2;
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: MockEventSource[] = [];
  url: string;
  readyState = MockEventSource.OPEN;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener() {}
  removeEventListener() {}

  close() {
    this.readyState = MockEventSource.CLOSED;
  }

  /** Modelliert den iOS-Fall: die Verbindung ist tot, ohne dass der Browser
   *  sie neu aufbaut — genau das, was useSSE's Backoff auffangen muss. */
  kill() {
    this.readyState = MockEventSource.CLOSED;
    this.onerror?.(new Event("error"));
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  // Absichtlich sofort veraltet: siehe Dateikommentar (1).
  const client = new QueryClient({
    defaultOptions: {
      queries: { staleTime: 0, gcTime: 5 * 60_000, refetchOnWindowFocus: true, retry: 0 },
    },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** Ein echter Fokuswechsel: TanStack's focusManager hoert auf `window` und
 *  meldet nur den WECHSEL nach sichtbar, also erst verstecken, dann zeigen. */
function refocus() {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  window.dispatchEvent(new Event("visibilitychange"));
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  window.dispatchEvent(new Event("visibilitychange"));
}

function sleep(ms: number) {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
}

describe("useChatStream — Historien-Abruf nur bei echtem Anlass", () => {
  let originalES: typeof EventSource;

  beforeEach(() => {
    MockEventSource.instances = [];
    mocks.history.mockClear();
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
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  });

  afterEach(() => {
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalES;
  });

  it("laedt beim Fokuswechsel NICHT neu, auch wenn die Abfrage veraltet ist", async () => {
    renderHook(() => useChatStream("a1"), { wrapper });

    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(1));
    expect(MockEventSource.instances.length).toBe(1);

    refocus();
    refocus();
    await sleep(150);

    expect(mocks.history).toHaveBeenCalledTimes(1);
    // Und der Fokus hat auch keinen zweiten Strom aufgemacht.
    expect(MockEventSource.instances.length).toBe(1);
  });

  it("laedt nach einem echten Strom-Wiederaufbau genau einmal neu", async () => {
    renderHook(() => useChatStream("a1"), { wrapper });

    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(1));
    const first = MockEventSource.instances.at(-1)!;
    expect(first).toBeTruthy();

    // Der erste Mount allein darf den Refetch NICHT ausloesen.
    await sleep(50);
    expect(mocks.history).toHaveBeenCalledTimes(1);

    // Verbindung stirbt (iOS/Hintergrund) → Backoff-Timer → neuer Strom.
    act(() => first.kill());
    expect(mocks.history).toHaveBeenCalledTimes(1); // noch im Backoff

    await waitFor(() => expect(MockEventSource.instances.length).toBe(2), { timeout: 4_000 });
    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(2));
    // Genau einmal — kein zweiter Refetch aus dem Wiederaufbau heraus.
    await sleep(200);
    expect(mocks.history).toHaveBeenCalledTimes(2);
  });

  /* Der Fall, den `staleTime: Infinity` sonst dauerhaft falsch macht: Beim
     VERLASSEN eines Agenten wird dessen Tailer abgeraeumt (release cancelt die
     Aufgabe), beim Zurueckkommen frisch am DATEIENDE aufgesetzt (acquire) —
     alles dazwischen Geschriebene kommt nie ueber den Strom. Ohne einen Abruf
     beim Wiedereintritt zeigte der Agent seinen Verlauf von VOR dem Wechsel,
     obwohl seither ein ganzes Gespraech lief.

     Der Wechsel ist ein UNMOUNT: `SessionsPage` haengt `key={selected.id}` an
     ChatView, damit ein Agentenwechsel den Strom sauber neu aufbaut. Nur
     CSS-Verstecken (Zurueck-Knopf auf dem Handy) laesst den Strom stehen und
     darf deshalb auch nicht neu laden — dafuer ist die Fokus-Regel unten da. */
  it("laedt beim Zurueckkehren zu einem Agenten genau einmal neu", async () => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { staleTime: 0, gcTime: 5 * 60_000, refetchOnWindowFocus: true, retry: 0 },
      },
    });
    const own = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const a1 = renderHook(() => useChatStream("a1"), { wrapper: own });
    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(1));

    // Wechsel auf einen anderen Agenten: ChatView wird ab- und neu aufgebaut.
    a1.unmount();
    const a2 = renderHook(() => useChatStream("a2"), { wrapper: own });
    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(2));

    // ... und zurueck. Der Cache von "a1" lebt noch (gleicher QueryClient,
    // gcTime 5 min) — genau darum geht es: er darf NICHT einfach bedient werden.
    a2.unmount();
    const a1b = renderHook(() => useChatStream("a1"), { wrapper: own });
    await waitFor(() => expect(mocks.history).toHaveBeenCalledTimes(3));

    // Genau einmal je Wiedereintritt.
    await sleep(200);
    expect(mocks.history).toHaveBeenCalledTimes(3);
    expect(mocks.history.mock.calls.map((c) => c[0])).toEqual(["a1", "a2", "a1"]);
    a1b.unmount();
  });
});
