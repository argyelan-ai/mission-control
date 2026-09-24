/**
 * useTerminalRemountSignal — Phase 15 T3.6 vitest.
 *
 * Coverage:
 *   1. when a terminal_remount event arrives, the callback fires with the parsed payload
 *   2. unmount closes the EventSource
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { useTerminalRemountSignal } from "../useTerminalRemountSignal";

// Stream auth: each (re)connect fetches a single-use ticket (lib/streamTicket.ts).
// Stubbed here — this test is about the stream, not the ticket round-trip.
vi.mock("@/lib/streamTicket", () => ({
  withStreamTicket: async (url: string) => `${url}${url.includes("?") ? "&" : "?"}ticket=test-ticket`,
}));

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  onmessage: ((e: MessageEvent) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(fn);
  }

  removeEventListener(type: string, fn: (e: MessageEvent) => void) {
    const arr = this.listeners[type] ?? [];
    this.listeners[type] = arr.filter((f) => f !== fn);
  }

  fire(type: string, data: unknown) {
    const evt = new MessageEvent(type, { data: JSON.stringify(data) });
    if (type === "message" && this.onmessage) this.onmessage(evt);
    (this.listeners[type] ?? []).forEach((f) => f(evt));
  }

  close() {
    this.closed = true;
  }
}

function Probe({ agentId, onSignal }: { agentId: string | null; onSignal: (p: unknown) => void }) {
  useTerminalRemountSignal(agentId, onSignal);
  return null;
}

describe("useTerminalRemountSignal", () => {
  let originalES: typeof EventSource;

  beforeEach(() => {
    MockEventSource.instances = [];
    originalES = globalThis.EventSource;
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      MockEventSource as unknown as typeof EventSource;
    // jsdom localStorage stub — getToken() reads from localStorage and the
    // default jsdom localStorage doesn't expose getItem in this test setup.
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

  it("invokes the callback with parsed payload on terminal_remount event", async () => {
    const onSignal = vi.fn();
    render(<Probe agentId="agent-x" onSignal={onSignal} />);

    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    // Ticket in the URL, never the login token.
    expect(MockEventSource.instances[0].url).toBe(
      "/api/v1/agents/agent-x/terminal-events/stream?ticket=test-ticket",
    );
    MockEventSource.instances[0].fire("terminal_remount", {
      reason: "runtime_switched",
      image_changed: true,
      ts: 1234,
    });
    expect(onSignal).toHaveBeenCalledWith({
      reason: "runtime_switched",
      image_changed: true,
      ts: 1234,
    });
  });

  it("closes the EventSource on unmount", async () => {
    const { unmount } = render(<Probe agentId="agent-x" onSignal={() => {}} />);
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    const inst = MockEventSource.instances[0];
    unmount();
    expect(inst.closed).toBe(true);
  });
});
