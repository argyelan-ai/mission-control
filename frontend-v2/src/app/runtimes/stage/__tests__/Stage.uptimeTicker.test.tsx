/**
 * Stage — 60s-Ticker für die Laufzeit-Ecke (Review-Nachtrag zu #446).
 *
 * TanStack Query's `structuralSharing` hält die Objekt-Referenz eines Polls
 * stabil, solange sich sein Inhalt nicht ändert — die Karten-eigene
 * Puls-Abfrage (`refetchInterval: 5_000`) refetcht zwar ständig, liefert im
 * Leerlauf aber denselben Inhalt und löst darum KEIN Re-Render aus. Ohne
 * einen eigenen Ticker bliebe "up X" stehen, bis zufällig eine andere Query
 * einen echten Wertwechsel bringt. Dieser Test hält ALLE anderen Queries
 * absichtlich exakt gleich über die Zeit (keine neuen Werte), damit ein
 * grün laufender Test wirklich den `useEffect`/`setInterval`-Ticker beweist
 * und nicht zufällig einen anderen Re-Render-Pfad trifft.
 *
 * Eigene Datei statt ein Fall in Stage.test.tsx: Fake-Timer + Framer
 * Motion's AnimatePresence/rAF vertragen sich schlecht (gleiches Problem wie
 * ToastRenderer.test.tsx) — der lokale framer-motion-Mock hier (Props weg,
 * kein Exit) soll nicht die anderen 11 echten-Timer-Fälle in Stage.test.tsx
 * mitbetreffen, darum ein separates Modul.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Stage } from "../Stage";
import { api } from "@/lib/api";
import type { Host, Runtime } from "@/lib/types";

vi.mock("framer-motion", () => ({
  motion: new Proxy(
    {},
    {
      get:
        (_target, tag: string) =>
        ({ children, layout: _layout, initial: _initial, animate: _animate, exit: _exit, transition: _transition, ...rest }: Record<string, unknown>) =>
          React.createElement(tag, rest, children as React.ReactNode),
    }
  ),
  AnimatePresence: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

const mockStore = vi.hoisted(() => ({
  state: { currentUser: { id: "u1", email: "a@b.c", name: "Admin", role: "admin" } as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function makeHost(over: Partial<Host>): Host {
  return {
    id: over.slug ?? "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

function makeRuntime(over: Partial<Runtime>): Runtime {
  return {
    id: over.slug ?? "rt", slug: "rt", display_name: "RT",
    runtime_type: "vllm_docker", provider: "vllm",
    endpoint: "http://192.0.2.10:8001/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "ready",
    ...over,
  };
}

// `findBy*`/`waitFor` poll via real setTimeout under the hood — frozen solid
// by `vi.useFakeTimers()` unless the test also advances that specific timer,
// which nothing here does. Flushing a few microtask turns (query fetch →
// setQueryData → React's microtask-based batched update) settles the
// initial paint without needing timers at all — plain `getByText` after this
// is synchronous and timer-independent.
async function flush() {
  for (let i = 0; i < 6; i++) {
    // eslint-disable-next-line no-await-in-loop
    await Promise.resolve();
  }
}

describe("Stage — 60s uptime ticker", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.restoreAllMocks();
    // Constant, never-changing payloads — structural sharing keeps the same
    // object reference across every refetch, so the ONLY thing that can
    // move the displayed text forward is the ticker itself.
    vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40 });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt", count: 0, agents: [] });
  });

  afterEach(() => {
    act(() => {
      vi.runOnlyPendingTimers();
    });
    vi.useRealTimers();
  });

  function renderStage(sinceMinutesAgo: number) {
    const since = new Date(Date.now() - sinceMinutesAgo * 60_000).toISOString();
    const rt = makeRuntime({ display_name: "Qwen3.8 27B", host: { id: "spark", slug: "spark", display_name: "DGX Spark" } });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <Stage
          runtime={rt}
          members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head" }]}
          live={{ reachable: true, served_model: "qwen38", latency_ms: 7, last_probe_at: "", consecutive_failures: 0, drift: false, serving_since: since }}
          onOpenCockpit={() => {}}
        />
      </QueryClientProvider>
    );
  }

  it("advances 'up X min' after 60s with no other query producing a new value", async () => {
    renderStage(4);
    await act(flush);
    expect(screen.getByText("up 4 min")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(screen.getByText("up 5 min")).toBeInTheDocument();
    expect(screen.queryByText("up 4 min")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(screen.getByText("up 6 min")).toBeInTheDocument();
  });

  it("clears the interval on unmount (no state update after unmount)", async () => {
    const { unmount } = renderStage(2);
    await act(flush);
    expect(screen.getByText("up 2 min")).toBeInTheDocument();

    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    // React's "state update on an unmounted component" warning is exactly
    // what a missing cleanup would produce here.
    expect(errorSpy).not.toHaveBeenCalledWith(expect.stringContaining("unmounted component"));
    errorSpy.mockRestore();
  });

  it("does not tick once the runtime stops serving (interval torn down)", async () => {
    const since = new Date(Date.now() - 4 * 60_000).toISOString();
    const rt = makeRuntime({ display_name: "Qwen3.8 27B", state: "failed", host: { id: "spark", slug: "spark", display_name: "DGX Spark" } });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <Stage
          runtime={rt}
          members={[{ host: makeHost({ slug: "spark", display_name: "DGX Spark" }), role: "head" }]}
          live={{ reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 5, drift: false, serving_since: since }}
          onOpenCockpit={() => {}}
        />
      </QueryClientProvider>
    );
    await act(flush);
    // HONESTY RULE: failed status never shows uptime, ticker doesn't even start.
    expect(screen.getByText("unreachable")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(screen.getByText("unreachable")).toBeInTheDocument();
    expect(screen.queryByText(/up \d/)).not.toBeInTheDocument();
  });
});
