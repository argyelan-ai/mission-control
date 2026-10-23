/**
 * Round-7 re-check (L5, r1 N1): the second wake path — "Wake" in the runtime
 * detail panel (POST /runtimes/{id}/wake) — says what the box card says.
 *
 * - 503 (the wake helper on the Mac sends no heartbeat): the helper sentence,
 *   not a bare "Wake failed.";
 * - a sent wake: the translated "needs 1–2 min" line, not the backend's raw
 *   German message in an English UI;
 * - a viewer gets no Wake (the route is operator-only, like the host route).
 *
 * Nothing is woken: every API call is mocked.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeDetailPanel } from "../RuntimeDetailPanel";
import { api } from "@/lib/api";
import type { Runtime } from "@/lib/types";

const mockStore = vi.hoisted(() => ({
  state: { currentUser: { id: "u1", email: "a@b.c", name: "Op", role: "operator" } as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const sleepingRuntime: Runtime = {
  id: "wake-rt", slug: "wake-rt", display_name: "Wake RT",
  runtime_type: "flask_wol", provider: "unsloth",
  endpoint: "http://192.0.2.20:8000/v1", healthcheck_path: "/v1/models",
  container_name: null, role_tags: [], supports_tools: true,
  supports_reasoning: false, supports_streaming: true,
  preferred_context_len: 8192, max_context_len: 32768,
  gpu_profile: "default", memory_notes: "", startup_notes: "",
  ui_order: 0, enabled: true, state: "stopped",
  power_managed: true, container_status: "asleep",
} as Runtime;

describe("RuntimeDetailPanel — Wake", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Op", role: "operator" };
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "wake-rt", count: 0, agents: [] });
  });

  it("503: says the wake helper on the Mac is not running", async () => {
    vi.spyOn(api.runtimes, "wake").mockRejectedValue(
      // The real body: the 503 detail carries the message and the reason.
      new Error('API 503: {"detail":{"message":"Weck-Helfer auf dem Mac läuft nicht (kein Lebenszeichen). Wake RT wurde nicht geweckt.","reason":"no_heartbeat"}}'),
    );
    renderWithQuery(<RuntimeDetailPanel open runtime={sleepingRuntime} onClose={() => {}} />);
    screen.getByTitle("Wake").click();
    const line = await screen.findByText("Not woken: the wake helper on the Mac is not running.");
    expect(screen.queryByText("Wake failed.")).not.toBeInTheDocument();
    // An error, like the box card's line — not the neutral note.
    expect(line).toHaveAttribute("role", "alert");
  });

  it("another failure: the plain failure line", async () => {
    vi.spyOn(api.runtimes, "wake").mockRejectedValue(new Error("API 400: Keine wol_mac_address konfiguriert."));
    renderWithQuery(<RuntimeDetailPanel open runtime={sleepingRuntime} onClose={() => {}} />);
    screen.getByTitle("Wake").click();
    expect(await screen.findByText("Wake failed.")).toHaveAttribute("role", "alert");
  });

  it("sent: the translated line, not the backend's raw German message", async () => {
    const wake = vi.spyOn(api.runtimes, "wake").mockResolvedValue({
      ok: true, message: "Wake-on-LAN ausgelöst für Wake RT. Box braucht ~1-2 Min bis erreichbar.",
    });
    renderWithQuery(<RuntimeDetailPanel open runtime={sleepingRuntime} onClose={() => {}} />);
    screen.getByTitle("Wake").click();
    await waitFor(() => expect(wake).toHaveBeenCalledWith("wake-rt"));
    const line = await screen.findByText("The box needs 1–2 min.");
    expect(line).toHaveAttribute("role", "status");
    expect(screen.queryByText(/ausgelöst/)).not.toBeInTheDocument();
  });

  it("a viewer only looks: no Wake", () => {
    mockStore.state.currentUser = { id: "u2", email: "v@b.c", name: "Viewer", role: "viewer" };
    renderWithQuery(<RuntimeDetailPanel open runtime={sleepingRuntime} onClose={() => {}} />);
    expect(screen.queryByTitle("Wake")).not.toBeInTheDocument();
  });
});
