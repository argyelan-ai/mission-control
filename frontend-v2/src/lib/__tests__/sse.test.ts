import { describe, it, expect, afterEach, vi } from "vitest";

/**
 * Operator-Befund 15.09.2026 (Handy, 390×430 px): der "Listening"-Punkt im
 * Thread-Panel und die Activity-Kachel der Startseite blieben stumm, die
 * Konsole zeigte ERR_CONNECTION_REFUSED auf http://localhost:8000 — 144
 * Fehlschlaege in ~7 Minuten, weil jeder Fehler sofort einen Reconnect
 * ausloest. Ursache: useAgentStream/useActivityStream/useApprovalStream
 * bauten ihre URL gegen einen hartkodierten localhost-Fallback statt gegen
 * die Origin, von der die App ausgeliefert wird. Der Chat selbst ueberlebte
 * nur, weil das Polling-Fallback (GET /thread?since_seq=N) einspringt.
 *
 * Geprueft wird die einzige nach aussen sichtbare Zusage: die URL, die im
 * EventSource landet.
 *   - ohne NEXT_PUBLIC_API_URL (Deployment-Fall) → same-origin, relativ
 *   - mit NEXT_PUBLIC_API_URL → genau diese Origin
 *
 * Security finding 24.09.2026: the URL used to carry the login JWT as
 * `?token=` and the reverse proxy logged it. The URL now carries a single-use
 * stream ticket, fetched per (re)connect; the JWT only travels in the
 * Authorization header of the ticket POST.
 */

type StreamHook = (
  onEvent: (event: string, data: Record<string, unknown>) => void,
) => void;

const CASES = [
  { hook: "useAgentStream", path: "/api/v1/agents/stream" },
  { hook: "useActivityStream", path: "/api/v1/activity/stream" },
  { hook: "useApprovalStream", path: "/api/v1/approvals/stream" },
] as const;

const ORIGIN = "https://mc.example.test";

class MockEventSource {
  static CLOSED = 2;
  static instances: MockEventSource[] = [];
  url: string;
  readyState = 0;
  onerror: ((e: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }
  close() {
    this.readyState = MockEventSource.CLOSED;
  }
  addEventListener() {}
  removeEventListener() {}
}

const JWT = "header.payload.signature";
let ticketCounter = 0;
const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => {
  ticketCounter += 1;
  return new Response(JSON.stringify({ ticket: `tkt-${ticketCounter}`, expires_in: 60 }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;

/**
 * Mountet alle drei Stream-Hooks und liefert die URLs ihrer EventSources.
 *
 * Dynamischer Import mit Ansage: `BASE_URL`/`sseUrls` werden beim Import aus
 * `NEXT_PUBLIC_API_URL` gebacken, der Fall "Env gesetzt" laesst sich daher nur
 * mit einem frischen Modul-Graph abbilden. React und die Testing-Library
 * kommen aus demselben frischen Graph — sonst rendert ein ReactDOM auf
 * Hooks einer zweiten React-Kopie ("Invalid hook call").
 */
async function mountStreams(apiUrl: string | undefined): Promise<string[]> {
  vi.resetModules();
  if (apiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
  else process.env.NEXT_PUBLIC_API_URL = apiUrl;

  const [react, testingLibrary, sse] = await Promise.all([
    import("react"),
    import("@testing-library/react"),
    import("@/lib/sse"),
  ]);

  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
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

  function Harness() {
    for (const c of CASES) {
      (sse[c.hook] as unknown as StreamHook)(() => {});
    }
    return null;
  }
  testingLibrary.render(react.createElement(Harness));
  // Each stream opens only after its ticket fetch resolved.
  await testingLibrary.waitFor(() =>
    expect(MockEventSource.instances).toHaveLength(CASES.length),
  );
  return MockEventSource.instances.map((es) => es.url);
}

afterEach(() => {
  vi.unstubAllGlobals();
  if (originalApiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
  else process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
});

describe("Stream-URLs der Chat-SSE", () => {
  it("bleibt same-origin, wenn keine API-URL konfiguriert ist", async () => {
    const urls = await mountStreams(undefined);

    expect(urls).toHaveLength(CASES.length);
    CASES.forEach((c, i) => {
      expect(urls[i].startsWith(`${c.path}?ticket=tkt-`)).toBe(true);
      expect(urls[i]).not.toContain("token=");
      expect(urls[i]).not.toContain(JWT);
      // Der Fehler von 15.09.: absolute URL auf einen fremden Host.
      expect(urls[i]).not.toContain("localhost:8000");
      expect(new URL(urls[i], window.location.href).origin).toBe(
        window.location.origin,
      );
    });
  });

  it("nutzt die konfigurierte API-Origin, wenn gesetzt", async () => {
    const urls = await mountStreams(ORIGIN);

    expect(urls).toHaveLength(CASES.length);
    CASES.forEach((c, i) => {
      expect(urls[i].startsWith(`${ORIGIN}${c.path}?ticket=tkt-`)).toBe(true);
      expect(urls[i]).not.toContain(JWT);
    });
  });

  it("holt pro Stream ein Ticket — JWT nur im Authorization-Header", async () => {
    await mountStreams(ORIGIN);

    expect(fetchMock).toHaveBeenCalledTimes(CASES.length);
    const paths = fetchMock.mock.calls.map(([url, init]) => {
      expect(url).toBe(`${ORIGIN}/api/v1/auth/stream-ticket`);
      expect(init?.method).toBe("POST");
      expect((init?.headers as Record<string, string>).Authorization).toBe(`Bearer ${JWT}`);
      return JSON.parse(String(init?.body)).path;
    });
    expect(paths.sort()).toEqual(CASES.map((c) => c.path).sort());
  });

  it("holt beim Reconnect ein NEUES Ticket (Tickets sind einmalig)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await mountStreams(undefined);
      const first = MockEventSource.instances[0];
      const firstTicket = new URL(first.url, window.location.href).searchParams.get("ticket");

      // Stream drops (server restart, spent ticket on a native retry, iOS).
      first.readyState = MockEventSource.CLOSED;
      first.onerror?.(new Event("error"));
      await vi.advanceTimersByTimeAsync(1_100);

      const { waitFor } = await import("@testing-library/react");
      await waitFor(() => expect(MockEventSource.instances.length).toBe(CASES.length + 1));
      const again = MockEventSource.instances.at(-1)!;
      const newTicket = new URL(again.url, window.location.href).searchParams.get("ticket");
      expect(again.url.startsWith(`${CASES[0].path}?ticket=`)).toBe(true);
      expect(newTicket).not.toBe(firstTicket);
      expect(fetchMock).toHaveBeenCalledTimes(CASES.length + 1);
    } finally {
      vi.useRealTimers();
    }
  });
});
