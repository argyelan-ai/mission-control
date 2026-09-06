/**
 * FleetStage — Cockpit-Öffnen/-Schliessen-Integration (Review #440 Fund 2,
 * 06.09.2026): das Zahnrad, das das Cockpit öffnet, muss beim Schliessen den
 * Fokus zurückbekommen (Spec §4 "Fokus-Rückgabe"). Getestet über den
 * einfachsten Pfad (`AsleepBox` in `sleepingGroups` — keine Bühne/kein
 * Duo-Umschalter/kein Leer-Zustand-Sonderfall nötig) — `openCockpit()`/
 * `closeCockpit()` leben nur in `FleetStage.tsx`, ein reiner `BoxCockpit`-
 * Test kann das Zusammenspiel mit dem auslösenden Element nicht abdecken.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FleetStage } from "../FleetStage";
import { api } from "@/lib/api";
import type { Host, HostMetrics, Runtime } from "@/lib/types";

const mockStore = vi.hoisted(() => ({
  state: { currentUser: { id: "u1", email: "a@b.c", name: "Admin", role: "admin" } as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function makeHost(over: Partial<Host> = {}): Host {
  return {
    id: "spark", slug: "spark", display_name: "DGX Spark", kind: "ssh",
    ssh_host: "192.0.2.10", ssh_user: null, ssh_key_path: null, ssh_credential_id: null,
    role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

function makeRuntime(over: Partial<Runtime> = {}): Runtime {
  return {
    id: "rt-1", slug: "spark-power", display_name: "Qwen3.8 Flash Next",
    runtime_type: "unsloth_porsche", provider: "vllm",
    endpoint: "http://192.0.2.10:8000/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "stopped", power_managed: true,
    ...over,
  };
}

const metrics: HostMetrics = { reachable: true, gpu_util_pct: 1, vram_used_mb: 100, vram_total_mb: 1000, gpu_temp_c: 40 };

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.nodes, "devices").mockResolvedValue([]);
  vi.spyOn(api.hosts, "metrics").mockResolvedValue(metrics);
  vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
  vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
  vi.spyOn(api.hosts, "metricsHistory").mockResolvedValue({ points: [] });
  vi.spyOn(api.hosts, "autostart").mockResolvedValue({
    host_id: "spark", enabled: false, recipe_slug: null, recipe_display_name: null,
    role: null, via_head: null, last_attempt_at: null, last_result: null,
  });
  vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "spark", count: 0, agents: [] });
  vi.spyOn(api.localRegistry, "list").mockResolvedValue({ recipes: [], total: 0, sources: [] });
});

describe("FleetStage — cockpit open/close focus return", () => {
  it("closing the cockpit returns focus to the gear button that opened it", async () => {
    renderWithQuery(
      <FleetStage stageGroups={[]} sleepingGroups={[{ host: makeHost(), runtimes: [makeRuntime()] }]} onOpen={() => {}} />
    );

    const gear = await screen.findByLabelText("Open cockpit");
    gear.focus();
    expect(document.activeElement).toBe(gear);

    await act(async () => { gear.click(); });
    const closeBtn = await screen.findByTestId("cockpit-close");
    await waitFor(() => expect(document.activeElement).toBe(closeBtn));

    await act(async () => { closeBtn.click(); });
    await waitFor(() => expect(screen.queryByTestId("box-cockpit")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(gear);
  });

  it("Escape also returns focus to the triggering gear button", async () => {
    renderWithQuery(
      <FleetStage stageGroups={[]} sleepingGroups={[{ host: makeHost(), runtimes: [makeRuntime()] }]} onOpen={() => {}} />
    );

    const gear = await screen.findByLabelText("Open cockpit");
    gear.focus();
    await act(async () => { gear.click(); });
    await screen.findByTestId("box-cockpit");

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    await waitFor(() => expect(screen.queryByTestId("box-cockpit")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(gear);
  });
});
