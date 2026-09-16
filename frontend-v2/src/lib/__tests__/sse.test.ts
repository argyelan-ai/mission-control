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

  function Harness() {
    for (const c of CASES) {
      (sse[c.hook] as unknown as StreamHook)(() => {});
    }
    return null;
  }
  testingLibrary.render(react.createElement(Harness));
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
      expect(urls[i].startsWith(`${c.path}?token=`)).toBe(true);
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
      expect(urls[i].startsWith(`${ORIGIN}${c.path}?token=`)).toBe(true);
    });
  });
});
